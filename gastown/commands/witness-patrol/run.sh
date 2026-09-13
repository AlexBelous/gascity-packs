#!/bin/sh
set -eu
exec python3 "${GC_PACK_DIR:?GC_PACK_DIR must be set}/assets/scripts/witness-patrol.py" "$@"
