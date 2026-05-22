FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gcc libc6-dev curl ca-certificates \
    && pip install --no-cache-dir ffsubsync==0.4.25 \
    && curl -sL "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_aarch64" -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp \
    && apt-get purge -y gcc libc6-dev curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY sub_fetcher.py .

CMD ["python3", "-u", "sub_fetcher.py"]
