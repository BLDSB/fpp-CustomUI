"""Terms of Service acceptance record.

One record per install, kept in AppSetting: the first person to agree (on the
setup page, or on the accept screen after an upgrade) covers the controller
until the Terms change. Bump TERMS_VERSION whenever the Terms text changes so
everyone is asked again; it matches the "Last updated" date on the Terms.
"""
import datetime

from flask import current_app

from app import db
from app.models import AppSetting

TERMS_VERSION = "2026-10-12-r3"

_VERSION_KEY = "terms_accepted_version"
_AT_KEY = "terms_accepted_at"
_VIA_KEY = "terms_accepted_via"


def terms_accepted():
    """True when the current Terms version has been accepted on this install.

    Fails open if the database cannot be read: a controller running a lit
    show must not lock its operators out over a bookkeeping check.
    """
    try:
        setting = db.session.get(AppSetting, _VERSION_KEY)
    except Exception:
        current_app.logger.exception("Could not read Terms acceptance — allowing access")
        return True
    return bool(setting and setting.value == TERMS_VERSION)


def record_acceptance(via):
    """Store that the current Terms were accepted, and when and where."""
    now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    for key, value in ((_VERSION_KEY, TERMS_VERSION), (_AT_KEY, now), (_VIA_KEY, via)):
        setting = db.session.get(AppSetting, key)
        if setting:
            setting.value = value
        else:
            db.session.add(AppSetting(key=key, value=value))
    db.session.commit()
    current_app.logger.info("Terms of Service %s accepted via %s", TERMS_VERSION, via)
