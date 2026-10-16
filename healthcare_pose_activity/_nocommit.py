#!/usr/bin/env python3
'''
Ask the Nemotron vision-language model about images and print its replies.

Nemotron 3 Nano Omni is served by vLLM behind an OpenAI-compatible API (the
``vllm-nemotron`` deployment in the ``videoflow`` namespace). The request has the shape
videoflow-contrib's ``SceneCommentator`` sends — a system prompt, then one user message
of text and base64 images, each image preceded by a caption — but this script needs
nothing beyond the Python standard library. Pillow, when installed, shrinks big images
before they are sent (``--max-px``); without it they go as they are.

    nemotron_client.py photo.jpg
    nemotron_client.py -p 'How many people are in the room?' cam0.jpg cam1.jpg
    nemotron_client.py --each --workers 4 frames/*.jpg     # one request (and reply) per image
    nemotron_client.py --think photo.jpg                  # let the model reason first; shows the reasoning
    nemotron_client.py -p 'Count the people.' --schema @count.schema.json photo.jpg

Images are file paths or http(s)/data URLs (URLs are fetched by the server, not here).
The server takes at most 8 images per request. ``--prompt``, ``--system`` and
``--schema`` read a file when given ``@path``. Replies go to stdout; reasoning, timing
and token counts go to stderr. ``--endpoint`` (``NEMOTRON_ENDPOINT``) and ``--model``
(``NEMOTRON_MODEL``) point it at any other OpenAI-compatible vision model.
'''
from __future__ import annotations

import argparse
import base64
import io
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
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
DEFAULT_PROMPT = 'Describe what you see.'

_THINK = re.compile(r'<think>.*?</think>', re.S)


def read_text(value : str) -> str:
    '''``@path`` is the contents of that file; anything else is the text itself.'''
    return Path(value[1:]).read_text() if value.startswith('@') else value


def image_part(source : str, max_px : int) -> dict:
    '''
    One ``image_url`` content part. URLs pass through; a file becomes a base64 data URL,
    re-encoded as JPEG with its long side at most ``max_px`` when Pillow is installed.
    '''
    if source.startswith(('http://', 'https://', 'data:')):
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


def build_request(images : list[str], prompt : str, model : str, max_tokens : int = 1024,
                  temperature : float = 0.2, enable_thinking : bool = False, max_px : int = 1024,
                  system_prompt : Optional[str] = None, schema : Optional[dict] = None,
                  captions : bool = True) -> dict:
    '''
    The chat-completions body: the prompt, then each image — preceded by a caption naming
    it when there are several, so the reply can refer to them.
    '''
    content : list[dict[str, Any]] = [{'type': 'text', 'text': prompt}]
    for i, source in enumerate(images, 1):
        if captions and len(images) > 1:
            name = '' if source.startswith(('http://', 'https://', 'data:')) else f' ({Path(source).name})'
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


def ask(images : list[str], args : argparse.Namespace) -> dict:
    '''One request; failures come back as ``{'error': ...}``, never raised.'''
    started = time.time()
    try:
        body = build_request(images, args.prompt, args.model, args.max_tokens, args.temperature, args.think,
                             args.max_px, args.system, args.schema, not args.no_captions)
        response = post(args.endpoint, body, args.timeout)
    except urllib.error.HTTPError as e:
        return {'images': images, 'error': f'HTTP {e.code}: {e.read().decode(errors = "replace")[:500]}'}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {'images': images, 'error': f'{type(e).__name__}: {e}'}
    return {'images': images, 'response': response, 'latency_s': time.time() - started}


def report(result : dict, raw : bool, header : bool) -> bool:
    '''Prints one result: the reply on stdout, the rest on stderr. False when it failed.'''
    if header:
        print(f'== {", ".join(result["images"])} ==', flush = True)
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
    parser.add_argument('images', nargs = '+', help = 'image files or http(s)/data URLs')
    parser.add_argument('-p', '--prompt', default = DEFAULT_PROMPT, help = f'question or instruction (default: {DEFAULT_PROMPT!r})')
    parser.add_argument('-s', '--system', help = 'system prompt')
    parser.add_argument('--schema', help = 'JSON schema the reply must follow (inline JSON or @file)')
    parser.add_argument('--each', action = 'store_true', help = 'one request per image instead of all in one')
    parser.add_argument('--workers', type = int, default = 4, help = 'requests in flight with --each (default: 4)')
    parser.add_argument('--think', action = 'store_true', help = 'let the model reason before answering (slower)')
    parser.add_argument('--endpoint', default = os.environ.get('NEMOTRON_ENDPOINT', DEFAULT_ENDPOINT),
                        help = 'OpenAI-compatible base URL (default: $NEMOTRON_ENDPOINT or %(default)s)')
    parser.add_argument('--model', default = os.environ.get('NEMOTRON_MODEL', DEFAULT_MODEL),
                        help = 'served model name (default: $NEMOTRON_MODEL or %(default)s)')
    parser.add_argument('--max-tokens', type = int, default = 1024, help = 'generation budget, reasoning included (default: 1024)')
    parser.add_argument('--temperature', type = float, default = 0.2, help = 'sampling temperature (default: 0.2)')
    parser.add_argument('--max-px', type = int,
                        help = 'downscale so the long side is at most this, needs Pillow; 0 sends files as they are (default: 1024)')
    parser.add_argument('--timeout', type = float, default = 120.0, help = 'seconds per request (default: 120)')
    parser.add_argument('--no-captions', action = 'store_true', help = 'do not label images with their number and name')
    parser.add_argument('--raw', action = 'store_true', help = 'print the whole JSON response')
    args = parser.parse_args(argv)

    try:
        args.prompt = read_text(args.prompt)
        args.system = read_text(args.system) if args.system else None
        args.schema = json.loads(read_text(args.schema)) if args.schema else None
    except (OSError, ValueError) as e:
        parser.error(str(e))
    if args.max_px is None:
        args.max_px = 1024
    elif args.max_px > 0 and Image is None:
        print('note: --max-px needs Pillow, which is not installed: images are sent at full size', file = sys.stderr)

    batches = [[image] for image in args.images] if args.each else [args.images]
    with ThreadPoolExecutor(max(1, args.workers)) as pool:
        # map keeps the input order: each result prints as soon as it and those before it are done.
        ok = [report(result, args.raw, header = len(batches) > 1)
              for result in pool.map(lambda batch: ask(batch, args), batches)]
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())
