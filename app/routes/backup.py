"""Full-controller backup and restore endpoints.

The original single-JSON backup lives on in settings.py (`/api/backup` and
`/api/restore`) so existing backup files keep working. This blueprint adds the
archive that can rebuild a whole controller — see app/fpp_backup.py for what
goes into it and why.

Uploads are chunked rather than sent as one request. waitress caps a single
request body at 1 GB and spools it to disk before Flask sees a byte of it, so
a large archive posted in one piece would need the whole thing free on the SD
card twice over. Fixed-size chunks appended to a temp file avoid both, and
give the browser a real progress bar for free.
"""
import json
import os
import re
import shutil
import time
import zipfile

from flask import Blueprint, Response, current_app, jsonify, request

from app import fpp_backup
from app.auth_utils import login_required

backup_bp = Blueprint("backup", __name__)

# Chunks stay under the app-wide 8 MB MAX_CONTENT_LENGTH; the browser sends
# 4 MB at a time (see settings.html).
MAX_CHUNK = 6 * 1024 * 1024

# Refuse an upload that could not be extracted afterwards. The archive itself
# plus the files unpacked out of it need roughly twice its size free.
SPACE_FACTOR = 2.5

_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
_STALE_AFTER = 3600  # abandoned uploads are swept after an hour


def _tmp_dir():
    """Where partial uploads are staged.

    Prefers FPP's own media/tmp so the archive lands on the media partition
    (the same disk its contents will be written to, so the free-space check
    means something). Falls back to the system temp dir off-controller.
    """
    root = fpp_backup.media_root()
    candidate = os.path.join(root, "tmp")
    if os.path.isdir(root):
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except OSError:
            pass
    import tempfile
    return tempfile.gettempdir()


def _upload_path(upload_id):
    return os.path.join(_tmp_dir(), f"fpp-ui-restore-{upload_id}.zip")


def _sweep_stale():
    now = time.time()
    try:
        entries = os.listdir(_tmp_dir())
    except OSError:
        return
    for name in entries:
        if not name.startswith("fpp-ui-restore-"):
            continue
        path = os.path.join(_tmp_dir(), name)
        try:
            if now - os.path.getmtime(path) > _STALE_AFTER:
                os.remove(path)
        except OSError:
            continue


# ── Backup ───────────────────────────────────────────────────────────────────

@backup_bp.get("/api/backup/survey")
@login_required
def backup_survey():
    """What can be backed up on this controller, and how big each part is."""
    return jsonify({
        "sections": fpp_backup.survey(),
        "labels": fpp_backup.SECTION_LABELS,
        "order": list(fpp_backup.ALL_SECTIONS),
    })


@backup_bp.get("/api/backup/full")
@login_required
def backup_full():
    """Stream the full backup archive."""
    requested = (request.args.get("include") or "").split(",")
    sections = {s.strip() for s in requested if s.strip() in fpp_backup.ALL_SECTIONS}
    if not sections:
        sections = set(fpp_backup.ALL_SECTIONS)

    identity = {}
    if "ui" in sections:
        # Carried so a restored controller keeps its own PIN, and so the
        # internal token still matches the /internal/ URLs baked into the
        # playlists FPP hands back on restore.
        for key in ("ADMIN_PASSWORD_HASH", "MASTER_PIN_HASH", "INTERNAL_TOKEN"):
            value = current_app.config.get(key) or ""
            if value:
                identity[key] = value

    manifest = fpp_backup.build_manifest(sections)
    filename = fpp_backup.archive_filename(manifest.get("source_host"))

    # The generator runs outside the request context, so anything that needs
    # current_app has to be bound now.
    app = current_app._get_current_object()

    def generate():
        with app.app_context():
            for chunk in fpp_backup.iter_archive(sections, identity):
                if chunk:
                    yield chunk

    return Response(
        generate(),
        mimetype="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-store",
        },
    )


# ── Restore: chunked upload ──────────────────────────────────────────────────

@backup_bp.post("/api/restore/chunk")
@login_required
def restore_chunk():
    upload_id = (request.args.get("id") or "").lower()
    if not _UPLOAD_ID_RE.match(upload_id):
        return jsonify({"error": "Invalid upload id"}), 400

    try:
        offset = int(request.args.get("offset") or 0)
        total = int(request.args.get("total") or 0)
    except ValueError:
        return jsonify({"error": "Invalid offset"}), 400

    data = request.get_data(cache=False)
    if len(data) > MAX_CHUNK:
        return jsonify({"error": "Chunk too large"}), 413

    path = _upload_path(upload_id)

    if offset == 0:
        _sweep_stale()
        try:
            free = shutil.disk_usage(_tmp_dir()).free
        except OSError:
            free = None
        if free is not None and total and free < total * SPACE_FACTOR:
            need = total * SPACE_FACTOR / (1024 ** 3)
            return jsonify({
                "error": f"Not enough free space to restore — about "
                         f"{need:.1f} GB is needed and {free / 1024 ** 3:.1f} GB is free."
            }), 507
        try:
            os.remove(path)
        except OSError:
            pass

    current = os.path.getsize(path) if os.path.exists(path) else 0
    if offset != current:
        # A retried or out-of-order chunk would silently corrupt the archive.
        return jsonify({"error": "Upload out of sync — start the restore again",
                        "expected_offset": current}), 409

    try:
        with open(path, "ab") as fh:
            fh.write(data)
    except OSError as exc:
        return jsonify({"error": f"Could not stage the upload: {exc}"}), 500

    return jsonify({"ok": True, "received": current + len(data)})


@backup_bp.post("/api/restore/apply")
@login_required
def restore_apply():
    upload_id = ((request.get_json(silent=True) or {}).get("id") or "").lower()
    if not _UPLOAD_ID_RE.match(upload_id):
        return jsonify({"error": "Invalid upload id"}), 400

    path = _upload_path(upload_id)
    if not os.path.exists(path):
        return jsonify({"error": "Upload not found — it may have expired"}), 404

    # Default on: restoring FPP's configuration leaves FPP showing a
    # "reboot required" banner, which is what the operator would do next anyway.
    reboot = (request.get_json(silent=True) or {}).get("reboot", True)

    try:
        return _apply_archive(path, reboot=bool(reboot))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def apply_archive_bytes(raw):
    """Restore an archive already held in memory.

    Used by the legacy /api/restore endpoint when a zip is dropped on it
    instead of a JSON backup — small archives only, since the whole body is
    buffered by then anyway.
    """
    path = os.path.join(_tmp_dir(), f"fpp-ui-restore-{os.urandom(8).hex()}.zip")
    try:
        with open(path, "wb") as fh:
            fh.write(raw)
        return _apply_archive(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ── Restore: the actual work ─────────────────────────────────────────────────

def _apply_archive(path, reboot=False):
    """Unpack and apply a full backup archive.

    Order matters. Media first (nothing else depends on it), then FPP's own
    configuration — which replaces the playlists directory wholesale — and
    only then this plugin's database, whose restore regenerates the scene,
    effect and custom playlists on top of what FPP just laid down. Identity
    is last because changing the URL path moves the page the operator is on.

    Every step is caught on its own: a controller with fppd down should still
    get its scenes and sequences back.
    """
    log = []

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return jsonify({"error": "That file is not a valid backup archive"}), 400

    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError:
            return jsonify({
                "error": "This zip has no manifest.json — it is not a Custom UI backup"
            }), 400
        except (ValueError, zipfile.BadZipFile) as exc:
            return jsonify({"error": f"Could not read the archive manifest: {exc}"}), 400

        if manifest.get("archive_version", 0) > fpp_backup.ARCHIVE_VERSION:
            return jsonify({
                "error": "This backup was made by a newer version of the UI — "
                         "update this controller before restoring it."
            }), 400

        # 1. Media
        try:
            fpp_backup.extract_media(zf, log)
        except Exception as exc:
            current_app.logger.exception("Media restore failed")
            log.append(("media", "error", f"Media files failed: {exc}"))

        # 2. FPP's own configuration
        names = zf.namelist()
        config_member = fpp_backup.find_config_member(names)
        if config_member:
            try:
                name = ""
                name_member = fpp_backup.find_config_name_member(names)
                if name_member:
                    name = zf.read(name_member).decode(errors="ignore").strip()
                message = fpp_backup.restore_fpp_config(zf.read(config_member), name)
                log.append(("controller_config", "ok", message))
            except Exception as exc:
                current_app.logger.exception("Controller config restore failed")
                log.append(("controller_config", "error", f"Controller configuration failed: {exc}"))
        else:
            log.append(("controller_config", "skipped", "No controller configuration in this backup"))

        # 3. This plugin's database
        if "ui/backup.json" in zf.namelist():
            from app.routes.settings import apply_ui_backup
            try:
                error = apply_ui_backup(json.loads(zf.read("ui/backup.json")))
                if error:
                    log.append(("ui", "error", error))
                else:
                    log.append(("ui", "ok", "Scenes, colors, playlists and settings restored"))
            except Exception as exc:
                current_app.logger.exception("UI restore failed")
                log.append(("ui", "error", f"UI data failed: {exc}"))
        else:
            log.append(("ui", "skipped", "No UI data in this backup"))

        # 4. Branding images
        try:
            fpp_backup.extract_uploads(zf, log)
            fpp_backup.check_branding_images(log)
        except Exception as exc:
            log.append(("uploads", "error", f"Branding images failed: {exc}"))

    # 5. Identity — PIN, recovery PIN and the internal token
    identity = manifest.get("identity") or {}
    restored_keys = []
    for key in ("ADMIN_PASSWORD_HASH", "MASTER_PIN_HASH", "INTERNAL_TOKEN"):
        value = identity.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        from app.routes.auth import write_env_key
        error = write_env_key(key, value.strip())
        if error:
            log.append(("identity", "error", f"{key}: {error}"))
        else:
            restored_keys.append(key)
    if restored_keys:
        log.append(("identity", "ok", "Login PIN and internal token restored from the backup"))

    # The token is baked into the FPP playlist URLs that step 3 just wrote, so
    # those have to be regenerated against the restored value.
    if "INTERNAL_TOKEN" in restored_keys:
        try:
            from app import regenerate_all_playlists
            regenerate_all_playlists(current_app)
            log.append(("playlists", "ok", "Playlists rewritten with the restored token"))
        except Exception as exc:
            log.append(("playlists", "error", f"Could not rewrite playlists: {exc}"))

    # 6. URL path last — this moves the page the operator is looking at
    new_url = None
    wanted_path = (manifest.get("ui_path") or "").strip()
    if wanted_path:
        from app import ui_path as ui_path_mod
        if wanted_path != ui_path_mod.current_path():
            error = ui_path_mod.apply(wanted_path)
            if error:
                log.append(("ui_path", "error", f"Could not move the UI to /{wanted_path}: {error}"))
            else:
                new_url = f"/{wanted_path}/"
                log.append(("ui_path", "ok", f"UI moved back to /{wanted_path}"))

    failed = [entry for entry in log if entry[1] == "error"]
    reboot_recommended = any(s == "controller_config" and st == "ok" for s, st, _m in log)

    # 7. Reboot, if asked and if FPP actually took a new configuration. FPP
    # raises its own "reboot required" banner after a config restore, and
    # several of the areas it restores (channel outputs, the models written
    # above) only take effect on a restart.
    rebooting = False
    if reboot and reboot_recommended:
        from app.routes.settings import trigger_reboot
        # Long enough for this response to reach the browser and be rendered
        # before systemd starts tearing the service down.
        error = trigger_reboot(delay=5, reason="a backup restore")
        if error:
            log.append(("reboot", "warning", f"Could not reboot automatically: {error}"))
        else:
            rebooting = True
            log.append(("reboot", "ok", "Rebooting the controller to finish applying the controller settings"))

    return jsonify({
        "ok": not failed,
        "log": [{"step": s, "status": st, "message": m} for s, st, m in log],
        "url": new_url,
        "reboot_recommended": reboot_recommended,
        "rebooting": rebooting,
    })
