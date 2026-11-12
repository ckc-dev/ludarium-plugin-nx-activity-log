# NXAL / Switch / Ludarium Test Plan

This is a physical-console test plan for validating the NXAL adapter and the
environment-scoped NXAL tools.

## Start from a blank slate

Do not begin with `dev_assets/export.json`, a previously imported export, a
pre-populated `importedData.json`, or an existing Ludarium NXAL environment.

Initial state:

- Nintendo Switch with NXAL 1.6.0 installed.
- NXAL has no imported data.
- At least two fresh Switch users, for example `NxTestA` and `NxTestB`.
- At least two installed games that can be launched and suspended.
- Ludarium running against a test database copy.
- No NXAL environment configured in Ludarium yet.

Back up the following before every destructive test:

- `/switch/NX-Activity-Log/export.json`
- `/switch/NX-Activity-Log/importedData.json`
- `/switch/NX-Activity-Log/import.json`
- Ludarium `ludarium.db`
- Every host-side export used by an environment

## Phase 0: blank NXAL baseline

1. Launch NXAL, confirm there are no sessions or imported data.
2. Launch a game, play for a short controlled period, suspend/close it.
3. Open another game, play briefly, close it.
4. Export from NXAL to `export.json`.
5. Copy that export to the host test directory as `blank-baseline.json`.
6. Validate it:

   ```bash
   python3 validate_import.py blank-baseline.json
   ```

Expected:

- Valid user/title/event structure.
- At least one complete `Launched…Closed` group.
- No PDM/imported-data assumptions.
- Host reads the same user and session count.
- Host session durations/total use active-play segments; summary/export values
  use NXAL-compatible accounting and may differ for malformed orphan focus.

Record:

- User names and IDs
- Title IDs and names
- Session count
- Total playtime
- First/last per title

## Phase 1: create real native events

Play controlled sessions and record the exact expected event stream.

### Paused session

1. Launch a game.
2. Play for 30 seconds.
3. Suspend/leave the game for 20 seconds.
4. Resume and play for 40 seconds.
5. Close the game.
6. Export.

Expected:

- `Launched`
- `Gained Focus`
- `Account Login`
- `Lost Focus`
- `Gained Focus`
- `Lost Focus`
- `Account Logout`
- `Closed`

Expected host result:

- One session
- Two active segments
- 70 seconds credited playtime
- Wall-clock end later than start + 70

### Zero-length or malformed focus cases

Create controlled fixtures by editing a copy of the export, not by relying on
normal gameplay:

- `Gained Focus` immediately followed by `Lost Focus`
- consecutive `Lost Focus` events
- `Lost Focus` separated from another `Lost Focus` by `Account Logout`
- `Gained Focus` with no following `Lost Focus`
- incomplete group at end of a title
- incomplete group followed by another title

Expected:

- Host shows zero-duration segments where applicable.
- Host warns for account-separated orphan focus.
- NXAL may count the orphan focus differently; record the difference.
- Fix export removes orphan events, sorts, and rewrites summary values.

## Phase 2: configure Ludarium

1. Create a Ludarium NXAL environment pointing at a **copy** of the Switch
   export.
2. Open the environment profile picker and bind each discovered Switch profile
   to a different GameTime user. Try binding two profiles to the same user and
   confirm the form refuses to save; an unmapped profile counts as using the
   environment default user for this check.
3. Open Sessions and Games.
4. Compare against the NXAL export.

Expected:

- All native sessions are present.
- Incomplete sessions show warnings.
- Exact duplicate groups show warnings only when the full event group matches.
- Parts are logical play segments, not raw events.
- The environment selector is used by all Tools-page NXAL tools; no free-form
  path fields.

## Phase 3: host session and part edits

Use a copy of the export and perform each operation, checking the resulting
file and NXAL import.

### Part operations

- Add a segment in an inactive gap.
- Move a segment start.
- Resize a segment.
- Delete a segment.
- Add a second segment separated by a pause.
- Delete all segments from a complete session.

Expected:

- Overlaps are rejected.
- A complete session cannot extend past `Closed`.
- An incomplete session cannot extend into the next session.
- Account events are preserved when valid.
- `firstPlayed` / `lastPlayed` remain preserved during adapter edits.
- Session IDs change after session/part edits and are re-resolved by the host.
- Session ID is stable across part edits.
- Event stream is valid for NXAL import.

### Session operations

- Add a session with an explicit host user; an unmapped/ambiguous NXAL profile
  must be rejected rather than assigned to the first profile.
- Change the session User to another mapped Switch profile, without editing
  its start or duration. Confirm the whole event group moves to that native
  profile in the file.
- Change User and start/duration in the same save. Confirm the move is atomic
  and the target has the edited timing.
- Move a session start.
- Change duration.
- Delete a session.
- Repair incomplete session.
- Repair exact duplicate sessions.
- Merge adjacent sessions.
- Merge duplicate game titles.

Expected:

- User moves preserve the native event group, including account events; no
  host-only override reappears.
- Source and target summaries recompute; an emptied source keeps PDM
  first/last but reports zero playtime/launches.
- Moving onto an existing target session with an overlapping span is rejected.
- Unmapped or ambiguous target profiles leave the file unchanged.
- Duplicate game merge does not duplicate events or playtime.
- Summary playtime/launches recompute.
- No invalid daisy-chained session boundaries.

### Multi-user isolation

1. Create two Switch users with the same title.
2. Create sessions at the same timestamp.
3. Edit parts for one user.
4. Export and import.

Expected:

- The other user's session is untouched.
- Session parts use exact session IDs.
- Profile bindings do not cause cross-user edits.

## Phase 4: Tools page

Open **Tools → NXAL**.

Expected tools:

- Validate export
- Verify imported data
- Fix export in place
- Merge exports in place
- Deduplicate exports in place
- Export summary

Each tool must show the NXAL environment selector. Fix/merge/deduplicate must
write in place and leave `.bak` backups. Fix/merge/deduplicate must also show
the two explicit options:

- Keep summary-only titles (default on).
- Recalculate first/last from events (default off).

### Validate export

1. Select a valid environment.
2. Run.

Expected:

- Full structural/data validation result.
- Errors make the result fail; warnings do not.
- No writes.

### Verify imported data

1. Place `import.json` and `importedData.json` in the same directory.
2. Select the environment whose path is `import.json`.
3. Run.

Expected:

- Compares the selected import with its sibling `importedData.json`.
- Reports event type conversion, user/title matching, summaries and version
  differences.
- Version differences are warnings, not false failures.

### Fix export in place

1. Run on a copy with malformed/duplicate/orphan events and unsorted groups.
2. Run once with defaults and once with each option toggled.
3. Check the result and `.bak`.

Expected:

- Invalid events removed.
- Duplicate events removed by strict native identity.
- Orphan focus removed.
- Events normalized within native session groups without interleaving
  duplicate/near-duplicate sessions.
- With **Recalculate first/last from events** off, valid PDM values remain.
- With it on, first/last are event-derived.
- With **Keep summary-only titles** on, a no-event summary remains untouched.
- With it off, a no-event summary is reset to zero.
- `playtime` / `launches` use NXAL-compatible `countPlaytime()` accounting.
- No incomplete-session closure is performed by this tool.

### Merge exports in place

1. Select the target environment.
2. Select one or more additional source environments.
3. Run.

Expected:

- Target is rewritten in place.
- Target has a `.bak`.
- Sources are read-only.
- Users/titles/events are merged by name/title/event identity.
- Events are normalized without crossing session boundaries.
- Summary-only titles are preserved by default.
- Valid PDM first/last values are preserved unless the recalculation option
  is enabled.
- NXAL-compatible playtime is used.
- Validate the merged output and import it into NXAL.

### Deduplicate exports in place

1. Select target and at least one source.
2. Test `match_by=name` and `match_by=id`.
3. Optional rename mapping.
4. Run.

Expected:

- Every selected file is rewritten in place with a `.bak`.
- Overlapping sessions are owned by the oldest export with `Closed`, or oldest
  `Launched` when no `Closed` exists.
- Events are normalized and summaries use NXAL-compatible playtime.
- Summary-only titles are kept exactly once in the oldest containing export.
- Merge of deduplicated exports converges with merging originals.
- Adjacent session merges work without being rejected by overlap validation.
- Malformed merge/dedup inputs are rejected with a clear error rather than
  partially rewritten.

### Export summary

Expected:

- Read-only user/title/launch/playtime summary.
- Not a substitute for full validation.

## Phase 5: import into NXAL

Use an empty Switch profile first.

1. Copy the host-modified export to
   `/switch/NX-Activity-Log/import.json`.
2. In NXAL, run Import.
3. Restart NXAL.
4. Confirm `importedData.json` exists.
5. Open All Activity, Recent Activity and Details for each test user.

Expected:

- Profile names must match exactly on the Switch.
- A missing profile causes the entire user's titles to be skipped.
- Complete sessions appear.
- Session ordering follows the event vector; host may sort differently.
- PDM-existing profiles append imported history; do not use this as a clean
  replacement test unless the profile is fresh.
- NXAL first/last may use PDM or imported summary fallback; host uses
  event-derived values.

Then export again and compare:

- Session count
- Per-session playtime
- Event order
- Summary first/last
- Orphan focus differences
- Incomplete/trailing-session behavior

## Phase 6: NXAL edge cases

Test on fresh and existing-PDM profiles:

- trailing incomplete group that is the last loaded group
- trailing incomplete group followed by another title
- accountless group
- unknown event type
- missing clock/steady timestamp
- duplicate user/title IDs
- unsorted events
- duplicate account events
- exact duplicate session groups
- future timestamp relative to the Switch clock
- session crossing midnight
- zero-duration segment
- two users with the same title and timestamp
- missing/renamed Switch profile

Expected:

- Sessions with no matching Switch profile are silently skipped by NXAL.
- Accountless groups are omitted by NXAL; the host warns and its Fix action
  rebuilds canonical account events. The summary excludes accountless groups
  until repaired. Deduplicate prefers an account-bearing copy of the same
  session over an accountless copy.
- A last-global trailing group is omitted by NXAL while host shows it.
- Host warnings and validation results make the distinction visible.

## Phase 7: CLI equivalents

Run every CLI on copies and compare with the Tools page.

```bash
python3 validate_import.py copy.json
python3 verify_import.py import.json importedData.json
python3 fix_import.py copy.json --warnings
python3 fix_import.py copy.json --warnings --recalculate-first-last
python3 merge_exports.py merged.json first.json second.json
python3 merge_exports.py --recalculate-first-last merged.json first.json second.json
python3 deduplicate_exports.py first.json second.json
python3 deduplicate_exports.py --drop-summary-only first.json second.json
```

For each:

- Confirm `.bak` behavior.
- Confirm decline paths make no changes.
- Confirm idempotence.
- Compare output with the corresponding Tools-page result.
- Validate every output before importing it.
- Import merged/deduplicated output into NXAL and compare session counts.

## Phase 8: round-trip

For each mutation path:

1. Start from a known export.
2. Edit via host.
3. Import to NXAL.
4. Export from NXAL.
5. Re-validate and re-import to host.
6. Compare session count, event stream, parts, summary first/last, playtime.

Expected:

- No duplicate sessions or playtime growth.
- Exact account events preserved unless a tool explicitly repairs them.
- Host-created events are valid NXAL input.
- Differences are limited to documented PDM/first-last and global ordering
  behavior.

## Exit criteria

The implementation is acceptable when:

- A blank Switch baseline survives host edit and NXAL import without invalid
  data.
- Every Tools-page operation is environment-scoped and in-place with backups.
- Every CLI has an equivalent UI tool with equivalent behavior.
- Session counts agree for a controlled fresh profile except where NXAL’s
  documented global/trailing or PDM/first-last behavior explains the result.
- No test loses a complete session silently.
- All differences are recorded with the expected NXAL reason.
