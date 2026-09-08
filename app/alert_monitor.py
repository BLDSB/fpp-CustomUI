"""Background thread that watches FPP's own scheduler and emails an alert
when a scheduled playlist fails to start — or when fppd stops answering at all.

Rather than re-deriving when a show should run from the raw schedule, this asks
fppd what it thinks is coming next. `/fppd/status` carries a `scheduler` block
whose `nextPlaylist.scheduledStartTime` is a Unix epoch with everything already
worked out: sunrise/sunset offsets, day-of-week rules, date ranges and odd/even
days. Reimplementing that here would mean tracking FPP's scheduler rules
forever, and getting Dusk-scheduled shows wrong in the meantime.
"""

import logging
import re
import smtplib
import threading
import time
from datetime import date, datetime
from email.mime.text import MIMEText

import requests

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_thread = None

# The start fppd last told us was coming: (playlist_name, start_epoch), or None.
_expected: tuple[str, int] | None = None
# Starts that have come due and are waiting to be verified:
# {(playlist_name, start_epoch): check_at_epoch}
_pending: dict[tuple[str, int], float] = {}
# Starts already resolved one way or the other, so the schedule fallback below
# cannot keep re-arming the same occurrence every minute:
# {(playlist_name, start_epoch): handled_at_epoch}
_handled: dict[tuple[str, int], float] = {}

# How far past a scheduled start the fallback will still pick it up. Long
# enough to cover a service restart during an outage, short enough that
# starting up late at night doesn't alert for every show earlier that day.
_FALLBACK_WINDOW = 3600

# Last poll's outcome, for the settings-page status panel.
_last_poll: float | None = None
_fppd_reachable: bool | None = None
# Consecutive failed status reads. fppd goes briefly unreachable whenever it is
# restarted, and a single 503 must not be enough to declare the show dark —
# the schedule fallback only engages once it has stayed down this many polls.
_status_failures = 0
_FALLBACK_AFTER_FAILURES = 3

# FPP stores day-of-week as an enum (Scheduler.h), not as the bitmask it uses
# internally. These are the INX_DAY_MASK_* values each enum maps to.
_DAY_ENUM_MASKS = {
    0: 0x04000,   # Sunday
    1: 0x02000,   # Monday
    2: 0x01000,   # Tuesday
    3: 0x00800,   # Wednesday
    4: 0x00400,   # Thursday
    5: 0x00200,   # Friday
    6: 0x00100,   # Saturday
    7: 0x07F00,   # Everyday
    8: 0x03E00,   # Mon-Fri
    9: 0x04100,   # Sat/Sun
    10: 0x02A00,  # Mon/Wed/Fri
    11: 0x01400,  # Tue/Thu
    12: 0x07C00,  # Sun-Thu
    13: 0x00300,  # Fri/Sat
}
_INX_DAY_MASK = 0x10000
_INX_ODD_DAY, _INX_EVEN_DAY = 14, 15

# Python weekday() (0=Mon…6=Sun) → the matching INX_DAY_MASK_* bit
_DOW_BITS = {0: 0x02000, 1: 0x01000, 2: 0x00800, 3: 0x00400,
             4: 0x00200, 5: 0x00100, 6: 0x04000}

# FPP counts odd/even days from its own epoch: the first commit to the FPP
# repository, 15 July 2013 (Scheduler.cpp).
_FPP_EPOCH = date(2013, 7, 15)


def _fpp(app, path):
    return f"{app.config['FPP_BASE_URL']}{path}"


def _to_int(value, default, lo=None, hi=None):
    """Parse an int from a settings value; fall back to default on garbage
    or out-of-range input so one bad DB value can't break the monitor."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return default
    if hi is not None and n > hi:
        return default
    return n


def _load_settings(app):
    with app.app_context():
        from app.models import AppSetting
        return {s.key: s.value for s in AppSetting.query.all()}


# Alerts can go to more than one person. Each of these settings holds one
# recipient, but we still split on commas/semicolons so a list pasted into a
# single box (the way the old single-recipient field was often used) works.
_RECIPIENT_KEYS = ("alert_email_to", "alert_email_to_2", "alert_email_to_3")
_RECIPIENT_SPLIT = re.compile(r"[,;]")


def _recipients(settings: dict) -> list[str]:
    """Every configured alert recipient, de-duplicated, in field order."""
    out, seen = [], set()
    for key in _RECIPIENT_KEYS:
        for addr in _RECIPIENT_SPLIT.split(settings.get(key) or ""):
            addr = addr.strip()
            if addr and addr.lower() not in seen:
                seen.add(addr.lower())
                out.append(addr)
    return out


def _fetch_status(app):
    """fppd's status, or None if it cannot be reached. None is meaningful —
    an unreachable fppd is itself an outage worth alerting on."""
    try:
        resp = requests.get(_fpp(app, "/fppd/status"), timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        _logger.debug("Alert monitor: cannot read FPP status: %s", exc)
        return None


def _next_start(status: dict):
    """The upcoming scheduled start fppd reports, as (playlist, epoch)."""
    nxt = (status.get("scheduler") or {}).get("nextPlaylist") or {}
    name = (nxt.get("playlistName") or "").strip()
    start = _to_int(nxt.get("scheduledStartTime"), 0)
    if not name or start <= 0:
        return None
    return name, start


def _is_running(status: dict, playlist: str, start_epoch: int) -> bool:
    """True if fppd shows this scheduled occurrence actually playing."""
    current = (status.get("scheduler") or {}).get("currentPlaylist") or {}
    name = (current.get("playlistName") or "").strip()
    if not name:
        # Older/alternate shape — fall back to the flat status fields.
        name = ((status.get("current_playlist") or {}).get("playlist") or "").strip()

    if name != playlist:
        return False

    # Matching the scheduled start pins this to the occurrence we are waiting
    # on rather than a manual run of the same playlist — but only when fppd
    # actually reports a scheduled time for it.
    scheduled = _to_int(current.get("scheduledStartTime"), 0)
    if scheduled and scheduled != start_epoch:
        return False

    playing = str(status.get("status_name", "")).lower() == "playing"
    return playing or status.get("status") == 1


def _entry_runs_today(entry, today: date) -> bool:
    """Whether a raw schedule entry is active today, using FPP's own day rules."""
    if not entry.get("enabled"):
        return False

    try:
        start = datetime.strptime(entry.get("startDate") or "2019-01-01", "%Y-%m-%d").date()
        end   = datetime.strptime(entry.get("endDate")   or "2099-12-31", "%Y-%m-%d").date()
        if not (start <= today <= end):
            return False
    except ValueError:
        pass  # unparseable dates shouldn't silence the alert

    day = _to_int(entry.get("day"), -1)
    if day < 0:
        return False

    if day in (_INX_ODD_DAY, _INX_EVEN_DAY):
        is_odd = (today - _FPP_EPOCH).days % 2 == 1
        return is_odd if day == _INX_ODD_DAY else not is_odd

    # "Day Mask" entries carry the bits directly; every other value is an enum
    # that FPP expands into those same bits.
    mask = day if day & _INX_DAY_MASK else _DAY_ENUM_MASKS.get(day, 0)
    return bool(mask & _DOW_BITS.get(today.weekday(), 0))


def _fetch_schedule(app):
    """The raw schedule. Apache serves this, so unlike /fppd/status it keeps
    answering while fppd is down — which is exactly when we need it."""
    try:
        resp = requests.get(_fpp(app, "/schedule"), timeout=5)
        resp.raise_for_status()
        return resp.json() or []
    except Exception as exc:
        _logger.debug("Alert monitor: cannot read FPP schedule: %s", exc)
        return []


def _schedule_starts(app, now: float) -> list[tuple[str, int]]:
    """Today's clock-time starts, straight from the schedule file.

    Only used when fppd is unreachable and cannot tell us itself. Sunrise and
    sunset entries are skipped: fppd is the only thing that resolves those, so
    a solar show is covered by the start we recorded while it was still alive.
    """
    today = datetime.fromtimestamp(now).date()
    out = []
    for entry in _fetch_schedule(app):
        playlist = (entry.get("playlist") or "").strip()
        if not playlist or not _entry_runs_today(entry, today):
            continue

        parts = (entry.get("startTime") or "").split(":")
        if len(parts) != 3:
            continue  # Dusk / SunRise / etc.
        try:
            h, m, s = (int(p) for p in parts)
        except ValueError:
            continue
        if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
            continue

        # No offset here on purpose: ScheduleEntry.cpp applies startTimeOffset
        # only when the start time has no colon in it, i.e. to solar entries,
        # which are precisely the ones this function skips.
        start = datetime.combine(today, datetime.min.time()).replace(
            hour=h, minute=m, second=s
        )
        out.append((playlist, int(start.timestamp())))
    return out


def _arm(key: tuple[str, int], delay_min: int, source: str):
    """Queue a scheduled start for verification. Caller holds _lock."""
    if key in _pending or key in _handled:
        return
    playlist, start_epoch = key
    check_at = start_epoch + delay_min * 60
    _pending[key] = check_at
    _logger.info(
        "Alert monitor: '%s' was due at %s (%s) — verifying at %s",
        playlist,
        datetime.fromtimestamp(start_epoch).strftime("%H:%M"),
        source,
        datetime.fromtimestamp(check_at).strftime("%H:%M"),
    )


def _process(app):
    global _expected, _last_poll, _fppd_reachable, _status_failures

    settings = _load_settings(app)
    if settings.get("alert_enabled") != "1":
        with _lock:
            _pending.clear()
            _handled.clear()
            _expected = None
        return

    delay_min = _to_int(settings.get("alert_delay_minutes"), 5, lo=1, hi=1440)
    status = _fetch_status(app)
    now = time.time()

    with _lock:
        _status_failures = 0 if status is not None else _status_failures + 1
        down_for_good = _status_failures >= _FALLBACK_AFTER_FAILURES

    # With fppd properly down we can't ask what was supposed to run, so read
    # the schedule file directly. Fetched outside the lock — network call.
    fallback = _schedule_starts(app, now) if down_for_good else []

    with _lock:
        _last_poll, _fppd_reachable = now, status is not None

        # A start we were watching has come and gone — put it on the clock.
        if _expected is not None and _expected[1] <= now:
            _arm(_expected, delay_min, "from fppd")
            _expected = None

        # fppd is unreachable: anything the schedule says should have started
        # recently gets checked too, so an fppd that died before we learned
        # about tonight's show still raises an alert.
        for key in fallback:
            if key[1] <= now <= key[1] + _FALLBACK_WINDOW:
                _arm(key, delay_min, "from schedule, fppd unreachable")

        if status is not None:
            # Anything confirmed playing is off the hook, even if it finishes
            # before its check time comes around.
            for key in list(_pending):
                if _is_running(status, *key):
                    _pending.pop(key, None)
                    _handled[key] = now
                    _logger.info("Alert monitor: '%s' is playing — no alert", key[0])

            # Only ever arm on a start that is still ahead of us, so a stale
            # value from fppd cannot fire an alert for something long past.
            nxt = _next_start(status)
            if nxt is not None and nxt[1] > now and nxt != _expected:
                _expected = nxt
                _logger.info(
                    "Alert monitor: watching '%s', due %s",
                    nxt[0], datetime.fromtimestamp(nxt[1]).strftime("%a %H:%M"),
                )

        due = []
        for key, check_at in list(_pending.items()):
            if check_at > now:
                continue
            if status is None and not down_for_good:
                # fppd has only just stopped answering — it may be restarting.
                # Leave this pending and decide on a later poll.
                continue
            _pending.pop(key, None)
            _handled[key] = now
            due.append(key)

        # Keep _handled from growing without bound across a long season.
        for key, when in list(_handled.items()):
            if now - when > 86400:
                _handled.pop(key, None)

    for playlist, _start in due:
        if status is None:
            _logger.warning(
                "Alert monitor: '%s' was due and fppd is not responding. Sending alert.",
                playlist,
            )
            _alert_fppd_down(settings, playlist)
        else:
            _logger.warning(
                "Alert monitor: '%s' should be playing but is not. Sending alert.",
                playlist,
            )
            _alert_not_playing(settings, playlist)


def _alert_not_playing(settings: dict, playlist_name: str):
    now_str = datetime.now().strftime("%I:%M %p")
    _send_email(
        settings,
        f"Show Alert: '{playlist_name}' is not playing",
        f"Show Alert\n\n"
        f"Playlist '{playlist_name}' was scheduled to start but is not playing "
        f"as of {now_str}.\n\n"
        f"Please check your controller.\n",
    )


def _alert_fppd_down(settings: dict, playlist_name: str):
    now_str = datetime.now().strftime("%I:%M %p")
    _send_email(
        settings,
        "Show Alert: the player is not responding",
        f"Show Alert\n\n"
        f"Playlist '{playlist_name}' was scheduled to start, but as of {now_str} "
        f"the player is not responding at all.\n\n"
        f"The show is almost certainly dark. Please check your controller.\n",
    )


def _smtp_config(settings: dict):
    return (
        (settings.get("alert_smtp_host") or "").strip(),
        _to_int(settings.get("alert_smtp_port"), 587, lo=1, hi=65535),
        (settings.get("alert_smtp_user") or "").strip(),
        (settings.get("alert_smtp_pass") or "").strip(),
        (settings.get("alert_email_from") or settings.get("alert_smtp_user") or "").strip(),
        _recipients(settings),
    )


def _send_email(settings: dict, subject: str, body: str):
    host, port, user, password, from_addr, to_addrs = _smtp_config(settings)

    if not all([host, user, password]) or not to_addrs:
        _logger.warning("Alert monitor: email not configured — skipping alert")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)

    # A schedule alert is one-shot — if this attempt is lost, no one is told
    # the show is dark. Retry a couple of times to ride out transient network
    # blips before giving up.
    last_error = None
    for attempt in range(3):
        try:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(user, password)
                refused = smtp.sendmail(from_addr, to_addrs, msg.as_string())
            delivered = [a for a in to_addrs if a not in refused]
            _logger.info("Alert monitor: sent alert to %s", ", ".join(delivered))
            if refused:
                # The send succeeded for the rest, so don't retry the whole
                # batch — just say who the server would not take.
                _logger.error(
                    "Alert monitor: recipients refused: %s", ", ".join(sorted(refused))
                )
            return
        except Exception as exc:
            last_error = exc
            _logger.warning("Alert monitor: email attempt %d/3 failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(10)
    _logger.error("Alert monitor: giving up on alert '%s': %s", subject, last_error)


def send_test_email(app) -> tuple[bool, str]:
    """Called from the settings API to send a test message. Returns
    (ok, detail) — detail is the error on failure, or the recipients it went
    to on success so the page can name them."""
    settings = _load_settings(app)
    host, port, user, password, from_addr, to_addrs = _smtp_config(settings)

    if not all([host, user, password]) or not to_addrs:
        return False, "Email not fully configured — fill in all fields and save first."

    msg = MIMEText(
        "This is a test alert from your lighting control UI.\n\n"
        "If you received this, email alerts are working correctly."
    )
    msg["Subject"] = "Show Alert — Test Message"
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, password)
            refused = smtp.sendmail(from_addr, to_addrs, msg.as_string())
    except Exception as exc:
        return False, str(exc)

    # Tell the user which addresses the server would not accept — a silent
    # "sent" for a typo'd recipient is the whole failure mode this test exists
    # to catch.
    if refused:
        return False, "Rejected by the mail server: " + ", ".join(sorted(refused))
    return True, ", ".join(to_addrs)


def monitor_state(app) -> dict:
    """What the monitor is watching right now — surfaced on the settings page
    so "is this thing actually armed?" is answerable without reading the log."""
    settings = _load_settings(app)
    with _lock:
        expected = _expected
        pending = dict(_pending)
        last = _last_poll
        fppd_ok = None if _fppd_reachable is None else (
            _status_failures < _FALLBACK_AFTER_FAILURES
        )

    state = {
        "enabled": settings.get("alert_enabled") == "1",
        "recipients": _recipients(settings),
        "fppd_reachable": fppd_ok,
        "last_poll": datetime.fromtimestamp(last).strftime("%I:%M %p") if last else None,
        "watching": None,
        "verifying": [],
    }
    if expected:
        state["watching"] = {
            "playlist": expected[0],
            "at": datetime.fromtimestamp(expected[1]).strftime("%a %b %d, %I:%M %p"),
        }
    for (playlist, _start), check_at in sorted(pending.items(), key=lambda kv: kv[1]):
        state["verifying"].append({
            "playlist": playlist,
            "at": datetime.fromtimestamp(check_at).strftime("%I:%M %p"),
        })
    return state


def _monitor_loop(app):
    time.sleep(45)  # let Flask finish starting up
    while True:
        try:
            _process(app)
        except Exception:
            _logger.exception("Alert monitor unexpected error")
        time.sleep(60)


def start_monitor(app):
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(
        target=_monitor_loop, args=(app,), daemon=True, name="alert-monitor"
    )
    _thread.start()
    _logger.info("Alert monitor started")
