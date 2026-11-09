"""The white channel of RGBW zones, driven through FPP's channel-range overlay.

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

Ranges outlive the models that were lit beside them, so this module owns their
lifecycle.  It remembers which models currently have white and:

* ``apply`` sets the white channels of the given models;
* ``drop`` takes some models' white away.  fppd cannot delete a range it was
  never given (it adds a bogus one instead), so this clears every range and
  sets the survivors again rather than deleting selectively;
* ``clear`` clears everything, and runs wherever the app switches overlay
  models off, so white never outlives the colors it belongs to.

The state is in memory.  After an fpp-ui restart ``drop`` and ``clear`` still
leave fppd with nothing it was not meant to keep, because both start from a
clear-all.
"""
import threading

import requests
from flask import current_app

from app.fpp_api import fpp_url
from app.models import rgbw_models, white_channels

# Channels per request.  They travel in the URL path, so keep it well short of
# the limit on a whole-display model with thousands of nodes.
_CHUNK = 250

_lock = threading.RLock()
_levels = {}   # overlay model -> white level currently forced on its channels


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


def drop(models):
    """Stop forcing the white channel of ``models``; others keep theirs."""
    with _lock:
        gone = [m for m in models if m in _levels]
        if not gone:
            return
        for m in gone:
            del _levels[m]
        try:
            _delete_all()
            channels = white_channels(_levels)
            for model, level in _levels.items():
                _set(channels.get(model, []), level)
        except requests.RequestException as exc:
            current_app.logger.warning("Could not drop white for %s: %s", ", ".join(gone), exc)


def clear():
    """Stop forcing any white channel.

    Skipped when there is nothing it could be holding, so an RGB-only show never
    pays for the extra request.
    """
    with _lock:
        if not _levels and not rgbw_models():
            return
        _levels.clear()
        try:
            _delete_all()
        except requests.RequestException as exc:
            current_app.logger.warning("Could not clear white: %s", exc)
