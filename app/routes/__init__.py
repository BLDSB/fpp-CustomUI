from flask import Blueprint, render_template

from app.auth_utils import login_required

main = Blueprint("main", __name__)


@main.route("/")
@login_required
def index():
    return render_template("home.html")


@main.route("/controls")
@login_required
def controls():
    return render_template("index.html")


# Additional route modules are registered in app/__init__.py as blueprints.
