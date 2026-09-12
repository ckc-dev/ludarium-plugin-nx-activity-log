"""GameTime plugin manifest: NX-ActivityLog adapter.

Phase A discovery surface (static exports). The advanced modules
(plugin_tools / plugin_health / blueprints) are imported here in one guarded
block; a failure in any one of them must never disable the adapter — so we
import the adapter BEFORE the advanced block, and keep schema building fully
independent of the advanced imports.
"""

import os
import sys

_this = os.path.dirname(os.path.abspath(__file__))
if _this not in sys.path:
    sys.path.insert(0, _this)

# ---- schema (no dependence on advanced modules) ----
PLUGIN_CONFIG_SCHEMA = [
    {
        "key": "profile_mapping",
        "label": "Profile mapping (JSON)",
        "type": "text",
        "default": "{}",
        "help": ("Map NXAL profile/user name -> GameTime user id as a JSON "
                 "object, e.g. {\"Grog\": 1}."),
    },
    {
        "key": "default_user_id",
        "label": "Default user id",
        "type": "number",
        "default": 1,
        "help": "GameTime user to own sessions with no profile-mapping entry.",
    },
]

PLUGIN_TYPE = "nx_activity_log"
PLUGIN_API_VERSION = 1
PLUGIN_VERSION = "0.1.0"
PLUGIN_AUTHOR = "GameTime"
PLUGIN_DISPLAY_NAME = "NX-ActivityLog"

# ---- adapter (had to come before the advanced block; host only needs the class) ----
from . import adapter as _adapter  # noqa: E402
PLUGIN_ADAPTER_CLASS = _adapter.NXALGameTrackerAdapter

# ---- advanced surface (guarded; never blocks the adapter/schema above) ----
try:
    from .plugin_tools import PLUGIN_TOOLS
    from .plugin_health import PLUGIN_HEALTH
    from .blueprints import PLUGIN_BLUEPRINT, PLUGIN_NAV

    PLUGIN_NAV = PLUGIN_NAV
except Exception:
    PLUGIN_TOOLS = []
    PLUGIN_HEALTH = None
    PLUGIN_BLUEPRINT = None
    PLUGIN_NAV = []

PLUGIN_INIT = None
