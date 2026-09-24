"""沙箱：编译执行 + 资源限制 + 超时终止 + 安全隔离。

支持两种后端：
  - DockerSandbox  使用 docker run 的 --memory/--cpus/--pids-limit/--network none
                   实现强隔离与精确资源限制；
  - NativeSandbox  使用 subprocess + setrlimit(RLIMIT_CPU/AS/...) + 看门狗线程
                   实现无 Docker 环境下的降级隔离。

上层通过 `get_sandbox()` 获得后端实例，对外接口统一为
`compile()` 与 `run()`，返回标准化的结果字典，屏蔽后端差异。

并发调度：本模块本身不做并发控制，由 judge.JudgeEngine 中的
信号量（max_concurrent）统一限流，从而在高并发提交下稳定排队。
"""
import os
import re
import shutil
import signal
import subprocess
import threading
import time

from backend import config
from backend.utils import truncate

# 运行结果状态（与评测内部状态一致）
ST_OK = "OK"
ST_TLE = "TLE"
ST_MLE = "MLE"
ST_OLE = "OLE"
ST_RE = "RE"
ST_CE = "CE"
ST_SE = "SE"

# 常见终止信号 -> 状态
_SIGNAL_STATE = {
    signal.SIGXCPU: ST_TLE,
    signal.SIGKILL: ST_TLE,   # 被看门狗 / OOM 杀掉时结合内存判断
    signal.SIGSEGV: ST_RE,
    signal.SIGABRT: ST_RE,
    signal.SIGFPE: ST_RE,
    signal.SIGBUS: ST_RE,
}


def _empty_result(status=ST_SE, message=""):
    return {
        "status": status,
        "exit_code": -1,
        "signal": 0,
        "time_ms": 0,
        "memory_kb": 0,
        "stdout": "",
        "stderr": truncate(message),
        "message": truncate(message),
    }


def _read_fd_capped(fd, cap_bytes):
    """读取一个文件描述符直到 EOF，但只保留前 cap_bytes，
    统计总字节数，防止子进程输出撑爆内存（OLE 判定依据）。"""
    chunks = []
    total = 0
    keep = cap_bytes
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        total += len(chunk)
        if keep > 0:
            take = chunk[:keep]
            chunks.append(take)
            keep -= len(take)
    try:
        os.close(fd)
    except OSError:
        pass
    return b"".join(chunks), total


class NativeSandbox:
    """原生子进程沙箱。

    安全隔离手段（无 Docker 时的降级方案）：
      - setrlimit：CPU / 地址空间 / 文件大小 / 进程数 / 文件描述符数；
      - 看门狗线程：超时强制 kill 整个进程组；
      - setsid：创建独立会话，避免信号波及评测进程；
      - 可选降权：若以 root 运行且存在 nobody 用户则 setuid(nobody)。
    """

    name = "native"

    def __init__(self):
        self._lock = threading.Lock()
        self._available = True

    @staticmethod
    def available():
        return True  # 只要 Python 能跑就能用

    def _setup_limits(self):
        """在 fork 后的子进程中设置资源限制（preexec_fn）。"""
        import resource
        limits = resource.getrlimit
        try:
            # 文件大小上限（防止写超大文件）
            limits(resource.RLIMIT_FSIZE)  # 占位，保持接口一致
            resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
            # 进程数上限
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
            # 文件描述符上限
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        except (ValueError, OSError):
            pass
        try:
            # 降权到 nobody
            if os.getuid() == 0:
                import pwd
                try:
                    nobody = pwd.getpwnam("nobody")
                    os.setgid(nobody.pw_gid)
                    os.setuid(nobody.pw_uid)
                except (KeyError, OSError):
                    pass
        except Exception:
            pass

    def _set_cpu_as(self, time_limit_seconds, memory_limit_kb):
        """在子进程中设置 CPU 与地址空间限制。"""
        import resource
        cpu = max(1, int(time_limit_seconds) + 1)
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        except (ValueError, OSError):
            pass
        # 地址空间软上限给足余量（防误杀），MLE 以 ru_maxrss 实测为准
        as_limit = max(256 * 1024, memory_limit_kb * 4)
        try:
            resource.setrlimit(resource.RLIMIT_AS, (as_limit * 1024, as_limit * 1024))
        except (ValueError, OSError):
            pass

    def compile(self, source, language, workdir, timeout_ms):
        """编译源代码，返回 result（status=OK 表示编译成功，CE 表示编译错误）。"""
        cfg = config.LANGUAGES[language]
        compile_cmd = cfg.get("compile_cmd")

        # 解释型语言也需要落盘源码供 run 执行
        src_file = os.path.join(workdir, f"main.{cfg['extension']}")
        with open(src_file, "w", encoding="utf-8") as f:
            f.write(source)
        if not compile_cmd:
            return _empty_result(ST_OK)

        try:
            proc = subprocess.run(
                compile_cmd,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_ms / 1000.0,
                preexec_fn=self._setup_limits,
            )
        except subprocess.TimeoutExpired:
            return _empty_result(ST_CE, "编译超时 (Compile Timeout)")
        except OSError as e:
            return _empty_result(ST_CE, f"编译器不可用: {e}")

        out = (proc.stdout or b"") + (proc.stderr or b"")
        if proc.returncode != 0:
            return _empty_result(ST_CE, out.decode("utf-8", "replace"))
        return _empty_result(ST_OK)

    def run(self, language, workdir, stdin_bytes, time_limit_ms, memory_limit_kb):
        """在已编译的 workdir 中运行程序，施加资源限制并测量时间/内存。"""
        cfg = config.LANGUAGES[language]
        run_cmd = cfg["run_cmd"]
        output_cap = config.DEFAULT_SETTINGS["judge"]["output_limit_kb"] * 1024

        import resource
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.monotonic()

        stdout_r, stdout_w = os.pipe()
        stderr_r, stderr_w = os.pipe()

        proc = None
        watchdog = None
        result = _empty_result(ST_SE)

        # 看门狗：wall-clock 超时强制 kill 进程组
        hard_timeout = max(2.0, time_limit_ms / 1000.0 * 1.5 + 0.5)

        def _kill():
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    try:
                        proc.kill()
                    except OSError:
                        pass

        watchdog = threading.Timer(hard_timeout, _kill)
        watchdog.daemon = True

        try:
            proc = subprocess.Popen(
                run_cmd,
                cwd=workdir,
                stdin=subprocess.PIPE,
                stdout=stdout_w,
                stderr=stderr_w,
                start_new_session=True,
                preexec_fn=lambda: self._set_cpu_as(
                    time_limit_ms / 1000.0, memory_limit_kb
                ),
            )
            # 关闭父进程侧写端
            os.close(stdout_w)
            os.close(stderr_w)

            watchdog.start()

            # 喂入输入（可能阻塞，若子进程提前退出会 BrokenPipe）
            try:
                if stdin_bytes:
                    proc.stdin.write(stdin_bytes)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

            # 并发读取输出（防管道阻塞）
            out_thread = threading.Thread(target=lambda: None)
            stdout_buf = {}
            stderr_buf = {}
            total_out = {}

            def _drain(rfd, buf, total):
                data, n = _read_fd_capped(rfd, output_cap)
                buf["data"] = data
                total["n"] = n

            t1 = threading.Thread(target=_drain, args=(stdout_r, stdout_buf, total_out))
            t2 = threading.Thread(target=_drain, args=(stderr_r, stderr_buf, {}))
            t1.start()
            t2.start()

            try:
                proc.wait()
            finally:
                watchdog.cancel()
                t1.join()
                t2.join()

            elapsed_ms = int((time.monotonic() - start) * 1000)
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            mem_kb = max(0, after.ru_maxrss - before.ru_maxrss)

            stdout = stdout_buf.get("data", b"")
            stderr = stderr_buf.get("data", b"")
            out_total = total_out.get("n", len(stdout))

            returncode = proc.returncode
            if returncode < 0:
                sig = -returncode
                state = _SIGNAL_STATE.get(sig, ST_RE)
                if state == ST_TLE and mem_kb > memory_limit_kb:
                    state = ST_MLE
                result = {
                    "status": state,
                    "exit_code": returncode,
                    "signal": sig,
                    "time_ms": elapsed_ms,
                    "memory_kb": mem_kb,
                    "stdout": stdout.decode("utf-8", "replace"),
                    "stderr": stderr.decode("utf-8", "replace"),
                    "message": f"被信号 {sig} 终止",
                }
            else:
                state = ST_OK
                if mem_kb > memory_limit_kb:
                    state = ST_MLE
                if out_total > output_cap:
                    state = ST_OLE
                result = {
                    "status": state,
                    "exit_code": returncode,
                    "signal": 0,
                    "time_ms": elapsed_ms,
                    "memory_kb": mem_kb,
                    "stdout": stdout.decode("utf-8", "replace"),
                    "stderr": stderr.decode("utf-8", "replace"),
                    "message": "",
                }
            if watchdog_hit := elapsed_ms > time_limit_ms:
                # 即使正常退出但超时，也判 TLE
                if result["status"] == ST_OK:
                    result["status"] = ST_TLE
                    result["message"] = f"超出时间限制 {time_limit_ms}ms"
        except OSError as e:
            result = _empty_result(ST_SE, f"运行失败: {e}")
        finally:
            if watchdog is not None:
                watchdog.cancel()
            for fd in (stdout_r, stderr_r):
                try:
                    os.close(fd)
                except OSError:
                    pass

        return result


class DockerSandbox:
    """Docker 容器沙箱：强隔离 + 精确资源限制。

    通过 --network none / --read-only / --pids-limit / --memory / --cpus /
    --ulimit 实现安全隔离；用 `timeout` 与 cgroup 内存限制实现超时与 OOM 终止。
    """

    name = "docker"

    def __init__(self):
        self._available = None
        self._lock = threading.Lock()

    def available(self):
        with self._lock:
            if self._available is None:
                if shutil.which("docker") is None:
                    self._available = False
                else:
                    try:
                        subprocess.run(
                            ["docker", "info"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=5,
                        )
                        self._available = True
                    except (subprocess.TimeoutExpired, OSError):
                        self._available = False
            return self._available

    def warmup(self):
        """后台预拉取语言镜像，避免首次评测因拉镜像而超时。"""
        for cfg in config.LANGUAGES.values():
            try:
                subprocess.Popen(
                    ["docker", "pull", cfg["docker_image"]],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass

    def _build_run(self, language, workdir, time_limit_ms, memory_limit_kb):
        cfg = config.LANGUAGES[language]
        image = cfg["docker_image"]
        cpu_sec = max(1, int(time_limit_ms / 1000.0) + 1)
        mem = f"{max(16, memory_limit_kb // 1024)}m"
        inner = " ".join(cfg["run_cmd"])
        # 运行后从 cgroup 读取峰值内存写到 stderr，供宿主解析
        script = (
            f"timeout {cpu_sec}s sh -c '{inner}' < /sandbox/input.txt\n"
            "code=$?\n"
            "echo __MEM_PEAK__$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo 0)__ >&2\n"
            "exit $code"
        )
        return [
            "docker", "run", "--rm", "-i",
            "--network", "none",
            "--read-only",
            "--user", "65534:65534",
            "--pids-limit", "64",
            "--memory", mem,
            "--memory-swap", mem,
            "--cpus", "1.0",
            "--ulimit", f"cpu={cpu_sec}:{cpu_sec + 1}",
            "--ulimit", "fsize=268435456:268435456",
            "--ulimit", "nofile=128:128",
            "-v", f"{workdir}:/sandbox:rw",
            "--workdir", "/sandbox",
            image,
            "/bin/sh", "-c", script,
        ]

    def compile(self, source, language, workdir, timeout_ms):
        cfg = config.LANGUAGES[language]
        compile_cmd = cfg.get("compile_cmd")

        # 解释型语言也需要落盘源码供 run 执行
        src_file = os.path.join(workdir, f"main.{cfg['extension']}")
        with open(src_file, "w", encoding="utf-8") as f:
            f.write(source)
        if not compile_cmd:
            return _empty_result(ST_OK)

        image = cfg["docker_image"]
        inner = " ".join(compile_cmd)
        cmd = [
            "docker", "run", "--rm", "-i",
            "--network", "none", "--read-only",
            "--user", "65534:65534",
            "--memory", "512m", "--cpus", "1.0",
            "-v", f"{workdir}:/sandbox:rw",
            "--workdir", "/sandbox",
            image, "/bin/sh", "-c", inner,
        ]
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout_ms / 1000.0,
            )
        except subprocess.TimeoutExpired:
            return _empty_result(ST_CE, "编译超时 (Compile Timeout)")
        except OSError as e:
            return _empty_result(ST_CE, f"docker 编译失败: {e}")

        if proc.returncode != 0:
            return _empty_result(ST_CE, (proc.stdout or b"").decode("utf-8", "replace"))
        return _empty_result(ST_OK)

    def run(self, language, workdir, stdin_bytes, time_limit_ms, memory_limit_kb):
        cfg = config.LANGUAGES[language]
        with open(os.path.join(workdir, "input.txt"), "wb") as f:
            f.write(stdin_bytes or b"")

        cmd = self._build_run(language, workdir, time_limit_ms, memory_limit_kb)
        start = time.monotonic()
        # 外层超时需留出镜像拉取/冷启动余量，正常运行由内层 timeout 精确控制
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=max(45, (time_limit_ms / 1000.0) * 3 + 15),
            )
        except subprocess.TimeoutExpired:
            return _empty_result(ST_SE, "docker 运行超时（可能镜像拉取失败）")
        except OSError as e:
            return _empty_result(ST_SE, f"docker 运行失败: {e}")

        elapsed_ms = int((time.monotonic() - start) * 1000)
        stdout = (proc.stdout or b"").decode("utf-8", "replace")
        stderr = (proc.stderr or b"").decode("utf-8", "replace")

        # 解析容器内 cgroup 报告的峰值内存（字节）
        m = re.search(r"__MEM_PEAK__(\d+)__", stderr)
        mem_kb = int(m.group(1)) // 1024 if m else 0
        # 移除标记，避免污染编译错误等展示
        stderr = re.sub(r"__MEM_PEAK__\d+__\s*", "", stderr)

        rc = proc.returncode
        if rc == 124:
            return {
                "status": ST_TLE, "exit_code": rc, "signal": 0,
                "time_ms": elapsed_ms, "memory_kb": mem_kb,
                "stdout": stdout, "stderr": stderr,
                "message": f"超出时间限制 {time_limit_ms}ms",
            }
        if rc == 137:
            return {
                "status": ST_MLE, "exit_code": rc, "signal": 9,
                "time_ms": elapsed_ms, "memory_kb": max(mem_kb, memory_limit_kb),
                "stdout": stdout, "stderr": stderr,
                "message": f"超出内存限制 {memory_limit_kb}KB（被 cgroup OOM 终止）",
            }
        if rc == 139:
            return {
                "status": ST_RE, "exit_code": rc, "signal": 11,
                "time_ms": elapsed_ms, "memory_kb": mem_kb,
                "stdout": stdout, "stderr": stderr, "message": "段错误 (Segmentation Fault)",
            }
        if rc != 0:
            return {
                "status": ST_RE, "exit_code": rc, "signal": 0,
                "time_ms": elapsed_ms, "memory_kb": mem_kb,
                "stdout": stdout, "stderr": stderr,
                "message": f"非零退出码 {rc}",
            }
        return {
            "status": ST_OK, "exit_code": 0, "signal": 0,
            "time_ms": elapsed_ms, "memory_kb": mem_kb,
            "stdout": stdout, "stderr": stderr, "message": "",
        }


def get_sandbox():
    """按系统配置选择沙箱后端（auto 时优先 Docker，失败回退 native）。"""
    from backend import config as cfg
    settings = cfg.ensure_dirs  # noqa
    mode = "auto"
    try:
        from backend.storage import read_json
        data = read_json(cfg.SETTINGS_FILE, cfg.DEFAULT_SETTINGS)
        mode = (data or {}).get("judge", {}).get("sandbox", "auto")
    except Exception:
        pass

    if mode == "native":
        return NativeSandbox()
    if mode == "docker":
        s = DockerSandbox()
        return s if s.available() else NativeSandbox()

    # auto
    docker = DockerSandbox()
    if docker.available():
        return docker
    return NativeSandbox()
