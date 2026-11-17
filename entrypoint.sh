#!/bin/sh
python /app/web.py &
exec python /app/lr_clips.py --out /clips --days 7
