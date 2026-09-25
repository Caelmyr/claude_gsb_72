"""评测引擎：提交队列 + 并发调度 + 完整评测生命周期。

流程：
  submit() 创建 PENDING 提交并写入分片 → 线程池消费 →
  编译 → 逐测试点运行沙箱并比对 → 汇总裁决 → 写回提交 →
  增量更新排行榜 → 防作弊检测。

高并发调度：使用 ThreadPoolExecutor + Semaphore 双重限流，
信号量读取系统设置的 max_concurrent，可运行时调整；
线程池提供更大的队列容量以缓冲突发提交。
"""
import os
import shutil
import subprocess
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from backend import config
from backend.storage import read_json, locked_update, list_files, list_dirs
from backend.utils import now_iso, now_ts, gen_id, truncate, prob_key, strip_code, page_rows, sort_list
from backend.sandbox import get_sandbox, ST_OK, ST_TLE, ST_MLE, ST_OLE, ST_RE, ST_CE, ST_SE
from backend.judge import comparator
from backend.judge import ranking
from backend.judge import cheat

# 终态状态集合：不在此集合内的提交视为「评测中」
ACTIVE_STATUSES = ("PENDING", "JUDGING")


def _submission_dir(contest_id):
    return os.path.join(config.SUBMISSIONS_DIR, contest_id)


def _submission_path(contest_id, user_id):
    return os.path.join(_submission_dir(contest_id), f"{user_id}.json")


def _read_shard(contest_id, user_id):
    return read_json(_submission_path(contest_id, user_id))


def _case_dir(problem_id):
    return os.path.join(config.TESTCASES_DIR, f"{problem_id}.json")


class JudgeEngine:
    """评测引擎单例。"""

    def __init__(self):
        self.sandbox = get_sandbox()
        self._executor = None
        self._semaphore = None
        self._recent = deque(maxlen=5000)      # 近期完整提交（含代码，供防作弊）
        self._index = {}                        # sub_id -> (contest_id, user_id)
        self._lock = threading.Lock()
        # 活动评测登记表（纯内存）：sub_id -> {status, token, stage, ...}
        # 这是队列展示「排队中 / 运行中」的唯一事实来源；提交入队即登记，
        # 判完（终态落盘）即摘除，保证不会把已判完的提交显示为排队。
        self._active = {}
        self._job_seq = 0
        self._finished_count = 0                # 已完成数（启动时以落盘终态校准）
        self._started = False
        # 题目名内存缓存（供管理队列展示，磁盘扫描最多 10 秒一次）
        self._problem_titles = {}
        self._title_cache_ts = 0.0

    # ---- 生命周期 ----
    def start(self):
        if self._started:
            return
        maxc = self._max_concurrent()
        self._semaphore = threading.Semaphore(maxc)
        self._executor = ThreadPoolExecutor(max_workers=max(8, maxc * 2),
                                            thread_name_prefix="judge")
        self._rebuild_index()
        self._started = True
        # 后台预热沙箱镜像（不阻塞）
        warmup = getattr(self.sandbox, "warmup", None)
        if warmup is not None:
            threading.Thread(target=warmup, daemon=True).start()

    def _max_concurrent(self):
        data = read_json(config.SETTINGS_FILE, config.DEFAULT_SETTINGS)
        try:
            return max(1, int((data or {}).get("judge", {}).get("max_concurrent", 4)))
        except (ValueError, TypeError):
            return 4

    def refresh_concurrency(self):
        """根据设置调整并发上限（运行时生效）。"""
        if self._semaphore is not None:
            self._semaphore._value = self._max_concurrent()

    def _rebuild_index(self):
        """扫描全部分片，重建内存索引与近期列表。

        同时：
          1. 以落盘的终态提交数校准「已完成」计数，使队列数字与实际评测对得上；
          2. 收集上次进程异常退出时卡在 PENDING/JUDGING 的提交，重置为 PENDING
             并重新入队（否则它们会永久挂起，且队列与实际情况不符）。
        """
        with self._lock:
            self._index.clear()
            self._active.clear()
            all_subs = []
            stuck = []
            finished = 0
            for cid in list_dirs(config.SUBMISSIONS_DIR):
                cdir = os.path.join(config.SUBMISSIONS_DIR, cid)
                for uid in list_files(cdir):
                    shard = read_json(os.path.join(cdir, uid + ".json"))
                    if not shard:
                        continue
                    for s in shard.get("submissions", []):
                        self._index[s["id"]] = (cid, uid)
                        all_subs.append(s)
                        if s.get("status") in ACTIVE_STATUSES:
                            stuck.append(s)
                        else:
                            finished += 1
            all_subs.sort(key=lambda s: s.get("created_at", ""))
            for s in all_subs[-5000:]:
                self._recent.append(s)
            self._finished_count = finished
        self._requeue_stuck(stuck)

    def _requeue_stuck(self, stuck):
        """把启动时发现的非终态提交重置为 PENDING 并重新评测。"""
        for s in stuck:
            sub_id = s["id"]
            cid, uid = s.get("contest_id"), s.get("user_id")
            if not cid or not uid:
                continue
            # 落盘状态重置为 PENDING，避免永远显示「运行中」
            self._update_shard(sub_id, lambda x: x.update(
                status="PENDING", judged_at=None, details=[], score=0))
            token = self._register_active(s, "PENDING")
            self._executor.submit(self._judge_job, sub_id, cid, uid, token)

    # ---- 活动评测登记表（纯内存，队列状态的唯一事实来源）----
    def _next_token(self):
        with self._lock:
            self._job_seq += 1
            return self._job_seq

    def _register_active(self, sub, status):
        """登记一条活动提交（新提交 / 重判 / 启动恢复），返回本次评测令牌。"""
        token = self._next_token()
        with self._lock:
            self._active[sub["id"]] = {
                "id": sub["id"],
                "contest_id": sub.get("contest_id", ""),
                "problem_id": sub.get("problem_id", ""),
                "user_id": sub.get("user_id", ""),
                "username": sub.get("username", ""),
                "nickname": sub.get("nickname", ""),
                "language": sub.get("language", ""),
                "status": status,                # PENDING=排队中 JUDGING=运行中
                "stage": "排队等待" if status == "PENDING" else "开始评测",
                "total_cases": 0,
                "done_cases": 0,
                "enqueued_ts": now_ts(),
                "started_ts": None,
                "token": token,
            }
        return token

    def _claim_active(self, sub_id, token):
        """评测线程拿到执行权后，把该提交标记为运行中；令牌不匹配则让出。"""
        with self._lock:
            item = self._active.get(sub_id)
            if item is None or item["token"] != token:
                return False
            item["status"] = "JUDGING"
            item["stage"] = "编译中"
            item["started_ts"] = now_ts()
            return True

    def _set_stage(self, sub_id, token, stage, total=None, done=None):
        """更新运行中提交的大致进度（编译 / 测试点 i/n），轻量纯内存操作。"""
        with self._lock:
            item = self._active.get(sub_id)
            if item is None or item["token"] != token:
                return
            item["stage"] = stage
            if total is not None:
                item["total_cases"] = total
            if done is not None:
                item["done_cases"] = done

    def _complete_active(self, sub_id, token):
        """评测完成：摘除活动登记并累加完成数（仅当前有效令牌可操作）。"""
        with self._lock:
            item = self._active.get(sub_id)
            if item is None or item["token"] != token:
                return False
            del self._active[sub_id]
            self._finished_count += 1
            return True

    def _snapshot_active(self):
        """返回活动提交的快照（排队优先，其次按运行开始/入队时间）。"""
        with self._lock:
            items = [dict(it) for it in self._active.values()]
        items.sort(key=lambda it: (
            0 if it["status"] == "PENDING" else 1,
            it.get("started_ts") or it["enqueued_ts"],
            it["enqueued_ts"],
        ))
        return items

    # ---- 提交 ----
    def submit(self, code, language, problem_id, contest_id, user):
        """创建提交并异步评测，返回完整提交记录（状态 PENDING）。"""
        if not self._started:
            self.start()
        sub = {
            "id": gen_id("s"),
            "contest_id": contest_id,
            "problem_id": problem_id,
            "user_id": user["id"],
            "username": user.get("username", ""),
            "nickname": user.get("nickname", ""),
            "language": language,
            "code": truncate(code, 100000),
            "status": "PENDING",
            "score": 0,
            "time_ms": 0,
            "memory_kb": 0,
            "created_at": now_iso(),
            "judged_at": None,
            "compile_message": "",
            "details": [],
            "similar": None,
            "ip": user.get("_ip", ""),
        }
        self._append_shard(sub)
        with self._lock:
            self._index[sub["id"]] = (contest_id, user["id"])
            self._recent.append(sub)
        token = self._register_active(sub, "PENDING")
        # 提交到线程池
        self._executor.submit(self._judge_job, sub["id"], contest_id, user["id"], token)
        return sub

    def _append_shard(self, sub):
        def _upd(shard):
            if shard is None:
                shard = {"contest_id": sub["contest_id"], "user_id": sub["user_id"],
                         "submissions": []}
            shard.setdefault("submissions", []).append(sub)
            return shard
        locked_update(_submission_path(sub["contest_id"], sub["user_id"]), _upd, default=None)

    def _update_shard(self, sub_id, update_fn):
        """按 id 在分片中更新某条提交。"""
        contest_id, user_id = self._index.get(sub_id, (None, None))
        if contest_id is None:
            return None
        path = _submission_path(contest_id, user_id)
        updated = {}

        def _upd(shard):
            if not shard:
                return shard
            for s in shard.get("submissions", []):
                if s["id"] == sub_id:
                    update_fn(s)
                    updated["s"] = s
                    break
            return shard

        locked_update(path, _upd, default=None)
        return updated.get("s")

    # ---- 评测任务 ----
    def _judge_job(self, sub_id, contest_id, user_id, token):
        """线程池任务：执行完整评测。

        token 标识本次评测归属；若期间又触发了重判（新 token），
        本任务的队列状态变更与完成计数都会被忽略，避免统计错乱。
        """
        self._semaphore.acquire()
        try:
            self._run_judge(sub_id, contest_id, user_id, token)
        except Exception as e:
            # 兜底：任何未预期异常也要把提交判成终态并移出队列，
            # 防止一条提交永远挂在「排队中/运行中」
            self._finalize(sub_id, "SE", 0, [], f"评测异常: {e}", 0, 0, token)
        finally:
            self._semaphore.release()

    def _run_judge(self, sub_id, contest_id, user_id, token):
        # 落盘标记 JUDGING
        sub = self._update_shard(sub_id, lambda s: s.update(status="JUDGING"))
        if sub is None:
            with self._lock:
                self._active.pop(sub_id, None)
            return
        # 真正拿到执行权的瞬间确认令牌仍有效；若评测期间又触发了重判，
        # 旧任务直接让出，不做任何编译/运行，避免浪费评测资源。
        if not self._claim_active(sub_id, token):
            return
        problem = self._load_problem(sub["problem_id"])
        cases = self._load_testcases(sub["problem_id"])
        contest = self._load_contest(contest_id)

        if problem is None:
            self._finalize(sub_id, "SE", 0, [], "题目不存在", 0, 0, token)
            return

        workdir = os.path.join(config.RUNS_DIR, sub_id)
        os.makedirs(workdir, exist_ok=True)

        # 1) 编译
        self._set_stage(sub_id, token, "编译中")
        time_limit = int(problem.get("time_limit_ms", 1000))
        mem_limit = int(problem.get("memory_limit_kb", 65536))
        compile_result = self.sandbox.compile(
            sub["code"], sub["language"], workdir, config.DEFAULT_SETTINGS["judge"]["compile_timeout_ms"]
        )
        if compile_result["status"] == ST_CE:
            self._finalize(sub_id, "CE", 0, [], compile_result["message"], 0, 0, token)
            shutil.rmtree(workdir, ignore_errors=True)
            return

        # 2) 逐测试点运行 + 比对
        comp_cfg = problem.get("comparison", {})
        details = []
        total_score = 0
        max_time = 0
        max_mem = 0
        final_status = "AC"
        full_points = sum(int(c.get("points", 0)) for c in cases) or int(problem.get("points", 100))
        total_cases = len(cases)

        for idx, case in enumerate(cases, 1):
            self._set_stage(sub_id, token, f"运行测试点 {idx}/{total_cases}",
                            total=total_cases, done=idx - 1)
            res = self.sandbox.run(
                sub["language"], workdir,
                (case.get("input") or "").encode("utf-8"),
                time_limit, mem_limit,
            )
            max_time = max(max_time, res["time_ms"])
            max_mem = max(max_mem, res["memory_kb"])

            case_status = self._verdict_from_run(res)
            case_points = 0
            msg = res.get("message", "")
            if case_status == "AC":
                # 运行 OK 后再做输出比对（含 special judge 自定义校验）
                ok, cmsg = self._check_output(comp_cfg, case, res["stdout"], workdir)
                msg = cmsg
                if not ok:
                    case_status = "WA"
                else:
                    case_points = int(case.get("points", 0))
            detail = {
                "case_id": case.get("id"),
                "status": case_status,
                "time_ms": res["time_ms"],
                "memory_kb": res["memory_kb"],
                "score": case_points if case_status == "AC" else 0,
                "message": msg,
            }
            details.append(detail)
            total_score += case_points
            if case_status != "AC" and final_status == "AC":
                final_status = case_status

        if final_status == "AC":
            total_score = full_points

        # 3) 写回（终态落盘后立即摘除活动登记，队列不会残留已判完的提交）
        self._set_stage(sub_id, token, "汇总结果", total=total_cases, done=total_cases)
        self._finalize(sub_id, final_status, total_score, details,
                       compile_result["message"], max_time, max_mem, token)
        shutil.rmtree(workdir, ignore_errors=True)

        # 4) 增量更新排行榜
        if contest is not None and contest.get("visble", True):
            user = {"id": user_id, "username": sub.get("username", ""),
                    "nickname": sub.get("nickname", "")}
            try:
                ranking.record_submission(contest, user, prob_key(sub), {
                    "status": final_status,
                    "score": total_score,
                    "time_ms": 0,
                    "memory_kb": max_mem,
                })
            except Exception:
                pass

        # 5) 防作弊检测
        self._anti_cheat(sub_id)

    @staticmethod
    def _verdict_from_run(res):
        st = res["status"]
        if st == ST_OK:
            return "AC"
        if st == ST_TLE:
            return "TLE"
        if st == ST_MLE:
            return "MLE"
        if st == ST_OLE:
            return "OLE"
        if st == ST_RE:
            return "RE"
        return "SE"

    def _check_output(self, comp_cfg, case, user_output, workdir):
        """对单个测试点做输出比对。

        mode=special 时运行自定义校验器（special judge），
        否则调用 comparator 做字符串/浮点/多答案比对。
        返回 (accepted, message)。
        """
        if comp_cfg.get("mode") == "special":
            return self._run_special_checker(
                comp_cfg.get("checker", ""),
                case.get("input") or "",
                user_output,
                case.get("output") or "",
                workdir,
            )
        return comparator.compare(case.get("output") or "", user_output, comp_cfg)

    @staticmethod
    def _run_special_checker(checker_source, input_text, user_out, expected_out, workdir):
        """运行自定义校验器（special judge）。

        校验器约定（管理员编写，Python 脚本）：
          python3 checker.py <输入文件> <用户输出文件> <标准输出文件>
        校验器读取三个文件后，向 stdout 打印判定，最后一行决定结果：
          以 AC 开头（不区分大小写）→ 判为通过；
          否则 → 判为 WA，整行作为提示信息。
        校验器执行受 8 秒超时与进程数限制保护。
        """
        if not checker_source.strip():
            return False, "题目未配置校验器"
        checker_path = os.path.join(workdir, "checker.py")
        in_path = os.path.join(workdir, "case_input.txt")
        out_path = os.path.join(workdir, "user_output.txt")
        exp_path = os.path.join(workdir, "expected_output.txt")
        try:
            with open(checker_path, "w", encoding="utf-8") as f:
                f.write(checker_source)
            with open(in_path, "w", encoding="utf-8") as f:
                f.write(input_text or "")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(user_out or "")
            with open(exp_path, "w", encoding="utf-8") as f:
                f.write(expected_out or "")
        except OSError as e:
            return False, f"校验器文件写入失败: {e}"

        def _limit():
            try:
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
                resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
            except Exception:
                pass

        try:
            proc = subprocess.run(
                ["python3", checker_path, in_path, out_path, exp_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=8, cwd=workdir, preexec_fn=_limit,
            )
        except subprocess.TimeoutExpired:
            return False, "校验器超时 (Checker Timeout)"
        except OSError as e:
            return False, f"校验器运行失败: {e}"

        raw = (proc.stdout or b"").decode("utf-8", "replace")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        verdict = lines[-1] if lines else ""
        if verdict.upper().startswith("AC"):
            return True, "通过（special judge）"
        return False, verdict or f"校验器异常退出码 {proc.returncode}"

    def _finalize(self, sub_id, status, score, details, compile_message, time_ms, memory_kb, token=None):
        def _upd(s):
            s.update(
                status=status, score=score, details=details,
                compile_message=truncate(compile_message, 4000),
                time_ms=time_ms, memory_kb=memory_kb, judged_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            )
        updated = self._update_shard(sub_id, _upd)
        # 同步内存 recent 列表中的状态
        settings = read_json(config.SETTINGS_FILE, config.DEFAULT_SETTINGS)
        if (settings or {}).get("judge", {}).get("sync_recent_cache", True):
            with self._lock:
                for s in self._recent:
                    if s["id"] == sub_id:
                        s.update(status=status, score=score, time_ms=time_ms,
                                 memory_kb=memory_kb, judged_at=now_iso(), details=details)
                        break
        # 终态已落盘：只有当前有效的评测令牌才摘除活动登记并计数，
        # 防止「评测期间触发重判」时旧任务把新任务误清出队列。
        if token is not None:
            self._complete_active(sub_id, token)
        return updated

    def _anti_cheat(self, sub_id):
        contest_id, user_id = self._index.get(sub_id, (None, None))
        sub = self._get_by_id(sub_id)
        if sub is None:
            return
        settings = read_json(config.SETTINGS_FILE, config.DEFAULT_SETTINGS)
        if not (settings or {}).get("anti_cheat", {}).get("enabled", True):
            return
        with self._lock:
            recent = list(self._recent)
        is_cheat, pair = cheat.detect_similarity(sub, recent)
        if pair:
            pair.setdefault("submission_id", sub_id)
            self._update_shard(sub_id, lambda s: s.update(similar=pair["similarity"]))
            cheat.record_report(pair)

    # ---- 读取 ----
    def _load_problem(self, problem_id):
        return read_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))

    def _load_testcases(self, problem_id):
        data = read_json(_case_dir(problem_id))
        return (data or {}).get("cases", []) if data else []

    def _load_contest(self, contest_id):
        return read_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"))

    def _get_by_id(self, sub_id):
        contest_id, user_id = self._index.get(sub_id, (None, None))
        if contest_id is None:
            return None
        shard = _read_shard(contest_id, user_id)
        if not shard:
            return None
        for s in shard.get("submissions", []):
            if s["id"] == sub_id:
                return s
        return None

    def get_submission(self, sub_id, include_code=True):
        sub = self._get_by_id(sub_id)
        if sub is None:
            return None
        if not include_code:
            sub = strip_code(sub)
        return sub

    def list_submissions(self, contest_id=None, user_id=None, problem_id=None,
                         limit=50, offset=0, include_code=False):
        """列出提交（默认取全局近期列表；有过滤条件时扫描分片）。"""
        with self._lock:
            recent = list(self._recent)
        # 无过滤条件：直接取内存近期列表
        if not contest_id and not user_id and not problem_id:
            rows = recent
        else:
            rows = []
            if contest_id:
                cdir = _submission_dir(contest_id)
                for uid in list_files(cdir):
                    shard = read_json(os.path.join(cdir, uid + ".json"))
                    if not shard:
                        continue
                    for s in shard.get("submissions", []):
                        if user_id and s["user_id"] != user_id:
                            continue
                        if problem_id and s["problem_id"] != problem_id:
                            continue
                        rows.append(s)
            else:
                rows = [s for s in recent
                        if (not user_id or s.get("username") == user_id)
                        and (not problem_id or s["problem_id"] == problem_id)]

        rows = sort_list(rows, key=lambda s: s.get("created_at", ""), reverse=True)
        total = len(rows)
        page = page_rows(rows, offset, limit)
        if not include_code:
            page = [{k: v for k, v in s.items() if k != "code"} for s in page]
        return {"total": total, "items": page}

    def rejudge(self, sub_id):
        """重判某条提交。"""
        sub = self._get_by_id(sub_id)
        if sub is None:
            return False
        was_finished = sub.get("status") not in ACTIVE_STATUSES
        self._update_shard(sub_id, lambda s: s.update(status="PENDING", judged_at=None,
                                                       details=[], score=0))
        # 重判一条已完成的提交：已完成数减 1（再次判完时会加回来）；
        # 若它本来就在队列里（排队中/运行中），计数不变，仅登记新令牌，
        # 旧评测任务的收尾动作因令牌不匹配而被忽略。
        token = self._register_active(sub, "PENDING")
        with self._lock:
            if was_finished and self._finished_count > 0:
                self._finished_count -= 1
        self._executor.submit(self._judge_job, sub_id, sub["contest_id"], sub["user_id"], token)
        return True

    # ---- 统计 ----
    def stats(self):
        """引擎统计。

        pending/running 直接来自活动登记表（实时，纯内存），
        finished 为落盘终态提交数（启动校准 + 提交/重判增量维护）。
        """
        with self._lock:
            pending = sum(1 for it in self._active.values() if it["status"] == "PENDING")
            running = sum(1 for it in self._active.values() if it["status"] == "JUDGING")
            finished = self._finished_count
            recent_count = len(self._recent)
            max_concurrent = self._semaphore._value if self._semaphore is not None else self._max_concurrent()
        return {
            "pending": pending,
            "running": running,
            "active": pending + running,
            "finished": finished,
            "recent_count": recent_count,
            "max_concurrent": max_concurrent,
            "sandbox": self.sandbox.name,
        }

    def queue_snapshot(self):
        """管理页实时队列快照（纯内存读取，不触碰磁盘，不影响评测速度）。

        返回排队中/运行中的数量、每条活动提交的大致状态，以及已完成数。
        """
        items = self._snapshot_active()
        now = now_ts()
        rows = []
        for it in items:
            # 排队耗时从入队起算；运行耗时从开始评测起算（毫秒）
            base = it["started_ts"] if it["status"] == "JUDGING" and it["started_ts"] else it["enqueued_ts"]
            rows.append({
                "id": it["id"],
                "contest_id": it["contest_id"],
                "problem_id": it["problem_id"],
                "problem_title": self._problem_titles.get(it["problem_id"], it["problem_id"]),
                "username": it["username"],
                "nickname": it["nickname"],
                "language": it["language"],
                "status": it["status"],
                "stage": it["stage"],
                "total_cases": it["total_cases"],
                "done_cases": it["done_cases"],
                "elapsed_ms": max(0, int((now - base) * 1000)),
                "enqueued_ts": it["enqueued_ts"],
                "started_ts": it["started_ts"],
            })
        pending = sum(1 for r in rows if r["status"] == "PENDING")
        running = len(rows) - pending
        with self._lock:
            finished = self._finished_count
            max_concurrent = self._semaphore._value if self._semaphore is not None else self._max_concurrent()
        return {
            "pending": pending,
            "running": running,
            "finished": finished,
            "max_concurrent": max_concurrent,
            "items": rows,
            "server_ts": now,
        }

    def refresh_problem_titles(self):
        """按需刷新题目名缓存（磁盘扫描限频 10 秒一次，避免影响评测）。"""
        if now_ts() - self._title_cache_ts < 10:
            return
        titles = {}
        for pid in list_files(config.PROBLEMS_DIR):
            p = read_json(os.path.join(config.PROBLEMS_DIR, f"{pid}.json"))
            if p:
                titles[pid] = p.get("title") or pid
        with self._lock:
            self._problem_titles = titles
            self._title_cache_ts = now_ts()


# 全局单例
engine = JudgeEngine()
