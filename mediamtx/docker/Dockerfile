# Static ffmpeg: the Mini cannot reach deb.debian.org, so do not apt-get install ffmpeg.
FROM mwader/static-ffmpeg:7.1 AS ffmpeg-base

FROM python:3.12-slim-bookworm

COPY --from=ffmpeg-base /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-base /ffprobe /usr/local/bin/ffprobe

WORKDIR /app
COPY app.py /app/app.py

ENV PYTHONUNBUFFERED=1 \
    MTX_HOST=mediamtx \
    RTSP_PORT=8554 \
    MEDIAMTX_LAN_IP=10.128.93.13 \
    API_PORT=8090 \
    DATA_PATH=/data/cameras.json

EXPOSE 8090
CMD ["python", "-u", "/app/app.py"]
