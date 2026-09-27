"""Tests for client-site publishing (site_proxy.py).

Run:  python3 -m pytest tests/ -q
A real upstream HTTP server on 127.0.0.1 stands in for a client instance.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import site_proxy  # noqa: E402


class Upstream(BaseHTTPRequestHandler):
    def _reply(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/admin/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        out = json.dumps({
            "method": self.command, "path": self.path, "body": body.decode(),
            "headers": {k.lower(): v for k, v in self.headers.items()},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "session=abc; Path=/site/shop/; HttpOnly")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _reply

    def log_message(self, *a):
        pass


@pytest.fixture()
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def portal(expired=False):
    async def home(request):
        return PlainTextResponse("portal")
    inner = Starlette(routes=[Route("/", home), Route("/api/x", home)])
    return TestClient(site_proxy.ClientSiteMiddleware(inner, expired_fn=lambda: expired))


@pytest.fixture()
def registry(tmp_path, monkeypatch, upstream):
    path = tmp_path / "sites.json"
    monkeypatch.setenv("CLIENT_SITES_FILE", str(path))

    def write(sites):
        path.write_text(json.dumps({"version": 1, "sites": sites}))
    write({"shop": {"port": upstream, "domains": ["shop.example.org"]}})
    return write


def test_registered_site_is_proxied_with_prefix_headers(registry):
    c = portal()
    r = c.get("/site/shop/products?x=1", headers={"X-Forwarded-For": "1.2.3.4, 9.9.9.9",
                                                    "X-Forwarded-Proto": "https",
                                                    "Authorization": "Bearer portal-code"})
    assert r.status_code == 200
    seen = r.json()
    assert seen["path"] == "/products?x=1"
    h = seen["headers"]
    assert h["x-forwarded-prefix"] == "/site/shop"
    assert h["x-forwarded-for"] == "9.9.9.9"          # right-most = fleet proxy's view
    assert h["x-forwarded-proto"] == "https"
    assert h["x-forwarded-host"] == "testserver"
    assert "authorization" not in h                   # portal code never forwarded
    assert "Path=/site/shop/" in r.headers["set-cookie"]


def test_encoded_path_is_forwarded_verbatim(registry):
    assert portal().get("/site/shop/blog/a%2Fb").json()["path"] == "/blog/a%2Fb"


def test_post_body_passes_through(registry):
    r = portal().post("/site/shop/contact", data={"name": "A"})
    assert r.json()["method"] == "POST" and r.json()["body"] == "name=A"


def test_bare_prefix_redirects_to_slash(registry):
    r = portal().get("/site/shop", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/site/shop/"


def test_redirect_location_passes_through(registry):
    r = portal().get("/site/shop/redirect", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin/login"


def test_unregistered_slug_falls_through_to_portal(registry):
    assert portal().get("/site/nope/").status_code == 404
    assert portal().get("/").text == "portal"


def test_custom_domain_serves_site_at_root(registry):
    r = portal().get("/api/x", headers={"Host": "shop.example.org"})
    assert r.json()["path"] == "/api/x"             # the site's, not the portal's
    assert "x-forwarded-prefix" not in r.json()["headers"]
    assert portal().get("/api/x").text == "portal"  # portal host unaffected


def test_disabled_or_bad_entries_are_not_served(registry, upstream):
    registry({"shop": {"port": upstream, "enabled": False},
              "bad": {"port": "8097"}, "Bad_Slug": {"port": upstream}})
    c = portal()
    for p in ("/site/shop/", "/site/bad/", "/site/Bad_Slug/"):
        assert c.get(p).status_code == 404


def test_expired_trial_serves_neutral_503(registry):
    r = portal(expired=True).get("/site/shop/")
    assert r.status_code == 503 and b"temporarily unavailable" in r.content


def test_site_down_is_502(registry, tmp_path):
    registry({"shop": {"port": 1025}})  # nothing listens there
    r = portal().get("/site/shop/")
    assert r.status_code == 502
