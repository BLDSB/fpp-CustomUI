import json
import logging
import re

from app import db

_logger = logging.getLogger(__name__)


def _loads_list(raw):
    """Parse a JSON list column, returning [] on corrupt or non-list data."""
    try:
        val = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return val if isinstance(val, list) else []

MAX_ZONES = 15
OVERLAY_MODELS = {"All"} | {f"Zone {i}" for i in range(1, MAX_ZONES + 1)}

# "Zone 3.2": the second xLights model grouped into Zone 3 (see ZoneMember).
_MEMBER_NAME_RE = re.compile(r"^Zone (\d+)\.(\d+)$")

# Channels per node that mean "RGB plus a white channel" (RGBW).
WHITE_CHANNELS_PER_NODE = 4


def clamp_white(value):
    """A white level as an int 0-255, or None for "not given" / not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, min(255, int(value)))
    except (TypeError, ValueError):
        return None


class SavedColor(db.Model):
    __tablename__ = "saved_colors"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    hex_value = db.Column(db.String(7), nullable=False)  # e.g. "#FF5733"
    # 0-255 white level, or NULL for a color saved before white existed.
    # Added by _add_missing_columns (create_all never adds columns).
    white = db.Column(db.Integer, nullable=True)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "hex_value": self.hex_value, "white": self.white}


class ColorButton(db.Model):
    __tablename__ = "color_buttons"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(64), nullable=False)
    saved_color_id = db.Column(
        db.Integer, db.ForeignKey("saved_colors.id"), nullable=False, index=True
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


class ZoneMember(db.Model):
    """One xLights model inside a grouped zone.

    A zone holding several models has one row here per model instead of a
    ZoneLayout row. Each member becomes its own FPP overlay model named
    "Zone <slot>.<position>"; the UI still shows one zone, and colors, scenes
    and effects fan out to the members (see expand_overlay_models). A zone with
    a single model keeps using ZoneLayout, so ungrouped setups are unchanged.

    Its own table for the same reason as ZoneLayout: create_all() never adds
    columns to an existing table.
    """
    __tablename__ = "zone_members"

    id = db.Column(db.Integer, primary_key=True)
    slot = db.Column(db.Integer, nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False)  # 1-based within the zone
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
        return f"Zone {self.slot}.{self.position}"

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
            "position": self.position,
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
    zones = db.relationship("SceneZone", backref="scene", lazy="selectin", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "zones": [z.to_dict() for z in self.zones],
        }


class SceneZone(db.Model):
    __tablename__ = "scene_zones"

    id = db.Column(db.Integer, primary_key=True)
    scene_id = db.Column(db.Integer, db.ForeignKey("scenes.id"), nullable=False, index=True)
    fpp_model = db.Column(db.String(32), nullable=False)
    hex_color = db.Column(db.String(7), nullable=False)
    # 0-255 white level, or NULL for a zone saved before white existed (those
    # leave the white channel alone, exactly as they always did).
    white = db.Column(db.Integer, nullable=True)

    def to_dict(self):
        return {"fpp_model": self.fpp_model, "hex_color": self.hex_color, "white": self.white}



class Holiday(db.Model):
    """Named yearly date range (month/day only) that schedule entries can link to."""
    __tablename__ = "holidays"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, unique=True)
    start_month = db.Column(db.Integer, nullable=False)
    start_day = db.Column(db.Integer, nullable=False)
    end_month = db.Column(db.Integer, nullable=False)
    end_day = db.Column(db.Integer, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "start_month": self.start_month,
            "start_day": self.start_day,
            "end_month": self.end_month,
            "end_day": self.end_day,
        }


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
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="CustomPlaylistItem.position",
    )

    def to_dict(self, lookups=None):
        return {
            "id": self.id,
            "name": self.name,
            "repeat": self.repeat,
            "random": self.random,
            "items": [i.to_dict(lookups) for i in sorted(self.items, key=lambda i: i.position)],
        }


class CustomPlaylistItem(db.Model):
    __tablename__ = "custom_playlist_items"

    ITEM_TYPES = ("scene", "effect", "sequence", "pause")

    id = db.Column(db.Integer, primary_key=True)
    playlist_id = db.Column(
        db.Integer, db.ForeignKey("custom_playlists.id"), nullable=False, index=True
    )
    position = db.Column(db.Integer, nullable=False, default=0)
    item_type = db.Column(db.String(16), nullable=False)
    ref_id = db.Column(db.Integer, nullable=True)      # Scene / EffectPreset id
    ref_name = db.Column(db.String(255), nullable=True)  # .fseq filename
    duration = db.Column(db.Integer, nullable=False, default=30)

    def resolve(self, lookups=None):
        """Display label plus whether the thing this points at still exists.

        A scene or preset can be deleted out from under a playlist, so this
        never raises — the builder shows a broken row instead of erroring.
        ``lookups`` is the result of ``ref_name_lookups()``; pass it when
        serialising many items so each one is a dict hit rather than a query.
        """
        if self.item_type in ("scene", "effect"):
            kind = "Scene" if self.item_type == "scene" else "Effect"
            if lookups is not None:
                name = lookups[self.item_type].get(self.ref_id)
            else:
                model = Scene if self.item_type == "scene" else EffectPreset
                row = db.session.get(model, self.ref_id) if self.ref_id else None
                name = row.name if row else None
            return (name, False) if name else (f"{kind} #{self.ref_id}", True)
        if self.item_type == "sequence":
            return (self.ref_name or "", False)
        return ("Pause", False)

    def to_dict(self, lookups=None):
        label, missing = self.resolve(lookups)
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


def ref_name_lookups():
    """``{"scene": {id: name}, "effect": {id: name}}`` in two column-only queries.

    Lets a list of playlists resolve every item's label without a query per item.
    """
    return {
        "scene": dict(db.session.query(Scene.id, Scene.name).all()),
        "effect": dict(db.session.query(EffectPreset.id, EffectPreset.name).all()),
    }


def is_managed_overlay(name):
    """True for an overlay model name this app owns: All, Zone N, or Zone N.M."""
    if name in OVERLAY_MODELS:
        return True
    m = _MEMBER_NAME_RE.match(name or "")
    return bool(m) and 1 <= int(m.group(1)) <= MAX_ZONES


def _rgbw_rows():
    """Every stored layout row (zone or group member) with a white channel."""
    return [
        *ZoneLayout.query.filter_by(channels_per_node=WHITE_CHANNELS_PER_NODE),
        *ZoneMember.query.filter_by(channels_per_node=WHITE_CHANNELS_PER_NODE),
    ]


def rgbw_models():
    """Overlay models whose nodes have a white channel (4 channels per node).

    Only imported layouts say how many channels a node has, so a zone that was
    never imported has no white channel as far as the app can tell.
    """
    return {row.fpp_model_name for row in _rgbw_rows()}


def white_channels(models):
    """``{overlay model: [FPP channel numbers]}`` of the white channel of each RGBW model.

    Channels are 1-based, as FPP's channel-range overlay takes them.  Node ``n``
    of a model starting at channel ``s`` occupies ``s + 4(n-1)`` to ``s + 4n - 1``
    and its white is the last of those.  Models without a white channel are left
    out, so an RGB-only zone simply has nothing to drive.
    """
    wanted = set(models)
    out = {}
    for row in _rgbw_rows():
        if row.fpp_model_name not in wanted:
            continue
        nodes = sorted({int(cell) for line in row.data.split(";")
                        for cell in line.split(",") if cell})
        out[row.fpp_model_name] = [
            row.start_channel + (n - 1) * WHITE_CHANNELS_PER_NODE + WHITE_CHANNELS_PER_NODE - 1
            for n in nodes
        ]
    return out


def has_white_channel(model_name):
    """True when sending a color to ``model_name`` can drive a white channel."""
    has_white = rgbw_models()
    return any(t in has_white for t in expand_overlay_models([model_name]))


def all_is_virtual():
    """True when "All" is not a real overlay model but a fan-out over the zones.

    A single overlay model has one channels-per-node value, so a show mixing
    3-channel (RGB) and 4-channel (RGBW) models has no correct whole-display
    composite. The import then stores none, and "All" drives every zone's
    models instead. An install that never imported keeps its own "All".
    """
    if ZoneLayout.query.filter_by(slot=0).first():
        return False
    return bool(
        ZoneMember.query.first()
        or ZoneLayout.query.filter(ZoneLayout.slot > 0).first()
    )


def _members_by_slot():
    out = {}
    for m in ZoneMember.query.order_by(ZoneMember.slot, ZoneMember.position).all():
        out.setdefault(m.slot, []).append(m.fpp_model_name)
    return out


def all_overlay_models():
    """Every overlay model this app drives, group members included."""
    names = set(OVERLAY_MODELS)
    for members in _members_by_slot().values():
        names.update(members)
    return names


def expand_overlay_models(names):
    """Swap each grouped zone for its member overlay models.

    Zones without members, and names that aren't zones at all ("All",
    "--All Models--"), pass through untouched. Order is kept, duplicates dropped.
    """
    members = _members_by_slot()
    virtual_all = "All" in names and all_is_virtual()
    laid_out = (
        {l.slot for l in ZoneLayout.query.filter(ZoneLayout.slot > 0).all()}
        if virtual_all else set()
    )
    out = []
    for name in names:
        m = re.match(r"^Zone (\d+)$", name or "")
        if name == "All" and virtual_all:
            targets = []
            for slot in range(1, MAX_ZONES + 1):
                targets += members.get(slot) or ([f"Zone {slot}"] if slot in laid_out else [])
        else:
            targets = members.get(int(m.group(1))) if m else None
        for t in (targets or [name]):
            if t not in out:
                out.append(t)
    return out


def get_all_zones():
    """Return all 16 zones in slot order, seeding defaults on first call."""
    existing = {z.slot: z for z in Zone.query.all()}
    zones = []
    needs_commit = False
    for slot in range(MAX_ZONES + 1):
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
            for slot in range(MAX_ZONES + 1):
                z = existing.get(slot)
                if z is None:
                    name = "All" if slot == 0 else f"Zone {slot}"
                    z = Zone(slot=slot, display_name=name, hidden=False)
                zones.append(z)
    return zones
