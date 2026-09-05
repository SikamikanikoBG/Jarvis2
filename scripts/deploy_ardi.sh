#!/usr/bin/env bash
# Deploy jarvis-core to ardi as a container. Additive: it never touches V1's jarvis-server.
#
#   bash scripts/deploy_ardi.sh            # build + run
#   PORT=9020 bash scripts/deploy_ardi.sh  # override the port
#
# The image is built ON ardi (linux/amd64) from a source tarball; the web SPA is built inside
# the image, so no node is needed on either side. State lives in the jarvis2-data volume.
set -euo pipefail

ARDI_HOST="${ARDI_HOST:-ardi@100.97.120.53}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10}"
PORT="${PORT:-9020}"
BIND="${BIND:-100.97.120.53}"
NAME="${NAME:-jarvis2-core}"
REMOTE_BUILD="/home/ardi/jarvis2/build"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SSH="ssh $SSH_OPTS $ARDI_HOST"
SCP="scp $SSH_OPTS"

echo "==> packing source"
ARCHIVE="$(mktemp -t jarvis2-src-XXXXXX.tar.gz)"
tar -czf "$ARCHIVE" -C "$REPO_ROOT" \
    --exclude='.git' --exclude='.venv' --exclude='node_modules' --exclude='data' \
    --exclude='web/dist' --exclude='**/__pycache__' --exclude='*.db' \
    Dockerfile pyproject.toml uv.lock packages web

echo "==> uploading ($(du -h "$ARCHIVE" | cut -f1))"
$SSH "rm -rf $REMOTE_BUILD && mkdir -p $REMOTE_BUILD"
# Pipe binary through scp, never through ssh stdin: msys mangles the stream on Windows.
$SCP "$ARCHIVE" "$ARDI_HOST:$REMOTE_BUILD/src.tar.gz"
rm -f "$ARCHIVE"

echo "==> building image on ardi"
$SSH "cd $REMOTE_BUILD && tar -xzf src.tar.gz && docker build -t jarvis2-core:latest . 2>&1 | tail -8"

echo "==> (re)starting $NAME on $BIND:$PORT"
TOKEN="$($SSH "docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $NAME 2>/dev/null | sed -n 's/^JARVIS_TOKEN=//p'" || true)"
if [ -z "${TOKEN:-}" ]; then
  TOKEN="${JARVIS_TOKEN:-$(openssl rand -hex 16)}"
  echo "    new token generated (shown once at the end)"
else
  echo "    reusing the existing token"
fi
$SSH "docker rm -f $NAME >/dev/null 2>&1 || true; \
  docker run -d --name $NAME --restart unless-stopped \
    -p $BIND:$PORT:9020 \
    -p 127.0.0.1:$PORT:9020 \
    -v jarvis2-data:/data \
    -e JARVIS_TOKEN=$TOKEN \
    -e TZ=Europe/Sofia \
    --add-host=host.docker.internal:host-gateway \
    jarvis2-core:latest >/dev/null"

echo "==> waiting for health"
for i in $(seq 1 30); do
  if $SSH "curl -fsS -m 3 http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "    healthy after ${i}s"
    break
  fi
  sleep 1
done
$SSH "docker ps --filter name=$NAME --format '    {{.Names}}  {{.Status}}  {{.Ports}}'"
$SSH "curl -fsS -m 5 http://127.0.0.1:$PORT/api/health" && echo
echo
echo "Jarvis V2 on ardi:  http://$BIND:$PORT/?token=$TOKEN"
echo "OpenAI-compatible:  http://$BIND:$PORT/v1  (model 'jarvis', bearer above)"
