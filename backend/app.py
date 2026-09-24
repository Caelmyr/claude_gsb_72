"""Flask 主应用：注册蓝图、服务前端静态页面、启动评测引擎。"""
import os

from flask import Flask, send_from_directory, jsonify, request
from flask_cors import CORS

from backend import config
from backend.api import register_all, err
from backend.api.contests import list_all
from backend.judge import engine


def create_app():
    app = Flask(__name__, static_folder=None)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # 提交代码上限 4MB
    CORS(app)

    # 启动前确保目录与默认设置存在
    config.ensure_dirs()
    from backend.seed import seed
    seed()

    register_all(app)

    # 评测引擎启动
    engine.start()

    # ---- 前端页面路由 ----
    PAGES = ["index", "problem", "editor", "submissions", "leaderboard",
             "contest", "users", "forum", "stats", "settings"]

    @app.get("/")
    def home():
        return send_from_directory(config.FRONTEND_DIR, "index.html")

    @app.get("/<page>.html")
    def page(page):
        if page in PAGES:
            return send_from_directory(config.FRONTEND_DIR, f"{page}.html")
        return err("页面不存在", 404, 404)

    @app.get("/assets/<path:filename>")
    def assets(filename):
        return send_from_directory(os.path.join(config.FRONTEND_DIR, "assets"), filename)

    @app.get("/api/health")
    def health():
        return jsonify({"code": 0, "data": {
            "status": "ok",
            "engine": engine.stats(),
            "contests": len(list_all()),
        }})

    @app.errorhandler(404)
    def not_found(e):
        return err("接口不存在", 404, 404)

    @app.errorhandler(413)
    def too_large(e):
        return err("请求体过大", 413, 413)

    @app.errorhandler(500)
    def server_error(e):
        return err("服务器内部错误", 500, 500)

    return app


app = create_app()
