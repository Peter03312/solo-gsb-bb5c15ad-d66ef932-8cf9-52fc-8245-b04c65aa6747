"""幂等初始化 SQLite 库表（容器启动入口与本地均可调用）。"""
from app.config import settings
from app.db import init_db

if __name__ == "__main__":
    init_db(settings.db_path)
    print(f"database initialized at {settings.db_path}")
