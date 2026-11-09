"""GameTime host manifest: NX-ActivityLog adapter.

Discovery contract (host reads these module attributes):
  PLUGIN_TYPE, PLUGIN_DISPLAY_NAME, PLUGIN_VERSION, PLUGIN_AUTHOR,
  PLUGIN_DESCRIPTION, PLUGIN_API_VERSION, PLUGIN_ADAPTER_CLASS,
  PLUGIN_BLUEPRINT, PLUGIN_NAV, PLUGIN_TOOLS, PLUGIN_HEALTH,
  PLUGIN_CONFIG_SCHEMA.

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
PLUGIN_API_VERSION = 2

PLUGIN_CONFIG_SCHEMA = [
    {
        "key": "profile_mapping",
        "label": "Profile mapping (JSON)",
        "type": "text",
        "default": "{}",
        "help": ("Map NXAL profile/user name -> GameTime user id as a JSON "
                 "object, e.g. {\"Grog\": 1}. Unmapped profiles use the "
                 "environment's default user."),
    },
]

from . import adapter  # noqa: E402

PLUGIN_ADAPTER_CLASS = adapter.NXALGameTrackerAdapter


def _import_advanced():
    """Import optional surfaces independently so one failure is isolated."""
    out = {"tools": [], "health": None, "blueprint": None, "nav": []}
    try:
        from .plugin_tools import PLUGIN_TOOLS
        out["tools"] = PLUGIN_TOOLS
    except Exception as error:
        out["tools_err"] = f"{type(error).__name__}: {error}"
    try:
        from .plugin_health import PLUGIN_HEALTH
        out["health"] = PLUGIN_HEALTH
    except Exception as error:
        out["health_err"] = f"{type(error).__name__}: {error}"
    try:
        from .blueprints import PLUGIN_BLUEPRINT, PLUGIN_NAV
        out["blueprint"] = PLUGIN_BLUEPRINT
        out["nav"] = PLUGIN_NAV
    except Exception as error:
        out["pages_err"] = f"{type(error).__name__}: {error}"
    return out


_adv = _import_advanced()
PLUGIN_TOOLS = _adv["tools"]
PLUGIN_HEALTH = _adv["health"]
PLUGIN_BLUEPRINT = _adv["blueprint"]
PLUGIN_NAV = _adv["nav"]
