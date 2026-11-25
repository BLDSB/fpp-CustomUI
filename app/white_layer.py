"""Channels held through FPP's channel-range overlay: RGBW white and dimmers.

FPP's overlay models are RGB: every model call (fill, pixel, effects) reaches
only the first three channels of a node, so the 4th channel of an RGBW node can
not be written through the model itself.  A second overlay model over the white
channels does not work either: fppd copies each active model's *whole* block
over its channel range every frame, so a white-only model overwrites the colors
with its zeros (and the transparent states that would avoid it are downgraded to
a plain copy whenever nothing else is running).

What fppd does offer is a channel-range overlay (``PUT /overlays/range``).  It
forces channels to a value after every model has been applied, takes a whole
list of channels in one request, and so can hold exactly the white channels
without touching the colors.

DMX fixtures with a master dimmer use the same mechanism: the dimmer sits
outside the color channels and must be at full while the app is lighting the
fixture, and must be let go afterwards so a sequence can drive it again.

Ranges outlive the models that were lit beside them, so this module owns their
lifecycle.  It remembers which models currently hold channels and:

* ``apply`` sets the white channels of the given models;
* ``hold_dimmers`` raises the dimmers of the given models;
* ``drop`` lets go of everything some models hold (they are being switched
  off).  fppd cannot delete a range it was never given (it adds a bogus one
  instead), so this clears every range and sets the survivors again rather than
  deleting selectively;
* ``drop_white`` / ``clear_white`` do the same for white alone, for a model that
  stays lit;
* ``clear`` clears everything, and runs wherever the app switches overlay
  models off, so nothing outlives the colors it belongs to.

fppd can also switch models off without the app (a playlist's lead-out, FPP's
own pages).  While dimmers are held, a watcher notices and lets go.

The state is in memory.  After an fpp-ui restart ``drop`` and ``clear`` still
leave fppd with nothing it was not meant to keep, because both start from a
clear-all.
"""
import threading
import time

import requests
from flask import current_app

from app.fpp_api import fpp_url, inactive_models
from app.models import FixtureDimmer, dimmer_channels, rgbw_models, white_channels

# Channels per request.  They travel in the URL path, so keep it well short of
# the limit on a whole-display model with thousands of nodes.
_CHUNK = 250

_DIMMER_FULL = 255
_WATCH_SECONDS = 5

_lock = threading.RLock()
_levels = {}     # overlay model -> white level currently forced on its channels
_dimmed = set()  # overlay models whose dimmers are currently held at full
_watcher = None


def _set(model_channels, level):
    for i in range(0, len(model_channels), _CHUNK):
        chunk = ",".join(str(c) for c in model_channels[i:i + _CHUNK])
        requests.put(fpp_url(f"/overlays/range/{chunk}/{level}"), timeout=5).raise_for_status()


def _delete_all():
    requests.put(
        fpp_url("/overlays/range"), json={"deleteAll": True}, timeout=5,
    ).raise_for_status()


def apply(levels):
    """Force the white channel of each model in ``{model: 0-255}``.

    Models without a white channel are ignored.  Raises ``requests`` errors so
    the caller can report that the controller could not be reached.
    """
    with _lock:
        channels = white_channels(levels)
        for model, level in levels.items():
            if model in channels:
                _set(channels[model], level)
                _levels[model] = level


def hold_dimmers(models):
    """Hold the dimmers of ``models`` at full.

    Models without dimmers are ignored.  Raises ``requests`` errors.
    """
    with _lock:
        for model, channels in dimmer_channels(models).items():
            _set(channels, _DIMMER_FULL)
            _dimmed.add(model)
        if _dimmed:
            _start_watcher(current_app._get_current_object())


def _restore_holds():
    """Clear every range, then set again what is still meant to be held."""
    _delete_all()
    white = white_channels(_levels)
    for model, level in _levels.items():
        _set(white.get(model, []), level)
    dimmers = dimmer_channels(_dimmed)
    for model in _dimmed:
        _set(dimmers.get(model, []), _DIMMER_FULL)


def _release(white, dimmers):
    gone = [m for m in white if m in _levels]
    dark = [m for m in dimmers if m in _dimmed]
    if not gone and not dark:
        return
    for m in gone:
        del _levels[m]
    _dimmed.difference_update(dark)
    try:
        _restore_holds()
    except requests.RequestException as exc:
        names = ", ".join(sorted(set(gone) | set(dark)))
        current_app.logger.warning("Could not release held channels for %s: %s", names, exc)


def drop(models):
    """Let go of everything ``models`` hold; others keep theirs."""
    with _lock:
        models = list(models)
        _release(models, models)


def drop_white(models):
    """Stop forcing the white channel of ``models``; their dimmers stay up."""
    with _lock:
        _release(list(models), [])


def clear_white():
    """Stop forcing any white channel; dimmers stay up."""
    with _lock:
        _release(list(_levels), [])


def clear():
    """Stop forcing any channel.

    Skipped when there is nothing it could be holding, so a show with neither
    white channels nor dimmers never pays for the extra request.
    """
    with _lock:
        if (not _levels and not _dimmed and not rgbw_models()
                and FixtureDimmer.query.first() is None):
            return
        _levels.clear()
        _dimmed.clear()
        try:
            _delete_all()
        except requests.RequestException as exc:
            current_app.logger.warning("Could not release held channels: %s", exc)


def release_switched_off():
    """Let go of the channels of any model fppd no longer has switched on."""
    with _lock:
        off = inactive_models(set(_levels) | _dimmed)
        if off:     # None means fppd could not be asked: keep holding
            drop(off)


def _start_watcher(app):
    global _watcher
    if not _WATCH_SECONDS or (_watcher and _watcher.is_alive()):
        return
    _watcher = threading.Thread(target=_watch, args=(app,), daemon=True,
                                name="dimmer-watch")
    _watcher.start()


def _watch(app):
    global _watcher
    while True:
        time.sleep(_WATCH_SECONDS)
        with app.app_context():
            try:
                release_switched_off()
            except Exception:
                app.logger.exception("Dimmer watch failed")
        with _lock:
            if not _dimmed:
                _watcher = None
                return
