#!/usr/bin/env bash
# Example client: compile a .c against flat header groups via the cache service.
#
# Usage:
#   ./scripts/example.sh <service-base-url> <source.c> <search-order> <group-dir>...
# e.g.
#   ./scripts/example.sh http://localhost:8000 src/main.c board,arch \
#       headers/board headers/arch
#
# Each group directory is uploaded flat: every *.h inside is sent as
# "<group-name>/<header.h>". The downloaded object is written next to the
# source as "<stem>.o". The X-Cache header shows HIT or MISS.
set -euo pipefail

BASE=${1:?base url required}
SRC=${2:?source .c required}
ORDER=${3:?comma-separated search order required}
shift 3

group_names=""
upload_args=()
for dir in "$@"; do
  group=$(basename "$dir")
  if [ -z "$group_names" ]; then
    group_names=$group
  else
    group_names="${group_names},${group}"
  fi
  for h in "$dir"/*.h; do
    [ -e "$h" ] || continue
    upload_args=("${upload_args[@]}" -F "headers=@${h};filename=${group}/$(basename "$h")")
  done
done

OUT=${SRC%.c}.o

echo "POST ${BASE}/compile  groups=${group_names}  order=${ORDER}"
# -D dumps response headers to stdout so the X-Cache line is visible.
curl -sS -D - -o "$OUT" "${BASE}/compile" \
  -F "source=@${SRC}" \
  -F "groups=${group_names}" \
  -F "search_order=${ORDER}" \
  "${upload_args[@]}" | grep -iE '^HTTP/|^x-cache|^x-used-headers|^content-disposition'
echo "saved -> $OUT"
file "$OUT"
