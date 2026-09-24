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
        self._pending_count = 0
        self._finished_count = 0
        self._started = False

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
        """扫描全部分片，重建内存索引与近期列表。"""
        with self._lock:
            self._index.clear()
            all_subs = []
            for cid in list_dirs(config.SUBMISSIONS_DIR):
                cdir = os.path.join(config.SUBMISSIONS_DIR, cid)
                for uid in list_files(cdir):
                    shard = read_json(os.path.join(cdir, uid + ".json"))
                    if not shard:
                        continue
                    for s in shard.get("submissions", []):
                        self._index[s["id"]] = (cid, uid)
                        all_subs.append(s)
            all_subs.sort(key=lambda s: s.get("created_at", ""))
            for s in all_subs[-5000:]:
                self._recent.append(s)

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
            self._pending_count += 1
        # 提交到线程池
        self._executor.submit(self._judge_job, sub["id"], contest_id, user["id"])
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
    def _judge_job(self, sub_id, contest_id, user_id):
        """线程池任务：执行完整评测。"""
        self._semaphore.acquire()
        try:
            self._run_judge(sub_id, contest_id, user_id)
        finally:
            self._semaphore.release()
            with self._lock:
                self._pending_count = max(0, self._pending_count - 1)
                self._finished_count += 1

    def _run_judge(self, sub_id, contest_id, user_id):
        sub = self._update_shard(sub_id, lambda s: s.update(status="JUDGING"))
        if sub is None:
            return
        problem = self._load_problem(sub["problem_id"])
        cases = self._load_testcases(sub["problem_id"])
        contest = self._load_contest(contest_id)

        if problem is None:
            self._finalize(sub_id, "SE", 0, [], "题目不存在", 0, 0)
            return

        workdir = os.path.join(config.RUNS_DIR, sub_id)
        os.makedirs(workdir, exist_ok=True)

        # 1) 编译
        time_limit = int(problem.get("time_limit_ms", 1000))
        mem_limit = int(problem.get("memory_limit_kb", 65536))
        compile_result = self.sandbox.compile(
            sub["code"], sub["language"], workdir, config.DEFAULT_SETTINGS["judge"]["compile_timeout_ms"]
        )
        if compile_result["status"] == ST_CE:
            self._finalize(sub_id, "CE", 0, [], compile_result["message"], 0, 0)
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

        for case in cases:
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

        # 3) 写回
        self._finalize(sub_id, final_status, total_score, details,
                       compile_result["message"], max_time, max_mem)
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

    def _finalize(self, sub_id, status, score, details, compile_message, time_ms, memory_kb):
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
            return updated
        with self._lock:
            for s in self._recent:
                if s["id"] == sub_id:
                    s.update(status=status, score=score, time_ms=time_ms,
                             memory_kb=memory_kb, judged_at=now_iso(), details=details)
                    break

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
        self._update_shard(sub_id, lambda s: s.update(status="PENDING", judged_at=None,
                                                       details=[], score=0))
        self._executor.submit(self._judge_job, sub_id, sub["contest_id"], sub["user_id"])
        return True

    # ---- 统计 ----
    def stats(self):
        with self._lock:
            return {
                "pending": self._pending_count,
                "finished": self._finished_count,
                "recent_count": len(self._recent),
                "sandbox": self.sandbox.name,
            }


# 全局单例
engine = JudgeEngine()
