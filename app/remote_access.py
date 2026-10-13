"""Remote access: join or leave the BLD tunnel from the Settings page.

The work is done by /usr/local/sbin/fpp-remote, a root-owned helper that this app
runs through a narrow sudo rule (same pattern as the URL path helper). Only a
validated enrolment code ever crosses that boundary; the portal address is fixed
in a root-owned file on the controller, not supplied by the browser.
"""
import json
import os
import re
import subprocess

from flask import current_app

HELPER_BIN = "/usr/local/sbin/fpp-remote"
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def available():
    return os.path.exists(HELPER_BIN)


def _run(args, timeout):
    return subprocess.run(["sudo", "-n", HELPER_BIN, *args],
                          capture_output=True, text=True, timeout=timeout)


def _last_line(text):
    lines = (text or "").strip().splitlines()
    return lines[-1] if lines else ""


def status():
    """What the Settings card shows. Never raises."""
    if not available():
        return {"available": False}
    try:
        result = _run(["status"], 15)
        data = json.loads(_last_line(result.stdout))
        data["available"] = True
        return data
    except Exception as exc:  # helper missing sudo rule, bad output, timeout
        current_app.logger.warning("fpp-remote status failed: %s", exc)
        return {"available": True, "error": "Could not read the remote access status."}


def clean_code(raw):
    """The code as the helper expects it, or None. Only surrounding whitespace is
    forgiven (a trailing newline from a paste); anything inside the code is refused."""
    code = str(raw or "").strip()
    if "enroll.sh" in code:                     # the whole command was pasted
        code = code.split()[-1]
    return code if CODE_RE.match(code) else None


def enroll(code):
    """Join the tunnel. Returns (result_dict, None) or (None, error_message)."""
    if not available():
        return None, ("Remote access is not installed on this controller. "
                      "Re-run the plugin install.")
    code = clean_code(code)
    if not code:
        return None, "That does not look like an enrolment code. Copy it again from the portal."
    try:
        result = _run(["enroll", code], 150)
    except subprocess.TimeoutExpired:
        return None, "Timed out joining the tunnel. Check the controller's internet connection."
    except Exception as exc:  # pragma: no cover - environment-dependent
        return None, f"Could not run the remote access helper: {exc}"

    if result.returncode != 0:
        detail = _last_line(result.stderr) or _last_line(result.stdout) \
            or f"exit code {result.returncode}"
        current_app.logger.warning("fpp-remote enroll failed: %s", detail)
        return None, detail
    try:
        return json.loads(_last_line(result.stdout)), None
    except ValueError:
        return None, "The helper finished but returned an unexpected answer."


def disconnect():
    """Leave the tunnel. Returns an error message or None."""
    if not available():
        return "Remote access is not installed on this controller."
    try:
        result = _run(["disconnect"], 30)
    except Exception as exc:  # pragma: no cover - environment-dependent
        return f"Could not run the remote access helper: {exc}"
    if result.returncode != 0:
        return _last_line(result.stderr) or "Could not disconnect."
    return None
