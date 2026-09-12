"""GameTime host manifest: NX-ActivityLog adapter (Phase B surface).

Discovery contract (host reads these module attributes):
  PLUGIN_TYPE, PLUGIN_DISPLAY_NAME, PLUGIN_VERSION, PLUGIN_AUTHOR,
  PLUGIN_DESCRIPTION, PLUGIN_API_VERSION, PLUGIN_ADAPTER_CLASS,
  PLUGIN_BLUEPRINT, PLUGIN_NAV, PLUGIN_TOOLS, PLUGIN_HEALTH,
  PLUGIN_CONFIG_SCHEMA, PLUGIN_INIT.

Live read/write over a single NXAL import.json export. Sessions are
Launched..Closed event groups per (user,title) mirroring NXAL getPDSessions;
playtime from Gained/Lost Focus steady pairs via nxal_common.calculate_playtime;
game ids are ``nx_<title-id>``; every mutation is written back byte-compatibly
through nxal_common.write_export_json.
"""

import os
import sys

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

PLUGIN_TYPE = "nx_activity_log"
PLUGIN_DISPLAY_NAME = "NX-ActivityLog"
PLUGIN_VERSION = "0.1.0"
PLUGIN_AUTHOR = "GameTime"
PLUGIN_DESCRIPTION = "Live read/write adapter over an NXAL import.json export"
PLUGIN_API_VERSION = 1

# ---- config schema (built once, before any advanced import) ----
def _build_config_schema():
    return [
        {
            "key": "profile_mapping",
            "label": "Profile mapping (JSON)",
            "type": "text",
            "default": "{}",
            "help": ("Map NXAL profile/user name -> GameTime user id as a JSON "
                     "object, e.g. {\"Grog\": 1}. Stored opaque per environment."),
        },
        {
            "key": "default_user_id",
            "label": "Default user id",
            "type": "number",
            "default": 1,
            "help": "GameTime user to own sessions when the profile mapping has "
                    "no entry for an NXAL user.",
        },
    ]


PLUGIN_CONFIG_SCHEMA = _build_config_schema()

from . import adapter  # noqa: E402

PLUGIN_ADAPTER_CLASS = adapter.NXALGameTrackerAdapter


def plugin_init(ctx):
    """Seed default settings on first boot (requestless-safe)."""
    for key, value in (
        ("import_path_hint", ""),
        ("default_merge_mode", "latest_wins"),
        ("verify_mode", "pedantic"),
    ):
        if ctx.get_setting(key, None) is None:
            ctx.set_setting(key, value)


PLUGIN_INIT = plugin_init

# ---- advanced surface (imported here; each is optional, guarded) ----
PLUGIN_TOOLS = []
PLUGIN_HEALTH = None
PLUGIN_BLUEPRINT = None
PLUGIN_NAV = []
try:
    from .plugin_tools import PLUGIN_TOOLS  # noqa: E402
    from .plugin_health import PLUGIN_HEALTH  # noqa: E402
    from .blueprints import PLUGIN_BLUEPRINT, PLUGIN_NAV  # noqa: E402
except Exception:
    pass


# ---- advanced surface (imported AFTER schema def so nothing pre-refs it) ----
def _import_advanced():
    out = {}
    try:
        from .plugin_tools import PLUGIN_TOOLS
        out["tools"] = PLUGIN_TOOLS
    except Exception as e:
        out["tools"] = []
        out["tools_err"] = f"{type(e).__name__}: {e}"
    try:
        from .plugin_health import PLUGIN_HEALTH
        out["health"] = PLUGIN_HEALTH
    except Exception as e:
        out["health"] = None
        out["health_err"] = f"{type(e).__name__}: {e}"
    try:
        from .blueprints import PLUGIN_BLUEPRINT, PLUGIN_NAV
        out["blueprint"] = PLUGIN_BLUEPRINT
        out["nav"] = PLUGIN_NAV
    except Exception as e:
        out["blueprint"] = None
        out["nav"] = []
        out["pages_err"] = f"{type(e).__name__}: {e}"
    return out


PLUGIN_TOOLS = []
PLUGIN_HEALTH = None
PLUGIN_BLUEPRINT = None
PLUGIN_NAV = []

_adv = _import_advanced()
PLUGIN_TOOLS = _adv.get("tools", [])
PLUGIN_HEALTH = _adv.get("health")
PLUGIN_BLUEPRINT = _adv.get("blueprint")
PLUGIN_NAV = _adv.get("nav", [])
