"""全局配置与路径管理。

所有数据目录都相对于项目根目录定位，保证系统可以任意位置启动。
"""
import os

# 项目根目录 = backend/ 的上一级
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

# 数据分片目录
PROBLEMS_DIR = os.path.join(DATA_DIR, "problems")
TESTCASES_DIR = os.path.join(DATA_DIR, "testcases")
CONTESTS_DIR = os.path.join(DATA_DIR, "contests")
SUBMISSIONS_DIR = os.path.join(DATA_DIR, "submissions")
SCORES_DIR = os.path.join(DATA_DIR, "scores")
USERS_DIR = os.path.join(DATA_DIR, "users")
FORUM_DIR = os.path.join(DATA_DIR, "forum")
SETTINGS_DIR = os.path.join(DATA_DIR, "settings")
RUNS_DIR = os.path.join(DATA_DIR, "runs")          # 沙箱运行临时目录
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "system.json")

# 服务配置
API_HOST = os.environ.get("OJ_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("OJ_PORT", "5000"))
SECRET_KEY = os.environ.get("OJ_SECRET", "online-judge-benchmark-secret-key")

# 评测引擎默认配置（可被 system.json 覆盖）
DEFAULT_SETTINGS = {
    "site_name": "在线代码评测与竞赛系统",
    "judge": {
        "max_concurrent": 4,               # 沙箱最大并发数
        "sandbox": "auto",                 # auto | docker | native
        "default_time_limit_ms": 1000,     # 默认时间限制（毫秒）
        "default_memory_limit_kb": 65536,  # 默认内存限制（KB）
        "compile_timeout_ms": 15000,       # 编译超时
        "run_timeout_multiplier": 3,       # 运行超时 = 时限 * 倍率（兜底）
        "output_limit_kb": 4096,           # 输出大小上限（KB）
        "sync_recent_cache": True,         # 是否同步内存近期提交缓存
        "redact_code": True,               # 列表视图是否脱敏代码
    },
    "anti_cheat": {
        "enabled": True,
        "similarity_threshold": 0.90,      # 归一化代码相似度阈值
        "window_seconds": 60,              # 相似提交的检测时间窗
    },
    "ranking": {
        "penalty_seconds": 20,                # ACM 每次错误提交罚时（秒）
    },
    "registration": {
        "allow": True,
    },
}

# 支持的语言（键为前端语言标识，值为沙箱执行配置）
LANGUAGES = {
    "python": {
        "name": "Python 3",
        "extension": "py",
        "compile_cmd": None,
        "run_cmd": ["python3", "main.py"],
        "docker_image": "python:3.11-slim",
        "native_interpreter": ["python3"],
    },
    "cpp": {
        "name": "C++17",
        "extension": "cpp",
        "compile_cmd": ["g++", "-O2", "-std=c++17", "main.cpp", "-o", "main"],
        "run_cmd": ["./main"],
        "docker_image": "gcc:12",
        "native_interpreter": ["g++"],
    },
    "c": {
        "name": "C11",
        "extension": "c",
        "compile_cmd": ["gcc", "-O2", "-std=c11", "main.c", "-o", "main"],
        "run_cmd": ["./main"],
        "docker_image": "gcc:12",
        "native_interpreter": ["gcc"],
    },
    "java": {
        "name": "Java 17",
        "extension": "java",
        "compile_cmd": ["javac", "Main.java"],
        "run_cmd": ["java", "-Xss64m", "Main"],
        "docker_image": "openjdk:17-slim",
        "native_interpreter": ["javac"],
    },
}

VERDICTS = ["PENDING", "JUDGING", "AC", "WA", "TLE", "MLE", "RE", "CE", "OLE", "SE"]


def ensure_dirs():
    """确保所有数据目录存在。"""
    for d in (PROBLEMS_DIR, TESTCASES_DIR, CONTESTS_DIR, SUBMISSIONS_DIR,
              SCORES_DIR, USERS_DIR, FORUM_DIR, SETTINGS_DIR, RUNS_DIR):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(SETTINGS_FILE):
        from backend.storage import atomic_write_json
        atomic_write_json(SETTINGS_FILE, DEFAULT_SETTINGS)
