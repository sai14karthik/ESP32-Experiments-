FROM python:3.12-slim-bookworm

RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg \
  && rm -rf /var/lib/apt/lists/*

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
