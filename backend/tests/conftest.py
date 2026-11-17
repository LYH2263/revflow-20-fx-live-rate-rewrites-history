"""测试隔离：每个测试模块用独立 SQLite 库，显式建库，不依赖 app startup 时机。

模块级 client 夹具启动 TestClient；startup 里的 init_db 因库里已有数据而短路，
真正的建库由本文件在 pin 路径后显式完成，杜绝跨模块路径串扰。
"""
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import os  # noqa: E402

os.environ.setdefault("FIXTURES_DIR", str(ROOT / "fixtures"))

from app import db, seed  # noqa: E402
from app.main import app  # noqa: E402


def _module_db_path(module_name: str) -> Path:
    d = ROOT / "backend" / f"data_{module_name}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "app.db"
    if path.exists():
        path.unlink()
    return path


@pytest.fixture(scope="module")
def client(request):
    path = _module_db_path(request.module.__name__)
    original = db.DB_PATH
    db.DB_PATH = path
    seed.init_db()  # 在 pin 住的专属库上显式建表 + 夹具
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        db.DB_PATH = original
