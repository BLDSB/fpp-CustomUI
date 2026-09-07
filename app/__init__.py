import json
import logging
import os
import threading
import time

from flask import Flask, jsonify, redirect, request, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

db = SQLAlchemy()

_logger = logging.getLogger(__name__)

_PRESET_FILE = "/home/fpp/media/config/commandPresets.json"


def _atomic_write_json(path, data, indent="\t"):
    """Write JSON via temp file + rename so a crash mid-write can't corrupt it."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _create_turn_off_lights_preset(app):
    """Create/update the 'Turn Off Lights' FPP Command Preset on startup.

    Pure local file write — safe to do before fppd is up. If the existing
    preset file is unreadable/corrupt we leave it alone rather than clobber
    presets the operator may be able to recover.
    """
    with app.app_context():
        try:
            if os.path.exists(_PRESET_FILE):
                with open(_PRESET_FILE, "r") as f:
                    data = json.load(f)
            else:
                data = {"commands": []}
        except (OSError, ValueError) as exc:
            app.logger.warning(
                "Could not read %s (%s) — leaving preset file untouched",
                _PRESET_FILE, exc,
            )
            return

        cmds = [c for c in data.get("commands", []) if c.get("name") != "Turn Off Lights"]
        cmds.extend([
            {"name": "Turn Off Lights", "command": "Overlay Model State",
             "args": ["All", "Disabled"], "multisyncCommand": False, "multisyncHosts": "", "presetSlot": 0},
            {"name": "Turn Off Lights", "command": "All Lights Off",
             "args": [], "multisyncCommand": False, "multisyncHosts": "", "presetSlot": 0},
            {"name": "Turn Off Lights", "command": "Stop Now",
             "args": [], "multisyncCommand": False, "multisyncHosts": "", "presetSlot": 0},
        ])
        data["commands"] = cmds
        try:
            _atomic_write_json(_PRESET_FILE, data)
        except OSError as exc:
            app.logger.warning("Could not write Turn Off Lights preset: %s", exc)


def _delete_legacy_turn_off_playlist(app):
    """Delete the old standalone 'Turn Off Lights' playlist if it still exists."""
    try:
        import requests
        from urllib.parse import quote
        fpp_base = app.config.get("FPP_BASE_URL", "http://localhost/api")
        requests.delete(
            f"{fpp_base}/playlist/{quote('Turn Off Lights', safe='')}",
            timeout=5,
        )
    except Exception as exc:
        _logger.debug("Legacy playlist cleanup skipped: %s", exc)


def _add_missing_columns(app):
    """Add columns that db.create_all() cannot add to an existing table.

    create_all() only ever creates missing *tables*, so a column added to a model
    after a controller is already in the field would silently not exist there and
    every query touching it would fail.  SQLite's ALTER TABLE ADD COLUMN is cheap
    and idempotent when guarded, so each new column is listed here once.

    Keep entries forever — a controller may upgrade from any older version.
    """
    wanted = [
        # (table, column, DDL type + default)
        ("custom_playlists", "random", "BOOLEAN NOT NULL DEFAULT 0"),
    ]
    from sqlalchemy import text
    for table, column, ddl in wanted:
        try:
            rows = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows:
                continue  # table does not exist yet; create_all will build it complete
            if any(r[1] == column for r in rows):
                continue
            db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            db.session.commit()
            app.logger.info("Added missing column %s.%s", table, column)
        except Exception as exc:
            db.session.rollback()
            app.logger.warning("Could not add column %s.%s: %s", table, column, exc)


def _drop_removed_columns(app):
    """Drop columns whose feature has been removed from the app.

    The mirror image of _add_missing_columns: a controller upgrading from an
    older version still has these columns, and they are NOT NULL with no server
    default, so an INSERT from the current model - which no longer supplies them
    - would fail.  SQLite has supported ALTER TABLE DROP COLUMN since 3.35 and
    FPP ships far newer, but the guard keeps an old runtime from bricking start.

    Keep entries forever - a controller may upgrade from any older version.
    """
    unwanted = [
        # (table, column) - Multisync, removed 2026-09-07
        ("effect_presets", "multisync"),
        ("effect_presets", "systems_json"),
    ]
    from sqlalchemy import text
    for table, column in unwanted:
        try:
            rows = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows:
                continue  # table does not exist yet
            if not any(r[1] == column for r in rows):
                continue  # already gone
            db.session.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
            db.session.commit()
            app.logger.info("Dropped removed column %s.%s", table, column)
        except Exception as exc:
            db.session.rollback()
            app.logger.warning("Could not drop column %s.%s: %s", table, column, exc)


def _regenerate_scene_playlists(app):
    """Rewrite all scene playlists to FPP after a restart."""
    with app.app_context():
        try:
            from app.models import Scene
            from app.routes.scenes import _write_scene_files
            for scene in Scene.query.all():
                _write_scene_files(scene)
        except Exception as exc:
            app.logger.warning("Could not regenerate scene playlists on startup: %s", exc)


def _regenerate_effect_playlists(app):
    """Rewrite all effect preset playlists to FPP after a restart."""
    with app.app_context():
        try:
            from app.models import EffectPreset
            from app.routes.effects import _write_effect_playlist
            for preset in EffectPreset.query.all():
                _write_effect_playlist(preset)
        except Exception as exc:
            app.logger.warning("Could not regenerate effect playlists on startup: %s", exc)


def _regenerate_custom_playlists(app):
    """Rewrite all user-built playlists to FPP after a restart."""
    with app.app_context():
        try:
            from app.models import CustomPlaylist
            from app.routes.custom_playlists import _write_custom_playlist
            for cp in CustomPlaylist.query.all():
                _write_custom_playlist(cp)
        except Exception as exc:
            app.logger.warning("Could not regenerate custom playlists on startup: %s", exc)


def regenerate_all_playlists(app):
    """Rewrite every playlist this UI owns to FPP.

    Used by the full-backup restore: the internal token is embedded in the
    callback URLs inside those playlist files, so restoring a token from a
    backup invalidates every one of them until they are written again.
    """
    _regenerate_scene_playlists(app)
    _regenerate_effect_playlists(app)
    _regenerate_custom_playlists(app)


def _fpp_is_ready(app):
    import requests
    try:
        resp = requests.get(
            f"{app.config.get('FPP_BASE_URL', 'http://localhost/api')}/fppd/status",
            timeout=5,
        )
        return resp.ok
    except requests.RequestException:
        return False


def _deferred_fpp_init(app):
    """Startup work that needs the FPP API, run off the main thread.

    Field Pis boot services in unpredictable order, so wait (with backoff) for
    fppd to answer before pushing playlists at it, rather than failing once at
    process start and leaving stale playlists until the next restart.
    """
    delay = 5
    deadline = time.monotonic() + 600  # keep trying for ~10 minutes
    while not _fpp_is_ready(app):
        if time.monotonic() > deadline:
            _logger.error(
                "FPP API never became ready — playlists were NOT "
                "regenerated. They will refresh on the next fpp-ui restart."
            )
            return
        _logger.info("FPP API not ready yet — retrying in %ss", delay)
        time.sleep(delay)
        delay = min(delay * 2, 60)

    _regenerate_scene_playlists(app)
    _regenerate_effect_playlists(app)
    _regenerate_custom_playlists(app)
    _delete_legacy_turn_off_playlist(app)
    _logger.info("Deferred FPP startup sync complete")


def create_app():
    app = Flask(__name__, template_folder="../templates")

    from app.config import Config
    app.config.from_object(Config)

    db.init_app(app)

    app.logger.info(
        "fpp-ui starting: FPP_BASE_URL=%s UI_PATH=/%s DB=%s admin_pin=%s master_pin=%s internal_token=%s",
        app.config.get("FPP_BASE_URL"),
        app.config.get("UI_PATH"),
        app.config.get("SQLALCHEMY_DATABASE_URI"),
        "set" if app.config.get("ADMIN_PASSWORD_HASH") else "UNSET (first-run setup)",
        "set" if app.config.get("MASTER_PIN_HASH") else "unset",
        "set" if app.config.get("INTERNAL_TOKEN") else "UNSET — scheduled scenes/effects will fail",
    )

    with app.app_context():
        from app import models  # noqa: F401
        try:
            db.create_all()
        except Exception:
            # Keep the service up (and its log readable over Dataplicity)
            # instead of crash-looping under systemd; requests that need the
            # DB will fail loudly until the underlying problem is fixed.
            app.logger.exception(
                "Could not initialize the database — is the instance/ directory "
                "writable and the disk not full? The UI will return errors until "
                "this is fixed."
            )
        _add_missing_columns(app)
        _drop_removed_columns(app)
        _create_turn_off_lights_preset(app)

    from app.routes import main as main_blueprint
    app.register_blueprint(main_blueprint)

    from app.routes.auth import auth_bp
    app.register_blueprint(auth_bp)

    from app.routes.playlists import playlists_bp
    app.register_blueprint(playlists_bp)

    from app.routes.colors import colors_bp
    app.register_blueprint(colors_bp)

    from app.routes.scheduler import scheduler_bp
    app.register_blueprint(scheduler_bp)

    from app.routes.settings import settings_bp
    app.register_blueprint(settings_bp)

    from app.routes.scenes import scenes_bp
    app.register_blueprint(scenes_bp)

    from app.routes.effects import effects_bp
    app.register_blueprint(effects_bp)

    from app.routes.custom_playlists import custom_playlists_bp
    app.register_blueprint(custom_playlists_bp)

    from app.routes.backup import backup_bp
    app.register_blueprint(backup_bp)

    # Background workers (daemon threads — zero cost when idle). The FPP
    # startup sync waits for fppd to come up before talking to it.
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Thread(
            target=_deferred_fpp_init, args=(app,), daemon=True,
            name="fpp-startup-sync",
        ).start()
        from app.alert_monitor import start_monitor
        start_monitor(app)

    @app.before_request
    def _require_provisioning():
        """Funnel a fresh install to the first-run setup page.

        Enforced on every request rather than relying on the redirect alone, so
        a stale session cookie cannot skip the claim.
        """
        from app.routes.auth import is_unprovisioned

        # FPP playlist callbacks authenticate by INTERNAL_TOKEN, not by session,
        # so they must keep working regardless of provisioning state — otherwise
        # clearing the PIN on a configured Pi would break every scene playlist.
        if request.path.startswith("/internal/"):
            return None
        # Let unknown paths 404 normally instead of redirecting to setup.
        if request.endpoint is None:
            return None
        if request.endpoint in ("static", "auth.setup"):
            return None
        if is_unprovisioned():
            return redirect(url_for("auth.setup"))
        return None

    # Allow the app to run behind a reverse proxy at a sub-path (e.g. /CustomUI).
    # Apache sets X-Forwarded-Prefix so url_for() generates correct links.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_prefix=1)

    @app.context_processor
    def inject_site_settings():
        from app.models import AppSetting
        from app.ui_path import reanchor_upload_url
        try:
            settings = {s.key: s.value for s in AppSetting.query.all()}
        except Exception:
            # A broken DB must not take down every page render (login included).
            app.logger.exception("Could not load site settings for template render")
            return {"site_settings": {}}
        # Uploaded images are stored as absolute URLs, so they carry whichever
        # URL prefix was live when they were uploaded. Re-anchor them to the
        # current one, or they 404 after the install is moved to a new path.
        for key in ("logo_url", "bg_image_url"):
            if settings.get(key):
                settings[key] = reanchor_upload_url(settings[key])
        return {"site_settings": settings}

    @app.errorhandler(500)
    def _internal_error(error):
        # A failed request may leave the SQLAlchemy session dirty; roll it back
        # so the next request doesn't inherit the failure.
        try:
            db.session.rollback()
        except Exception:
            pass
        if request.path.startswith("/api/") or request.path.startswith("/internal/"):
            return jsonify({"error": "Internal server error — check fpp-ui.log on the controller"}), 500
        return "Internal server error — check fpp-ui.log on the controller.", 500

    return app
