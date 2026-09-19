#!/bin/zsh
# Start Command Line OpenD from the folder named by OPEND_DIR in .env.
#
# First run:   ./opend.sh          (interactive: enter moomoo ID, password, phone code)
# Later runs:  ./opend.sh daemon   (reuses the remembered login, no console, backgrounds itself)
#
# OpenD listens on MOOMOO_HOST:MOOMOO_PORT from .env (default 127.0.0.1:11111).
set -euo pipefail

cd "$(dirname "$0")"
set -a; source .env; set +a

OPEND_DIR="${OPEND_DIR:-opend}"
BINARY="$OPEND_DIR/OpenD.app/Contents/MacOS/OpenD"

if [[ ! -x "$BINARY" ]]; then
  echo "OpenD binary not found at $BINARY" >&2
  echo "Download 'Command Line OpenD' for Mac from https://www.moomoo.com/download/OpenAPI," >&2
  echo "unzip it, and set OPEND_DIR in .env to the folder that contains OpenD.app and OpenD.xml" >&2
  exit 1
fi

case "${1:-}" in
  "")
    exec "$BINARY"
    ;;
  daemon)
    nohup "$BINARY" -login_by_remember=1 -console=0 > "$OPEND_DIR/opend.out" 2>&1 &
    echo "OpenD started in the background (pid $!), log at $OPEND_DIR/opend.out"
    ;;
  *)
    echo "usage: ./opend.sh [daemon]" >&2
    exit 2
    ;;
esac
