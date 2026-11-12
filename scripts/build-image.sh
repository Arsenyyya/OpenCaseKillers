#!/bin/sh
# BuildKit on some Docker Desktop versions rejects non-ASCII checkout paths.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_dir=$(mktemp -d /tmp/support-insight-build.XXXXXX)
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM
python3 "$project_dir/scripts/prepare_build_context.py" "$project_dir" "$build_dir"
cd "$build_dir"
docker build --progress=plain -t support-insight-agent:local .
