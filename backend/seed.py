"""示例数据初始化：默认管理员、示例用户、示例题目与测试用例、示例竞赛。

幂等：仅在用户目录为空时执行，重复启动不会覆盖已有数据。
"""
import os
import time

from backend import config
from backend.storage import atomic_write_json, read_json, list_files
from backend.utils import now_iso, hash_password, gen_id


def _make_user(user_id, username, nickname, password, role="user"):
    salt, digest = hash_password(password)
    return {
        "id": user_id, "username": username, "nickname": nickname,
        "salt": salt, "password_hash": digest, "role": role,
        "email": f"{username}@example.com", "created_at": now_iso(),
        "last_login": None, "is_banned": False,
    }


def _iso(dt):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(dt))


def seed():
    if list_files(config.USERS_DIR):
        return  # 已有数据，跳过

    # 1) 用户
    users = [
        _make_user("admin", "admin", "管理员", "admin123", role="admin"),
        _make_user("alice", "alice", "爱丽丝", "123456"),
        _make_user("bob", "bob", "鲍勃", "123456"),
        _make_user("charlie", "charlie", "查理", "123456"),
    ]
    for u in users:
        atomic_write_json(os.path.join(config.USERS_DIR, f"{u['id']}.json"), u)

    # 2) 题目
    problems = []

    def add_problem(p):
        problems.append(p)
        atomic_write_json(os.path.join(config.PROBLEMS_DIR, f"{p['id']}.json"), p)

    add_problem({
        "id": "p1001", "title": "A+B 问题", "difficulty": 1,
        "description": "输入两个整数 A 和 B，输出它们的和。",
        "input_description": "一行两个整数 A 和 B（绝对值不超过 10^9）。",
        "output_description": "一个整数，表示 A+B 的和。",
        "samples": [{"input": "1 2", "output": "3"},
                    {"input": "-5 8", "output": "3"}],
        "hint": "注意整数范围。", "tags": ["入门", "数学"],
        "time_limit_ms": 1000, "memory_limit_kb": 65536,
        "languages": ["python", "cpp", "c", "java"], "points": 100,
        "comparison": {"mode": "exact", "ignore_whitespace": True},
        "created_at": now_iso(), "updated_at": now_iso(),
    })

    add_problem({
        "id": "p1002", "title": "排序", "difficulty": 2,
        "description": "给定 N 个整数，将它们从小到大排序后输出。",
        "input_description": "第一行一个整数 N，第二行 N 个整数。",
        "output_description": "一行 N 个升序排列的整数，空格分隔。",
        "samples": [{"input": "5\n3 1 4 1 5", "output": "1 1 3 4 5"}],
        "hint": "", "tags": ["排序", "算法"],
        "time_limit_ms": 1000, "memory_limit_kb": 65536,
        "languages": ["python", "cpp", "c", "java"], "points": 100,
        "comparison": {"mode": "exact", "ignore_whitespace": True},
        "created_at": now_iso(), "updated_at": now_iso(),
    })

    add_problem({
        "id": "p1003", "title": "浮点数求和", "difficulty": 2,
        "description": "给定两个浮点数，输出它们的和，保留 2 位小数。允许浮点误差。",
        "input_description": "一行两个浮点数。",
        "output_description": "一个浮点数（保留 2 位小数，误差 ≤ 1e-6 判定通过）。",
        "samples": [{"input": "1.1 2.2", "output": "3.30"}],
        "hint": "本题使用浮点误差比较。", "tags": ["数学", "浮点"],
        "time_limit_ms": 1000, "memory_limit_kb": 65536,
        "languages": ["python", "cpp", "c", "java"], "points": 100,
        "comparison": {"mode": "float", "float_tolerance": 1e-6},
        "created_at": now_iso(), "updated_at": now_iso(),
    })

    add_problem({
        "id": "p1004", "title": "两数之和（多解）", "difficulty": 3,
        "description": "给定整数数组与目标值 target，请输出所有相加等于 target 的下标对 (i, j)（i < j）。每行输出一对，顺序任意。",
        "input_description": "第一行 N 与 target，第二行 N 个整数。",
        "output_description": "每行两个下标 i j（空格分隔，i < j），所有解都需输出，顺序无关。",
        "samples": [{"input": "4 9\n2 7 11 15", "output": "0 1"}],
        "hint": "本题为多答案题，所有解都需要输出，输出顺序无关。", "tags": ["哈希", "多解"],
        "time_limit_ms": 1000, "memory_limit_kb": 65536,
        "languages": ["python", "cpp", "c", "java"], "points": 100,
        "comparison": {"mode": "unordered", "ignore_whitespace": True},
        "created_at": now_iso(), "updated_at": now_iso(),
    })

    add_problem({
        "id": "p1005", "title": "斐波那契数列", "difficulty": 3,
        "description": "输出斐波那契数列第 N 项（从 0 开始：F(0)=0, F(1)=1），结果对 1e9+7 取模。",
        "input_description": "一个整数 N（0 ≤ N ≤ 10^6）。",
        "output_description": "一个整数，F(N) mod (10^9+7)。",
        "samples": [{"input": "10", "output": "55"}],
        "hint": "注意时间限制与取模。", "tags": ["动态规划", "数学"],
        "time_limit_ms": 2000, "memory_limit_kb": 65536,
        "languages": ["python", "cpp", "c", "java"], "points": 100,
        "comparison": {"mode": "exact", "ignore_whitespace": True},
        "created_at": now_iso(), "updated_at": now_iso(),
    })

    # 特殊判题（special judge）：任意一个合法解即可
    _any_pair_checker = '''\
import sys


def main():
    inp = open(sys.argv[1]).read().split()
    out = open(sys.argv[2]).read().split()
    n = int(inp[0])
    target = int(inp[1])
    a = list(map(int, inp[2:2 + n]))
    if len(out) != 2:
        print("WA 应输出两个下标")
        return
    try:
        i, j = int(out[0]), int(out[1])
    except ValueError:
        print("WA 下标不是整数")
        return
    if not (0 <= i < n and 0 <= j < n) or i >= j:
        print("WA 下标越界或不满足 i<j")
        return
    if a[i] + a[j] != target:
        print("WA a[%d]+a[%d]=%d != %d" % (i, j, a[i] + a[j], target))
        return
    print("AC")


main()
'''
    add_problem({
        "id": "p1006", "title": "两数之和（任意解）", "difficulty": 3,
        "description": "给定整数数组与目标值 target，输出任意一对相加等于 target 的下标对 (i, j)（i < j）。只需输出一个合法解即可。",
        "input_description": "第一行 N 与 target，第二行 N 个整数。",
        "output_description": "一行两个下标 i j（空格分隔，i < j），满足 a[i]+a[j]==target。",
        "samples": [{"input": "4 9\n2 7 11 15", "output": "0 1"}],
        "hint": "本题使用自定义校验器（special judge），任意合法解均判为通过。", "tags": ["哈希", "特殊判题"],
        "time_limit_ms": 1000, "memory_limit_kb": 65536,
        "languages": ["python", "cpp", "c", "java"], "points": 100,
        "comparison": {"mode": "special", "checker": _any_pair_checker},
        "created_at": now_iso(), "updated_at": now_iso(),
    })

    add_problem({
        "id": "p1007", "title": "素数判定", "difficulty": 2,
        "description": "给定一个整数 N，判断它是否为素数。若是素数输出 YES，否则输出 NO。",
        "input_description": "一行一个整数 N（1 ≤ N ≤ 10^9）。",
        "output_description": "一行 YES 或 NO。",
        "samples": [{"input": "17", "output": "YES"},
                    {"input": "24", "output": "NO"}],
        "hint": "1 不是素数。可只枚举到 sqrt(N)。", "tags": ["数学", "数论"],
        "time_limit_ms": 1000, "memory_limit_kb": 65536,
        "languages": ["python", "cpp", "c", "java"], "points": 100,
        "comparison": {"mode": "exact", "ignore_whitespace": True},
        "created_at": now_iso(), "updated_at": now_iso(),
    })

    add_problem({
        "id": "p1008", "title": "字符串反转", "difficulty": 1,
        "description": "给定一个仅包含小写字母的字符串，输出它的反转串。",
        "input_description": "一行一个字符串（长度不超过 10^5）。",
        "output_description": "一行反转后的字符串。",
        "samples": [{"input": "hello", "output": "olleh"},
                    {"input": "abcde", "output": "edcba"}],
        "hint": "可用切片 s[::-1] 或双指针。", "tags": ["字符串", "入门"],
        "time_limit_ms": 1000, "memory_limit_kb": 65536,
        "languages": ["python", "cpp", "c", "java"], "points": 100,
        "comparison": {"mode": "exact", "ignore_whitespace": True},
        "created_at": now_iso(), "updated_at": now_iso(),
    })

    # 3) 测试用例
    cases = {
        "p1001": [
            {"id": 1, "input": "1 2", "output": "3", "points": 20},
            {"id": 2, "input": "-5 8", "output": "3", "points": 20},
            {"id": 3, "input": "0 0", "output": "0", "points": 20},
            {"id": 4, "input": "123456 654321", "output": "777777", "points": 20},
            {"id": 5, "input": "-1000000000 1000000000", "output": "0", "points": 20},
        ],
        "p1002": [
            {"id": 1, "input": "5\n3 1 4 1 5", "output": "1 1 3 4 5", "points": 20},
            {"id": 2, "input": "3\n9 9 9", "output": "9 9 9", "points": 20},
            {"id": 3, "input": "1\n42", "output": "42", "points": 20},
            {"id": 4, "input": "6\n-2 -5 0 3 -1 8", "output": "-5 -2 -1 0 3 8", "points": 20},
            {"id": 5, "input": "4\n1000000 999999 1 2", "output": "1 2 999999 1000000", "points": 20},
        ],
        "p1003": [
            {"id": 1, "input": "1.1 2.2", "output": "3.30", "points": 34},
            {"id": 2, "input": "0.1 0.2", "output": "0.30", "points": 33},
            {"id": 3, "input": "-1.5 3.5", "output": "2.00", "points": 33},
        ],
        "p1004": [
            {"id": 1, "input": "4 9\n2 7 11 15", "output": "0 1", "points": 50},
            {"id": 2, "input": "5 6\n3 2 4 2 3", "output": "0 4\n1 2\n2 3", "points": 50},
        ],
        "p1005": [
            {"id": 1, "input": "0", "output": "0", "points": 20},
            {"id": 2, "input": "1", "output": "1", "points": 20},
            {"id": 3, "input": "10", "output": "55", "points": 20},
            {"id": 4, "input": "30", "output": "832040", "points": 20},
            {"id": 5, "input": "1000000", "output": "918091266", "points": 20},
        ],
        "p1006": [
            {"id": 1, "input": "4 9\n2 7 11 15", "output": "0 1", "points": 50},
            {"id": 2, "input": "6 8\n1 5 3 7 4 4", "output": "0 3", "points": 50},
        ],
        "p1007": [
            {"id": 1, "input": "17", "output": "YES", "points": 20},
            {"id": 2, "input": "24", "output": "NO", "points": 20},
            {"id": 3, "input": "1", "output": "NO", "points": 20},
            {"id": 4, "input": "2", "output": "YES", "points": 20},
            {"id": 5, "input": "999983", "output": "YES", "points": 20},
        ],
        "p1008": [
            {"id": 1, "input": "hello", "output": "olleh", "points": 25},
            {"id": 2, "input": "abcde", "output": "edcba", "points": 25},
            {"id": 3, "input": "a", "output": "a", "points": 25},
            {"id": 4, "input": "level", "output": "level", "points": 25},
        ],
    }
    for pid, cs in cases.items():
        atomic_write_json(os.path.join(config.TESTCASES_DIR, f"{pid}.json"),
                          {"problem_id": pid, "cases": cs})

    # 4) 竞赛（已开始，含封榜时间）
    now = time.time()
    contest = {
        "id": "c1", "title": "2026 秋季程序设计新生赛", "description": "面向新生的入门程序设计竞赛。",
        "start_time": _iso(now - 600),
        "end_time": _iso(now + 7200),
        "freeze_time": _iso(now + 3600),
        "freeze_enabled": True,
        "mode": "acm",
        "problems": [
            {"problem_id": "p1001", "points": 100, "order": 1},
            {"problem_id": "p1002", "points": 100, "order": 2},
            {"problem_id": "p1003", "points": 100, "order": 3},
            {"problem_id": "p1004", "points": 100, "order": 4},
            {"problem_id": "p1005", "points": 100, "order": 5},
            {"problem_id": "p1006", "points": 100, "order": 6},
            {"problem_id": "p1007", "points": 100, "order": 7},
            {"problem_id": "p1008", "points": 100, "order": 8},
        ],
        "visible": True,
        "created_at": now_iso(),
    }
    atomic_write_json(os.path.join(config.CONTESTS_DIR, "c1.json"), contest)
