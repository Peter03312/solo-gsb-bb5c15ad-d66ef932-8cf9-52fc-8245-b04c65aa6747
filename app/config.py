import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """运行参数，全部来自环境变量；每次取值都重新读取，便于测试切换数据库。"""

    @property
    def db_path(self) -> str:
        return os.environ.get("SEAL_DB_PATH", "/data/seal.db")

    @property
    def expiry_interval_seconds(self) -> float:
        return float(os.environ.get("SWEEP_INTERVAL_SECONDS", "1.0"))


settings = Settings()
