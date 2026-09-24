"""统计报表 API。"""
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from flask import Blueprint, request

from backend import config
from backend.api import ok
from backend.storage import read_json, list_files, list_dirs, io_stats
from backend.utils import parse_time, now_ts, prob_key
from backend.judge import engine

stats_bp = Blueprint("stats", __name__)


def _iter_submissions():
    """遍历所有提交分片，产出提交记录。"""
    for s in list(engine._recent):
        yield s


def _iter_problems():
    for pid in list_files(config.PROBLEMS_DIR):
        p = read_json(os.path.join(config.PROBLEMS_DIR, f"{pid}.json"))
        if p:
            yield p


@stats_bp.get("/stats/overview")
def overview():
    users = len(list_files(config.USERS_DIR))
    problems = len(list_files(config.PROBLEMS_DIR))
    contests = len(list_files(config.CONTESTS_DIR))

    verdict_counter = Counter()
    lang_counter = Counter()
    problem_counter = Counter()
    problem_ac = Counter()
    hour_counter = Counter()
    user_counter = Counter()
    total = 0
    ac = 0

    now = now_ts()
    cutoff = now - 24 * 3600

    for s in _iter_submissions():
        total += 1
        st = s.get("status", "PENDING")
        verdict_counter[st] += 1
        lang_counter[s.get("language", "?")] += 1
        problem_counter[s.get("problem_id")] += 1
        user_counter[s.get("username", s.get("user_id"))] += 1
        if st == "AC":
            ac += 1
            problem_ac[prob_key(s)] += 1
        t = parse_time(s.get("created_at"))
        if t is not None and t >= cutoff:
            hour_counter[datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H")] += 1

    # 填充 24 小时时间轴（缺失小时补 0）
    hours = []
    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    for i in range(23, -1, -1):
        key = (base - timedelta(hours=i)).strftime("%Y-%m-%dT%H")
        hours.append({"hour": key, "count": hour_counter.get(key, 0)})

    problem_stats = []
    for p in _iter_problems():
        pid = p.get("id")
        total_p = problem_counter.get(pid, 0)
        ac_p = problem_ac.get(pid, 0)
        problem_stats.append({
            "id": pid, "title": p.get("title"),
            "submissions": total_p, "accepted": ac_p,
            "pass_rate": round(ac_p / total_p, 4) if total_p else 0,
        })
    problem_stats.sort(key=lambda x: -x["submissions"])

    top_users = [
        {"username": u, "submissions": c}
        for u, c in user_counter.most_common(9)
    ]

    return ok({
        "counts": {"users": users, "problems": problems, "contests": contests,
                   "submissions": total, "accepted": ac,
                   "ac_rate": round(ac / total, 4) if total else 0},
        "verdicts": dict(verdict_counter),
        "languages": dict(lang_counter),
        "timeline": hours,
        "problem_stats": problem_stats,
        "top_users": top_users,
        "engine": engine.stats(),
        "io": io_stats(),
    })


@stats_bp.get("/stats/verdicts")
def verdicts():
    c = Counter()
    for s in _iter_submissions():
        c[s.get("status", "PENDING")] += 1
    return ok(dict(c))


@stats_bp.get("/stats/cheat-report")
def cheat_report():
    from backend.api import get_current_user, err
    user = get_current_user()
    if not user or user.get("role") != "admin":
        return err("需要管理员权限", 403, 403)
    from backend.judge import cheat
    return ok(cheat.get_report())
