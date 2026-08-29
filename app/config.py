import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
    FPP_BASE_URL = os.environ.get("FPP_BASE_URL", "http://localhost/api")
    ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
    MASTER_PIN_HASH = os.environ.get("MASTER_PIN_HASH", "")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///fpp_ui.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Secret token for the /internal/* endpoints called by FPP playlists.
    # Generate with: python -c "import secrets; print(secrets.token_hex(24))"
    INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB upload limit
    # Public URL path Apache serves this install at (e.g. "cityname" for
    # http://<pi-ip>/cityname). Apache owns the routing; this is read only so
    # the UI can show and change it. Written by fpp-ui-set-path.
    UI_PATH = os.environ.get("UI_PATH", "CustomUI")
