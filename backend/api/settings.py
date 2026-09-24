"""系统设置 API。"""
from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_admin
from backend.storage import read_json, locked_update
from backend.judge import engine

settings_bp = Blueprint("settings", __name__)

_PUBLIC_KEYS = ("site_name", "registration")


def _load():
    data = read_json(config.SETTINGS_FILE, config.DEFAULT_SETTINGS) or {}
    merged = dict(config.DEFAULT_SETTINGS)
    merged.update(data)
    return merged


@settings_bp.get("/settings")
def get_settings():
    data = _load()
    from backend.api import get_current_user
    user = get_current_user()
    if user and user.get("role") == "admin":
        data["_sandbox"] = engine.sandbox.name
        data["_languages"] = {k: v["name"] for k, v in config.LANGUAGES.items()}
        return ok(data)
    return ok({k: data.get(k) for k in _PUBLIC_KEYS})


@settings_bp.put("/settings")
@require_admin
def update_settings():
    data = request.get_json(silent=True) or {}
    allowed = ("site_name", "judge", "anti_cheat", "ranking", "registration")

    def _upd(cur):
        if cur is None:
            cur = {}
        for key in allowed:
            if key in data:
                cur[key] = data[key]
        return cur

    updated = locked_update(config.SETTINGS_FILE, _upd, default=None)
    engine.refresh_concurrency()
    return ok(updated)
