#!/usr/bin/env python3
'''
Ask the Nemotron omni model about images and sound and print its replies.

Nemotron 3 Nano Omni is served by vLLM behind an OpenAI-compatible API (the
``vllm-nemotron`` deployment in the ``videoflow`` namespace). The request has the shape
videoflow-contrib's ``SceneCommentator`` sends — a system prompt, then one user message
of text and base64 images, each image preceded by a caption — but this script needs
nothing beyond the Python standard library. Pillow, when installed, shrinks big images
before they are sent (``--max-px``); without it they go as they are. ffmpeg, when on the
PATH, turns audio into mono FLAC at 16 kHz, the rate the model hears at, and takes the
sound track out of video files (``--audio-rate``); without it audio files go as they are.

    nemotron_client.py photo.jpg
    nemotron_client.py -p 'How many people are in the room?' cam0.jpg cam1.jpg
    nemotron_client.py --each --workers 4 frames/*.jpg     # one request (and reply) per image
    nemotron_client.py --think photo.jpg                  # let the model reason first; shows the reasoning
    nemotron_client.py -p 'Count the people.' --schema @count.schema.json photo.jpg
    nemotron_client.py -p 'Transcribe this audio.' --top-k 1 meeting.wav
    nemotron_client.py -p 'What is going on in the room?' cam0.jpg room.wav   # sees and hears
    nemotron_client.py -p 'Summarize what is said.' recording.mp4             # its sound track only
    nemotron_client.py --each -p 'Transcribe this audio.' clips/*.wav

Inputs are files or http(s)/data URLs (URLs are fetched by the server, not here). Audio
is told from images by its file type; a video is sent as its sound track only. The server
takes at most 8 images and 1 audio clip, of up to 10 minutes, per request: ``--each``
sends several clips one by one. ``--prompt``, ``--system`` and ``--schema`` read a file
when given ``@path``. Replies go to stdout; reasoning, timing and token counts go to
stderr. ``--endpoint`` (``NEMOTRON_ENDPOINT``) and ``--model`` (``NEMOTRON_MODEL``) point
it at any other OpenAI-compatible multimodal model.
'''
from __future__ import annotations

import argparse
import base64
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None

#: The ``vllm`` Service in namespace ``videoflow`` (ClusterIP: reachable from every cluster node).
DEFAULT_ENDPOINT = 'http://10.43.41.152:8000/v1'
DEFAULT_MODEL = 'nemotron'
#: The model's audio sample rate (``sound_config.sampling_rate``).
DEFAULT_AUDIO_RATE = 16000

_THINK = re.compile(r'<think>.*?</think>', re.S)
_URL_PREFIXES = ('http://', 'https://', 'data:')


def read_text(value : str) -> str:
    '''``@path`` is the contents of that file; anything else is the text itself.'''
    return Path(value[1:]).read_text() if value.startswith('@') else value


def kind(source : str) -> str:
    '''``'audio'`` for sound or a video (sent as its sound track), ``'image'`` for anything else.'''
    if source.startswith('data:'):
        mime = source[5:].split(',', 1)[0].split(';', 1)[0]
    else:
        path = urllib.parse.urlparse(source).path if source.startswith(_URL_PREFIXES) else source
        mime = mimetypes.guess_type(path)[0] or ''
    return 'audio' if mime.startswith(('audio/', 'video/')) else 'image'


def default_prompt(kinds : list[str]) -> str:
    if 'audio' not in kinds:
        return 'Describe what you see.'
    return 'Describe what you see and hear.' if 'image' in kinds else 'Describe what you hear.'


def image_part(source : str, max_px : int) -> dict:
    '''
    One ``image_url`` content part. URLs pass through; a file becomes a base64 data URL,
    re-encoded as JPEG with its long side at most ``max_px`` when Pillow is installed.
    '''
    if source.startswith(_URL_PREFIXES):
        return {'type': 'image_url', 'image_url': {'url': source}}
    data = Path(source).read_bytes()
    mime = mimetypes.guess_type(source)[0] or 'image/jpeg'
    if max_px > 0 and Image is not None:
        img = Image.open(io.BytesIO(data))
        if max(img.size) > max_px:
            img = ImageOps.exif_transpose(img).convert('RGB')
            img.thumbnail((max_px, max_px), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality = 85)
            data, mime = buf.getvalue(), 'image/jpeg'
    return {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,' + base64.b64encode(data).decode()}}


def audio_part(source : str, rate : int) -> dict:
    '''
    One ``audio_url`` content part. URLs pass through; a file becomes a base64 data URL,
    re-encoded by ffmpeg as mono FLAC at ``rate`` Hz (a video gives its sound track) when
    ffmpeg is on the PATH, else sent as it is.
    '''
    if source.startswith(_URL_PREFIXES):
        return {'type': 'audio_url', 'audio_url': {'url': source}}
    mime = mimetypes.guess_type(source)[0] or 'audio/wav'
    ffmpeg = shutil.which('ffmpeg') if rate > 0 else None
    if ffmpeg:
        # A file, not a pipe: FLAC written to a pipe has no length in its header.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'audio.flac'
            run = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-i', source, '-map', '0:a:0',
                                  '-ac', '1', '-ar', str(rate), str(out)], capture_output = True)
            if run.returncode != 0:
                if b'matches no streams' in run.stderr:
                    raise ValueError(f'{source} has no sound track')
                raise ValueError(f'ffmpeg could not read the sound of {source}: '
                                 f'{run.stderr.decode(errors = "replace").strip()[-300:]}')
            data, mime = out.read_bytes(), 'audio/flac'
    elif mime.startswith('video/'):
        raise ValueError(f'{source}: sending the sound track of a video needs ffmpeg on the PATH '
                         'and --audio-rate above 0')
    else:
        data = Path(source).read_bytes()
    return {'type': 'audio_url', 'audio_url': {'url': f'data:{mime};base64,' + base64.b64encode(data).decode()}}


def build_request(inputs : list[str], prompt : Optional[str], model : str, max_tokens : int = 1024,
                  temperature : float = 0.2, enable_thinking : bool = False, max_px : int = 1024,
                  system_prompt : Optional[str] = None, schema : Optional[dict] = None,
                  captions : bool = True, audio_rate : int = DEFAULT_AUDIO_RATE,
                  top_k : Optional[int] = None) -> dict:
    '''
    The chat-completions body: the prompt (by default, one that fits the inputs), then each
    input. Images are preceded by a caption naming them when there are several, so the
    reply can refer to them; audio gets none, as the chat template does not number clips.
    '''
    kinds = [kind(source) for source in inputs]
    n_images = kinds.count('image')
    content : list[dict[str, Any]] = [{'type': 'text', 'text': prompt or default_prompt(kinds)}]
    i = 0
    for source, k in zip(inputs, kinds):
        if k == 'audio':
            content.append(audio_part(source, audio_rate))
            continue
        i += 1
        if captions and n_images > 1:
            name = '' if source.startswith(_URL_PREFIXES) else f' ({Path(source).name})'
            content.append({'type': 'text', 'text': f'Image {i}{name}:'})
        content.append(image_part(source, max_px))
    messages = [{'role': 'user', 'content': content}]
    if system_prompt:
        messages.insert(0, {'role': 'system', 'content': system_prompt})
    body : dict[str, Any] = {
        'model': model,
        'messages': messages,
        'max_tokens': max_tokens,
        'temperature': temperature,
        # Nemotron 3 thinks before answering unless told not to; its reasoning comes back
        # separately (vLLM's reasoning parser) and counts against max_tokens.
        'chat_template_kwargs': {'enable_thinking': enable_thinking},
    }
    if top_k is not None:
        body['top_k'] = top_k
    if schema is not None:
        body['response_format'] = {'type': 'json_schema',
                                   'json_schema': {'name': 'reply', 'schema': schema, 'strict': True}}
    return body


def post(endpoint : str, body : dict, timeout_s : float) -> dict:
    req = urllib.request.Request(endpoint.rstrip('/') + '/chat/completions',
                                 data = json.dumps(body).encode(), method = 'POST',
                                 headers = {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout = timeout_s) as resp:
        return json.loads(resp.read())


def ask(inputs : list[str], args : argparse.Namespace) -> dict:
    '''One request; failures come back as ``{'error': ...}``, never raised.'''
    started = time.time()
    try:
        body = build_request(inputs, args.prompt, args.model, args.max_tokens, args.temperature, args.think,
                             args.max_px, args.system, args.schema, not args.no_captions,
                             args.audio_rate, args.top_k)
        response = post(args.endpoint, body, args.timeout)
    except urllib.error.HTTPError as e:
        return {'inputs': inputs, 'error': f'HTTP {e.code}: {e.read().decode(errors = "replace")[:500]}'}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {'inputs': inputs, 'error': f'{type(e).__name__}: {e}'}
    return {'inputs': inputs, 'response': response, 'latency_s': time.time() - started}


def report(result : dict, raw : bool, header : bool) -> bool:
    '''Prints one result: the reply on stdout, the rest on stderr. False when it failed.'''
    if header:
        print(f'== {", ".join(result["inputs"])} ==', flush = True)
    if 'error' in result:
        print(f'error: {result["error"]}', file = sys.stderr, flush = True)
        return False
    response = result['response']
    if raw:
        print(json.dumps(response, indent = 1), flush = True)
        return True
    try:
        choice = response['choices'][0]
        message = choice['message']
    except (KeyError, IndexError, TypeError):
        print(f'error: no reply in {json.dumps(response)[:500]}', file = sys.stderr, flush = True)
        return False
    reasoning = message.get('reasoning') or message.get('reasoning_content')
    if reasoning:
        print(f'[reasoning] {reasoning.strip()}', file = sys.stderr, flush = True)
    print(_THINK.sub('', message.get('content') or '').strip(), flush = True)
    usage = response.get('usage') or {}
    cut = ', cut off by --max-tokens' if choice.get('finish_reason') == 'length' else ''
    print(f'[{result["latency_s"]:.2f} s, {usage.get("prompt_tokens", "?")} prompt + '
          f'{usage.get("completion_tokens", "?")} completion tokens{cut}]', file = sys.stderr, flush = True)
    return True


def main(argv : Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description = __doc__, formatter_class = argparse.RawDescriptionHelpFormatter)
    parser.add_argument('inputs', nargs = '+', help = 'image, audio or video files, or http(s)/data URLs')
    parser.add_argument('-p', '--prompt', help = "question or instruction (default: 'Describe what you see.', "
                                                 "'... hear.' or '... see and hear.', to fit the inputs)")
    parser.add_argument('-s', '--system', help = 'system prompt')
    parser.add_argument('--schema', help = 'JSON schema the reply must follow (inline JSON or @file)')
    parser.add_argument('--each', action = 'store_true', help = 'one request per input instead of all in one')
    parser.add_argument('--workers', type = int, default = 4, help = 'requests in flight with --each (default: 4)')
    parser.add_argument('--think', action = 'store_true', help = 'let the model reason before answering (slower)')
    parser.add_argument('--endpoint', default = os.environ.get('NEMOTRON_ENDPOINT', DEFAULT_ENDPOINT),
                        help = 'OpenAI-compatible base URL (default: $NEMOTRON_ENDPOINT or %(default)s)')
    parser.add_argument('--model', default = os.environ.get('NEMOTRON_MODEL', DEFAULT_MODEL),
                        help = 'served model name (default: $NEMOTRON_MODEL or %(default)s)')
    parser.add_argument('--max-tokens', type = int, default = 1024, help = 'generation budget, reasoning included (default: 1024)')
    parser.add_argument('--temperature', type = float, default = 0.2, help = 'sampling temperature (default: 0.2)')
    parser.add_argument('--top-k', type = int, help = 'sample from the k likeliest tokens only; 1 is greedy, '
                                                      'which the model card advises for transcripts')
    parser.add_argument('--max-px', type = int,
                        help = 'downscale so the long side is at most this, needs Pillow; 0 sends files as they are (default: 1024)')
    parser.add_argument('--audio-rate', type = int,
                        help = f'resample audio to mono FLAC at this rate, needs ffmpeg; 0 sends files as they are '
                               f'(default: {DEFAULT_AUDIO_RATE})')
    parser.add_argument('--timeout', type = float, default = 120.0, help = 'seconds per request (default: 120)')
    parser.add_argument('--no-captions', action = 'store_true', help = 'do not label images with their number and name')
    parser.add_argument('--raw', action = 'store_true', help = 'print the whole JSON response')
    args = parser.parse_args(argv)

    try:
        args.prompt = read_text(args.prompt) if args.prompt else None
        args.system = read_text(args.system) if args.system else None
        args.schema = json.loads(read_text(args.schema)) if args.schema else None
    except (OSError, ValueError) as e:
        parser.error(str(e))
    if args.max_px is None:
        args.max_px = 1024
    elif args.max_px > 0 and Image is None:
        print('note: --max-px needs Pillow, which is not installed: images are sent at full size', file = sys.stderr)
    if args.audio_rate is None:
        args.audio_rate = DEFAULT_AUDIO_RATE
    elif args.audio_rate > 0 and shutil.which('ffmpeg') is None:
        print('note: --audio-rate needs ffmpeg, which is not on the PATH: audio files are sent as they are',
              file = sys.stderr)

    batches = [[source] for source in args.inputs] if args.each else [args.inputs]
    with ThreadPoolExecutor(max(1, args.workers)) as pool:
        # map keeps the input order: each result prints as soon as it and those before it are done.
        ok = [report(result, args.raw, header = len(batches) > 1)
              for result in pool.map(lambda batch: ask(batch, args), batches)]
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())