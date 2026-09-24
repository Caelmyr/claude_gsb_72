"""JSON 文件存储层：分片、原子写入与细粒度锁。

设计目标（对应项目难点「JSON 高并发写入锁粒度控制」）：
  1. 每个分片文件对应一把独立的 threading.Lock，不同分片互不阻塞；
  2. 锁按路径惰性创建并由全局字典统一管理，避免锁对象被 GC 回收；
  3. 写入采用「临时文件 + 原子 rename」保证并发下不产生半写文件；
  4. 读操作不做全局加锁，只依赖 rename 的原子性保证读到完整旧/新版本。

并发模型：评测线程池会高频写 submissions 与 scores 分片，本层保证
「按文件粒度加锁」，而不是一把大锁串行所有写操作。
"""
import json
import os
import tempfile
import threading

_global_lock = threading.Lock()   # 保护 _locks 字典自身
_locks = {}                        # path -> threading.Lock

# 进程内读写计数（用于统计报表，非关键路径）
_reads = 0
_writes = 0
_counter_lock = threading.Lock()


def _path_lock(path):
    """获取某个文件的专属锁（惰性创建）。"""
    key = os.path.normpath(os.path.abspath(path))
    with _global_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def read_json(path, default=None):
    """读取 JSON 文件，不存在或损坏时返回 default。"""
    global _reads
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        # 文件正在被重命名窗口内读取或已损坏：退化为 default
        return default
    with _counter_lock:
        _reads += 1
    return data


def atomic_write_json(path, data):
    """原子写 JSON：先写临时文件再 rename。"""
    global _writes
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    lock = _path_lock(path)
    with lock:
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            # 清理临时文件后继续抛出
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise
        with _counter_lock:
            _writes += 1


def locked_update(path, update_fn, default=None):
    """对单个 JSON 文件做「读-改-写」，全程持该文件锁，保证原子更新。

    update_fn(data) 返回新数据；这是排行榜/提交分片增量更新的核心原语。
    """
    lock = _path_lock(path)
    with lock:
        data = read_json(path, default)
        new_data = update_fn(data)
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(new_data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise
        global _writes
        with _counter_lock:
            _writes += 1
        return new_data


def list_files(directory, suffix=".json"):
    """列出目录下所有指定后缀的文件（不含后缀的文件名列表）。"""
    if not os.path.isdir(directory):
        return []
    names = []
    for name in os.listdir(directory):
        if name.endswith(suffix) and not name.startswith("."):
            names.append(name[: -len(suffix)] if suffix else name)
    names.sort()
    return names


def list_dirs(directory):
    """列出目录下的所有子目录名（用于分片容器，如 submissions 按竞赛分目录）。"""
    if not os.path.isdir(directory):
        return []
    names = [n for n in os.listdir(directory)
             if os.path.isdir(os.path.join(directory, n)) and not n.startswith(".")]
    names.sort()
    return names


def load_all(directory, suffix=".json"):
    """读取目录下所有 JSON 分片，返回 (id -> data) 字典。"""
    result = {}
    for name in list_files(directory, suffix):
        data = read_json(os.path.join(directory, name + suffix))
        if data is not None:
            result[name] = data
    return result


def io_stats():
    """返回进程内累计读写计数（供统计报表展示）。"""
    with _counter_lock:
        return {"reads": _reads, "writes": _writes}
