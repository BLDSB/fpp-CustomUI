"""Full-controller backup: this UI's data plus FPP's own config and media.

The Settings page's original backup covered only this plugin's database. That
is enough to move scenes and colors between installs, but not to rebuild a
controller: FPP's own settings, channel outputs, pixel overlay models,
schedule and command presets live on FPP's side, and the sequences and audio
are plain files under the media directory.

Three sources are stitched into one zip here:

  * this plugin's database, via the same version-4 payload the old JSON
    backup used (unchanged, so old backups still restore);
  * FPP's native "all" configuration backup, which FPP itself knows how to
    write and read back — see /opt/fpp/www/backup.php. Reimplementing that
    would mean tracking every config file FPP adds, so it is delegated;
  * the media directories, read straight off disk. The service runs as the
    `fpp` user (deploy/fpp-ui.service), so /home/fpp/media is directly
    readable and writable and no HTTP round trip is needed.

The archive is produced as a generator rather than a temp file: a controller
with a full SD card must still be able to back itself up, and a few hundred
megabytes of sequences would otherwise have to be staged on the same disk.
"""
import json
import os
import re
import zipfile
from urllib.parse import quote

import requests
from flask import current_app

from app import db

# Bumped only when the archive layout changes in a way a reader must know
# about. The `ui/backup.json` member carries its own independent version.
ARCHIVE_VERSION = 1

# Media directories worth carrying between controllers. Deliberately excludes
# logs, cache, tmp, backups and plugins: those are either regenerated, huge,
# or (in the case of plugins) managed by FPP's own plugin installer.
MEDIA_SECTIONS = ("sequences", "music", "videos", "images", "effects", "scripts")

# Non-media sections, in the order they appear in the archive.
CORE_SECTIONS = ("ui", "uploads", "controller_config")

ALL_SECTIONS = CORE_SECTIONS + MEDIA_SECTIONS

SECTION_LABELS = {
    "ui": "Scenes, colors, playlists & settings",
    "uploads": "Logo & background images",
    "controller_config": "Controller settings, outputs, models & schedule",
    "sequences": "Sequences",
    "music": "Audio",
    "videos": "Videos",
    "images": "Images",
    "effects": "Effects",
    "scripts": "Scripts",
}

_COPY_CHUNK = 1024 * 1024

# Where the controller's own configuration backup sits inside the archive.
# LEGACY_* are the names used before the sections were renamed; restore still
# reads them so archives downloaded earlier keep working.
CONFIG_MEMBER = "controller/config-backup.json"
CONFIG_NAME_MEMBER = "controller/config-backup-name.txt"
LEGACY_CONFIG_MEMBER = "fpp/config-backup.json"
LEGACY_CONFIG_NAME_MEMBER = "fpp/config-backup-name.txt"


def find_config_member(names):
    """The config-backup member present in an archive, new name or old."""
    for member in (CONFIG_MEMBER, LEGACY_CONFIG_MEMBER):
        if member in names:
            return member
    return None


def find_config_name_member(names):
    for member in (CONFIG_NAME_MEMBER, LEGACY_CONFIG_NAME_MEMBER):
        if member in names:
            return member
    return None


def media_root():
    return current_app.config.get("FPP_MEDIA_ROOT", "/home/fpp/media")


def uploads_dir():
    return os.path.join(current_app.static_folder, "uploads")


def _fpp(path):
    return f"{current_app.config['FPP_BASE_URL']}{path}"


# ── Path safety ───────────────────────────────────────────────────────────────

_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Matches a stored logo/background URL that points at a local upload, whatever
# URL prefix the install was on when it was saved.
_UPLOAD_URL_RE = re.compile(r"/static/uploads/(?P<name>[^/?#]+)$")


def safe_relpath(name):
    """Normalise an archive member name, or return None if it escapes.

    Zip members are attacker-controlled the moment a backup file is emailed
    around, so absolute paths, drive letters and any `..` segment are refused
    outright rather than normalised away.
    """
    name = (name or "").replace("\\", "/").strip()
    if not name or name.startswith("/") or _DRIVE_RE.match(name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


# ── Survey (what is on this box, and how big) ────────────────────────────────

def _dir_stats(path, skip=()):
    count = total = 0
    if os.path.isdir(path):
        for dirpath, _dirnames, filenames in os.walk(path):
            for fname in filenames:
                if fname in skip:
                    continue
                try:
                    total += os.path.getsize(os.path.join(dirpath, fname))
                    count += 1
                except OSError:
                    continue
    return count, total


def survey():
    """Per-section availability and size, for the checkbox list in Settings."""
    from app.routes.settings import build_ui_payload

    out = {}

    try:
        payload = json.dumps(build_ui_payload())
        out["ui"] = {"available": True, "files": 1, "bytes": len(payload.encode())}
    except Exception as exc:  # pragma: no cover - defensive
        current_app.logger.warning("Backup survey: UI payload failed: %s", exc)
        out["ui"] = {"available": False, "files": 0, "bytes": 0}

    # .gitkeep only exists to keep the (otherwise ignored) uploads directory in
    # the repo — it is not branding, and counting it would offer an empty section.
    count, total = _dir_stats(uploads_dir(), skip=(".gitkeep",))
    out["uploads"] = {"available": count > 0, "files": count, "bytes": total}

    # FPP writes a fresh config backup on demand, so its size is not known
    # until one is made. Report reachability and leave the size unknown
    # rather than guessing — it is a few hundred kilobytes either way.
    try:
        resp = requests.get(_fpp("/backups/configuration/list"), timeout=10)
        reachable = resp.ok
    except requests.RequestException:
        reachable = False
    out["controller_config"] = {"available": reachable, "files": 1, "bytes": None}

    root = media_root()
    for name in MEDIA_SECTIONS:
        count, total = _dir_stats(os.path.join(root, name))
        out[name] = {
            "available": os.path.isdir(os.path.join(root, name)),
            "files": count,
            "bytes": total,
        }

    return out


# ── FPP's own configuration backup ───────────────────────────────────────────

def capture_fpp_config():
    """Make FPP write a fresh 'all' config backup and return (name, bytes).

    Raises requests.RequestException / ValueError on failure; callers treat a
    missing FPP config as a warning, not a failed backup.
    """
    # Deliberately no trigger_source: FPP keeps only the newest five backups
    # per source (fpp_backup_max_plugin_backups_key in backup.php), which
    # would quietly prune these. Unsourced backups fall under the normal
    # keep-60 rule instead.
    resp = requests.post(
        _fpp("/backups/configuration"),
        json={"backup_comment": "Full backup from the Custom UI"},
        timeout=180,
    )
    resp.raise_for_status()

    filename = ""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        path = body.get("backup_file_path") or body.get("backup_filename") or ""
        if isinstance(path, str) and path.endswith(".json"):
            filename = os.path.basename(path)

    if not filename:
        # performBackup's reply shape has changed across FPP versions; the
        # listing is the stable fallback.
        listing = requests.get(_fpp("/backups/configuration/list"), timeout=30).json()
        entries = [
            e for e in listing
            if isinstance(e, dict) and e.get("backup_filename")
            and not e.get("backup_alternative_location")
        ]
        if not entries:
            raise ValueError("The controller reported no configuration backups")
        entries.sort(key=lambda e: int(e.get("backup_time_unix") or 0), reverse=True)
        filename = entries[0]["backup_filename"]

    raw = _read_fpp_backup(filename)
    if not _looks_like_fpp_backup(raw):
        raise ValueError(
            f"The controller did not return a usable configuration backup for {filename}"
        )
    return filename, raw


# Areas FPP always writes into a config backup. Used to tell a real backup
# apart from the small JSON error document its download endpoint returns when
# it cannot find the file.
_FPP_BACKUP_MARKERS = ("settings", "show_setup", "channelOutputs", "misc_configs")


def _looks_like_fpp_backup(raw):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and any(k in data for k in _FPP_BACKUP_MARKERS)


def _read_fpp_backup(filename, attempts=5):
    """Fetch a config backup FPP has just written.

    FPP's POST can return before the file it names is readable, and its
    download endpoint answers that gap with a 200 and a "File not found" JSON
    document rather than an error — which would otherwise end up archived as
    if it were the backup. Read the file off disk first (this service runs as
    `fpp`, which owns it), fall back to the API, and retry briefly either way.
    """
    import time

    local = os.path.join(media_root(), "config", "backups", os.path.basename(filename))
    last = b""
    for attempt in range(attempts):
        if attempt:
            time.sleep(0.5 * attempt)
        try:
            with open(local, "rb") as fh:
                last = fh.read()
            if _looks_like_fpp_backup(last):
                return last
        except OSError:
            pass
        try:
            resp = requests.get(
                _fpp(f"/backups/configuration/JsonBackups/{quote(filename)}"), timeout=180
            )
            if resp.ok and _looks_like_fpp_backup(resp.content):
                return resp.content
            last = resp.content or last
        except requests.RequestException as exc:
            current_app.logger.warning("FPP backup download failed: %s", exc)
    return last


def restore_fpp_config(raw, filename=None):
    """Hand a captured FPP config backup back to FPP to restore.

    The file has to exist in FPP's own backup directory before the restore
    endpoint will read it, so it is written there first (as `fpp`, the user
    this service runs as) and then restored by name.
    """
    name = filename or "fpp-CustomUI-restore.json"
    name = os.path.basename(name)
    if not name.endswith(".json"):
        name += ".json"

    backup_dir = os.path.join(media_root(), "config", "backups")
    os.makedirs(backup_dir, exist_ok=True)
    with open(os.path.join(backup_dir, name), "wb") as fh:
        fh.write(raw)

    resp = requests.post(
        _fpp(f"/backups/configuration/restore/JsonBackups/{quote(name)}"),
        data="all",
        timeout=300,
    )
    resp.raise_for_status()
    try:
        result = resp.json()
    except ValueError:
        result = {}

    # FPP answers {"Success": "Ok"|"Failed"|"Error", "Message": ...}
    status = str(result.get("Success", "")).lower()
    if status and status not in ("ok", "true", "success", "1"):
        raise ValueError(result.get("Message") or "The controller rejected the configuration restore")

    try:
        requests.get(_fpp("/schedule/reload"), timeout=30)
    except requests.RequestException as exc:
        current_app.logger.warning("Schedule reload after restore failed: %s", exc)

    current_app.logger.info("FPP restore result: %s", json.dumps(result)[:4000])
    summary = _summarise_fpp_restore(result.get("Message"))

    overlay_note = _restore_overlay_models(raw)
    if overlay_note:
        summary += "; " + overlay_note
    return summary


def _restore_overlay_models(raw):
    """Write back the pixel overlay models FPP's own restore leaves behind.

    FPP 9.5 backs the models up (they are in the archive as `model-overlays`
    and report VALID_DATA on restore) but has no branch that writes them back
    — only the legacy channelmemorymaps path does, and modern backups do not
    contain that key. Everything this UI paints lives on those models, so a
    restored controller with no Zone 1-15 would come back dark. Written here
    instead, matching the format create_overlay_models uses.

    Returns a note for the restore log, or "" if there was nothing to do.
    """
    from app.routes.settings import OVERLAY_CONFIG_PATH

    try:
        overlays = json.loads(raw).get("model-overlays")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(overlays, dict) or not overlays.get("models"):
        return ""

    tmp_path = OVERLAY_CONFIG_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(OVERLAY_CONFIG_PATH), exist_ok=True)
        with open(tmp_path, "w") as fh:
            json.dump(overlays, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, OVERLAY_CONFIG_PATH)
    except OSError as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        current_app.logger.error("Could not restore model-overlays.json: %s", exc)
        return "pixel overlay models could not be written"

    # fppd only reads model-overlays.json at startup.
    import subprocess
    try:
        subprocess.run(["sudo", "systemctl", "restart", "fppd"], timeout=30, check=True)
    except Exception as exc:
        current_app.logger.warning("Could not restart fppd after restore: %s", exc)
        return (f"{len(overlays['models'])} pixel overlay models restored "
                "(restart the controller to load them)")

    return f"{len(overlays['models'])} pixel overlay models restored"


def _any_success(node):
    """True if anywhere in FPP's nested result tree something was applied."""
    if isinstance(node, dict):
        if node.get("SUCCESS") is True:
            return True
        return any(_any_success(v) for v in node.values())
    return False


def _summarise_fpp_restore(message):
    """Turn FPP's deeply nested restore report into one readable line.

    An area reporting SUCCESS false usually means there was nothing of that
    kind configured (no DMX inputs, no email), not a failure — so the summary
    names what was applied rather than counting errors. The full report goes
    to the log for anyone who needs it.
    """
    if not isinstance(message, dict):
        return str(message) if message else "Controller configuration restored"
    applied = sorted(area for area, detail in message.items() if _any_success(detail))
    if not applied:
        return "Controller configuration restored, but no areas reported changes"
    return "Controller configuration restored: " + ", ".join(applied)


# ── Streaming zip writer ─────────────────────────────────────────────────────

class _Sink:
    """Write-only file object that buffers what zipfile emits.

    Deliberately has no `seek`: ZipFile then marks the stream unseekable and
    writes data descriptors after each member instead of rewinding to patch
    its header, which is what makes streaming possible at all.
    """

    def __init__(self):
        self._chunks = []
        self._pos = 0

    def write(self, data):
        self._chunks.append(bytes(data))
        self._pos += len(data)
        return len(data)

    def tell(self):
        return self._pos

    def flush(self):
        pass

    def drain(self):
        chunks, self._chunks = self._chunks, []
        return b"".join(chunks)


def _iter_files(root):
    """Yield (absolute path, path relative to root) in a stable order."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fname in sorted(filenames):
            full = os.path.join(dirpath, fname)
            if not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            yield full, rel


def iter_archive(sections, identity=None):
    """Generate the backup zip a chunk at a time.

    `sections` is the set of section names to include; `identity` is the
    secrets block for the manifest (may be None).
    """
    from app.routes.settings import build_ui_payload

    sink = _Sink()
    warnings = []

    def add_bytes(name, raw, compress=zipfile.ZIP_DEFLATED):
        info = zipfile.ZipInfo(name)
        info.compress_type = compress
        info.external_attr = 0o644 << 16
        zf.writestr(info, raw)

    zf = zipfile.ZipFile(sink, "w", allowZip64=True)
    try:
        if "ui" in sections:
            add_bytes("ui/backup.json", json.dumps(build_ui_payload(), indent=2).encode())
            yield sink.drain()

        if "uploads" in sections:
            root = uploads_dir()
            if os.path.isdir(root):
                for full, rel in _iter_files(root):
                    if rel == ".gitkeep":
                        continue
                    for chunk in _add_file(zf, sink, full, f"ui/uploads/{rel}"):
                        yield chunk

        if "controller_config" in sections:
            try:
                fpp_name, raw = capture_fpp_config()
                add_bytes(CONFIG_MEMBER, raw)
                add_bytes(CONFIG_NAME_MEMBER, fpp_name.encode())
            except Exception as exc:
                # A controller with fppd down must still get its own data out.
                current_app.logger.warning("Could not capture FPP config backup: %s", exc)
                warnings.append(f"Controller configuration could not be captured: {exc}")
            yield sink.drain()

        root = media_root()
        for section in MEDIA_SECTIONS:
            if section not in sections:
                continue
            src = os.path.join(root, section)
            if not os.path.isdir(src):
                continue
            for full, rel in _iter_files(src):
                for chunk in _add_file(zf, sink, full, f"media/{section}/{rel}",
                                       compress=zipfile.ZIP_STORED):
                    yield chunk

        add_bytes("manifest.json", json.dumps(
            build_manifest(sections, identity, warnings), indent=2
        ).encode())
    finally:
        zf.close()

    tail = sink.drain()
    if tail:
        yield tail


def _add_file(zf, sink, path, arcname, compress=zipfile.ZIP_DEFLATED):
    """Stream one file into the archive, yielding output as it accumulates."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        current_app.logger.warning("Backup: skipping %s (%s)", path, exc)
        return

    info = zipfile.ZipInfo(arcname)
    info.compress_type = compress
    info.external_attr = 0o644 << 16
    try:
        with open(path, "rb") as src, zf.open(info, "w", force_zip64=size >= 2 ** 31) as dest:
            while True:
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                dest.write(chunk)
                out = sink.drain()
                if out:
                    yield out
    except OSError as exc:
        # The member is already open in the archive at this point; a truncated
        # entry is better than a failed backup, so log and carry on.
        current_app.logger.warning("Backup: read error on %s (%s)", path, exc)
    out = sink.drain()
    if out:
        yield out


def build_manifest(sections, identity=None, warnings=None):
    from app import ui_path as ui_path_mod

    fpp_version = ""
    hostname = ""
    try:
        info = requests.get(_fpp("/system/info"), timeout=5).json()
        fpp_version = str(info.get("Version") or "")
        hostname = str(info.get("HostName") or "")
    except (requests.RequestException, ValueError):
        pass

    import datetime
    return {
        "archive_version": ARCHIVE_VERSION,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "source_host": hostname,
        "fpp_version": fpp_version,
        "ui_path": ui_path_mod.current_path(),
        "sections": sorted(sections),
        "identity": identity or {},
        "warnings": warnings or [],
    }


def archive_filename(hostname=""):
    import datetime
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    host = re.sub(r"[^A-Za-z0-9_-]", "", hostname or "") or "fpp"
    return f"full-backup-{host}-{stamp}.zip"


# ── Restore helpers ──────────────────────────────────────────────────────────

def extract_media(zf, log):
    """Merge the archive's media files into the media directory."""
    root = media_root()
    written = 0
    skipped = 0
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith("media/"):
            continue
        rel = safe_relpath(info.filename[len("media/"):])
        if not rel:
            skipped += 1
            continue
        section = rel.split("/")[0]
        if section not in MEDIA_SECTIONS:
            skipped += 1
            continue
        dest = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
        written += 1
    if written or skipped:
        msg = f"Restored {written} media file(s)"
        if skipped:
            msg += f" ({skipped} skipped as unsafe or unknown)"
        log.append(("media", "ok", msg))
    return written


def extract_uploads(zf, log):
    """Restore the uploaded logo/background images."""
    from app.routes.settings import _ALLOWED_IMAGE_EXTS

    dest_dir = uploads_dir()
    os.makedirs(dest_dir, exist_ok=True)
    written = 0
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith("ui/uploads/"):
            continue
        rel = safe_relpath(info.filename[len("ui/uploads/"):])
        if not rel or "/" in rel:
            continue
        # This directory is served by Flask's static handler, so hold a
        # restored backup to the same file types the upload form accepts.
        if os.path.splitext(rel)[1].lower() not in _ALLOWED_IMAGE_EXTS:
            current_app.logger.warning("Restore: skipping non-image upload %r", rel)
            continue
        with zf.open(info) as src, open(os.path.join(dest_dir, rel), "wb") as out:
            out.write(src.read())
        written += 1
    if written:
        log.append(("uploads", "ok", f"Restored {written} branding image(s)"))
    return written


def check_branding_images(log):
    """Flag settings that point at branding images the archive did not carry.

    The logo and background are stored in the database as URLs but live on
    disk under app/static/uploads, which is gitignored — so a plugin
    reinstall wipes the files and leaves the URLs behind. A backup taken in
    that state restores dead links, which shows up as broken images with no
    explanation. Say it plainly in the restore log instead.
    """
    from app.models import AppSetting

    missing = []
    for key, label in (("logo_url", "logo"), ("bg_image_url", "background")):
        setting = db.session.get(AppSetting, key)
        match = _UPLOAD_URL_RE.search(setting.value) if setting and setting.value else None
        if match and not os.path.exists(os.path.join(uploads_dir(), match.group("name"))):
            missing.append(label)

    if missing:
        log.append(("uploads", "warning",
                    f"The {' and '.join(missing)} image is set but not in this backup — "
                    "re-upload it on the Settings page."))
    return missing
