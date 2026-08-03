#!/usr/bin/env python3
# =====================================================================================
#  api.py — sync-related HTTP endpoints (new Blueprint, registered from main.py)
# =====================================================================================
#  Adds three JSON routes. Nothing here touches any existing route in main.py — it
#  is registered alongside the existing app, not merged into it.
# =====================================================================================

from flask import Blueprint, jsonify, session

import sync

bp = Blueprint("sync_api", __name__, url_prefix="/sync")


def _login_required_json(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify(error="not authenticated"), 401
        return view(*args, **kwargs)
    return wrapped


@bp.route("/status")
@_login_required_json
def status():
    """Polled by the UI badge (see main.py patch: templates poll this every ~20s).
    Maps sync.get_status() onto the five states from the spec:
      🟢 online & idle/synced   🔴 offline   🔄 syncing   ✅ just synced   ⚠ failed
    """
    s = sync.get_status()
    if s["sync_state"] == "syncing":
        icon, label = "🔄", "Syncing"
    elif s["sync_state"] == "failed":
        icon, label = "⚠", "Sync Failed"
    elif not s["online"]:
        icon, label = "🔴", "Offline"
    elif s["sync_state"] == "synced":
        icon, label = "✅", "Synced"
    else:
        icon, label = "🟢", "Online"
    return jsonify(
        icon=icon, label=label,
        online=s["online"], sync_state=s["sync_state"],
        last_sync_at=s["last_sync_at"], last_error=s["last_error"],
        pending_count=s["pending_count"],
    )


@bp.route("/manual", methods=["POST"])
@_login_required_json
def manual_sync():
    """Bound to the "Sync Now" button in settings.html (see main.py patch)."""
    result = sync.full_sync("manual")
    return jsonify(result)
