"""User-built playlists: an ordered mix of scenes, effects, sequences and pauses.

A scene or effect preset already registers its own one-item FPP playlist so it
can be scheduled on its own.  This module is the other direction — it treats
those same entries as building blocks and concatenates as many as you like into
a single FPP playlist, so a show can run scene, then effect, then scene.

The FPP copy is derived output: it is rewritten from the DB rows on every save,
on restore, and on startup.
"""

from random import shuffle

import requests
from flask import Blueprint, current_app, jsonify, render_template, request

from app import db
from app.auth_utils import login_required
from app.fpp_playlist import (
    build_playlist_def,
    effect_entries,
    pause_item,
    scene_entries,
    sequence_entry,
)
from app.models import CustomPlaylist, CustomPlaylistItem, EffectPreset, Scene

custom_playlists_bp = Blueprint("custom_playlists", __name__)

# Names FPP or this app already uses for generated playlists.
_RESERVED_PREFIXES = ("Scene - ", "Effect - ")
_RESERVED_NAMES = ("Current-Sequence", "Turn Off Lights")


def _fpp(path):
    return f"{current_app.config['FPP_BASE_URL']}{path}"


def _playlist_entries(cp):
    """Flatten the stored items into FPP playlist entries.

    Scene and effect items pass clear=True: inside a show each item should start
    from a known overlay state rather than inheriting colors from the item
    before it.

    A randomized playlist is shuffled here, at the *item* level, so each scene
    keeps the pause that holds it.  FPP's own "random" flag is left at 0 because
    it shuffles entries individually, which would scatter those pauses across
    the show.  The trade-off is that the order is fixed once written, so
    _write_custom_playlist is called again on every play to reshuffle.
    """
    items = sorted(cp.items, key=lambda i: i.position)
    if cp.random:
        shuffle(items)

    entries = []
    for item in items:
        hold = max(1, int(item.duration or 1))
        if item.item_type == "scene":
            if db.session.get(Scene, item.ref_id) is None:
                continue  # scene deleted since the playlist was built — skip it
            entries.extend(scene_entries(item.ref_id, hold, clear=True))
        elif item.item_type == "effect":
            if db.session.get(EffectPreset, item.ref_id) is None:
                continue
            entries.extend(effect_entries(item.ref_id, hold, clear=True))
        elif item.item_type == "sequence":
            if item.ref_name:
                entries.append(sequence_entry(item.ref_name))
        elif item.item_type == "pause":
            entries.append(pause_item(hold))
    return entries


def _write_custom_playlist(cp):
    entries = _playlist_entries(cp)
    if not entries:
        # FPP will not play an empty playlist; leave a pause so the name still
        # resolves and the show is visible (and stoppable) rather than missing.
        entries = [pause_item(5)]
    playlist_def = build_playlist_def(cp.name, entries, "FPP UI Playlist", cp.repeat)
    try:
        requests.post(_fpp(f"/playlist/{cp.name}"), json=playlist_def, timeout=5).raise_for_status()
        return True
    except requests.RequestException as exc:
        current_app.logger.warning(
            "Could not register FPP playlist for custom playlist %d: %s", cp.id, exc
        )
        return False


def _delete_custom_playlist(cp):
    try:
        requests.delete(_fpp(f"/playlist/{cp.name}"), timeout=5)
    except requests.RequestException:
        pass


def _fpp_playlist_names():
    """Names FPP already knows about, or None if FPP could not be reached."""
    try:
        resp = requests.get(_fpp("/playlists"), timeout=5)
        resp.raise_for_status()
        data = resp.json()
        names = data if isinstance(data, list) else data.get("playlists", [])
        return {n for n in names if isinstance(n, str)}
    except (requests.RequestException, ValueError):
        return None


def _validate_name(name, existing_id=None):
    """Return an error string, or None if the name is usable.

    The name goes into an FPP URL path unencoded, so path characters are
    rejected outright rather than escaped — same rule as effect presets.
    """
    if not name or len(name) > 64:
        return "Name required (max 64 chars)"
    if "/" in name or "\\" in name or ".." in name:
        return "Name cannot contain slashes or .."
    if name in _RESERVED_NAMES or name.startswith(_RESERVED_PREFIXES):
        return "That name is reserved — pick another"

    clash = CustomPlaylist.query.filter_by(name=name).first()
    if clash and clash.id != existing_id:
        return "A playlist with that name already exists"

    # Do not silently overwrite a playlist that FPP already has and we do not own.
    fpp_names = _fpp_playlist_names()
    if fpp_names is not None and name in fpp_names and clash is None:
        return "The controller already has a playlist with that name"
    return None


def _parse_items(raw):
    """Validate the incoming item list. Returns (items, error)."""
    if not isinstance(raw, list):
        return None, "Items must be a list"
    if not raw:
        return None, "Add at least one item"
    if len(raw) > 100:
        return None, "Too many items (max 100)"

    items = []
    for pos, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return None, "Malformed item"
        item_type = entry.get("item_type")
        if item_type not in CustomPlaylistItem.ITEM_TYPES:
            return None, f"Unknown item type: {item_type}"

        try:
            duration = int(entry.get("duration") or 30)
        except (TypeError, ValueError):
            return None, "Duration must be a number"
        duration = max(1, min(duration, 86400))

        ref_id, ref_name = None, None
        if item_type in ("scene", "effect"):
            try:
                ref_id = int(entry.get("ref_id"))
            except (TypeError, ValueError):
                return None, f"Missing {item_type} reference"
            model = Scene if item_type == "scene" else EffectPreset
            if db.session.get(model, ref_id) is None:
                return None, f"That {item_type} no longer exists"
        elif item_type == "sequence":
            ref_name = (entry.get("ref_name") or "").strip()
            if not ref_name:
                return None, "Missing sequence name"
            if "/" in ref_name or "\\" in ref_name or ".." in ref_name:
                return None, "Invalid sequence name"
            if len(ref_name) > 255:
                return None, "Sequence name too long"

        items.append(CustomPlaylistItem(
            position=pos, item_type=item_type, ref_id=ref_id,
            ref_name=ref_name, duration=duration,
        ))
    return items, None


@custom_playlists_bp.get("/playlists")
@login_required
def playlists_page():
    return render_template("playlists.html")


@custom_playlists_bp.get("/api/custom-playlists")
@login_required
def list_custom_playlists():
    rows = CustomPlaylist.query.order_by(CustomPlaylist.name).all()
    return jsonify([p.to_dict() for p in rows])


@custom_playlists_bp.post("/api/custom-playlists")
@login_required
def create_custom_playlist():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()

    error = _validate_name(name)
    if error:
        return jsonify({"error": error}), 400

    items, error = _parse_items(data.get("items"))
    if error:
        return jsonify({"error": error}), 400

    cp = CustomPlaylist(
        name=name,
        repeat=bool(data.get("repeat", True)),
        random=bool(data.get("random", False)),
    )
    cp.items = items
    db.session.add(cp)
    db.session.commit()

    _write_custom_playlist(cp)
    return jsonify(cp.to_dict()), 201


@custom_playlists_bp.put("/api/custom-playlists/<int:playlist_id>")
@login_required
def update_custom_playlist(playlist_id):
    cp = db.session.get(CustomPlaylist, playlist_id)
    if not cp:
        return jsonify({"error": "Not found"}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()

    error = _validate_name(name, existing_id=cp.id)
    if error:
        return jsonify({"error": error}), 400

    items, error = _parse_items(data.get("items"))
    if error:
        return jsonify({"error": error}), 400

    old_name = cp.name
    cp.name = name
    cp.repeat = bool(data.get("repeat", True))
    cp.random = bool(data.get("random", False))
    cp.items = items  # cascade delete-orphan drops the previous rows
    db.session.commit()

    # A rename leaves the old playlist behind on FPP under its old name.
    if old_name != name:
        try:
            requests.delete(_fpp(f"/playlist/{old_name}"), timeout=5)
        except requests.RequestException:
            pass

    _write_custom_playlist(cp)
    return jsonify(cp.to_dict())


@custom_playlists_bp.delete("/api/custom-playlists/<int:playlist_id>")
@login_required
def delete_custom_playlist(playlist_id):
    cp = db.session.get(CustomPlaylist, playlist_id)
    if not cp:
        return jsonify({"error": "Not found"}), 404
    _delete_custom_playlist(cp)
    db.session.delete(cp)
    db.session.commit()
    return jsonify({"ok": True})
