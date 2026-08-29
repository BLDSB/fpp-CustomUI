import ipaddress
import os

import bcrypt
from dotenv import set_key
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from app.auth_utils import login_required
from app import ui_path as ui_path_mod

auth_bp = Blueprint("auth", __name__)


def is_unprovisioned():
    """True on a fresh install — no admin PIN and no master PIN set yet.

    Both are checked: a box with only a master PIN is still reachable and must
    not be re-claimable through the setup flow.
    """
    return not current_app.config.get("ADMIN_PASSWORD_HASH", "") and not current_app.config.get(
        "MASTER_PIN_HASH", ""
    )


def _client_is_local():
    """True when the request came from the local network.

    Defence in depth for the first-run claim window, not a real access control —
    anyone already on the LAN passes. ProxyFix(x_for=1) means remote_addr is the
    true client rather than Apache.
    """
    try:
        ip = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _persist_pin(config_key, new_pin, label="PIN"):
    """Hash a 4-digit PIN, write it to .env, and update the live config.

    Returns None on success, or an (error_message, http_status) tuple.
    """
    new_pin = str(new_pin).strip()
    if not new_pin.isdigit() or len(new_pin) != 4:
        return (f"{label} must be exactly 4 digits.", 400)

    new_hash = bcrypt.hashpw(new_pin.encode("utf-8"), bcrypt.gensalt()).decode()

    env_path = os.path.normpath(os.path.join(current_app.root_path, "..", ".env"))
    try:
        set_key(env_path, config_key, new_hash, quote_mode="never")
    except Exception as exc:
        current_app.logger.warning("Could not persist %s to .env: %s", config_key, exc)
        return ("Could not save PIN — is the .env file writable?", 500)

    current_app.config[config_key] = new_hash
    return None


@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run claim: the first visitor on the LAN chooses the admin PIN."""
    if not is_unprovisioned():
        return redirect(url_for("auth.login"))

    if not _client_is_local():
        return render_template("setup.html", blocked=True), 403

    error = None
    if request.method == "POST":
        pin     = request.form.get("pin", "")
        confirm = request.form.get("confirm", "")
        # Optional: name this install, which moves it off the default
        # /CustomUI path (e.g. /cityname). Blank keeps the current path.
        new_path = (request.form.get("ui_path") or "").strip()

        # Validated before the PIN is written so a bad name leaves the
        # controller unprovisioned and re-runnable rather than half-set-up.
        path_error = ui_path_mod.validate(new_path) if new_path else None

        if pin != confirm:
            error = "PINs did not match."
        elif path_error:
            error = path_error
        else:
            # Re-checked above, but two visitors can race here — the second loses.
            failure = _persist_pin("ADMIN_PASSWORD_HASH", pin)
            if failure is None:
                session.clear()
                session["logged_in"] = True
                session["is_master"] = False

                if new_path and new_path != ui_path_mod.current_path():
                    if ui_path_mod.apply(new_path) is None:
                        # url_for() would still build links for the OLD prefix
                        # on this response, so redirect to an explicit path.
                        return redirect(f"/{new_path}/")
                    # PIN is set and the session is valid — the old path still
                    # works, so finish setup there rather than stranding them.
                    current_app.logger.warning(
                        "Setup could not move the UI to /%s — staying on /%s.",
                        new_path, ui_path_mod.current_path(),
                    )
                return redirect(url_for("main.index"))
            error = failure[0]

    return render_template(
        "setup.html", error=error, ui_path=ui_path_mod.current_path(),
        reserved_paths=sorted(ui_path_mod.RESERVED_PATHS),
    )


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        stored_hash = current_app.config.get("ADMIN_PASSWORD_HASH", "")

        master_hash = current_app.config.get("MASTER_PIN_HASH", "")

        admin_ok = stored_hash and bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
        master_ok = master_hash and bcrypt.checkpw(password.encode("utf-8"), master_hash.encode("utf-8"))

        if admin_ok or master_ok:
            session.clear()
            session["logged_in"] = True
            session["is_master"] = bool(master_ok)
            return redirect(url_for("main.index"))

        error = "Invalid PIN."

    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.post("/api/change-pin")
@login_required
def change_pin():
    data = request.get_json(silent=True) or {}
    current_pw = data.get("current_pin", "")
    new_pin    = str(data.get("new_pin", "")).strip()

    stored_hash = current_app.config.get("ADMIN_PASSWORD_HASH", "")
    if not stored_hash or not bcrypt.checkpw(
        current_pw.encode("utf-8"), stored_hash.encode("utf-8")
    ):
        return jsonify({"error": "Current PIN is incorrect."}), 400

    failure = _persist_pin("ADMIN_PASSWORD_HASH", new_pin, label="New PIN")
    if failure is not None:
        message, status = failure
        return jsonify({"error": message}), status

    return jsonify({"ok": True})


@auth_bp.post("/api/set-master-pin")
@login_required
def set_master_pin():
    data = request.get_json(silent=True) or {}
    new_pin = str(data.get("new_pin", "")).strip()

    failure = _persist_pin("MASTER_PIN_HASH", new_pin, label="Master PIN")
    if failure is not None:
        message, status = failure
        return jsonify({"error": message}), status

    return jsonify({"ok": True})


# Backward-compat alias kept so any existing bookmarks/scripts still work
@auth_bp.post("/api/change-password")
@login_required
def change_password():
    return change_pin()
