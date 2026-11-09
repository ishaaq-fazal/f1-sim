"""Production settings and the health check used by the host."""
from server.config import _normalise_db_url


def test_render_style_database_urls_use_psycopg():
    assert _normalise_db_url("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert _normalise_db_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _normalise_db_url("sqlite:///data/x.db") == "sqlite:///data/x.db"


def test_healthz_reports_ok_without_details(alice):
    r = alice.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_settings_always_have_a_strong_secret(tmp_path, monkeypatch):
    """Regression: an empty SECRET_KEY in dev must not leave the app signing tokens with ''."""
    import pytest

    import server.config as cfg
    monkeypatch.setattr(cfg, "ROOT", tmp_path)
    first = cfg._load(cfg.Settings(env="dev", secret_key="")).secret_key
    assert len(first) >= 32
    assert cfg._load(cfg.Settings(env="dev", secret_key="")).secret_key == first   # reused across restarts
    prod = dict(env="prod", allowed_origins=["https://example.com"])
    with pytest.raises(RuntimeError):
        cfg._load(cfg.Settings(secret_key="", **prod))
    with pytest.raises(RuntimeError):
        cfg._load(cfg.Settings(secret_key="too-short", **prod))
    assert cfg._load(cfg.Settings(secret_key="x" * 40, **prod)).secret_key == "x" * 40
