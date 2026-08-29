"""Public URL path for this install (e.g. /cityname instead of /CustomUI).

The path is enforced entirely by Apache: it proxies /<UI_PATH>/ to Flask on
port 5000 and sets X-Forwarded-Prefix, which ProxyFix turns into correct
url_for() links. Nothing in the app routes on it. Changing the path therefore
means rewriting Apache config, which is root-only work — so it is delegated to
/usr/local/sbin/fpp-ui-set-path via sudo.

Only a bare, re-validated name is ever passed to that script; no configuration
text crosses the boundary.
"""
import os
import re
import subprocess

from flask import current_app

SETPATH_BIN = "/usr/local/sbin/fpp-ui-set-path"
DEFAULT_PATH = "CustomUI"

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# Kept in sync with the RESERVED list in deploy/fpp-ui-set-path.sh. Duplicated
# deliberately: the shell copy is what actually enforces it (it runs as root
# and is the trust boundary); this copy drives friendly errors, and is handed
# to the templates so the browser can warn before a round trip.
RESERVED_PATHS = {
    "api", "cgi-bin", "fpp", "css", "js", "images", "fonts", "doc", "docs",
    "php", "uploads", "media", "config", "backups", "logs", "tmp", "plugin",
    "plugins", "sequence", "playlist", "playlists", "effects", "models",
    "outputs", "overlays", "channel", "events", "ws", "remote", "proxy",
    "system", "status", "settings", "cache", "deps", "themes", "i18n",
    "icons", "favicon.ico", "robots.txt",
}


def validate(name):
    """Return an error string, or None if the name is usable."""
    name = (name or "").strip()
    if not name:
        return "Enter a name for the URL."
    if not _NAME_RE.match(name):
        return ("Use 1-32 characters: letters, numbers, hyphen or underscore "
                "(no spaces, slashes or punctuation).")
    if name.lower() in RESERVED_PATHS:
        return f"'{name}' is reserved by FPP — pick a different name."
    return None


def current_path():
    """The path this install is currently served at."""
    configured = (current_app.config.get("UI_PATH") or "").strip()
    return configured if _NAME_RE.match(configured or "") else DEFAULT_PATH


def apply(name):
    """Point Apache at /<name>. Returns an error string, or None on success.

    Runs synchronously so failures are reported rather than silently leaving
    the user on a dead URL. The helper validates its own input and rolls the
    config back if Apache rejects it, so a failure here leaves the current
    path working. Apache is reloaded gracefully, which lets the in-flight
    request that triggered this finish on the old config.
    """
    error = validate(name)
    if error:
        return error
    name = name.strip()

    if not os.path.exists(SETPATH_BIN):
        return ("The path helper is not installed. Re-run the plugin install, "
                "or set the path over SSH.")

    try:
        result = subprocess.run(
            ["sudo", "-n", SETPATH_BIN, name],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "Timed out while reconfiguring Apache. Check the controller over SSH."
    except Exception as exc:  # pragma: no cover - environment-dependent
        return f"Could not run the path helper: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = detail[-1] if detail else f"exit code {result.returncode}"
        current_app.logger.warning("fpp-ui-set-path failed: %s", detail)
        return f"Could not change the URL: {detail}"

    current_app.config["UI_PATH"] = name
    return None


_UPLOAD_RE = re.compile(r"/static/uploads/(?P<name>[^/?#]+)$")


def reanchor_upload_url(value):
    """Re-point a stored upload URL at the install's current URL prefix.

    Uploaded logos and backgrounds are saved to the database as absolute URLs,
    which bakes in whatever prefix was in use at upload time (/CustomUI/static/
    uploads/logo.webp). Moving the install to a new path would leave those
    pointing at a dead address, so they are rewritten on the way out.

    External URLs and anything that is not an upload are returned untouched.
    """
    if not value or "://" in value:
        return value
    match = _UPLOAD_RE.search(value)
    if not match:
        return value
    from flask import url_for
    return url_for("static", filename="uploads/" + match.group("name"))
