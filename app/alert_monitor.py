"""Background thread that watches the FPP schedule and emails an alert
when a scheduled playlist fails to start within the configured delay."""

import logging
import smtplib
import threading
import time
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText

import requests

_logger = logging.getLogger(__name__)
_pending: dict[str, datetime] = {}  # {playlist_name: when_to_check}
_lock = threading.Lock()
_thread = None

# Python weekday() (0=Mon…6=Sun) → FPP day bitmask
_DOW_BITS = {
    0: 0x02000,  # Monday
    1: 0x01000,  # Tuesday
    2: 0x00800,  # Wednesday
    3: 0x00400,  # Thursday
    4: 0x00200,  # Friday
    5: 0x00100,  # Saturday
    6: 0x04000,  # Sunday
}


def _fpp(app, path):
    return f"{app.config['FPP_BASE_URL']}{path}"


def _load_settings(app):
    with app.app_context():
        from app.models import AppSetting
        return {s.key: s.value for s in AppSetting.query.all()}


def _entry_active_today(entry, today: date) -> bool:
    if not entry.get("enabled"):
        return False

    # Date range
    try:
        start = datetime.strptime(entry.get("startDate") or "2019-01-01", "%Y-%m-%d").date()
        end   = datetime.strptime(entry.get("endDate")   or "2099-12-31", "%Y-%m-%d").date()
        if not (start <= today <= end):
            return False
    except ValueError:
        pass

    # Day-of-week bitmask
    day_mask = int(entry.get("day") or 0)
    dow_bit  = _DOW_BITS.get(today.weekday(), 0)
    return bool(day_mask & dow_bit)


def _parse_hms(s: str):
    """Return (h, m, s) from 'HH:MM:SS', or None for sunrise/sunset strings."""
    if not s or ":" not in s:
        return None
    try:
        parts = s.split(":")
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return None


def _process_schedule(app):
    settings = _load_settings(app)

    if settings.get("alert_enabled") != "1":
        with _lock:
            _pending.clear()
        return

    delay_min = max(1, int(settings.get("alert_delay_minutes") or 5))

    try:
        resp = requests.get(_fpp(app, "/schedule"), timeout=5)
        resp.raise_for_status()
        schedule = resp.json() or []
    except Exception as exc:
        _logger.debug("Alert monitor: cannot read FPP schedule: %s", exc)
        return

    now = datetime.now()
    today = now.date()
    window_start = now - timedelta(seconds=70)  # one full poll cycle

    with _lock:
        for entry in schedule:
            playlist = (entry.get("playlist") or "").strip()
            if not playlist:
                continue
            if not _entry_active_today(entry, today):
                continue

            hms = _parse_hms(entry.get("startTime") or "")
            if hms is None:
                continue  # sunrise/sunset — skip for now

            start_dt = datetime(today.year, today.month, today.day, *hms)
            if window_start <= start_dt <= now and playlist not in _pending:
                check_at = now + timedelta(minutes=delay_min)
                _pending[playlist] = check_at
                _logger.info(
                    "Alert monitor: '%s' started at %s — will verify at %s",
                    playlist, start_dt.strftime("%H:%M"), check_at.strftime("%H:%M"),
                )

        due = [(name, t) for name, t in list(_pending.items()) if t <= now]

    for playlist, _ in due:
        with _lock:
            _pending.pop(playlist, None)
        _verify_and_alert(app, playlist, settings)


def _verify_and_alert(app, expected_playlist: str, settings: dict):
    try:
        resp = requests.get(_fpp(app, "/fppd/status"), timeout=5)
        resp.raise_for_status()
        status = resp.json()
    except Exception as exc:
        _logger.warning("Alert monitor: cannot check FPP status: %s", exc)
        return

    current  = (status.get("current_playlist") or {}).get("playlist", "")
    fpp_mode = str(status.get("status", "")).lower()
    playing  = fpp_mode == "playing" or status.get("status") == 1

    if playing and current:
        _logger.info("Alert monitor: '%s' is playing — no alert", current)
        return

    _logger.warning(
        "Alert monitor: '%s' should be playing but FPP is idle (status=%s). Sending alert.",
        expected_playlist, fpp_mode,
    )
    _send_email(settings, expected_playlist)


def _send_email(settings: dict, playlist_name: str):
    host     = (settings.get("alert_smtp_host") or "").strip()
    port     = int(settings.get("alert_smtp_port") or 587)
    user     = (settings.get("alert_smtp_user") or "").strip()
    password = (settings.get("alert_smtp_pass") or "").strip()
    from_addr = (settings.get("alert_email_from") or user).strip()
    to_addr   = (settings.get("alert_email_to") or "").strip()

    if not all([host, user, password, to_addr]):
        _logger.warning("Alert monitor: email not configured — skipping alert")
        return

    now_str = datetime.now().strftime("%I:%M %p")
    subject = f"FPP Alert: '{playlist_name}' is not playing"
    body = (
        f"FPP Show Alert\n\n"
        f"Playlist '{playlist_name}' was scheduled to start but is not playing as of {now_str}.\n\n"
        f"Please check your FPP controller.\n"
    )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(from_addr, [to_addr], msg.as_string())
        _logger.info("Alert monitor: sent alert to %s", to_addr)
    except Exception as exc:
        _logger.error("Alert monitor: failed to send email: %s", exc)


def send_test_email(app) -> tuple[bool, str]:
    """Called from the settings API to send a test message. Returns (ok, error_msg)."""
    settings = _load_settings(app)
    host     = (settings.get("alert_smtp_host") or "").strip()
    port     = int(settings.get("alert_smtp_port") or 587)
    user     = (settings.get("alert_smtp_user") or "").strip()
    password = (settings.get("alert_smtp_pass") or "").strip()
    from_addr = (settings.get("alert_email_from") or user).strip()
    to_addr   = (settings.get("alert_email_to") or "").strip()

    if not all([host, user, password, to_addr]):
        return False, "Email not fully configured — fill in all fields and save first."

    msg = MIMEText(
        "This is a test alert from your FPP Custom UI.\n\n"
        "If you received this, email alerts are working correctly."
    )
    msg["Subject"] = "FPP Alert — Test Message"
    msg["From"]    = from_addr
    msg["To"]      = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(from_addr, [to_addr], msg.as_string())
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _monitor_loop(app):
    time.sleep(45)  # let Flask finish starting up
    while True:
        try:
            _process_schedule(app)
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
