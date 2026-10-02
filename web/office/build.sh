#!/bin/sh
# Build the Office: the pixel-agents webview (https://github.com/pixel-agents-hq/pixel-agents, MIT,
# pinned below) that the core serves at /pixel-office/, plus boot.json — the decoded sprite messages the
# pixel-agents server would send on connect, which the core replays (features/office.py).
#
#   sh web/office/build.sh [out-dir]       # default: web/office/dist (gitignored)
#
# The Dockerfile runs this in its own stage. Locally the core finds web/office/dist by itself.
set -eu

PIXEL_AGENTS_SHA="${PIXEL_AGENTS_SHA:-3537e140c2094761beae748592aeb92ece8edfdd}"
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/dist}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> pixel-agents @ $PIXEL_AGENTS_SHA"
url="https://codeload.github.com/pixel-agents-hq/pixel-agents/tar.gz/$PIXEL_AGENTS_SHA"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" | tar -xz -C "$work" --strip-components=1
else
    wget -qO- "$url" | tar -xz -C "$work" --strip-components=1
fi

cd "$work"
node "$here/patch-transport.mjs" webview-ui/src/transport/index.ts
echo "==> npm ci (webview only)"
npm ci --ignore-scripts -w webview-ui --no-audit --no-fund
echo "==> vite build -> $out"
(cd webview-ui && npx vite build --outDir "$out" --emptyOutDir)
cp "$here/dump-assets.ts" ./dump-assets.ts
npx tsx dump-assets.ts webview-ui/public "$out/boot.json"
cp LICENSE "$out/LICENSE.pixel-agents.txt"
echo "$PIXEL_AGENTS_SHA" > "$out/VERSION"
echo "==> office built: $out"
