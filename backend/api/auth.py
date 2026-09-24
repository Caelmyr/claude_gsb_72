"""认证与用户管理 API。"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, require_admin, \
    get_current_user, find_user_by_username, find_user_by_id
from backend.storage import read_json, locked_update, atomic_write_json, list_files
from backend.utils import now_iso, gen_id, hash_password, verify_password, sign_token

auth_bp = Blueprint("auth", __name__)


def _public_user(u):
    if not u:
        return None
    return {k: u[k] for k in ("id", "username", "nickname", "role", "email",
                              "created_at", "is_banned") if k in u}


@auth_bp.post("/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    nickname = (data.get("nickname") or "").strip() or username

    if not username or not password:
        return err("用户名和密码不能为空", 400)
    if len(password) < 4:
        return err("密码至少 4 位", 400)
    if find_user_by_username(username):
        return err("用户名已存在", 400)

    settings = read_json(config.SETTINGS_FILE, config.DEFAULT_SETTINGS)
    if not (settings or {}).get("registration", {}).get("allow", True):
        return err("系统已关闭开放注册", 403)

    user_id = gen_id("u")
    salt, digest = hash_password(password)
    user = {
        "id": user_id, "username": username, "nickname": nickname,
        "salt": salt, "password_hash": digest, "role": "user",
        "email": data.get("email", ""), "created_at": now_iso(),
        "last_login": None, "is_banned": False,
    }
    atomic_write_json(os.path.join(config.USERS_DIR, f"{user_id}.json"), user)
    token = sign_token(user_id, config.SECRET_KEY)
    return ok({"token": token, "user": _public_user(user)})


@auth_bp.post("/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user = find_user_by_username(username)
    if not user or not verify_password(password, user.get("salt"), user.get("password_hash")):
        return err("用户名或密码错误", 401, 401)
    if user.get("is_banned"):
        return err("账号已被封禁", 403, 403)

    def _upd(u):
        u["last_login"] = now_iso()
        return u
    locked_update(os.path.join(config.USERS_DIR, f"{user['id']}.json"), _upd)
    token = sign_token(user["id"], config.SECRET_KEY)
    return ok({"token": token, "user": _public_user(user)})


@auth_bp.get("/auth/me")
@require_auth
def me():
    return ok(_public_user(request.user))


@auth_bp.post("/auth/logout")
def logout():
    # token 无状态，前端清除即可
    return ok()


# ---- 用户管理（管理员） ----
@auth_bp.get("/users")
@require_admin
def list_users():
    users = []
    for uid in list_files(config.USERS_DIR):
        u = read_json(os.path.join(config.USERS_DIR, f"{uid}.json"))
        if u:
            users.append(_public_user(u))
    users.sort(key=lambda u: u.get("created_at", ""))
    return ok(users)


@auth_bp.post("/users")
@require_admin
def create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or "123456"
    if not username:
        return err("用户名不能为空", 400)
    if find_user_by_username(username):
        return err("用户名已存在", 400)
    user_id = gen_id("u")
    salt, digest = hash_password(password)
    user = {
        "id": user_id, "username": username,
        "nickname": data.get("nickname") or username,
        "salt": salt, "password_hash": digest,
        "role": data.get("role", "user"),
        "email": data.get("email", ""), "created_at": now_iso(),
        "last_login": None, "is_banned": False,
    }
    atomic_write_json(os.path.join(config.USERS_DIR, f"{user_id}.json"), user)
    return ok(_public_user(user))


@auth_bp.put("/users/<user_id>")
@require_admin
def update_user(user_id):
    user = find_user_by_id(user_id)
    if not user:
        return err("用户不存在", 404)
    data = request.get_json(silent=True) or {}

    def _upd(u):
        if "nickname" in data:
            u["nickname"] = data["nickname"] or u.get("username", "")
        if "email" in data:
            u["email"] = data["email"]
        if "role" in data and data["role"] in ("admin", "judge", "user"):
            u["role"] = data["role"]
        if "is_banned" in data:
            u["is_banned"] = bool(data["is_banned"])
        if data.get("password"):
            salt, digest = hash_password(data["password"])
            u["salt"], u["password_hash"] = salt, digest
        return u

    updated = locked_update(os.path.join(config.USERS_DIR, f"{user_id}.json"), _upd)
    return ok(_public_user(updated))


@auth_bp.delete("/users/<user_id>")
@require_admin
def delete_user(user_id):
    if user_id == request.user["id"]:
        return err("不能删除当前登录账号", 400)
    path = os.path.join(config.USERS_DIR, f"{user_id}.json")
    if not os.path.exists(path):
        return err("用户不存在", 404)
    os.remove(path)
    return ok()
