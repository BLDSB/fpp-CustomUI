"""Decisions and wording for the "show starts soon" reminder emails.

Everything here is pure: it takes fppd's resolved schedule and the persisted
reminder state and returns what to send. The monitor thread in alert_monitor.py
owns the network calls, the clock and the database, which keeps the awkward
rules (gaps, moved dates, first-install baseline) testable without Flask.

Like the missed-show alert, this never re-derives the schedule from raw entries.
`/fppd/schedule` `items` already have solar times, day rules, date ranges and
holidays worked out.
"""

from datetime import date, datetime, timedelta

DEFAULT_LEAD_DAYS = 1
DEFAULT_SEND_HOUR = 9
DEFAULT_GAP_DAYS = 14

# Reminder records older than this are dropped so the state cannot grow forever.
_PRUNE_AFTER_DAYS = 400


def playlist_starts(sched: dict) -> list[dict]:
    """Every playlist start in fppd's look-ahead, soonest first.

    Starts that a higher-priority show already occupies are dropped, using the
    same rule as the Controls page preview: while a show is running, a start
    whose priority is not better than the running one never plays.
    """
    entries = {e.get("id"): e for e in sched.get("entries", [])}
    running = []  # (end epoch, priority) of shows that have started and not ended
    out = []
    for item in sorted(sched.get("items", []), key=lambda i: int(i.get("startTime", 0) or 0)):
        if item.get("command") != "Start Playlist":
            continue
        args = item.get("args") or []
        name = str(args[0]).strip() if args else ""
        if not name:
            continue
        entry = entries.get(item.get("id"), {})
        if entry and not entry.get("enabled", 1):
            continue
        start = int(item.get("startTime", 0) or 0)
        end = int(item.get("endTime", 0) or 0)
        priority = item.get("priority", 0)
        running = [r for r in running if r[0] > start]
        if running and priority >= running[-1][1]:
            continue
        running.append((end, priority))
        out.append({"name": name, "start": start, "end": end})
    return out


def _day(epoch: float) -> date:
    return datetime.fromtimestamp(epoch).date()


def _iso(d: date) -> str:
    return d.isoformat()


def due_reminders(
    starts: list[dict],
    now: float,
    *,
    lead_days: int,
    gap_days: int,
    last_active: dict,
    notified: dict,
) -> list[dict]:
    """Which shows deserve a reminder right now.

    `last_active` maps playlist -> ISO date it last played; `notified` maps
    playlist -> {"date": ISO date of the start we told them about, ...}.
    Returns dicts: {name, start, end, kind: "new" | "changed", previous: ISO|None}.
    """
    today = _day(now)
    horizon = today + timedelta(days=lead_days)

    # Only each playlist's next start matters for deciding to remind.
    nxt = {}
    for s in starts:
        if s["start"] > now and s["name"] not in nxt:
            nxt[s["name"]] = s

    due = []
    for name, s in nxt.items():
        start_day = _day(s["start"])
        if start_day > horizon:
            continue

        record = notified.get(name)
        record_day = None
        if record:
            try:
                record_day = date.fromisoformat(record.get("date", ""))
            except ValueError:
                record_day = None

        # Told about this exact day already — nothing more to say.
        if record_day == start_day:
            continue

        if record_day is not None and record_day >= today:
            # A reminder is out for a start that hasn't happened yet, and the
            # show has moved since. Say so rather than leave them expecting
            # the old date.
            due.append({**s, "kind": "changed", "previous": _iso(record_day)})
            continue

        # Otherwise this is a fresh run. Remind only after a real gap — a
        # nightly show that played yesterday must never email again.
        last = last_active.get(name)
        if last:
            try:
                idle = (today - date.fromisoformat(last)).days
            except ValueError:
                idle = gap_days
            if idle < gap_days:
                continue
        due.append({**s, "kind": "new", "previous": None})
    return due


def mark_notified(notified: dict, due: list[dict], now: float) -> dict:
    """The reminder state after the emails for `due` went out."""
    out = dict(notified)
    stamp = _iso(_day(now))
    for d in due:
        out[d["name"]] = {"date": _iso(_day(d["start"])), "sent": stamp}
    return out


def prune(notified: dict, now: float) -> dict:
    cutoff = _day(now) - timedelta(days=_PRUNE_AFTER_DAYS)
    keep = {}
    for name, rec in notified.items():
        try:
            if date.fromisoformat(rec.get("date", "")) >= cutoff:
                keep[name] = rec
        except ValueError:
            continue
    return keep


def baseline(starts: list[dict], now: float, lead_days: int) -> tuple[dict, dict]:
    """First run of the feature: treat shows already about to start as known.

    Without this, turning the feature on during a nightly run would remind
    about every show that is in progress, because we have no history for it.
    Returns (last_active, notified) seeds.
    """
    today = _day(now)
    horizon = today + timedelta(days=lead_days)
    seen_active, seen_notified = {}, {}
    for s in starts:
        if s["start"] <= now:
            continue
        d = _day(s["start"])
        if d <= horizon and s["name"] not in seen_notified:
            seen_active[s["name"]] = _iso(today)
            seen_notified[s["name"]] = {"date": _iso(d), "sent": _iso(today)}
    return seen_active, seen_notified


def run_summary(starts: list[dict], name: str, first_start: float, distance_days: int, now: float) -> str:
    """How long a show runs, as far as the look-ahead can see."""
    mine = [s["start"] for s in starts if s["name"] == name and s["start"] >= first_start]
    if not mine:
        return ""
    days = sorted({_day(t) for t in mine})
    last = days[-1]
    edge = _day(now) + timedelta(days=max(distance_days - 1, 1))
    if len(days) == 1:
        return "Plays once in the schedule window." if last < edge else ""
    tail = " and possibly beyond" if last >= edge else ""
    return f"Plays {len(days)} days through {last.strftime('%a %b %d')}{tail}."


def _clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%I:%M %p").lstrip("0")


def _when(d: date, today: date) -> str:
    delta = (d - today).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return f"on {d.strftime('%A, %B %d')}"


def build_agenda(starts: list[dict], now: float, days: int = 28) -> list[str]:
    """Every scheduled show start in the next `days` days, one line each,
    grouped under its date. Empty list when nothing is scheduled."""
    cutoff = now + days * 86400
    lines, current = [], None
    for s in starts:
        if not (now <= s["start"] <= cutoff):
            continue
        d = _day(s["start"])
        if d != current:
            if current is not None:
                lines.append("")
            lines.append(d.strftime("%A, %B %d").replace(" 0", " "))
            current = d
        span = _clock(s["start"])
        if s.get("end"):
            span += f" - {_clock(s['end'])}"
        lines.append(f"  {span}  {s['name']}")
    return lines


def build_reminder(
    due: list[dict],
    problems: dict,
    summaries: dict,
    now: float,
    agenda: list[str] | None = None,
    agenda_days: int = 28,
) -> tuple[str, str]:
    """(subject, body) for one digest covering every show in `due`.

    `problems` maps playlist -> list of pre-flight problem strings (absent or
    empty means it checked out). `summaries` maps playlist -> run summary text.
    """
    today = _day(now)
    flagged = [d for d in due if problems.get(d["name"])]

    if len(due) == 1:
        d = due[0]
        when = _when(_day(d["start"]), today)
        if d["kind"] == "changed":
            subject = f"Show reminder: '{d['name']}' date changed — now plays {when}"
        else:
            subject = f"Show reminder: '{d['name']}' plays {when}"
    else:
        subject = f"Show reminder: {len(due)} shows are about to play"
    if flagged:
        subject += " (needs attention)"

    lines = ["Show Reminder", ""]
    if flagged:
        lines += [
            "NEEDS ATTENTION: " + ", ".join(f"'{d['name']}'" for d in flagged)
            + " has a problem that should be fixed before it plays.",
            "",
        ]
    for d in sorted(due, key=lambda x: x["start"]):
        start_day = _day(d["start"])
        lines.append(f"'{d['name']}' plays {_when(start_day, today)}"
                     f" ({start_day.strftime('%A, %B %d')})")
        lines.append(f"  Starts: {_clock(d['start'])}")
        if d.get("end"):
            lines.append(f"  Ends:   {_clock(d['end'])}")
        if d["kind"] == "changed" and d.get("previous"):
            prev = date.fromisoformat(d["previous"])
            lines.append(f"  Changed: you were previously told {prev.strftime('%A, %B %d')}.")
        summary = summaries.get(d["name"])
        if summary:
            lines.append(f"  {summary}")
        issues = problems.get(d["name"])
        if issues:
            lines.append("  Problems found:")
            lines += [f"    - {p}" for p in issues]
        else:
            lines.append("  Checked: playlist and its files look ready.")
        lines.append("")
    if agenda is not None:
        lines.append(f"Everything scheduled in the next {agenda_days} days:")
        lines.append("")
        lines += agenda if agenda else ["  Nothing scheduled."]
        lines.append("")
    lines.append("You are receiving this because this show has not played recently.")
    return subject, "\n".join(lines) + "\n"


def build_nothing_scheduled(days: int) -> tuple[str, str]:
    return (
        "Show reminder: nothing is scheduled",
        "Show Reminder\n\n"
        f"No shows are scheduled to play in the next {days} day{'s' if days != 1 else ''}.\n\n"
        "If that is expected, no action is needed. Otherwise, check your schedule.\n",
    )


def preflight(name: str, load_playlist, sequence_names: set | None, media_names: set | None) -> list[str]:
    """Problems that would stop `name` from playing properly.

    `load_playlist(name)` returns the FPP playlist dict or None. The name sets
    are the files FPP reports; pass None when a listing could not be fetched
    and that class of check is skipped rather than guessed at.
    """
    problems = []
    seen = set()

    def norm_seq(n):
        n = str(n or "").strip()
        return n[:-5] if n.lower().endswith(".fseq") else n

    def walk(pl_name, top):
        if pl_name in seen:
            return
        seen.add(pl_name)
        data = load_playlist(pl_name)
        if not isinstance(data, dict):
            problems.append(f"Playlist '{pl_name}' could not be found." if top
                            else f"Sub-playlist '{pl_name}' could not be found.")
            return
        items = [
            e for section in ("leadIn", "mainPlaylist", "leadOut")
            for e in (data.get(section) or [])
            if isinstance(e, dict) and e.get("enabled", 1)
        ]
        playable = [e for e in items if e.get("type") not in ("pause", "command", "remap")]
        if top and not playable:
            problems.append(f"Playlist '{pl_name}' is empty — nothing would play.")
        for e in items:
            kind = e.get("type")
            if kind in ("sequence", "both") and sequence_names is not None:
                seq = norm_seq(e.get("sequenceName"))
                if seq and seq not in sequence_names:
                    problems.append(f"Sequence file missing: {seq}")
            if kind in ("media", "both") and media_names is not None:
                media = str(e.get("mediaName") or "").strip()
                if media and media not in media_names:
                    problems.append(f"Audio/media file missing: {media}")
            if kind == "playlist":
                sub = e.get("name") or e.get("playlistName")
                if sub:
                    walk(sub, False)

    walk(name, True)
    # Same file referenced twice is one problem, not two.
    return list(dict.fromkeys(problems))
