#!/usr/bin/env python3
# =====================================================================================
#  network.py — internet connectivity detection
# =====================================================================================
#  A single job: answer "is there internet right now?" cheaply and reliably.
#
#  Why not just try Supabase directly on every request? Two reasons:
#   1. We want the SAME answer to drive both "should I sync?" and any UI badge
#      (🟢/🔴), so it's centralized here instead of duplicated in sync.py and api.py.
#   2. A raw socket connect to a well-known host is faster and has fewer false
#      negatives than an HTTPS request to Supabase (which could be down while the
#      rest of the internet is fine — that failure belongs to cloud.py, not here).
#
#  Result is cached for CHECK_INTERVAL seconds so pages/requests never block on a
#  live network probe — the background sync loop (sync.py) refreshes it.
# =====================================================================================

import socket
import threading
import time

CHECK_HOSTS = [
    ("8.8.8.8", 53),          # Google DNS — fast, almost always reachable
    ("1.1.1.1", 53),          # Cloudflare DNS — fallback if the above is blocked
]
TIMEOUT_SECONDS = 2.5
CHECK_INTERVAL = 15  # seconds between automatic re-checks

_lock = threading.Lock()
_state = {"online": False, "last_checked": 0.0}


def _probe() -> bool:
    for host, port in CHECK_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=TIMEOUT_SECONDS):
                return True
        except OSError:
            continue
    return False


def is_online(force: bool = False) -> bool:
    """Return True/False for internet connectivity. Cached for CHECK_INTERVAL
    seconds unless force=True (e.g. the manual "Sync Now" button should always
    probe fresh rather than trust a stale cached result)."""
    with _lock:
        now = time.time()
        if force or (now - _state["last_checked"]) > CHECK_INTERVAL:
            _state["online"] = _probe()
            _state["last_checked"] = now
        return _state["online"]


def last_checked_age_seconds() -> float:
    with _lock:
        return time.time() - _state["last_checked"]
