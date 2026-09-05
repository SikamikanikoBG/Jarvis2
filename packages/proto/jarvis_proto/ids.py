"""Time-ordered, prefix-typed ids: ``run_18f3a2c1b9e4_7d2f9a1c``.

Sortable by creation time (millisecond hex prefix), readable in logs, safe in URLs.
"""

import secrets
import time


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):x}_{secrets.token_hex(4)}"
