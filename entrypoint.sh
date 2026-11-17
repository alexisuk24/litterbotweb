#!/bin/sh
for d in /clips /data; do
    if [ -d "$d" ] && [ ! -w "$d" ]; then
        echo "ERROR: $d is not writable by uid $(id -u). Fix on the host with:" >&2
        echo "  sudo chown -R $(id -u):$(id -g) <host folder mounted at $d>" >&2
        exit 1
    fi
done
python /app/web.py &
exec python /app/lr_clips.py --out /clips --days 7
