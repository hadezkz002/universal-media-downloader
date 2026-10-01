# syntax=docker/dockerfile:1
FROM denoland/deno:bin-2.9.7 AS deno

FROM python:3.12-slim

# ffmpeg: merging/conversion. deno: JavaScript runtime yt-dlp needs for YouTube.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WORK_DIR=/tmp/umd-work YTDLP_JS_RUNTIMES=deno PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health',timeout=4)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 30"]
