#!/usr/bin/env bash
#
# Serve the redaction demo from this machine and expose it publicly.
#
#   ./serve.sh                      # local only, http://localhost:7860
#   ./serve.sh --public             # also open an ngrok tunnel
#   ./serve.sh --public my-demo     # ...on your reserved ngrok domain
#
# One-time setup for --public:
#   1. Free account at https://dashboard.ngrok.com/signup
#   2. ngrok config add-authtoken <token>
#   3. Reserve a free static domain at
#      https://dashboard.ngrok.com/domains  and pass its first label above.
#      Without one, ngrok issues a new random URL on every restart — useless
#      in anything you have already sent to somebody.
#
# The tunnel lives only while this machine is awake and online. `caffeinate`
# below stops macOS sleeping and taking the demo down with it.

set -euo pipefail

PORT="${PORT:-7860}"
IMAGE="pii-redactor"
CONTAINER="pii-demo"
PUBLIC=false
DOMAIN=""

for arg in "$@"; do
    case "$arg" in
        --public) PUBLIC=true ;;
        --*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) DOMAIN="$arg" ;;
    esac
done

command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon is not running" >&2; exit 1; }

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "building $IMAGE (first run takes a while)..."
    docker build -t "$IMAGE" .
fi

echo "starting $CONTAINER on port $PORT..."
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" -p "$PORT:7860" "$IMAGE" >/dev/null

printf "waiting for the model to load"
for _ in $(seq 1 60); do
    if curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1; then
        echo " ready."
        break
    fi
    printf "."
    sleep 5
done
curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1 || {
    echo " failed. Container logs:" >&2
    docker logs --tail 30 "$CONTAINER" >&2
    exit 1
}

echo "local:  http://localhost:$PORT"

if [ "$PUBLIC" = false ]; then
    echo "run with --public to expose it via ngrok."
    exit 0
fi

command -v ngrok >/dev/null || { echo "ngrok is not installed (brew install ngrok)" >&2; exit 1; }
ngrok config check >/dev/null 2>&1 || {
    echo "ngrok has no authtoken. Run: ngrok config add-authtoken <token>" >&2
    exit 1
}

# Keep the machine awake for as long as the tunnel runs; a sleeping laptop
# takes the public URL down with it.
if command -v caffeinate >/dev/null; then
    caffeinate -is &
    CAFFEINATE_PID=$!
    trap 'kill "$CAFFEINATE_PID" 2>/dev/null || true' EXIT
fi

echo "opening tunnel — leave this terminal running; Ctrl-C stops the demo."
if [ -n "$DOMAIN" ]; then
    exec ngrok http --url="${DOMAIN}.ngrok-free.app" "$PORT"
else
    echo "note: no reserved domain given, so this URL changes on every restart."
    exec ngrok http "$PORT"
fi
