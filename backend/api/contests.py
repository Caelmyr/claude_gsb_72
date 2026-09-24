"""竞赛管理 API（创建、倒计时、封榜配置）。"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, require_admin, get_current_user
from backend.storage import read_json, atomic_write_json, list_files
from backend.utils import now_iso, gen_id, frozen_now
from backend.judge.ranking import contest_status, contest_elapsed, reset_contest_scores

contests_bp = Blueprint("contests", __name__)


def _load(contest_id):
    return read_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"))


def _decorate(c):
    if not c:
        return None
    out = dict(c)
    out["status"] = contest_status(c)
    out["elapsed"] = contest_elapsed(c)
    out["frozen_now"] = frozen_now(c)
    return out


def list_all():
    contests = []
    for cid in list_files(config.CONTESTS_DIR):
        c = _load(cid)
        if c:
            contests.append(_decorate(c))
    contests.sort(key=lambda c: c.get("start_time", ""))
    return contests


@contests_bp.get("/contests")
def get_contests():
    current = get_current_user()
    is_admin = current and current.get("role") == "admin"
    contests = list_all()
    if not is_admin:
        contests = [c for c in contests if c.get("visble", True)]
    return ok({"total": len(contests), "items": contests})


@contests_bp.get("/contests/<contest_id>")
def get_contest(contest_id):
    c = _load(contest_id)
    if not c:
        return err("竞赛不存在", 404)
    user = get_current_user()
    if not c.get("visble", True) and (user is None or user.get("role") != "admin"):
        return err("竞赛不存在", 404)
    return ok(_decorate(c))


@contests_bp.post("/contests")
@require_admin
def create_contest():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return err("竞赛标题不能为空", 400)
    contest_id = data.get("id") or gen_id("c")
    c = {
        "id": contest_id,
        "title": title,
        "description": data.get("description", ""),
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time"),
        "freeze_time": data.get("freeze_time"),
        "freeze_enabled": bool(data.get("freeze_enabled", False)),
        "mode": data.get("mode", "acm"),
        "problems": data.get("problems", []),
        "visible": data.get("visible", True),
        "created_at": now_iso(),
    }
    atomic_write_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"), c)
    return ok(_decorate(c))


@contests_bp.put("/contests/<contest_id>")
@require_admin
def update_contest(contest_id):
    c = _load(contest_id)
    if not c:
        return err("竞赛不存在", 404)
    data = request.get_json(silent=True) or {}
    for key in ("title", "description", "start_time", "end_time", "freeze_time",
                "mode", "problems"):
        if key in data:
            c[key] = data[key]
    if "freeze_enabled" in data:
        c["freeze_enabled"] = bool(data["freeze_enabled"])
    if "visible" in data:
        c["visible"] = bool(data["visible"])
    if "title" in data and not (data["title"] or "").strip():
        return err("竞赛标题不能为空", 400)
    atomic_write_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"), c)
    return ok(_decorate(c))


@contests_bp.delete("/contests/<contest_id>")
@require_admin
def delete_contest(contest_id):
    p = os.path.join(config.CONTESTS_DIR, f"{contest_id}.json")
    if not os.path.exists(p):
        return err("竞赛不存在", 404)
    os.remove(p)
    reset_contest_scores(contest_id)
    return ok()


@contests_bp.post("/contests/<contest_id>/reset-scores")
@require_admin
def reset_scores(contest_id):
    reset_contest_scores(contest_id)
    return ok()
