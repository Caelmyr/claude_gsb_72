"""防作弊检测。

主要手段：
  1. 代码相似度检测：对源码做归一化（去注释、去空白、统一标识符）后计算
     SimHash 指纹，再对相近指纹做精确相似度（汉明距离）聚类；
  2. 时间窗检测：同一题目在短时间内出现高度相似提交则标记；
  3. 完全一致检测：直接哈希比对，捕获原样抄袭。

检测结果写入 data/settings/cheat_report.json，并可在提交记录中标记。
"""
import hashlib
import os
import re

from backend import config
from backend.storage import read_json, locked_update
from backend.utils import now_iso

REPORT_FILE = os.path.join(config.SETTINGS_DIR, "cheat_report.json")

# 注释 / 字符串 / 空白 的正则（语言相关，做尽力归一化）
_COMMENT_RE = re.compile(r"(#.*$|//.*$|/\*.*?\*/|/\*.*)", re.MULTILINE | re.DOTALL)
_IDENT_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b")
_WS_RE = re.compile(r"\s+")


def normalize_code(code):
    """归一化源码：去注释、去空白、统一标识符为占位符。

    归一化后保留结构信息（关键字、操作符、括号、字符串字面量），
    使「改变量名/换行/加注释」的抄袭能被识别。
    """
    if not code:
        return ""
    code = _COMMENT_RE.sub(" ", code)
    # 字符串字面量统一为占位符（保留结构但不因文案差异误判）
    code = re.sub(r'"[^"\n]*"', '"S"', code)
    code = re.sub(r"'[^'\n]*'", "'S'", code)
    # 数字统一
    code = re.sub(r"\b\d+(\.\d+)?\b", "N", code)
    # 标识符统一为 V
    code = _IDENT_RE.sub("V", code)
    # 折叠空白
    code = _WS_RE.sub("", code)
    return code


def _simhash(text, bits=64):
    """计算文本的 SimHash 指纹（64bit）。"""
    v = [0] * bits
    # 以 3-gram 作为特征
    grams = set()
    n = len(text)
    for i in range(max(1, n - 2)):
        grams.add(text[i:i + 3])
    if not grams:
        grams.add(text)
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest()[:16], 16)
        for b in range(bits):
            if (h >> b) & 1:
                v[b] += 1
            else:
                v[b] -= 1
    fp = 0
    for b in range(bits):
        if v[b] > 0:
            fp |= (1 << b)
    return fp


def _hamming(a, b):
    return bin(a ^ b).count("1")


def _record_for(pair):
    a, b = pair
    return {
        "id": a["id"],
        "user_id": a["user_id"],
        "username": a.get("username", ""),
        "problem_id": a.get("problem_id", ""),
        "created_at": a.get("created_at", ""),
    }


def detect_similarity(submission, all_recent):
    """对一次新提交与近期提交做相似度检测。

    返回 (is_cheat: bool, similar_pair: dict|None)。
    """
    code_norm = normalize_code(submission.get("code", ""))
    if len(code_norm) < 50:
        return False, None
    fp = _simhash(code_norm)

    best = None
    best_score = 0.0
    threshold = float(
        config.DEFAULT_SETTINGS["anti_cheat"]["similarity_threshold"]
    )
    for other in all_recent:
        if other.get("id") == submission.get("id"):
            continue
        if other.get("problem_id") != submission.get("problem_id"):
            continue
        if other.get("user_id") == submission.get("user_id"):
            continue
        o_norm = normalize_code(other.get("code", ""))
        if len(o_norm) < 50:
            continue
        o_fp = _simhash(o_norm)
        ham = _hamming(fp, o_fp)
        # 汉明距离 -> 近似相似度：<=3 视为高相似，<=10 需精确验证
        if ham > 12:
            continue
        if o_norm == code_norm:
            sim = 1.0
        else:
            # 精确 Jaccard（3-gram）
            a = set(code_norm[i:i + 3] for i in range(max(1, len(code_norm) - 2)))
            b = set(o_norm[i:i + 3] for i in range(max(1, len(o_norm) - 2)))
            if not a or not b:
                sim = 0.0
            else:
                sim = len(a & b) / len(a | b)
        if sim > best_score:
            best_score = sim
            best = {"a": _record_for(submission), "b": _record_for(other), "similarity": round(sim, 4)}
    if best and best_score >= threshold:
        return True, best
    return False, best


def record_report(entry):
    """把一次疑似作弊对写入报告文件。"""
    def _upd(d):
        if d is None:
            d = {"reports": [], "updated_at": ""}
        d.setdefault("reports", []).insert(0, entry)
        d["reports"] = d["reports"][:500]
        d["updated_at"] = now_iso()
        return d
    locked_update(REPORT_FILE, _upd, default=None)


def get_report():
    """读取防作弊报告。"""
    return read_json(REPORT_FILE, {"reports": [], "updated_at": ""})


def sha1_code(code):
    return hashlib.sha1((code or "").encode("utf-8", "replace")).hexdigest()
