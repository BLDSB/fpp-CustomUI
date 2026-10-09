import threading
import time
from urllib.parse import quote

import requests
from flask import Blueprint, current_app, jsonify, request

from app.auth_utils import login_required
from app.fpp_api import fpp_url

# Shared so the overlay reset lives in one place. It never stops playback —
# callers that need that call _stop_current() themselves.
from app.routes.scenes import _reset_overlays
from app.validation import json_object

playlists_bp = Blueprint("playlists", __name__)


def _stop_current():
    """Stop whatever FPP is currently playing."""
    invalidate_status_cache()
    try:
        requests.get(fpp_url("/playlists/stop"), timeout=5)
    except requests.RequestException:
        pass


@playlists_bp.get("/api/playlists")
@login_required
def list_playlists():
    """Return the playlist names known to FPP."""
    try:
        resp = requests.get(fpp_url("/playlists"), timeout=5)
        resp.raise_for_status()
        data = resp.json()
        # FPP may return a bare list or {"playlists": [...]}
        playlists = data if isinstance(data, list) else data.get("playlists", [])
        playlists = [p for p in playlists if isinstance(p, str)]
        return jsonify({"playlists": sorted(playlists)})
    except requests.RequestException as exc:
        current_app.logger.error("FPP list playlists error: %s", exc)
        return jsonify({"error": "Could not reach the controller"}), 502


_SECTIONS = ("leadIn", "mainPlaylist", "leadOut")


def _entry_has_audio(entry, load, seen):
    """True if one playlist entry plays an audio file, directly or via a sub-playlist."""
    if not isinstance(entry, dict) or not entry.get("enabled", 1):
        return False
    # "both" is sequence + media; "media" is audio alone. A plain "sequence"
    # entry never plays audio — FPP only starts media from these two types.
    if entry.get("type") in ("both", "media"):
        return bool(str(entry.get("mediaName") or "").strip())
    if entry.get("type") == "playlist":
        return _playlist_has_audio(entry.get("name") or entry.get("playlistName"), load, seen)
    return False


def _playlist_has_audio(name, load, seen):
    if not name or name in seen:   # seen guards against playlists that include each other
        return False
    seen.add(name)
    data = load(name)
    if not isinstance(data, dict):
        return False
    return any(
        _entry_has_audio(e, load, seen)
        for section in _SECTIONS
        for e in (data.get(section) or [])
    )


@playlists_bp.get("/api/playlists/audio")
@login_required
def playlists_with_audio():
    """Names of the playlists that play an audio file.

    The media file is recorded in the playlist itself (`mediaName`), so the
    .fseq files never need to be opened.  Playlist definitions are read straight
    from FPP's playlists directory — a handful of small files — falling back to
    FPP's API when that directory is not available.  The Controls page asks once
    per browser session and again on Refresh.
    """
    import json
    import os

    root = os.path.join(current_app.config.get("FPP_MEDIA_ROOT", "/home/fpp/media"), "playlists")
    cache = {}

    def load(name):
        if name in cache:
            return cache[name]
        data = None
        path = os.path.join(root, f"{name}.json")
        if "/" not in name and "\\" not in name and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = None
        else:
            try:
                resp = requests.get(fpp_url(f"/playlist/{quote(name, safe='')}"), timeout=5)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError):
                data = None
        cache[name] = data
        return data

    try:
        resp = requests.get(fpp_url("/playlists"), timeout=5)
        resp.raise_for_status()
        raw = resp.json()
        names = raw if isinstance(raw, list) else raw.get("playlists", [])
    except (requests.RequestException, ValueError) as exc:
        current_app.logger.error("FPP list playlists error: %s", exc)
        return jsonify({"error": "Could not reach the controller"}), 502

    audio = [n for n in names if isinstance(n, str) and _playlist_has_audio(n, load, set())]
    return jsonify({"audio": sorted(audio)})


@playlists_bp.post("/api/playlists/<name>/play")
@login_required
def play_playlist(name):
    """Start a named playlist on FPP.

    Scene playlists ('Scene - ...') and effect playlists ('Effect - ...') are
    applied directly by our Flask app rather than via FPP, because FPP skips
    command-only playlists that have total_duration=0.  Those playlists still
    exist on FPP so the scheduler can run them; this route is the instant
    start used by the Controls tab.  Everything else goes to FPP normally.
    """
    if "/" in name or "\\" in name or ".." in name:
        return jsonify({"error": "Invalid playlist name"}), 400

    # Effect playlists: look up the preset in the DB and fire the effect directly.
    if name.startswith("Effect - "):
        preset_name = name[len("Effect - "):]
        from app.models import EffectPreset
        from app.routes.effects import _run_preset
        preset = EffectPreset.query.filter_by(name=preset_name).first()
        if preset:
            _stop_current()
            _reset_overlays()
            ok, error = _run_preset(preset)
            if not ok:
                return jsonify({"error": f"Could not run effect: {error}"}), 502
            return jsonify({"ok": True})

    # Scene playlists: look up the scene in the DB and apply it directly.
    if name.startswith("Scene - "):
        scene_name = name[len("Scene - "):]
        from app.models import Scene
        from app.routes.scenes import _apply_scene
        scene = Scene.query.filter_by(name=scene_name).first()
        if scene:
            _stop_current()
            ok, errors = _apply_scene(scene)  # clears overlays and effects itself
            if not ok:
                return jsonify({"error": f"Failed zones: {', '.join(errors)}"}), 502
            return jsonify({"ok": True})

    # A randomized playlist is shuffled by us when it is written, so rewrite it
    # here to get a fresh order on every play. FPP's own random flag can't be
    # used — see _playlist_entries in app/routes/custom_playlists.py.
    from app.models import CustomPlaylist
    from app.routes.custom_playlists import _write_custom_playlist
    cp = CustomPlaylist.query.filter_by(name=name).first()
    if cp is not None and cp.random:
        _write_custom_playlist(cp)

    # Regular playlist: stop current, clear overlays, then start via FPP.
    _stop_current()
    _reset_overlays()

    try:
        data = json_object()
        repeat = bool(data.get("repeat", True))
        repeat_str = "true" if repeat else "false"
        # Not /playlist/<name>/start/<repeat>: FPP 9.5.x decodes the name and
        # pastes it unencoded into an internal URL, so any name containing a
        # space fails there — silently, with an HTTP 200 and nothing playing.
        resp = requests.get(
            fpp_url(f"/command/Start%20Playlist/{quote(name, safe='')}/{repeat_str}/false"),
            timeout=5,
        )
        resp.raise_for_status()
        return jsonify({"ok": True})
    except requests.RequestException as exc:
        current_app.logger.error("FPP start playlist '%s' error: %s", name, exc)
        return jsonify({"error": "Could not start playlist"}), 502


@playlists_bp.post("/api/playlists/stop")
@login_required
def stop_playback():
    """Stop FPP playback, clear running effects, and deactivate all overlay models."""
    _stop_current()
    _reset_overlays()
    return jsonify({"ok": True})


@playlists_bp.post("/api/overlays/release")
@login_required
def release_overlays():
    """Clear running effects and deactivate every overlay model.

    Deliberately leaves playback alone, so whatever FPP is scheduled to play
    shows through again.  This is what the Colors and Effects pages call when
    the user switches output off: /colors/stop deactivates the models but never
    stops an effect, and /api/effects/stop with no models resolves to "All",
    which FPP does not read as every model.
    """
    _reset_overlays()
    return jsonify({"ok": True})


@playlists_bp.get("/api/sequences")
@login_required
def list_sequences():
    """Return the sequence names known to FPP."""
    try:
        resp = requests.get(fpp_url("/sequence"), timeout=5)
        resp.raise_for_status()
        data = resp.json()
        sequences = data if isinstance(data, list) else []
        sequences = [s for s in sequences if isinstance(s, str)]
        return jsonify({"sequences": sorted(sequences)})
    except requests.RequestException as exc:
        current_app.logger.error("FPP list sequences error: %s", exc)
        return jsonify({"error": "Could not reach the controller"}), 502


@playlists_bp.post("/api/sequences/<name>/play")
@login_required
def play_sequence(name):
    """Play a named sequence by wrapping it in a single-item FPP playlist.

    All sequence playback is routed through the 'Current-Sequence' playlist so that
    stop, loop, and preemption all work via FPP's normal /playlists/stop API.
    The playlist definition is saved to FPP via its own POST API (no direct
    filesystem writes needed).
    """
    if "/" in name or "\\" in name or ".." in name:
        return jsonify({"error": "Invalid sequence name"}), 400

    # Stop whatever is currently playing so the new selection always preempts.
    _stop_current()

    _reset_overlays()

    data = json_object()
    repeat = bool(data.get("repeat", True))
    seq_file = name if name.endswith(".fseq") else f"{name}.fseq"

    # Build a single-sequence playlist and push it to FPP via its REST API.
    playlist_def = {
        "name": "Current-Sequence",
        "version": 4,
        "repeat": 1 if repeat else 0,
        "loopCount": 0,
        "desc": "",
        "random": 0,
        "empty": False,
        "leadIn": [],
        "mainPlaylist": [
            {
                "type": "sequence",
                "enabled": 1,
                "playOnce": 0 if repeat else 1,
                "sequenceName": seq_file,
                "displayMode": "argsOnly",
                "timecode": "Default",
                "duration": 86400,
            }
        ],
        "leadOut": [],
    }

    try:
        resp = requests.post(fpp_url("/playlist/Current-Sequence"), json=playlist_def, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.error("Could not save temp sequence playlist: %s", exc)
        return jsonify({"error": "Could not prepare sequence for playback"}), 500

    repeat_str = "true" if repeat else "false"
    try:
        resp = requests.get(fpp_url(f"/playlist/Current-Sequence/start/{repeat_str}"), timeout=5)
        resp.raise_for_status()
        return jsonify({"ok": True})
    except requests.RequestException as exc:
        current_app.logger.error("FPP start sequence playlist error: %s", exc)
        return jsonify({"error": "Could not start sequence"}), 502


# Every open Controls page polls this every few seconds (the kiosk, plus any
# phone on the hotspot). A short shared cache lets them all be answered by one
# call to fppd instead of one each. Playback changes made through this app
# clear it, so the page never shows the state from before its own click.
_STATUS_TTL = 2.0
_status_lock = threading.Lock()
_status_cache = {"at": 0.0, "body": None}


def invalidate_status_cache():
    with _status_lock:
        _status_cache["body"] = None


@playlists_bp.after_request
def _drop_stale_status(response):
    # Any POST here (play, stop, start a sequence) changes what fppd reports.
    if request.method == "POST":
        invalidate_status_cache()
    return response


@playlists_bp.get("/api/fppd/status")
@login_required
def fpp_status():
    """Proxy the FPP daemon status endpoint (cached for a couple of seconds)."""
    with _status_lock:
        if _status_cache["body"] is not None and time.monotonic() - _status_cache["at"] < _STATUS_TTL:
            return jsonify(_status_cache["body"])
        # Held across the fetch on purpose: concurrent pollers wait for the one
        # request in flight rather than each starting their own.
        try:
            resp = requests.get(fpp_url("/fppd/status"), timeout=5)
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            current_app.logger.error("FPP status error: %s", exc)
            return jsonify({"error": "Could not reach the controller"}), 502
        _status_cache["at"] = time.monotonic()
        _status_cache["body"] = body
    return jsonify(body)
