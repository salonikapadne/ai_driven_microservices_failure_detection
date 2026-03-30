#!/bin/sh
set -eu
# Reset live copy from immutable seed on every container start (repeatable demo).
mkdir -p /app/live
cp -f /app/seed/app.py /app/live/app.py
echo "buggy-service: restored live/app.py from seed (buggy baseline)"
exec python /app/live/app.py
