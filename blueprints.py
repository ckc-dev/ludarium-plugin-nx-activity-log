"""Ludarium: PLUGIN_BLUEPRINT + PLUGIN_NAV for the NXAL adapter."""

from flask import Blueprint, render_template

bp = Blueprint("nx_activity_log", __name__,
               template_folder="templates")


@bp.route("/")
def index():
    return render_template("nx_activity_log/index.html")


PLUGIN_BLUEPRINT = bp
PLUGIN_NAV = [
    {"label": "NXAL", "endpoint": "nx_activity_log.index", "icon": "bi-controller"},
]
