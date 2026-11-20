#!/bin/bash
D=$(dirname "$0"); N=${1:-120}
for i in $(seq -w 1 $N); do
  T=$(python3 -c 'import time;print(f"{time.time():.3f}")')
  "$D/axprobe" dump --root window --out "$D/dumps/stream/$i-$T.json" >/dev/null 2>&1
  sleep 0.35
done
