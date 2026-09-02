import logging
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

_logger = logging.getLogger(__name__)


def _clean_base_url(raw):
    """Normalize FPP_BASE_URL; fall back to the local API on garbage."""
    url = (raw or "").strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        if raw:
            _logger.warning(
                "Ignoring invalid FPP_BASE_URL %r — using http://localhost/api", raw
            )
        return "http://localhost/api"
    return url


_secret = (os.environ.get("SECRET_KEY") or "").strip()
if not _secret or _secret == "change-me-in-production":
    # An ephemeral key keeps sessions unforgeable even if .env is missing or
    # was never provisioned; the cost is that logins reset on restart.
    _logger.warning(
        "SECRET_KEY is missing from .env — using an ephemeral key. "
        "Sessions will not survive a service restart until it is set."
    )
    _secret = secrets.token_hex(32)

_db_uri = os.environ.get("DATABASE_URL", "sqlite:///fpp_ui.db")


class Config:
    SECRET_KEY = _secret
    FPP_BASE_URL = _clean_base_url(os.environ.get("FPP_BASE_URL"))
    ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
    MASTER_PIN_HASH = os.environ.get("MASTER_PIN_HASH", "")
    SQLALCHEMY_DATABASE_URI = _db_uri
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # The alert-monitor thread and request threads share the SQLite file; a
    # longer busy timeout stops "database is locked" errors under write overlap.
    if _db_uri.startswith("sqlite"):
        SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 15}}
    # Secret token for the /internal/* endpoints called by FPP playlists.
    # Generate with: python -c "import secrets; print(secrets.token_hex(24))"
    INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB upload limit
    # Public URL path Apache serves this install at (e.g. "cityname" for
    # http://<pi-ip>/cityname). Apache owns the routing; this is read only so
    # the UI can show and change it. Written by fpp-ui-set-path.
    UI_PATH = os.environ.get("UI_PATH", "CustomUI")
