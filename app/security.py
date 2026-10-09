"""Cross-cutting request protections: CSRF origin check, response headers,
JSON error bodies, and a small in-memory rate limiter.

Everything here is applied once, in ``init_security``, so individual routes do
not each have to remember it.
"""
import ipaddress
import threading
import time
from functools import wraps
from urllib.parse import urlsplit

from flask import current_app, jsonify, request
from werkzeug.exceptions import HTTPException

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _host_of(url):
    """Lower-cased ``host[:port]`` of an Origin/Referer value ("" if unusable)."""
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def _allowed_hosts():
    """Hosts a same-site browser request may legitimately name.

    Behind Apache the Host header is 127.0.0.1:5000, so the original host
    arrives in X-Forwarded-Host (Apache appends, so the last entry is the one
    Apache wrote and the client cannot spoof it). ``TRUSTED_ORIGINS`` in .env
    covers a proxy that rewrites both.
    """
    hosts = {request.host.lower()}
    forwarded = request.headers.get("X-Forwarded-Host", "")
    if forwarded:
        hosts.add(forwarded.split(",")[-1].strip().lower())
    hosts.update(current_app.config.get("TRUSTED_ORIGINS", ()))
    return hosts


def _is_local_host(host):
    """True for a LAN-style host: a private/loopback IP, ``localhost``, ``*.local``
    or a single-label name like ``backpack`` — never a public hostname."""
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0].lstrip("[")
    if not name:          # "Origin: null" (sandboxed pages) has no host at all
        return False
    try:
        ip = ipaddress.ip_address(name)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return name == "localhost" or name.endswith(".local") or "." not in name


def _behind_proxy_without_host():
    """True when Apache forwarded the request but did not say which host the
    browser used (the Host header is the loopback address Flask listens on)."""
    return (not request.headers.get("X-Forwarded-Host")
            and request.host.lower().split(":")[0] in ("127.0.0.1", "localhost", "[::1]"))


def _is_api_request():
    return request.path.startswith(("/api/", "/internal/"))


def _csrf_blocked():
    """True when a state-changing request came from a different site.

    Browsers always attach Origin (or at least Referer) to cross-site POSTs, and
    a forged page cannot change them. Requests with neither header are not
    browser-driven cross-site forms (curl, FPP's own callbacks), so they pass.
    ``/internal/`` is token-authenticated and carries no session cookie.
    """
    if request.method not in _UNSAFE_METHODS or request.path.startswith("/internal/"):
        return False
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return False
    origin_host = _host_of(origin)
    if origin_host in _allowed_hosts():
        return False
    # Apache on a stock install does not pass the browser's Host through, so the
    # Origin cannot be compared with anything. Fall back to "the page that sent
    # this was served from the local network" — a public site is still refused.
    return not (_behind_proxy_without_host() and _is_local_host(origin_host))


def init_security(app):
    @app.before_request
    def _reject_cross_site_writes():
        if _csrf_blocked():
            current_app.logger.warning(
                "Blocked cross-site %s %s (origin %r; host %r, X-Forwarded-Host %r)",
                request.method, request.path,
                request.headers.get("Origin") or request.headers.get("Referer"),
                request.host, request.headers.get("X-Forwarded-Host"),
            )
            message = "Cross-site request blocked"
            if _is_api_request():
                return jsonify({"error": message}), 403
            return message, 403

    @app.after_request
    def _security_headers(response):
        # setdefault: never fight a header Apache or a route chose deliberately.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(HTTPException)
    def _http_error(error):
        # Without this, a 400/404/405/413 on an API call comes back as an HTML
        # page the front end cannot parse; the 500 handler is registered
        # separately and handles unexpected exceptions.
        if _is_api_request() and error.code and error.code >= 400:
            return jsonify({"error": error.description or error.name}), error.code
        return error


# ── Rate limiting ────────────────────────────────────────────────────────────
# Per-client sliding window held in memory. Single process (waitress threads),
# so a lock is enough; the table is pruned so it cannot grow without bound.

_rate_lock = threading.Lock()
_rate_hits: dict[tuple, list] = {}


def rate_limited(limit, per_seconds):
    """Allow at most ``limit`` calls per ``per_seconds`` per client IP per route."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            now = time.monotonic()
            key = (view.__name__, request.remote_addr or "unknown")
            with _rate_lock:
                if len(_rate_hits) > 500:
                    for k in [k for k, v in _rate_hits.items() if not v or v[-1] < now - 3600]:
                        del _rate_hits[k]
                hits = [t for t in _rate_hits.get(key, []) if t > now - per_seconds]
                if len(hits) >= limit:
                    _rate_hits[key] = hits
                    retry = max(1, int(per_seconds - (now - hits[0])))
                    response = jsonify({"error": f"Too many requests — try again in {retry} seconds."})
                    response.headers["Retry-After"] = str(retry)
                    return response, 429
                hits.append(now)
                _rate_hits[key] = hits
            return view(*args, **kwargs)
        return wrapper
    return decorator
