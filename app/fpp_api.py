"""Small helpers shared by every module that talks to FPP's REST API."""
import time
from urllib.parse import quote

import requests
from flask import current_app


def fpp_url(path, app=None):
    """Full FPP API URL for ``path`` (e.g. ``"/fppd/status"``).

    Uses ``current_app`` unless ``app`` is given — background threads such as
    the alert monitor have no request/app context and pass their app in.
    """
    base = (app or current_app).config["FPP_BASE_URL"]
    return f"{base}{path}"


def playlist_url(name):
    """URL of the named FPP playlist, with the name percent-encoded.

    Playlist names come from users (scene, preset and playlist names); encoding
    keeps ``/``, ``?``, ``#`` and ``%`` inside the name from rewriting the path.
    """
    return fpp_url(f"/playlist/{quote(name, safe='')}")


def fpp_error_text(exc):
    """A user-safe sentence for a failed call to FPP; the detail goes to the log.

    ``str(exc)`` for a ``requests`` failure embeds the controller's address and
    connection internals, which the browser has no business seeing (and cannot
    act on).
    """
    current_app.logger.warning("FPP call failed: %s", exc)
    if isinstance(exc, requests.Timeout):
        return "The controller took too long to respond."
    if isinstance(exc, requests.ConnectionError):
        return "Could not reach the controller — is the player running?"
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return f"The controller returned an error{f' (HTTP {status})' if status else ''}."
    return "The controller sent a response this page could not use."


def hex_to_rgb(hex_color):
    """``"#ff8800"`` -> ``(255, 136, 0)``."""
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# FPP can answer 200 to "switch this overlay model on" and then not do it: the
# first activation after the "Stop Effects" command the app sends before every
# scene or effect is sometimes ignored, and a second one always sticks. Nothing
# in the response says it happened - the model just stays off and the lights do
# nothing. So after switching models on, read back whether they are on, and
# send the activation again for any that are not.
_ACTIVATION_SETTLE = 0.25   # seconds fppd needs to have applied the request
_ACTIVATION_RETRIES = 2


def inactive_models(names):
    """Of ``names``, the overlay models FPP reports as not switched on.

    Names FPP does not know (e.g. ``--All Models--``) are ignored. Returns
    ``None`` when the check itself cannot be made, so a caller can tell "all
    on" (``[]``) from "could not look".
    """
    try:
        resp = requests.get(fpp_url("/overlays/models"), timeout=5)
        resp.raise_for_status()
        models = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(models, list):
        return None
    wanted = set(names)
    return [m["Name"] for m in models
            if isinstance(m, dict) and m.get("Name") in wanted and not m.get("isActive")]


def ensure_models_active(names, reactivate):
    """Make sure ``names`` really are on, re-sending the activation if not.

    ``reactivate(models)`` repeats the original activation for just the models
    that came back off. Returns the models still off after the retries (``[]``
    when everything is on, or when FPP could not be asked).
    """
    if not names:
        return []
    off = []
    for attempt in range(_ACTIVATION_RETRIES + 1):
        time.sleep(_ACTIVATION_SETTLE)
        off = inactive_models(names)
        if not off:
            return []
        if attempt == _ACTIVATION_RETRIES:
            break
        current_app.logger.warning(
            "FPP ignored the switch-on for %s - sending it again (attempt %d)",
            ", ".join(off), attempt + 1,
        )
        reactivate(off)
    current_app.logger.error("FPP still reports %s as off after %d retries",
                             ", ".join(off), _ACTIVATION_RETRIES)
    return off
