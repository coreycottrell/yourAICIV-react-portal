"""Client business sites, published through this portal.

A client site is a delivery-engine instance the AI stamps out
(apps/client-starter/clone_client.sh in the birth template). It listens on
127.0.0.1:<port> only. This portal already has a public address, so it
publishes each registered site at

    https://<portal address>/site/<slug>/

and, once the client's own domain points at this portal, at

    https://<client domain>/

No new DNS, tunnel or proxy config is needed for the first form.

The registry is a JSON file the AI writes with the template's
tools/client_sites.py (default ~/.client-sites.json, override with
CLIENT_SITES_FILE):

    {"version": 1,
     "sites": {"janes-wellness": {"port": 5101, "dir": "...",
                                  "domains": ["janeswellness.com"]}}}

Only slugs in the registry are served, and only ever to 127.0.0.1, so the
proxy cannot be pointed at another host. Site traffic is public by design:
the portal's access code is not required and is never forwarded. The site's
own /admin pages are protected by the site's own login.

While the AI's trial is expired (or its trial record is untrusted) the sites
answer 503 with a neutral page: nobody is watching an expired AI's sites.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Optional

import httpx

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
PREFIX = "/site/"
MAX_BODY = 20 * 1024 * 1024  # the client app itself caps uploads at 16 MB

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}
# Replaced by values the portal computes; never passed through from the browser.
_DROP_REQUEST = _HOP_BY_HOP | {
    "host", "content-length", "authorization", "forwarded",
    "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
    "x-forwarded-port", "x-forwarded-prefix", "x-real-ip",
}

_UNAVAILABLE = (
    b"<!doctype html><html><head><meta charset='utf-8'>"
    b"<meta name='viewport' content='width=device-width,initial-scale=1'>"
    b"<title>Temporarily unavailable</title></head>"
    b"<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;"
    b"padding:0 1rem;color:#333'><h1 style='font-size:1.4rem'>This site is temporarily "
    b"unavailable</h1><p>Please try again a little later.</p></body></html>"
)


def registry_path() -> Path:
    return Path(os.environ.get("CLIENT_SITES_FILE") or (Path.home() / ".client-sites.json"))


_cache: dict = {"key": None, "sites": {}, "domains": {}}


def load_sites() -> tuple[dict, dict]:
    """(slug -> port, domain -> slug), re-read whenever the file changes."""
    path = registry_path()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(path), None, None)
    if key == _cache["key"]:
        return _cache["sites"], _cache["domains"]
    sites: dict = {}
    domains: dict = {}
    if key[1] is not None:
        try:
            raw = json.loads(path.read_text())
            entries = raw.get("sites", {}) if isinstance(raw, dict) else {}
            for slug, entry in (entries.items() if isinstance(entries, dict) else []):
                if not (isinstance(slug, str) and SLUG_RE.match(slug) and isinstance(entry, dict)):
                    continue
                if entry.get("enabled") is False:
                    continue
                port = entry.get("port")
                if not (isinstance(port, int) and 1024 <= port <= 65535):
                    continue
                sites[slug] = port
                for d in entry.get("domains") or []:
                    if isinstance(d, str) and d.strip():
                        domains[d.strip().lower().rstrip(".")] = slug
        except (OSError, ValueError) as e:
            print(f"[sites] registry {path} unreadable: {e}")
    _cache.update(key=key, sites=sites, domains=domains)
    return sites, domains


def _header(scope, name: bytes) -> Optional[str]:
    for k, v in scope.get("headers", []):
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def route_for(scope) -> Optional[tuple[str, int, str, str]]:
    """(slug, port, prefix, upstream path) if this request belongs to a site."""
    if scope.get("type") not in ("http", "websocket"):
        return None
    sites, domains = load_sites()
    host = (_header(scope, b"host") or "").split(":")[0].strip().lower().rstrip(".")
    path = scope.get("path", "") or "/"
    if host and host in domains and domains[host] in sites:
        slug = domains[host]
        return slug, sites[slug], "", path
    if not path.startswith(PREFIX):
        return None
    rest = path[len(PREFIX):]
    slug, _, tail = rest.partition("/")
    if slug not in sites:
        return None
    return slug, sites[slug], PREFIX + slug, "/" + tail


class ClientSiteMiddleware:
    """Pure-ASGI: serve registered client sites, pass everything else on."""

    def __init__(self, app, expired_fn=None):
        self.app = app
        self.expired_fn = expired_fn
        self._client: Optional[httpx.AsyncClient] = None
        self._loop = None

    def client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:  # one pool per event loop
            self._loop = loop
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=5.0), follow_redirects=False,
                trust_env=False)
        return self._client

    async def __call__(self, scope, receive, send):
        route = route_for(scope)
        if route is None:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await receive()
            return await send({"type": "websocket.close", "code": 4404})
        slug, port, prefix, upstream_path = route

        if prefix and scope.get("path") == prefix:  # /site/<slug> -> /site/<slug>/
            qs = scope.get("query_string", b"")
            loc = prefix + "/" + ("?" + qs.decode("latin-1") if qs else "")
            return await _simple(send, 308, b"", [(b"location", loc.encode("latin-1"))])

        if self.expired_fn is not None:
            try:
                expired = bool(self.expired_fn())
            except Exception:
                expired = True
            if expired:
                return await _simple(send, 503, _UNAVAILABLE,
                                     [(b"content-type", b"text/html; charset=utf-8"),
                                      (b"retry-after", b"3600")])

        body = bytearray()
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body.extend(msg.get("body", b""))
            if len(body) > MAX_BODY:
                return await _simple(send, 413, b"Request too large",
                                     [(b"content-type", b"text/plain")])
            if not msg.get("more_body"):
                break

        headers = [(k.decode("latin-1"), v.decode("latin-1"))
                   for k, v in scope.get("headers", []) if k.decode("latin-1").lower() not in _DROP_REQUEST]
        host = _header(scope, b"host") or ""
        # The portal sits behind the fleet's TLS proxy: the visitor is the
        # right-most X-Forwarded-For entry that proxy appended; with no proxy
        # in front, it is the TCP peer.
        xff = _header(scope, b"x-forwarded-for")
        peer = (scope.get("client") or ("", 0))[0]
        visitor = xff.split(",")[-1].strip() if xff else peer
        proto = (_header(scope, b"x-forwarded-proto") or scope.get("scheme", "http")).split(",")[0].strip()
        headers += [("host", f"127.0.0.1:{port}"), ("x-forwarded-for", visitor),
                    ("x-forwarded-proto", proto), ("x-forwarded-host", host)]
        if prefix:
            headers.append(("x-forwarded-prefix", prefix))

        raw = (scope.get("raw_path") or b"").decode("latin-1")  # keep %-encoding intact
        if raw.startswith(prefix + "/"):
            upstream_path = raw[len(prefix):]
        qs = scope.get("query_string", b"").decode("latin-1")
        url = f"http://127.0.0.1:{port}{upstream_path}" + (f"?{qs}" if qs else "")
        try:
            req = self.client().build_request(scope["method"], url, headers=headers,
                                              content=bytes(body))
            resp = await self.client().send(req, stream=True)
        except httpx.HTTPError as e:
            print(f"[sites] {slug}: upstream 127.0.0.1:{port} unreachable: {type(e).__name__}")
            return await _simple(send, 502, _UNAVAILABLE,
                                 [(b"content-type", b"text/html; charset=utf-8")])
        try:
            out = []
            local = f"http://127.0.0.1:{port}"
            for k, v in resp.headers.raw:
                name = k.decode("latin-1").lower()
                if name in _HOP_BY_HOP:
                    continue
                if name == "location":
                    loc = v.decode("latin-1")
                    if loc.startswith(local):  # never leak the loopback address
                        loc = f"{proto}://{host}{prefix}{loc[len(local):]}"
                    v = loc.encode("latin-1")
                out.append((k.lower(), v))
            await send({"type": "http.response.start", "status": resp.status_code,
                        "headers": out})
            async for chunk in resp.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await resp.aclose()


async def _simple(send, status: int, body: bytes, headers: list):
    await send({"type": "http.response.start", "status": status,
                "headers": headers + [(b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
