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

## Acknowledgements

This plugin is designed to interoperate with
[NX-Activity-Log](https://github.com/tallbl0nde/NX-Activity-Log) by tallbl0nde,
which is distributed under the MIT License. The upstream project supplied the
export format and behavior this adapter follows; its copyright and license are
preserved in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

This project is not affiliated with or endorsed by Nintendo.

## License

This plugin is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, version 3 only.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See [`LICENSE`](LICENSE) for the full terms.

Copyright (C) 2026 Ludarium contributors.
