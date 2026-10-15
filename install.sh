#!/usr/bin/env bash
# grDownload installer — installs the `grdl` CLI and checks dependencies.
#
#   curl -fsSL https://raw.githubusercontent.com/yass-gr/grDownload/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/yass-gr/grDownload/main/install.sh | bash -s -- --deps
#
# --deps also installs missing yt-dlp/ffmpeg/aria2 (via grdl setup).
set -euo pipefail

REPO="yass-gr/grDownload"
BRANCH="${GRDL_BRANCH:-main}"
BIN_DIR="${GRDL_BIN_DIR:-$HOME/.local/bin}"
RAW_BASE="https://raw.githubusercontent.com/$REPO/$BRANCH"

WITH_DEPS=0
for arg in "$@"; do
  case "$arg" in
    --deps) WITH_DEPS=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

mkdir -p "$BIN_DIR"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/grdl.py" ]; then
  echo "Installing grdl from local checkout: $SELF_DIR/grdl.py"
  cp "$SELF_DIR/grdl.py" "$BIN_DIR/grdl"
else
  echo "Downloading grdl from $RAW_BASE/grdl.py"
  curl -fsSL "$RAW_BASE/grdl.py" -o "$BIN_DIR/grdl"
fi
chmod +x "$BIN_DIR/grdl"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note: $BIN_DIR is not in your PATH — add it, e.g.:" 
     echo "      export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

echo "Installed: $BIN_DIR/grdl ($("$BIN_DIR/grdl" --version 2>/dev/null || echo 'version check failed'))"

if [ "$WITH_DEPS" -eq 1 ]; then
  "$BIN_DIR/grdl" setup
else
  "$BIN_DIR/grdl" doctor || true
fi

echo
echo "Try it:  grdl 'https://www.youtube.com/watch?v=jNQXAC9IVRw'"
