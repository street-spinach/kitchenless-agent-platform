"""`/owner/` serves the owner app's web export (experience EX-I45)."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from services.api import main as api
from services.api.routes import owner_static


@pytest.fixture
def dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "owner_dist"
    (root / "_expo" / "static" / "js").mkdir(parents=True)
    (root / "index.html").write_text("<html>owner index</html>")
    (root / "_expo" / "static" / "js" / "app.js").write_text("console.log('owner')")
    (tmp_path / "secret.txt").write_text("not public")
    monkeypatch.setattr(owner_static, "dist_root", lambda: root)
    return root


def _client() -> TestClient:
    return TestClient(api.create_app())


def test_owner_static_fallback(dist: Path) -> None:
    client = _client()
    asset = client.get("/owner/_expo/static/js/app.js")
    assert asset.status_code == 200
    assert "console.log" in asset.text
    # The app's own routes, the root and a directory all get index.html.
    for path in ("/owner/", "/owner/auth", "/owner/orders/123", "/owner/_expo"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "owner index" in r.text, path
        assert r.headers["content-type"].startswith("text/html"), path


def test_csp_header(dist: Path) -> None:
    client = _client()
    for path in ("/owner/", "/owner/auth", "/owner/_expo/static/js/app.js"):
        assert client.get(path).headers["content-security-policy"] == owner_static.CSP
    assert owner_static.CSP == "default-src 'self'; connect-src 'self'"


def test_path_outside_dist_is_not_served(dist: Path) -> None:
    client = _client()
    for path in ("/owner/../secret.txt", "/owner/%2e%2e/secret.txt", "/owner/..%2Fsecret.txt"):
        r = client.get(path)
        assert "not public" not in r.text, path


def test_no_export_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner_static, "dist_root", lambda: tmp_path / "missing")
    r = _client().get("/owner/auth")
    assert r.status_code == 404
    assert r.headers["content-security-policy"] == owner_static.CSP
