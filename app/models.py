import json
import logging

from app import db

_logger = logging.getLogger(__name__)


def _loads_list(raw):
    """Parse a JSON list column, returning [] on corrupt or non-list data."""
    try:
        val = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return val if isinstance(val, list) else []

OVERLAY_MODELS = {"All"} | {f"Zone {i}" for i in range(1, 16)}


class SavedColor(db.Model):
    __tablename__ = "saved_colors"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    hex_value = db.Column(db.String(7), nullable=False)  # e.g. "#FF5733"

    def to_dict(self):
        return {"id": self.id, "name": self.name, "hex_value": self.hex_value}


class ColorButton(db.Model):
    __tablename__ = "color_buttons"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(64), nullable=False)
    saved_color_id = db.Column(
        db.Integer, db.ForeignKey("saved_colors.id"), nullable=False
    )

    def to_dict(self):
        return {"id": self.id, "label": self.label}


class AppSetting(db.Model):
    """Key-value store for UI configuration (logo, background image, site name)."""
    __tablename__ = "app_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=True)


class Zone(db.Model):
    """Pixel Overlay zone configuration. Slot 0 = 'All', slots 1-15 = 'Zone 1'-'Zone 15'."""
    __tablename__ = "zones"

    slot = db.Column(db.Integer, primary_key=True)
    display_name = db.Column(db.String(64), nullable=False)
    hidden = db.Column(db.Boolean, nullable=False, default=False)

    @property
    def fpp_model_name(self):
        return "All" if self.slot == 0 else f"Zone {self.slot}"

    def to_dict(self):
        return {
            "slot": self.slot,
            "fpp_model_name": self.fpp_model_name,
            "display_name": self.display_name,
            "hidden": self.hidden,
        }


class ZoneLayout(db.Model):
    """Matrix geometry for a zone's FPP pixel overlay model.

    Kept in its own table rather than as columns on Zone: the app only ever calls
    db.create_all(), which adds missing tables but never missing columns, so new
    columns would silently not exist on already-deployed controllers.

    A zone with no row here keeps FPP's default rectangular handling.
    """
    __tablename__ = "zone_layouts"

    slot = db.Column(db.Integer, primary_key=True)
    source_name = db.Column(db.String(64), nullable=True)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    node_count = db.Column(db.Integer, nullable=False)
    start_channel = db.Column(db.Integer, nullable=False)
    channel_count = db.Column(db.Integer, nullable=False)
    channels_per_node = db.Column(db.Integer, nullable=False, default=3)
    data = db.Column(db.Text, nullable=False)
    imported_at = db.Column(db.String(32), nullable=True)

    @property
    def fpp_model_name(self):
        return "All" if self.slot == 0 else f"Zone {self.slot}"

    def to_grid(self):
        """The dict shape app.overlay_layout produces and consumes."""
        return {
            "width": self.width,
            "height": self.height,
            "node_count": self.node_count,
            "placed": self.node_count,
            "collisions": 0,
            "start_channel": self.start_channel,
            "channel_count": self.channel_count,
            "channels_per_node": self.channels_per_node,
            "data": self.data,
        }

    def to_dict(self, include_data=False):
        out = {
            "slot": self.slot,
            "fpp_model_name": self.fpp_model_name,
            "source_name": self.source_name,
            "width": self.width,
            "height": self.height,
            "node_count": self.node_count,
            "start_channel": self.start_channel,
            "channel_count": self.channel_count,
            "channels_per_node": self.channels_per_node,
            "imported_at": self.imported_at,
        }
        if include_data:
            out["data"] = self.data
        return out


class Scene(db.Model):
    __tablename__ = "scenes"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, unique=True)
    zones = db.relationship("SceneZone", backref="scene", lazy=True, cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "zones": [z.to_dict() for z in self.zones],
        }


class SceneZone(db.Model):
    __tablename__ = "scene_zones"

    id = db.Column(db.Integer, primary_key=True)
    scene_id = db.Column(db.Integer, db.ForeignKey("scenes.id"), nullable=False)
    fpp_model = db.Column(db.String(32), nullable=False)
    hex_color = db.Column(db.String(7), nullable=False)

    def to_dict(self):
        return {"fpp_model": self.fpp_model, "hex_color": self.hex_color}



class EffectPreset(db.Model):
    __tablename__ = "effect_presets"

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(64), nullable=False)
    effect_name = db.Column(db.String(128), nullable=False)
    models_json = db.Column(db.Text, nullable=False, default="[]")
    args_json   = db.Column(db.Text, nullable=False, default="[]")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            # FPP playlist auto-created for this preset (see app/routes/effects.py)
            "playlist": f"Effect - {self.name}",
            "effect_name": self.effect_name,
            "models": _loads_list(self.models_json),
            "args": _loads_list(self.args_json),
        }


class CustomPlaylist(db.Model):
    """A user-built playlist: an ordered mix of scenes, effects, sequences and pauses.

    Kept here as well as on FPP because FPP's copy is derived output — it gets
    rewritten from these rows on every save, on restore, and on startup.
    """
    __tablename__ = "custom_playlists"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, unique=True)
    repeat = db.Column(db.Boolean, nullable=False, default=True)
    # Shuffled by us, not by FPP: FPP's own "random" shuffles playlist entries
    # individually, which would separate each scene's URL command from the pause
    # that holds it. See _playlist_entries in app/routes/custom_playlists.py.
    random = db.Column(db.Boolean, nullable=False, default=False)
    items = db.relationship(
        "CustomPlaylistItem",
        backref="playlist",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="CustomPlaylistItem.position",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "repeat": self.repeat,
            "random": self.random,
            "items": [i.to_dict() for i in sorted(self.items, key=lambda i: i.position)],
        }


class CustomPlaylistItem(db.Model):
    __tablename__ = "custom_playlist_items"

    ITEM_TYPES = ("scene", "effect", "sequence", "pause")

    id = db.Column(db.Integer, primary_key=True)
    playlist_id = db.Column(
        db.Integer, db.ForeignKey("custom_playlists.id"), nullable=False
    )
    position = db.Column(db.Integer, nullable=False, default=0)
    item_type = db.Column(db.String(16), nullable=False)
    ref_id = db.Column(db.Integer, nullable=True)      # Scene / EffectPreset id
    ref_name = db.Column(db.String(255), nullable=True)  # .fseq filename
    duration = db.Column(db.Integer, nullable=False, default=30)

    def resolve(self):
        """Display label plus whether the thing this points at still exists.

        A scene or preset can be deleted out from under a playlist, so this
        never raises — the builder shows a broken row instead of erroring.
        """
        if self.item_type == "scene":
            row = db.session.get(Scene, self.ref_id) if self.ref_id else None
            return (row.name, False) if row else (f"Scene #{self.ref_id}", True)
        if self.item_type == "effect":
            row = db.session.get(EffectPreset, self.ref_id) if self.ref_id else None
            return (row.name, False) if row else (f"Effect #{self.ref_id}", True)
        if self.item_type == "sequence":
            return (self.ref_name or "", False)
        return ("Pause", False)

    def to_dict(self):
        label, missing = self.resolve()
        return {
            "id": self.id,
            "position": self.position,
            "item_type": self.item_type,
            "ref_id": self.ref_id,
            "ref_name": self.ref_name,
            "duration": self.duration,
            "label": label,
            "missing": missing,
        }


def get_all_zones():
    """Return all 16 zones in slot order, seeding defaults on first call."""
    existing = {z.slot: z for z in Zone.query.all()}
    zones = []
    needs_commit = False
    for slot in range(16):
        if slot not in existing:
            name = "All" if slot == 0 else f"Zone {slot}"
            z = Zone(slot=slot, display_name=name, hidden=False)
            db.session.add(z)
            zones.append(z)
            needs_commit = True
        else:
            zones.append(existing[slot])
    if needs_commit:
        try:
            db.session.commit()
        except Exception as exc:
            # Two request threads can race to seed the same slots (PK collision),
            # or the DB may be momentarily unwritable. Roll back and serve
            # whatever is queryable; missing slots get transient defaults so the
            # page still renders.
            db.session.rollback()
            _logger.warning("Zone seed commit failed (likely concurrent seed): %s", exc)
            existing = {z.slot: z for z in Zone.query.all()}
            zones = []
            for slot in range(16):
                z = existing.get(slot)
                if z is None:
                    name = "All" if slot == 0 else f"Zone {slot}"
                    z = Zone(slot=slot, display_name=name, hidden=False)
                zones.append(z)
    return zones
