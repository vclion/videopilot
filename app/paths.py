"""项目目录与持久化数据库位置。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_DIR = DATA_DIR / "db"


def database_path(name: str) -> Path:
    """新数据库集中保存；已有部署仍可读取根目录中的旧数据库。"""
    legacy = PROJECT_ROOT / name
    if legacy.is_file():
        return legacy
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return DB_DIR / name
