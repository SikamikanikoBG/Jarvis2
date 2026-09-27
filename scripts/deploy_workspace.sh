#!/usr/bin/env bash
# Deploy the Jarvis workspace container on ardi: Jarvis's own Linux host (shell_run + fs_*), always up
# next to the core, so generic work does not depend on the laptop or the VM being awake.
#
#   bash scripts/deploy_workspace.sh
#
# Same pattern as deploy_ardi.sh: source tarball -> image built on ardi. The container sits on the
# private `jarvis2` network with no published port: only the core reaches it (http://jarvis2-workspace:9030/mcp).
# Files live in the jarvis2-workspace volume, the config (and its bearer token) in jarvis2-workspace-config.
# The first deploy registers it in the core's Settings.mcp_servers as `workspace`.
set -euo pipefail

ARDI_HOST="${ARDI_HOST:-ardi@100.97.120.53}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10}"
NAME="${NAME:-jarvis2-workspace}"
CORE="${CORE:-jarvis2-core}"
NET="${NET:-jarvis2}"
REMOTE_BUILD="/home/ardi/jarvis2/build-workspace"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ABOUT="Jarvis's own always-on Linux workspace next to the core (bash, python3, git, curl, jq, pandoc, headless Chromium, office/PDF libs; files persist in /workspace) - use it for everything not tied to a Windows desktop."

SSH="ssh $SSH_OPTS $ARDI_HOST"
SCP="scp $SSH_OPTS"

echo "==> packing source"
ARCHIVE="$(mktemp -t jarvis2-ws-XXXXXX.tar.gz)"
tar -czf "$ARCHIVE" -C "$REPO_ROOT" \
    --exclude='.git' --exclude='.venv' --exclude='node_modules' --exclude='**/__pycache__' \
    Dockerfile.workspace pyproject.toml uv.lock packages
$SSH "rm -rf $REMOTE_BUILD && mkdir -p $REMOTE_BUILD"
$SCP "$ARCHIVE" "$ARDI_HOST:$REMOTE_BUILD/src.tar.gz"
rm -f "$ARCHIVE"

echo "==> building image on ardi"
$SSH "cd $REMOTE_BUILD && tar -xzf src.tar.gz && docker build -f Dockerfile.workspace -t jarvis2-workspace:latest . 2>&1 | tail -5"

echo "==> network $NET (core + workspace)"
$SSH "docker network inspect $NET >/dev/null 2>&1 || docker network create $NET >/dev/null; \
      docker network connect $NET $CORE 2>/dev/null || true"

echo "==> seeding the config (first deploy only; the daemon adds its token on first start)"
# Files go over scp, never ssh stdin: ardi's login shell swallows it, so a heredoc arrives empty.
SEED="$(mktemp -t jarvis2-ws-host-XXXXXX.toml)"
cat > "$SEED" <<TOML
name = "workspace"
about = "$ABOUT"
listen = "0.0.0.0:9030"

[fs]
roots = ["/workspace"]

[shell]
allow = true

[screen]
enabled = false

[desktop]
enabled = false
TOML
$SCP "$SEED" "$ARDI_HOST:$REMOTE_BUILD/host.toml"
rm -f "$SEED"
$SSH "docker volume create jarvis2-workspace-config >/dev/null; \
  docker run --rm -v jarvis2-workspace-config:/config -v $REMOTE_BUILD/host.toml:/seed.toml:ro --user 0 \
    --entrypoint sh jarvis2-workspace:latest -c 'test -s /config/host.toml || cp /seed.toml /config/host.toml; chown -R 1000 /config'"

echo "==> (re)starting $NAME"
$SSH "docker rm -f $NAME >/dev/null 2>&1 || true; \
  docker run -d --name $NAME --restart unless-stopped --network $NET \
    -v jarvis2-workspace:/workspace -v jarvis2-workspace-config:/config \
    --add-host=host.docker.internal:host-gateway \
    jarvis2-workspace:latest >/dev/null"
for i in $(seq 1 60); do
  if $SSH "docker exec $NAME curl -fsS -m 3 http://127.0.0.1:9030/healthz" >/dev/null 2>&1; then
    echo "    healthy after ${i}s"
    break
  fi
  sleep 1
done

echo "==> registering in the core as 'workspace' (skipped when already there)"
REG="$(mktemp -t jarvis2-ws-reg-XXXXXX.py)"
cat > "$REG" <<'PY'
import json, os, urllib.request

def api(method, body=None):
    req = urllib.request.Request(
        "http://127.0.0.1:9020/api/settings",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + os.environ["JARVIS_TOKEN"], "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=60))

servers = api("GET")["mcp_servers"]  # PATCH replaces the whole list: send it back complete
if any(s["name"] == "workspace" for s in servers):
    print("    already registered")
else:
    servers.append({
        "name": "workspace", "transport": "streamable_http", "url": "http://jarvis2-workspace:9030/mcp",
        "headers": {"Authorization": "Bearer " + os.environ["HOST_TOKEN"]}, "enabled": True, "timeout_s": 120,
    })
    api("PATCH", {"mcp_servers": servers})
    print("    registered")
PY
$SCP "$REG" "$ARDI_HOST:$REMOTE_BUILD/register.py"
rm -f "$REG"
$SSH "docker cp $REMOTE_BUILD/register.py $CORE:/tmp/register-workspace.py && \
  docker exec -e HOST_TOKEN=\"\$(docker exec $NAME sed -n 's/^token = \"\(.*\)\"$/\1/p' /config/host.toml)\" \
    $CORE /app/.venv/bin/python /tmp/register-workspace.py; docker exec $CORE rm -f /tmp/register-workspace.py"
