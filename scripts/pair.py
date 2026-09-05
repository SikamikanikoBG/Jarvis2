"""Print the pairing URL and a QR code in the terminal — scan it with the phone.

    uv run python scripts/pair.py --base http://100.97.120.53:9020 --token <owner token>

Asks the core for /api/pair (so the URL is whatever the core knows it is reachable on,
settings.public_url winning) and renders the QR as text. Warns when the URL is not https:
the phone's microphone and PWA install both need a secure context.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def fetch_pair(base: str, token: str | None) -> dict[str, str]:
    req = urllib.request.Request(
        base.rstrip("/") + "/api/pair", headers={"Authorization": f"Bearer {token}"} if token else {}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9020")
    ap.add_argument("--token", default=None)
    ap.add_argument("--url", default=None, help="encode this URL instead of asking the core")
    args = ap.parse_args()

    if args.url:
        url = args.url
    else:
        try:
            url = fetch_pair(args.base, args.token)["url"]
        except Exception as exc:  # noqa: BLE001
            print(f"could not reach {args.base}/api/pair: {exc}", file=sys.stderr)
            return 1

    import qrcode

    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    print()
    qr.print_ascii(invert=True)
    print(f"\n  {url}\n")
    if not url.startswith("https://"):
        print("  WARNING: not https - the phone will refuse the microphone (STT) and the PWA")
        print("  install on this URL. Put the core behind Tailscale Serve and set")
        print("  settings.public_url to the https:// address.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
