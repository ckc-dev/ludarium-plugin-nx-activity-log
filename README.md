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

## Session model

NXAL sessions are atomic `Launched…Closed` event groups, even when they cross
midnight. The host must not split them in storage. `duration` is credited focus
playtime; the adapter also returns `end_time` and `active_segments` because a
NXAL session can contain pauses and therefore have `end - start != duration`.
Charts and calendar views clip the active segments to their selected range.

Synthesized sessions include `Account Login` and `Account Logout` events,
which NXAL uses to attribute a session to a user. Session parts are keyed by
title and event type as well as timestamp; required boundary/account events
are read-only. A session without a `Closed` event is marked incomplete in the
host; its **Fix** action adds the missing account-logout/closed events.

Session part **Add play segment** adds credited focus time in a gap. It does
not insert a pause: edit the surrounding focus events for pause changes.
New games are written with a zero-valued summary so NXAL's importer keeps
them even before their first session.

NXAL has no native session id, so the adapter derives a deterministic host id
from the NXAL profile, title id and `Launched` event identity. It stays stable
across duration/part edits and incomplete-session repair; moving the start
creates a new id and the host migrates the user override. Exact ids also let
two sessions starting in the same second be edited or deleted independently.
Two completely identical `Launched` events in one title remain inherently
indistinguishable without a persistent native field.

## Acknowledgements

This plugin is designed to interoperate with
the maintained
[NX-Activity-Log fork](https://github.com/zdm65477730/NX-Activity-Log), based on
the archived
[tallbl0nde/NX-Activity-Log](https://github.com/tallbl0nde/NX-Activity-Log).
The upstream project supplied the export format and behavior this adapter
follows; its MIT copyright and license are preserved in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

This project is not affiliated with or endorsed by Nintendo.

## License

This plugin is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, version 3 only.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See [`LICENSE`](LICENSE) for the full terms.

Copyright (C) 2026 Ludarium contributors.

## Continuous integration

The plugin CI checks out the private Ludarium host into a temporary `host/`
directory so the plugin can run its full integration suite against the current
host contract. The workflow requires a repository secret named
`HOST_REPO_TOKEN`: a fine-grained token with read-only access to
`ckc-dev/ludarium`. No host code is vendored or linked into this repository.
