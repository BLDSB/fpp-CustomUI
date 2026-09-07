"""Builders for FPP version-4 playlist JSON.

Scene and effect playlists used to build these dicts inline, one playlist per
saved item.  The entries are composable, though — a URL command plus a pause is
just as valid as the third entry of a playlist as it is the first — so they live
here and get concatenated by app/routes/custom_playlists.py.

The exact shape below is load-bearing and was arrived at by trial and error on
real hardware.  Do not reorder or restructure it:

  * The URL command must be in mainPlaylist, never leadIn.  In leadIn it blocks
    FPP from ever reaching mainPlaylist.
  * leadOut is pause(3) FIRST, then the Stop Effects command.  Reversed, the
    overlays are never cleared.
  * The stop command is "Overlay Model Effect" with args
    ["--All Models--", "Enabled", "Stop Effects"] — not "Overlay Model State",
    not "Overlay Model Effect Stop".
  * leadIn is an empty list.
"""

from flask import current_app

# Flask is reached directly here rather than through Apache: FPP runs the URL
# command locally and the UI path is user-configurable, so a fixed loopback
# address is the only stable target.
_FLASK_BASE = "http://localhost:5000"


def url_cmd(u):
    return {"type": "command", "enabled": 1, "command": "URL",
            "args": [u, "GET", ""], "startDelay": 0, "endDelay": 0}


def overlay_effect(model, state, action):
    return {"type": "command", "enabled": 1, "command": "Overlay Model Effect",
            "args": [model, state, action], "startDelay": 0, "endDelay": 0}


def pause_item(d):
    return {"type": "pause", "enabled": 1, "duration": d,
            "startDelay": 0, "endDelay": 0}


def sequence_entry(seq_name, duration=None):
    """A .fseq entry that plays once and lets FPP advance at its natural end.

    playlists.py's single-sequence wrapper pins duration to 86400 because it is
    the only entry and must not self-terminate; inside a multi-item playlist
    that would stall the show, so duration is omitted unless a caller has a real
    length to declare.
    """
    seq_file = seq_name if seq_name.endswith(".fseq") else f"{seq_name}.fseq"
    entry = {
        "type": "sequence",
        "enabled": 1,
        "playOnce": 1,
        "sequenceName": seq_file,
        "displayMode": "argsOnly",
        "timecode": "Default",
    }
    if duration:
        entry["duration"] = duration
    return entry


def _internal_url(kind, obj_id, clear=False):
    token = current_app.config.get("INTERNAL_TOKEN", "")
    url = f"{_FLASK_BASE}/internal/{kind}/{obj_id}/apply?token={token}"
    if clear:
        url += "&clear=1"
    return url


def scene_entries(scene_id, hold, clear=False):
    """Apply a scene through Flask, then hold it for `hold` seconds.

    Pixel overlay models are a persistent layer — the colors stay set until
    something clears them — so the pause is what gives the entry its duration
    and keeps FPP's player active.

    `clear` turns off every overlay model before the colors are set.  Off for a
    standalone scene playlist (the historical behavior); on inside a built
    playlist, so zones the scene does not name do not keep colors from whatever
    item ran before it.
    """
    return [url_cmd(_internal_url("scene", scene_id, clear)), pause_item(hold)]


def effect_entries(preset_id, hold, clear=False):
    """Fire an effect preset through Flask, then let it run for `hold` seconds.

    `clear` behaves as it does for scenes: off for a standalone effect playlist,
    on inside a built playlist so a model the preset does not drive is not left
    lit by whatever ran before it.
    """
    return [url_cmd(_internal_url("effect", preset_id, clear)), pause_item(hold)]


def standard_lead_out():
    """Clear the overlay layer when FPP stops the playlist gracefully.

    Runs when the scheduler reaches an entry's endTime with stopType=Graceful.
    """
    return [
        pause_item(3),
        overlay_effect("--All Models--", "Enabled", "Stop Effects"),
    ]


def build_playlist_def(name, entries, desc, repeat=True):
    return {
        "name": name,
        "version": 4,
        "repeat": 1 if repeat else 0,
        "loopCount": 0,
        "desc": desc,
        "random": 0,
        "empty": False,
        "leadIn": [],
        "mainPlaylist": entries,
        "leadOut": standard_lead_out(),
    }
