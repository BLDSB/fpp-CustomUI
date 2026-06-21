import json

from app import db

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
    multisync   = db.Column(db.Boolean, nullable=False, default=False)
    systems_json = db.Column(db.Text, nullable=False, default="[]")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "effect_name": self.effect_name,
            "models": json.loads(self.models_json),
            "args": json.loads(self.args_json),
            "multisync": self.multisync,
            "systems": json.loads(self.systems_json),
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
        db.session.commit()
    return zones
