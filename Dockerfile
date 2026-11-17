FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "pylitterbot>=2025.6.5" flask
COPY lr_clips.py web.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh
WORKDIR /app
ENTRYPOINT ["/app/entrypoint.sh"]
