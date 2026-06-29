import os

import bcrypt
from dotenv import set_key
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from app.auth_utils import login_required

auth_bp = Blueprint("auth", __name__)


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

    if not new_pin.isdigit() or len(new_pin) != 4:
        return jsonify({"error": "New PIN must be exactly 4 digits."}), 400

    new_hash = bcrypt.hashpw(new_pin.encode("utf-8"), bcrypt.gensalt()).decode()

    env_path = os.path.normpath(os.path.join(current_app.root_path, "..", ".env"))
    try:
        set_key(env_path, "ADMIN_PASSWORD_HASH", new_hash, quote_mode="never")
    except Exception as exc:
        current_app.logger.warning("Could not persist PIN hash to .env: %s", exc)
        return jsonify({"error": "Could not save PIN — is the .env file writable?"}), 500

    current_app.config["ADMIN_PASSWORD_HASH"] = new_hash
    return jsonify({"ok": True})


@auth_bp.post("/api/set-master-pin")
@login_required
def set_master_pin():
    data = request.get_json(silent=True) or {}
    new_pin = str(data.get("new_pin", "")).strip()

    if not new_pin.isdigit() or len(new_pin) != 4:
        return jsonify({"error": "Master PIN must be exactly 4 digits."}), 400

    new_hash = bcrypt.hashpw(new_pin.encode("utf-8"), bcrypt.gensalt()).decode()

    env_path = os.path.normpath(os.path.join(current_app.root_path, "..", ".env"))
    try:
        set_key(env_path, "MASTER_PIN_HASH", new_hash, quote_mode="never")
    except Exception as exc:
        current_app.logger.warning("Could not persist master PIN hash to .env: %s", exc)
        return jsonify({"error": "Could not save master PIN — is the .env file writable?"}), 500

    current_app.config["MASTER_PIN_HASH"] = new_hash
    return jsonify({"ok": True})


# Backward-compat alias kept so any existing bookmarks/scripts still work
@auth_bp.post("/api/change-password")
@login_required
def change_password():
    return change_pin()
