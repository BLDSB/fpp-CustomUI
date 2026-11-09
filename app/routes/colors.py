import re
import threading

import requests
from flask import Blueprint, current_app, jsonify, render_template

from app import db
from app.auth_utils import login_required
from app.fpp_api import ensure_models_active, fpp_url, hex_to_rgb
from app import white_layer
from app.models import (
    OVERLAY_MODELS, ColorButton, SavedColor, all_is_virtual, all_overlay_models,
    clamp_white, expand_overlay_models,
)
from app.routes.settings import selectable_zones
from app.validation import json_object, page_args, str_field

colors_bp = Blueprint("colors", __name__)

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# A real "All" overlay model is switched off by the next zone send.  A virtual
# "All" (see all_is_virtual) is not a model at all: sending it lights every
# zone's member models, so that send has to be undone by turning those members
# off instead.  Only the *first* zone send after an All may do that — the page
# sends its selected zones in parallel and they must not switch each other off —
# and a zone set on its own earlier must survive later zone sends, so FPP's own
# state cannot tell us when.  Hence a flag, guarded so the cleanup finishes
# before the next send looks at it.
_all_state_lock = threading.Lock()
_all_fan_out_lit = False


def mark_overlays_cleared():
    """Forget a lit virtual All; call wherever every overlay model is switched off."""
    global _all_fan_out_lit
    with _all_state_lock:
        _all_fan_out_lit = False


def _white_field(data):
    """``(white, error_response)`` from the optional ``white`` field (0-255).

    A missing or null field is fine and means "not given"; anything that is not
    a whole number in range is rejected rather than quietly clamped.
    """
    raw = data.get("white")
    if raw is None:
        return None, None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 255:
        return None, (jsonify({"error": "White must be a whole number from 0 to 255"}), 400)
    return clamp_white(raw), None


@colors_bp.get("/colors")
@login_required
def colors_page():
    return render_template("colors.html", zones=selectable_zones())


@colors_bp.post("/colors/send")
@login_required
def send_color():
    data = json_object()
    hex_val = data.get("hex", "")
    model = data.get("model", "All")

    if not _HEX_RE.match(hex_val):
        return jsonify({"error": "Invalid hex color"}), 400
    if model not in OVERLAY_MODELS:
        return jsonify({"error": "Invalid overlay model"}), 400
    white, err = _white_field(data)
    if err:
        return err

    r, g, b = hex_to_rgb(hex_val)

    try:
        requests.get(fpp_url("/playlists/stop"), timeout=5)
    except requests.RequestException:
        pass

    # Deactivate conflicting models before activating the target.
    # "All" overlaps every zone, so only one group can be active at a time.
    # A grouped zone is really several member overlay models.
    global _all_fan_out_lit
    targets = expand_overlay_models([model])
    with _all_state_lock:
        if model == "All":
            conflicts = sorted(all_overlay_models() - {"All"})
            _all_fan_out_lit = all_is_virtual()
        else:
            conflicts = ["All"]
            if _all_fan_out_lit:
                # The lit zones are the leftovers of a virtual All; take them
                # down so only what was asked for stays on, as a real All
                # model switching off would.
                conflicts += sorted(all_overlay_models() - {"All"} - set(targets))
                _all_fan_out_lit = False

        for conflict in conflicts:
            try:
                requests.put(
                    fpp_url(f"/overlays/model/{conflict}/state"),
                    json={"State": 0},
                    timeout=3,
                )
            except requests.RequestException:
                pass
        # Whatever white those models held goes with them.
        white_layer.drop(conflicts)

    def switch_on(models):
        for target in models:
            requests.put(
                fpp_url(f"/overlays/model/{target}/state"),
                json={"State": 1},
                timeout=5,
            ).raise_for_status()

            requests.put(
                fpp_url(f"/overlays/model/{target}/fill"),
                json={"RGB": [r, g, b]},
                timeout=5,
            ).raise_for_status()

    try:
        switch_on(targets)
        # FPP sometimes ignores the first switch-on; confirm and repeat if so.
        still_off = ensure_models_active(targets, switch_on)
        if white is not None:
            # RGBW zones only; the rest have no white channel and ignore it.
            white_layer.apply({t: white for t in targets})
    except requests.RequestException as exc:
        current_app.logger.error("FPP send color error: %s", exc)
        return jsonify({"error": "Could not send color to the controller"}), 502
    if still_off:
        return jsonify({"error": f"The controller did not switch on: {', '.join(still_off)}"}), 502

    return jsonify({"ok": True})


@colors_bp.post("/colors/stop")
@login_required
def stop_color():
    """Deactivate all pixel overlay models."""
    return _deactivate_all_overlays()



def _deactivate_all_overlays():
    """Deactivate every known overlay model (fire-and-forget per model)."""
    mark_overlays_cleared()
    white_layer.clear()
    errors = []
    for model in sorted(all_overlay_models()):
        try:
            resp = requests.put(
                fpp_url(f"/overlays/model/{model}/state"),
                json={"State": 0},
                timeout=5,
            )
            # 404 means the model doesn't exist in FPP — already off, not an error
            if not resp.ok and resp.status_code != 404:
                resp.raise_for_status()
        except requests.RequestException as exc:
            current_app.logger.warning("FPP deactivate %s error: %s", model, exc)
            errors.append(model)

    if errors:
        return jsonify({"error": f"Could not deactivate: {', '.join(errors)}"}), 502
    return jsonify({"ok": True})


@colors_bp.post("/colors/save")
@login_required
def save_color():
    """Save a color and create a quick-access button for it in one step."""
    data = json_object()
    name = str_field(data, "name")
    hex_val = data.get("hex", "")

    if not name or len(name) > 64:
        return jsonify({"error": "Name is required (max 64 chars)"}), 400
    if not _HEX_RE.match(hex_val):
        return jsonify({"error": "Invalid hex color"}), 400
    white, err = _white_field(data)
    if err:
        return err

    color = SavedColor(name=name, hex_value=hex_val, white=white)
    db.session.add(color)
    db.session.flush()

    button = ColorButton(label=name, saved_color_id=color.id)
    db.session.add(button)
    db.session.commit()

    return jsonify({
        "id": button.id, "label": button.label,
        "hex_value": color.hex_value, "white": color.white,
    }), 201


@colors_bp.get("/colors/buttons")
@login_required
def list_buttons():
    limit, offset = page_args()
    rows = (
        db.session.query(ColorButton, SavedColor)
        .join(SavedColor, ColorButton.saved_color_id == SavedColor.id)
        .order_by(ColorButton.id)
        .limit(limit).offset(offset)
        .all()
    )
    return jsonify([
        {"id": btn.id, "label": btn.label, "hex_value": color.hex_value, "white": color.white}
        for btn, color in rows
    ])


@colors_bp.delete("/colors/buttons/<int:button_id>")
@login_required
def delete_button(button_id):
    btn = db.session.get(ColorButton, button_id)
    if not btn:
        return jsonify({"error": "Not found"}), 404

    color_id = btn.saved_color_id
    db.session.delete(btn)

    remaining = db.session.query(ColorButton).filter_by(saved_color_id=color_id).count()
    if remaining == 0:
        color = db.session.get(SavedColor, color_id)
        if color:
            db.session.delete(color)

    db.session.commit()
    return jsonify({"ok": True})
