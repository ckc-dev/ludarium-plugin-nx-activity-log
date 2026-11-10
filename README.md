# Ludarium NX-ActivityLog Plugin

Live read/write tracker adapter for NX-ActivityLog import/export files.

- `PLUGIN_TYPE`: `nx_activity_log`
- `PLUGIN_DISPLAY_NAME`: `NX-ActivityLog`
- Plugin API: 2
- Adapter: `adapter.py`
- Event/export codec: `nxal_codec.py`

From this plugin repository, with the host application checkout on `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/host python -m unittest discover -s tests -q
```
