# Ludarium NX-ActivityLog Plugin

Live read/write tracker adapter for NX-ActivityLog import/export files.

- `PLUGIN_TYPE`: `nx_activity_log`
- `PLUGIN_DISPLAY_NAME`: `NX-ActivityLog`
- Plugin API: 2
- Adapter: `adapter.py`
- Event/export codec: `nxal_codec.py`
- Shared repair/summary helpers: `nxal_repair.py`

From this plugin repository, with the host application checkout on `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/host python -m unittest discover -s tests -q
```

For a full physical-console checklist starting from a blank Switch, see
[`SWITCH_TEST_PLAN.md`](SWITCH_TEST_PLAN.md).

## Session model

NXAL sessions are atomic `Launched…Closed` event groups, even when they cross
midnight. The host must not split them in storage. A trailing group without a
`Closed` event is still shown as incomplete so it can be repaired. `duration`
is credited focus playtime; the adapter also returns `end_time` and
`active_segments` because a NXAL session can contain pauses and therefore have
`end - start != duration`. Charts and calendar views clip the active segments
to their selected range.

NXAL requires `Account Login` / `Account Logout` events for session
accounting; native ownership comes from the containing `users[]` profile, not
from an account id on the event. Existing valid account events are preserved
where they occur, including when a session moves to another profile; canonical
Login/Logout events are generated when a session has none, and a missing
Logout is added when an incomplete session is repaired. Session parts expose
only logical active-play segments: users add, move, resize, or delete play
intervals, and the adapter rebuilds the native event group, summaries, and part
ids. Part ids include the exact session id, so two native profiles with the
same title cannot edit each other's sessions. Boundary/account events are
never user-editable. Segment edits reject overlaps and cannot extend past a
complete session's existing `Closed` boundary or into the next session; use
the whole-session editor first when a complete session needs more room.

Malformed persisted data is surfaced rather than silently "fixed" on read.
An orphan `Lost Focus`, an account event with missing timestamps, or an
unknown event type marks the session for **Fix**, which rebuilds the group
from completed focus pairs while preserving valid account events. This matters
because NXAL's `countPlaytime()` can count an account-separated orphan
`Lost Focus` a second time; the adapter deliberately counts completed
Gained/Lost pairs and offers repair instead of reproducing that double count.

Exact duplicate native event groups are marked **Duplicate session** by the
adapter. The host can show both as overlapping, but overlap is only a warning;
the duplicate warning is exact. Its **Fix** action removes all but one copy of
the identical group.

## Profile bindings

Profile-to-user mappings are host data, not plugin JSON. The environment editor
discovers native NXAL profiles from the configured file and lets each profile
be mapped to a GameTime user or left unmapped for the environment default.
Bindings are one-to-one inside one NXAL environment: a GameTime user cannot
own two native profiles, because the session editor needs one unambiguous
target when moving a session inside the NXAL file. An unmapped profile counts
as using the default user for this check. The same GameTime user may still map
to different profiles in different NXAL files.

Native ids are the stable key; display names are refreshed from the file.
Deleting a GameTime user leaves the binding unmapped rather than producing an
"Unknown" session. Stale profiles remain visible until explicitly removed.

NXAL reports `native_user_id`, so the host treats native ownership as
authoritative and ignores host session overrides for this adapter. Existing
override rows are cleaned when a native session is edited; they do not mask
newly moved native sessions.

## Host tools

The Tools page exposes environment-scoped equivalents of the CLI scripts:

- Validate export
- Verify imported data (`import.json` against sibling `importedData.json`)
- Fix export in place
- Merge exports in place
- Deduplicate exports in place
- Export summary

Tools use the selected NXAL environment path; there are no free-form path
inputs. Fix/merge/deduplicate write in place and create `.bak` backups first.
All three offer two explicit options:

- **Keep summary-only titles** keeps titles that have a summary but no events.
  NXAL can export these when PDM has statistics but no event session.
- **Recalculate first/last from events** is off by default. Leaving it off
  preserves valid PDM-backed `firstPlayed`/`lastPlayed`; enabling it replaces
  them with event-derived values.

Fix sorts/normalizes events and repairs playtime/launches using NXAL-compatible
`countPlaytime()` accounting. Merge uses the selected environment as its target
and writes the additional source environments into it; deduplicate rewrites the
selected environment and every additional source environment in place. Both
preserve/merge summary-only titles unless explicitly told to drop them.

**Add play segment** adds credited focus time in an inactive gap. Pauses are
the gaps between play segments, so moving or deleting a segment changes the
pause without a separate pause editor. New games are written with a
zero-valued summary so NXAL's importer keeps them even before their first
session. NXAL cannot create a user/profile because its id is generated by the
console, so adding a game/session requires an existing user profile in the
export. The add-session forms require a host user; the NXAL adapter maps that
user through host-managed native profile bindings and refuses an unmapped or
ambiguous selection. Bindings are selected in the environment editor from the
profiles found in the NXAL file, not typed as JSON.

Changing the User in the session editor moves the whole `Launched…Closed`
event group to the target native profile's title collection. Timing edits and
the ownership move happen in one adapter write. The group is not rebuilt, so
existing account events and focus intervals are preserved; the target title is
created when needed, target overlaps are rejected, and both source and target
summaries are recomputed. If the source title loses its last session, its PDM
`firstPlayed`/`lastPlayed` stay intact while `playtime` and `launches` return to
zero.

NXAL has no native session id, so the adapter derives a host id from the native
NXAL user id, title id and the full event group identity. Including every event
makes collisions between different sessions practically impossible; identical
duplicate groups still intentionally share an id until repaired. Editing a
session changes its id, and moving it to another native profile changes the
user-id component. The host reloads the row and clears any old host override;
native ownership is authoritative. The native NXAL user id keeps identity
stable across profile renames.

`firstPlayed` / `lastPlayed` are preserved from the existing summary during
adapter edits, because NXAL takes them from the console PDM database rather
than deriving them from the event stream. Repair tools preserve valid PDM
values by default and offer an explicit event-derived recalculation option.
### Accounting model

Host session rows, active segments and the session-table total use **active
play accounting** (`nxal_codec.calculate_playtime()`): the sum of completed
focus intervals. This is intentional because users edit logical play segments;
the displayed session duration must equal the sum of those editable segments,
and an account-separated orphan focus must not be double-counted.

Exported/imported **summary fields** use NXAL-compatible
`countPlaytime()` accounting (`nxal_codec.calculate_nxal_playtime()`). Launch
counts are session-based and only account-bearing sessions count. As a result,
a malformed orphan-focus session can show a different active-play duration in
the host than its NXAL summary value. That difference is deliberate and is the
condition the warning/Fix path reports.

Accountless groups are reported explicitly by the adapter and can be repaired
or merged with an account-bearing copy. Merge/deduplicate normalize event
groups and reject structurally malformed inputs rather than partially
rewriting them.

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
