# syntax=docker/dockerfile:1

# ---- minimal ffmpeg: only what's needed to remux (possibly AES-encrypted) HLS
# segments, TS or fMP4, carrying H.264 + AAC into MP4 without re-encoding.
FROM debian:trixie-slim AS ffmpeg
ARG FFMPEG_VERSION=9.0.2
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config xz-utils \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e \
    https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz /tmp/ffmpeg.tar.xz
RUN mkdir /tmp/ffmpeg && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1 \
    && cd /tmp/ffmpeg \
    && ./configure --prefix=/opt/ffmpeg \
        --disable-everything --disable-autodetect --disable-network \
        --disable-doc --disable-debug --disable-ffplay --disable-x86asm \
        --enable-static --disable-shared --enable-small \
        --enable-protocol=file,crypto,pipe \
        --enable-demuxer=hls,mpegts,mov,aac,h264 \
        --enable-muxer=mp4,mov \
        --enable-parser=h264,aac \
        --enable-decoder=h264,aac \
        --enable-bsf=aac_adtstoasc,h264_mp4toannexb,extract_extradata \
    && make -j"$(nproc)" \
    && make install \
    && strip /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe \
    && /opt/ffmpeg/bin/ffmpeg -hide_banner -demuxers | grep -q ' hls ' \
    && /opt/ffmpeg/bin/ffmpeg -hide_banner -protocols | grep -q crypto

# ---- app image
FROM python:3.15-slim

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe /usr/local/bin/

# App deps live in a venv owned by the app user so the runtime pylitterbot
# self-update works without root. The base image's own pip (and the libraries
# it vendors) is removed afterwards since nothing uses it.
RUN useradd --uid 1000 --user-group --no-create-home --home-dir /app app \
    && python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade \
        "pylitterbot>=2025.8.0" flask aiohttp certifi urllib3 \
    && (python -m pip uninstall -y pip setuptools 2>/dev/null || true) \
    && rm -rf /usr/local/lib/python3*/ensurepip \
    && chown -R app:app /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/litterrobot.db

WORKDIR /app
ADD --checksum=sha256:b38076762f7363bc9e912b68b8e034826798db5df26bb61f000ec2e7a3137bc7 \
    https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js /app/static/vendor/chart.umd.min.js
COPY lr_clips.py lr_db.py web.py entrypoint.sh /app/
COPY templates /app/templates
COPY static /app/static
RUN chmod 755 /app/entrypoint.sh && chmod -R a+rX /app

USER app
ENTRYPOINT ["/app/entrypoint.sh"]
