"""评测队列状态 API（管理员）。

数据来自评测引擎的内存快照，不扫描磁盘，
可供管理页面高频轮询而不影响评测速度。
"""
from flask import Blueprint, request

from backend.api import ok, require_admin
from backend.judge import engine
from backend.utils import clamp

judge_bp = Blueprint("judge", __name__)


@judge_bp.get("/judge/queue")
@require_admin
def queue_status():
    """实时评测队列：排队中 / 运行中 / 已完成数量及队列明细。"""
    limit = clamp(request.args.get("limit", 50), 1, 200)
    return ok(engine.queue_status(limit=limit))
