import pytest

from shamba_desk import db


@pytest.fixture(autouse=True)
def recorded_weather(monkeypatch):
    """Tests replay the recorded cassette, so they run offline and see the same
    rainfall every day. Tests that want live weather set SHAMBA_WEATHER=live."""
    monkeypatch.setenv("SHAMBA_WEATHER", "replay")
    monkeypatch.delenv("SHAMBA_WEATHER_CASSETTE", raising=False)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("SHAMBA_DB", str(path))
    db.seed(path)
    c = db.connect(path)
    yield c
    c.close()
