"""scripts/tunnel.py: URL parsing, token masking and the through-the-tunnel check (no network)."""

import importlib.util
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tunnel():
    spec = importlib.util.spec_from_file_location("tunnel", ROOT / "scripts" / "tunnel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_quick_url_from_cloudflared_log(tunnel):
    banner = "2026-11-14T06:20:01Z INF |  https://sample-words-for-testing.trycloudflare.com  |"
    assert tunnel.parse_quick_url(banner) == "https://sample-words-for-testing.trycloudflare.com"
    assert tunnel.parse_quick_url("INF Requesting new quick Tunnel on https://api.trycloudflare.com...") is None
    assert tunnel.parse_quick_url("INF Registered tunnel connection connIndex=0") is None


def test_mask_never_shows_the_whole_token(tunnel):
    token = "abcdEFGHijklMNOPqrstUVWXyz0123456789"
    shown = tunnel.mask(token)
    assert token not in shown and shown.startswith("abcd") and "36 chars" in shown


def _patch_client(monkeypatch, tunnel, handler):
    real = httpx.Client
    monkeypatch.setattr(tunnel.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(tunnel.time, "sleep", lambda s: None)


def test_check_passes_when_the_lock_holds(monkeypatch, tunnel):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200 if req.headers.get("X-KraftSync-Token") == "t" * 30 else 401)
    _patch_client(monkeypatch, tunnel, handler)
    assert tunnel.check("https://x.trycloudflare.com", "t" * 30) == []


def test_check_flags_an_open_service(monkeypatch, tunnel):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})          # answers everything, token or not
    _patch_client(monkeypatch, tunnel, handler)
    problems = tunnel.check("https://x.trycloudflare.com", "t" * 30)
    assert any("WITHOUT the token answered 200" in p for p in problems)


def test_check_reports_an_unreachable_tunnel(monkeypatch, tunnel):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(502)
    _patch_client(monkeypatch, tunnel, handler)
    assert "does not reach the service" in tunnel.check("https://x.trycloudflare.com", "t" * 30, timeout_s=0.01)[0]


def test_build_command_protocol(tunnel):
    local = "http://127.0.0.1:1"
    assert tunnel.build_command("cf", local) == ["cf", "tunnel", "--no-autoupdate", "--url", local]
    assert tunnel.build_command("cf", local, "http2") == [
        "cf", "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", local]
    assert tunnel.build_command("cf", local, "http2", "kraftsync") == [
        "cf", "tunnel", "--no-autoupdate", "--protocol", "http2", "run", "--url", local, "kraftsync"]
