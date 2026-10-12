import pytest

from shamba_desk import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("SHAMBA_DB", str(path))
    db.seed(path)
    c = db.connect(path)
    yield c
    c.close()
