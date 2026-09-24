"""评测结果精确比对。

支持四种比对模式（由题目 `comparison.mode` 指定）：
  - exact      逐行精确匹配（默认忽略行尾空白，可选忽略全部空白 / 大小写）；
  - float      将输出视为数值序列，逐个数做绝对/相对误差比较，解决浮点误差；
  - unordered  多答案：按「多重集」比较行，输出顺序无关（如求所有可行解）；
  - lines      逐行比较并给出首个不一致的位置（便于 WA 定位）。

比对结果统一返回 (accepted: bool, message: str)。
"""
import re
from collections import Counter

_NUM_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


def _split_lines(text):
    """按行拆分，去掉末尾 '\r'。"""
    if text is None:
        return []
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _normalize_lines(text, ignore_whitespace=False, ignore_case=False):
    """标准化文本为行列表（可去空行、去全部空白、转小写）。"""
    lines = []
    for line in _split_lines(text):
        if ignore_whitespace:
            line = "".join(line.split())
        else:
            line = line.rstrip()
        if ignore_case:
            line = line.lower()
        lines.append(line)
    # 去除首尾空行
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _extract_numbers(text):
    """提取文本中的全部数值 token（返回字符串列表，保留原始精度）。"""
    return _NUM_RE.findall(text or "")


def _numbers_close(a, b, tolerance):
    """两个数值字符串在误差范围内是否相等。"""
    try:
        fa, fb = float(a), float(b)
    except ValueError:
        return a == b
    if fa == fb:
        return True
    diff = abs(fa - fb)
    # 绝对误差 + 相对误差 双阈值
    if diff <= tolerance:
        return True
    scale = max(abs(fa), abs(fb), 1.0)
    return diff / scale <= tolerance


def _compare_exact(expected, actual, comp):
    ignore_ws = comp.get("ignore_whitespace", True)
    ignore_case = comp.get("ignore_case", False)
    exp = _normalize_lines(expected, ignore_ws, ignore_case)
    act = _normalize_lines(actual, ignore_ws, ignore_case)
    if exp == act:
        return True, ""
    # 定位首个差异
    for i, (e, a) in enumerate(zip(exp, act)):
        if e != a:
            return False, f"第 {i + 1} 行不一致：期望 `{e[:80]}`，得到 `{a[:80]}`"
    return False, f"行数不一致：期望 {len(exp)} 行，得到 {len(act)} 行"


def _compare_lines(expected, actual, comp):
    """逐行比较，遇到首个不同即返回（等价于 exact，但显式报告）。"""
    return _compare_exact(expected, actual, comp)


def _compare_float(expected, actual, comp):
    tolerance = float(comp.get("float_tolerance", 1e-6))
    exp_nums = _extract_numbers(expected)
    act_nums = _extract_numbers(actual)
    if len(exp_nums) != len(act_nums):
        return False, (
            f"数值数量不一致：期望 {len(exp_nums)} 个，得到 {len(act_nums)} 个"
        )
    for i, (e, a) in enumerate(zip(exp_nums, act_nums)):
        if not _numbers_close(e, a, tolerance):
            return False, (
                f"第 {i + 1} 个数值偏差超限：期望 {e}，得到 {a}"
                f"（误差限 {tolerance}）"
            )
    return True, ""


def _compare_unordered(expected, actual, comp):
    ignore_ws = comp.get("ignore_whitespace", True)
    ignore_case = comp.get("ignore_case", False)
    exp = [l for l in _normalize_lines(expected, ignore_ws, ignore_case) if l != ""]
    act = [l for l in _normalize_lines(actual, ignore_ws, ignore_case) if l != ""]
    ec, ac = Counter(exp), Counter(act)
    if ec == ac:
        return True, ""
    missing = list((ec - ac).elements())
    extra = list((ac - ec).elements())
    msg = []
    if missing:
        msg.append(f"缺失 {len(missing)} 行（如 `{missing[0][:60]}`）")
    if extra:
        msg.append(f"多余 {len(extra)} 行（如 `{extra[0][:60]}`）")
    return False, "；".join(msg)


_COMPARATORS = {
    "exact": _compare_exact,
    "lines": _compare_lines,
    "float": _compare_float,
    "unordered": _compare_unordered,
}


def compare(expected, actual, comp=None):
    """对单个测试点做输出比对。

    参数：
      expected  标准输出文本
      actual    程序实际输出文本
      comp      题目比对配置 dict（含 mode 等字段）
    返回 (accepted, message)。
    """
    comp = comp or {}
    mode = comp.get("mode", "exact")
    fn = _COMPARATORS.get(mode, _compare_exact)
    accepted, msg = fn(expected or "", actual or "", comp)
    if accepted:
        return True, "输出正确"
    return False, msg


def summarize(expected, actual, comp=None):
    """返回比对结论字符串（用于详情展示）。"""
    ok, msg = compare(expected, actual, comp)
    return ("正确" if ok else "错误") + ("" if ok else f"：{msg}")
