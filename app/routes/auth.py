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

        if stored_hash and bcrypt.checkpw(
            password.encode("utf-8"), stored_hash.encode("utf-8")
        ):
            session.clear()
            session["logged_in"] = True
            return redirect(url_for("main.index"))

        error = "Invalid password."

    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.post("/api/change-password")
@login_required
def change_password():
    data = request.get_json(silent=True) or {}
    current_pw = data.get("current_password", "")
    new_pw = data.get("new_password", "")

    stored_hash = current_app.config.get("ADMIN_PASSWORD_HASH", "")
    if not stored_hash or not bcrypt.checkpw(
        current_pw.encode("utf-8"), stored_hash.encode("utf-8")
    ):
        return jsonify({"error": "Current password is incorrect."}), 400

    if len(new_pw) < 8:
        return jsonify({"error": "New password must be at least 8 characters."}), 400

    new_hash = bcrypt.hashpw(new_pw.encode("utf-8"), bcrypt.gensalt()).decode()

    env_path = os.path.normpath(os.path.join(current_app.root_path, "..", ".env"))
    try:
        set_key(env_path, "ADMIN_PASSWORD_HASH", new_hash, quote_mode="never")
    except Exception as exc:
        current_app.logger.warning("Could not persist password hash to .env: %s", exc)
        return jsonify({"error": "Could not save password — is the .env file writable?"}), 500

    current_app.config["ADMIN_PASSWORD_HASH"] = new_hash
    return jsonify({"ok": True})
