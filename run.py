#!/usr/bin/env python3
"""
在线代码评测与竞赛系统 - 一键启动脚本
"""
import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.app import app
from backend.config import API_HOST, API_PORT


def open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://localhost:{API_PORT}")


PAGES = [
    ("index.html", "题目列表"),
    ("problem.html", "题目详情"),
    ("editor.html", "代码编辑器"),
    ("submissions.html", "提交记录与评测结果"),
    ("leaderboard.html", "实时排行榜"),
    ("contest.html", "竞赛管理"),
    ("users.html", "用户管理"),
    ("forum.html", "讨论区"),
    ("stats.html", "统计报表"),
    ("settings.html", "系统设置"),
]


if __name__ == "__main__":
    print("\n" + "=" * 62)
    print("   🏆 在线代码评测与竞赛系统  (Online Judge & Contest)")
    print("=" * 62)

    print("\n🚀 系统启动中...\n")
    print("📍 页面导航:")
    print("   ┌─────────────────────────────────────────────────────┐")
    for page, title in PAGES:
        url = f"http://localhost:{API_PORT}/{page}"
        print(f"   │  {title:<12} {url:<40}│")
    print("   └─────────────────────────────────────────────────────┘")

    print("\n📡 后端 API:")
    print(f"   http://localhost:{API_PORT}/api/")

    print("\n" + "-" * 62)
    print("🔑 默认账号:  admin / admin123   （管理员）")
    print("              alice / 123456    （普通用户）")
    print("-" * 62)

    print("\n⚠️  按 Ctrl+C 停止服务器\n")

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host=API_HOST, port=API_PORT, debug=False, threaded=True)
