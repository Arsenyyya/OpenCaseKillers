#!/bin/sh
# BuildKit on some Docker Desktop versions rejects non-ASCII checkout paths.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_dir=$(mktemp -d /tmp/support-insight-build.XXXXXX)
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM
python3 - "$project_dir" "$build_dir" <<'PY'
import shutil
import sys
shutil.copytree(sys.argv[1], sys.argv[2], dirs_exist_ok=True,
    ignore=shutil.ignore_patterns('.venv','.cache','.env','.local','.git','__pycache__','.pytest_cache','.ruff_cache'))
PY
cd "$build_dir"
docker build --progress=plain -t support-insight-agent:local .
