#!/usr/bin/env bash
# Install the frw command into the plugin's data folder, and keep it current.
#
#   bootstrap.sh <plugin root> <plugin data>
#
# Makes <plugin data>/venv and <plugin data>/frw (a wrapper the skills call).
# Reinstalls only when the shipped Python package changes, so the SessionStart
# hook costs a hash on most sessions. Trips, prices and reports live in
# $FRW_HOME (default ~/.family-resort-watch), not here, so they survive the
# plugin being updated or reinstalled.
set -euo pipefail

root=${1:?usage: bootstrap.sh <plugin root> <plugin data>}
data=${2:?usage: bootstrap.sh <plugin root> <plugin data>}
quiet=${FRW_BOOTSTRAP_QUIET:-}

say() { [ -n "$quiet" ] || echo "family-resort-watch: $*"; }

python=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    python=$(command -v "$candidate")
    break
  fi
done
if [ -z "$python" ]; then
  echo "family-resort-watch: needs Python 3.11 or newer on PATH (https://www.python.org/downloads/)." >&2
  exit 1
fi

mkdir -p "$data"
stamp_file="$data/install.stamp"
stamp=$(cd "$root/python" && find pyproject.toml src/family_resort_watch -type f ! -path "*/__pycache__/*" | LC_ALL=C sort | xargs shasum | shasum | cut -c1-16)

if [ -x "$data/venv/bin/frw" ] && [ "$(cat "$stamp_file" 2>/dev/null)" = "$stamp" ]; then
  exit 0
fi

say "installing the frw command (first run or plugin update)..."
if [ ! -x "$data/venv/bin/python" ]; then
  "$python" -m venv "$data/venv"
fi
# Build from a copy: the plugin folder may be read-only, and a build leaves
# build/ and *.egg-info behind.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -R "$root/python/pyproject.toml" "$root/python/src" "$work/"
"$data/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade --force-reinstall --no-deps "$work" >&2
"$data/venv/bin/python" -m pip install --quiet --disable-pip-version-check "$work" >&2

cat > "$data/frw" <<WRAP
#!/usr/bin/env bash
exec "$data/venv/bin/frw" "\$@"
WRAP
chmod +x "$data/frw"
echo "$stamp" > "$stamp_file"
say "ready: $data/frw"
