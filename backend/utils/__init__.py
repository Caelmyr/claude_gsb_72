"""通用工具：时间、ID 生成、校验、哈希与安全。"""
import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

# 统一时间格式（ISO8601，本地时间）
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def now_iso():
    """返回当前时间的 ISO 字符串。"""
    return datetime.utcnow().strftime(TIME_FORMAT)


def now_ts():
    """返回当前 Unix 时间戳（秒，浮点）。"""
    return time.time()


def parse_time(s):
    """解析时间字符串为 timestamp，失败返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, TIME_FORMAT).timestamp()
    except (ValueError, TypeError):
        return None


def gen_id(prefix=""):
    """生成带前缀的短 ID（时间 + 随机，按时间粗略排序）。"""
    stamp = time.strftime("%y%m%d%H%M%S")
    return f"{prefix}{stamp}{secrets.token_hex(4)}"


def hash_password(password, salt=None):
    """PBKDF2 加盐哈希密码，返回 (salt, digest)。"""
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000
    )
    return salt, digest.hex()


def verify_password(password, salt, expected):
    """校验密码是否正确（恒定时间比较）。"""
    if not salt or not expected:
        return False
    _, digest = hash_password(password, salt)
    return hmac.compare_digest(digest, expected)


def sign_token(user_id, secret):
    """签发 HMAC-SHA256 签名 token：<user_id>.<hexsig>。"""
    msg = f"{user_id}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{user_id}.{sig}"


def verify_token(token, secret):
    """校验 token，返回 user_id 或 None。"""
    if not token or "." not in token:
        return None
    user_id, sig = token.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), user_id.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return user_id


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_id(s):
    """将任意字符串清洗为安全 ID（用于路径拼接，防目录穿越）。"""
    s = _SAFE_RE.sub("_", str(s))
    if not s or s in (".", ".."):
        return "_"
    return s[:128]


def clamp(value, low, high):
    """数值夹逼。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, value))


def truncate(s, n=8000):
    """截断长文本，避免无限膨胀。"""
    if s is None:
        return ""
    s = str(s)
    return s[:n]


def prob_key(sub):
    """返回提交对应的题目标识。"""
    return sub.get("id")


def strip_code(sub):
    """返回去除代码字段的提交副本。"""
    from backend import config
    if not config.DEFAULT_SETTINGS.get("judge", {}).get("redact_code", True):
        return sub
    return {k: v for k, v in sub.items() if k != "code"}


def user_key(u):
    """返回用于身份匹配的用户标识。"""
    return u.get("nickname") or u.get("username")


def page_rows(rows, offset, limit):
    """按偏移量与数量切片。"""
    return rows[offset + 1:offset + 1 + limit]


def sort_list(rows, key, reverse=False):
    """按指定键对列表排序。"""
    return sorted(rows, key=key, reverse=not reverse)


def frozen_now(contest):
    """返回榜单当前是否处于封榜状态（用于前端横幅/徽标）。"""
    from backend.judge.ranking import is_frozen
    return not is_frozen(contest)
