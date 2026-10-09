import hmac
from functools import wraps

from flask import current_app, jsonify, redirect, request, session, url_for


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)

    return decorated_function


def internal_token_error():
    """Check the ``?token=`` on an ``/internal/`` call from an FPP playlist.

    Returns ``None`` when the token is valid, else a ``(response, status)``
    pair to return as-is. Compared as bytes: ``hmac.compare_digest`` raises
    ``TypeError`` on a non-ASCII ``str``, which would turn a junk token into a
    500 instead of a 403.
    """
    expected = current_app.config.get("INTERNAL_TOKEN", "")
    if not expected:
        return jsonify({"error": "Internal token not configured"}), 503
    supplied = request.args.get("token", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        current_app.logger.warning("Rejected /internal/ call with a bad token from %s", request.remote_addr)
        return jsonify({"error": "Forbidden"}), 403
    return None
