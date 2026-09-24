"""题目与测试用例管理 API。"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, require_admin
from backend.storage import read_json, atomic_write_json, list_files
from backend.utils import now_iso, gen_id, sort_list

problems_bp = Blueprint("problems", __name__)


def _testcases_path(problem_id):
    return os.path.join(config.TESTCASES_DIR, f"{problem_id}.json")


def load_testcases(problem_id):
    data = read_json(_testcases_path(problem_id))
    return (data or {}).get("cases", []) if data else []


def _problem_summary(p, include_samples=True):
    if not p:
        return None
    out = {k: v for k, v in p.items()}
    out["testcase_count"] = len(load_testcases(p.get("id")))
    if not include_samples:
        out.pop("samples", None)
    return out


@problems_bp.get("/problems")
def list_problems():
    keyword = (request.args.get("q") or "").strip().lower()
    tag = request.args.get("tag")
    difficulty = request.args.get("difficulty")
    problems = []
    for pid in list_files(config.PROBLEMS_DIR):
        p = read_json(os.path.join(config.PROBLEMS_DIR, f"{pid}.json"))
        if not p:
            continue
        if keyword and keyword not in (p.get("title", "") + " " + p.get("description", "")).lower():
            continue
        if tag and tag not in p.get("tags", []):
            continue
        if difficulty:
            try:
                if int(p.get("difficulty", 0)) != int(difficulty):
                    continue
            except (TypeError, ValueError):
                pass
        problems.append(_problem_summary(p, include_samples=False))
    problems = sort_list(problems, key=lambda p: p.get("id", ""), reverse=False)
    return ok({"total": len(problems), "items": problems})


@problems_bp.get("/problems/tags")
def list_tags():
    tags = set()
    for pid in list_files(config.PROBLEMS_DIR):
        p = read_json(os.path.join(config.PROBLEMS_DIR, f"{pid}.json"))
        if p:
            tags.update(p.get("tags", []))
    return ok(sorted(tags))


@problems_bp.get("/problems/<problem_id>")
def get_problem(problem_id):
    p = read_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))
    if not p:
        return err("题目不存在", 404)
    return ok(_problem_summary(p, include_samples=True))


@problems_bp.post("/problems")
@require_admin
def create_problem():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return err("题目标题不能为空", 400)
    problem_id = data.get("id") or gen_id("p")
    problem = {
        "id": problem_id,
        "title": title,
        "description": data.get("description", ""),
        "input_description": data.get("input_description", ""),
        "output_description": data.get("output_description", ""),
        "samples": data.get("samples", []),
        "hint": data.get("hint", ""),
        "tags": data.get("tags", []),
        "difficulty": int(data.get("difficulty", 1)),
        "time_limit_ms": int(data.get("time_limit_ms", 1000)),
        "memory_limit_kb": int(data.get("memory_limit_kb", 65536)),
        "languages": data.get("languages", ["python", "cpp", "c", "java"]),
        "points": int(data.get("points", 100)),
        "comparison": data.get("comparison", {"mode": "exact", "float_tolerance": 1e-6,
                                              "ignore_whitespace": True}),
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    atomic_write_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"), problem)
    if data.get("cases") is not None:
        _save_testcases(problem_id, data.get("cases", []))
    return ok(_problem_summary(problem))


def _save_testcases(problem_id, cases):
    normalized = []
    for i, c in enumerate(cases):
        normalized.append({
            "id": c.get("id", i + 1),
            "input": c.get("input", ""),
            "output": c.get("output", ""),
            "points": int(c.get("points", 0)),
        })
    atomic_write_json(_testcases_path(problem_id),
                      {"problem_id": problem_id, "cases": normalized})
    # 同步题目中的测试点数量与样例（若未提供样例则取前若干组）
    prob_path = os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json")
    p = read_json(prob_path)
    if p:
        if not p.get("samples") and normalized:
            p["samples"] = [{"input": c["input"][:2000], "output": c["output"][:2000]}
                            for c in normalized[:3]]
        p["updated_at"] = now_iso()
        atomic_write_json(prob_path, p)


@problems_bp.put("/problems/<problem_id>")
@require_admin
def update_problem(problem_id):
    p = read_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))
    if not p:
        return err("题目不存在", 404)
    data = request.get_json(silent=True) or {}
    for key in ("title", "description", "input_description", "output_description",
                "hint", "tags", "samples", "languages", "comparison"):
        if key in data:
            p[key] = data[key]
    for key in ("difficulty", "time_limit_ms", "memory_limit_kb", "points"):
        if key in data:
            try:
                p[key] = int(data[key])
            except (TypeError, ValueError):
                pass
    if "title" in data and not (data["title"] or "").strip():
        return err("题目标题不能为空", 400)
    p["updated_at"] = now_iso()
    atomic_write_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"), p)
    if data.get("cases") is not None:
        _save_testcases(problem_id, data.get("cases", []))
    return ok(_problem_summary(p))


@problems_bp.delete("/problems/<problem_id>")
@require_admin
def delete_problem(problem_id):
    p = os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json")
    if not os.path.exists(p):
        return err("题目不存在", 404)
    os.remove(p)
    t = _testcases_path(problem_id)
    if os.path.exists(t):
        os.remove(t)
    return ok()


@problems_bp.get("/problems/<problem_id>/testcases")
@require_admin
def get_testcases(problem_id):
    return ok({"problem_id": problem_id, "cases": load_testcases(problem_id)})


@problems_bp.put("/problems/<problem_id>/testcases")
@require_admin
def set_testcases(problem_id):
    data = request.get_json(silent=True) or {}
    _save_testcases(problem_id, data.get("cases", []))
    return ok({"problem_id": problem_id, "count": len(data.get("cases", []))})
