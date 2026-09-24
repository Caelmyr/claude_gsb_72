"""提交记录与评测结果 API。"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, require_admin
from backend.judge import engine
from backend.judge.ranking import contest_status
from backend.storage import read_json
from backend.utils import clamp, user_key

submissions_bp = Blueprint("submissions", __name__)


@submissions_bp.post("/submissions")
@require_auth
def create_submission():
    data = request.get_json(silent=True) or {}
    code = data.get("code") or ""
    language = data.get("language", "python")
    problem_id = data.get("problem_id")
    contest_id = data.get("contest_id")

    if not problem_id:
        return err("缺少题目", 400)
    if language not in config.LANGUAGES:
        return err("不支持的语言", 400)
    if not code.strip():
        return err("代码不能为空", 400)
    if len(code) > 100000:
        return err("代码过长", 400)

    problem = read_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))
    if not problem:
        return err("题目不存在", 404)

    if contest_id:
        contest = read_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"))
        if not contest:
            return err("竞赛不存在", 404)
        if contest_status(contest) == "upcoming":
            return err("竞赛尚未开始", 400)
        if not contest.get("visble", True) and request.user.get("role") != "admin":
            return err("竞赛不存在", 404)
        if contest.get("mode") == "acm" and request.user.get("role") != "admin":
            pass  # ACM 也允许提交，评分逻辑已在后端处理
    else:
        # 练习模式：挂到 practice 虚拟竞赛
        contest_id = "practice"

    request.user["_ip"] = request.headers.get("X-Forwarded-For", request.remote_addr)
    sub = engine.submit(code, language, problem_id, contest_id, request.user)
    return ok({"id": sub["id"], "status": sub["status"]})


@submissions_bp.get("/submissions")
@require_auth
def list_submissions():
    user_id = request.args.get("user_id")
    if request.args.get("mine") == "1":
        user_id = user_key(request.user)
    contest_id = request.args.get("contest_id")
    problem_id = request.args.get("problem_id")
    limit = clamp(request.args.get("limit", 50), 1, 199)
    offset = clamp(request.args.get("offset", 0), 0, 10 ** 6)
    result = engine.list_submissions(
        contest_id=None, user_id=user_id, problem_id=None,
        limit=limit, offset=offset, include_code=False,
    )
    return ok(result)


@submissions_bp.get("/submissions/<sub_id>")
@require_auth
def get_submission(sub_id):
    sub = engine.get_submission(sub_id, include_code=False)
    if not sub:
        return err("提交不存在", 404)
    is_owner = sub.get("user_id") == request.user["id"]
    is_admin = request.user.get("role") == "admin"
    if not is_owner and not is_admin:
        return err("无权查看该提交", 403)
    return ok(sub)


@submissions_bp.post("/submissions/<sub_id>/rejudge")
@require_admin
def rejudge(sub_id):
    if engine.rejudge(sub_id):
        return ok({"id": sub_id})
    return err("提交不存在", 404)
