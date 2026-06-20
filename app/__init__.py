import json
import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

db = SQLAlchemy()


def _create_turn_off_lights_preset(app):
    """Create/update the 'Turn Off Lights' FPP Command Preset on startup."""
    with app.app_context():
        preset_file = "/home/fpp/media/config/commandPresets.json"
        try:
            if os.path.exists(preset_file):
                with open(preset_file, "r") as f:
                    data = json.load(f)
            else:
                data = {"commands": []}

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
            with open(preset_file, "w") as f:
                json.dump(data, f, indent="\t")
        except Exception as exc:
            app.logger.warning("Could not write Turn Off Lights preset: %s", exc)

        # Delete the old standalone playlist if it still exists
        try:
            import requests
            from urllib.parse import quote
            fpp_base = app.config.get("FPP_BASE_URL", "http://localhost/api")
            requests.delete(
                f"{fpp_base}/playlist/{quote('Turn Off Lights', safe='')}",
                timeout=5,
            )
        except Exception:
            pass


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


def create_app():
    app = Flask(__name__, template_folder="../templates")

    from app.config import Config
    app.config.from_object(Config)

    db.init_app(app)

    with app.app_context():
        from app import models  # noqa: F401
        db.create_all()
        _regenerate_scene_playlists(app)
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

    # Allow the app to run behind a reverse proxy at a sub-path (e.g. /CustomUI).
    # Apache sets X-Forwarded-Prefix so url_for() generates correct links.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_prefix=1)

    @app.context_processor
    def inject_site_settings():
        from app.models import AppSetting
        settings = {s.key: s.value for s in AppSetting.query.all()}
        return {"site_settings": settings}

    @app.after_request
    def set_csp(response):
        # FPP's Apache uses 'Header set' (not 'Header always set') for its
        # restrictive CSP, so setting our own header here prevents Apache
        # from overwriting it. We need img-src * to allow external logo and
        # background image URLs entered in Settings.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src * data: blob:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "font-src 'self' data:; "
            "object-src 'none';"
        )
        return response

    return app
