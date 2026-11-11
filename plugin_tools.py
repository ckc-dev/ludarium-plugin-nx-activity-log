"""Environment-scoped host tools for NXAL exports."""

import io
import os
import shutil
from contextlib import redirect_stdout

from nxal_codec import (
    VerificationResult,
    find_duplicate_events,
    load_json,
    write_export_json,
)
from nxal_repair import repair_export, summary_for_events

from . import deduplicate_exports, merge_exports, validate_import, verify_import


def _path(params, ctx):
    path = (params.get('path') or '').strip()
    if not path:
        path = (ctx.path or '').strip()
    if not path:
        return None, ("Select an NXAL environment or provide a path to an "
                      "NXAL import.json.")
    if not os.path.isfile(path):
        return None, f"File not found: {path}"
    return path, None


def _lines(result):
    lines = []
    lines.extend(f"ERROR: {message}" for message in result.errors)
    lines.extend(f"WARN: {message}" for message in result.warnings)
    if not lines:
        lines.append("No errors or warnings.")
    lines.append(
        f"RESULT: {len(result.errors)} error(s), "
        f"{len(result.warnings)} warning(s)")
    return lines


def _load_for_tool(params, ctx):
    path, error = _path(params, ctx)
    if error:
        return None, None, error
    try:
        return path, load_json(path), None
    except Exception as error:
        return None, None, f"Could not read {path}: {error}"


def verify_export(params, ctx):
    """Quick read-only export summary (not the full validator)."""
    path, data, error = _load_for_tool(params, ctx)
    if error:
        return {'ok': False, 'message': error, 'lines': []}
    users = data.get('users', [])
    titles = sum(len(user.get('titles', [])) for user in users)
    summary = [
        summary_for_events(title.get('events', []))
        for user in users for title in user.get('titles', [])
    ]
    launches = sum(item['launches'] for item in summary)
    minutes = sum(item['playtime'] for item in summary) / 60.0
    duplicate_events = sum(
        len(find_duplicate_events(title.get('events', [])))
        for user in users for title in user.get('titles', []))
    lines = [
        f"File: {path}",
        f"Users: {len(users)}",
        f"Titles: {titles}",
        f"Launches: {launches}",
        f"Playtime: {minutes:.1f} min",
    ]
    if duplicate_events:
        lines.append(f"WARN: {duplicate_events} duplicate event(s) present")
    lines.append(f"RESULT: {len(users)} user(s), {titles} title(s)")
    return {'ok': True, 'message': 'Export summary', 'lines': lines}


def validate_export(params, ctx):
    """Run the full NXAL structural/data validator."""
    path, data, error = _load_for_tool(params, ctx)
    if error:
        return {'ok': False, 'message': error, 'lines': []}
    result = VerificationResult()
    with redirect_stdout(io.StringIO()):
        validate_import.validate_metadata(data, result)
        validate_import.validate_users(data, result)
    return {
        'ok': result.passed,
        'message': 'Export is valid' if result.passed else 'Export has errors',
        'lines': _lines(result),
    }


def verify_imported_data(params, ctx):
    """Compare the selected import.json with its sibling importedData.json."""
    path, _data, error = _load_for_tool(params, ctx)
    if error:
        return {'ok': False, 'message': error, 'lines': []}
    imported_path = os.path.join(
        os.path.dirname(path), 'importedData.json')
    if not os.path.isfile(imported_path):
        return {
            'ok': False,
            'message': f"importedData.json not found beside {path}",
            'lines': [],
        }
    try:
        imported_data = load_json(imported_path)
    except Exception as read_error:
        return {
            'ok': False,
            'message': f"Could not read {imported_path}: {read_error}",
            'lines': [],
        }
    result = VerificationResult()
    with redirect_stdout(io.StringIO()):
        verify_import.verify_metadata(_data, imported_data, result)
        verify_import.verify_users(_data, imported_data, result)
    return {
        'ok': result.passed,
        'message': ('Import matches its generated data' if result.passed
                    else 'Import verification found errors'),
        'lines': _lines(result),
    }


def fix_export(params, ctx):
    """Repair the selected export in place, with a .bak backup.

    This is deliberately a tool action, not an adapter read/edit action:
    it sorts events, removes invalid/duplicate/orphan events, and recomputes
    the summary first/last timestamps/playtime/launches.
    """
    path, data, error = _load_for_tool(params, ctx)
    if error:
        return {'ok': False, 'message': error, 'lines': []}
    try:
        shutil.copy2(path, path + '.bak')
    except OSError as backup_error:
        return {
            'ok': False,
            'message': f"Could not create backup: {backup_error}",
            'lines': [],
        }

    stats = repair_export(data)
    write_export_json(path, data)
    return {
        'ok': True,
        'message': f"Repaired {path}",
        'lines': [
            f"Backup: {path}.bak",
            f"Titles updated: {stats['titles']}",
            f"Events removed: {stats['events_removed']}",
            f"  Invalid: {stats['invalid']}",
            f"  Duplicates: {stats['duplicates']}",
            f"  Orphan focus: {stats['orphans']}",
            'Events sorted and summary timestamps repaired.',
        ],
    }


# Backwards-compatible name used by existing tests and older tool configs.
def fix_import(params, ctx):
    return fix_export(params, ctx)


def _renames(value):
    result = {}
    for line in str(value or '').replace(',', '\n').splitlines():
        if '=' not in line:
            continue
        old, new = line.split('=', 1)
        old, new = old.strip(), new.strip()
        if old and new:
            result[old] = new
    return result


def _selected_paths(params, ctx):
    path, error = _path(params, ctx)
    if error:
        return [], error
    paths = [path]
    for source in params.get('sources') or []:
        if source and source not in paths:
            paths.append(source)
    return paths, None


def merge_export(params, ctx):
    """Merge all selected environments into the selected target in place."""
    paths, error = _selected_paths(params, ctx)
    if error:
        return {'ok': False, 'message': error, 'lines': []}
    if len(paths) < 2:
        return {
            'ok': False,
            'message': 'Select at least one additional source environment.',
            'lines': [],
        }
    try:
        stats = merge_exports.merge_paths(
            paths[0], paths, backup=True)
    except Exception as merge_error:
        return {'ok': False, 'message': str(merge_error), 'lines': []}
    return {
        'ok': True,
        'message': f"Merged into {paths[0]}",
        'lines': [
            f"Target: {stats['path']}",
            f"Backup: {stats['backup'] or '(none)'}",
            f"Users: {stats['users']}",
            f"Titles: {stats['titles']}",
            f"Events: {stats['events']}",
        ],
    }


def deduplicate_export(params, ctx):
    """Deduplicate all selected environments in place."""
    paths, error = _selected_paths(params, ctx)
    if error:
        return {'ok': False, 'message': error, 'lines': []}
    if len(paths) < 2:
        return {
            'ok': False,
            'message': 'Select at least one additional source environment.',
            'lines': [],
        }
    match_by = params.get('match_by') or 'name'
    try:
        results = deduplicate_exports.deduplicate_paths(
            paths, match_by=match_by,
            renames=_renames(params.get('renames')), backup=True)
    except Exception as dedup_error:
        return {'ok': False, 'message': str(dedup_error), 'lines': []}
    lines = []
    for stats in results:
        lines.extend([
            f"File: {stats['path']}",
            f"Backup: {stats['backup'] or '(none)'}",
            f"Sessions: {stats['sessions_after']}/"
            f"{stats['sessions_before']}",
            f"Events: {stats['events_after']}/{stats['events_before']}",
            f"Titles: {stats['titles_after']}",
        ])
    return {
        'ok': True,
        'message': 'Deduplication complete',
        'lines': lines,
    }


_SOURCE_FIELD = {
    'name': 'sources',
    'label': 'Additional source environments',
    'type': 'environment',
    'multiple': True,
    'required': True,
    'help': 'The selected environment is the target; select one or more '
            'additional NXAL environments.',
}

PLUGIN_TOOLS = [
    {
        'key': 'validate_export',
        'name': 'Validate export',
        'description': 'Run the full NXAL structural/data validator in place.',
        'needs_environment': True,
        'fields': [],
        'execute': validate_export,
    },
    {
        'key': 'verify_import',
        'name': 'Verify imported data',
        'description': 'Compare the selected import.json with its sibling '
                       'importedData.json.',
        'needs_environment': True,
        'fields': [],
        'execute': verify_imported_data,
    },
    {
        'key': 'fix_import',
        'name': 'Fix export in place',
        'description': 'Back up, sort, repair malformed/duplicate events, and '
                       'fix summary first/last/playtime/launches.',
        'needs_environment': True,
        'fields': [],
        'execute': fix_export,
    },
    {
        'key': 'merge_exports',
        'name': 'Merge exports in place',
        'description': 'Merge selected source environments into the selected '
                       'target, with a backup.',
        'needs_environment': True,
        'fields': [_SOURCE_FIELD],
        'execute': merge_export,
    },
    {
        'key': 'deduplicate_exports',
        'name': 'Deduplicate exports in place',
        'description': 'Remove overlapping sessions across the selected '
                       'environments, with backups.',
        'needs_environment': True,
        'fields': [
            _SOURCE_FIELD,
            {
                'name': 'match_by',
                'label': 'Match users by',
                'type': 'select',
                'options': [
                    {'value': 'name', 'label': 'Profile name'},
                    {'value': 'id', 'label': 'Native user id'},
                ],
                'default': 'name',
            },
            {
                'name': 'renames',
                'label': 'Rename matches',
                'type': 'text',
                'placeholder': 'OldName=NewName, Other=Name',
                'help': 'Optional; only used with name matching.',
            },
        ],
        'execute': deduplicate_export,
    },
    {
        'key': 'verify_export',
        'name': 'Export summary',
        'description': 'Quick read-only user/title/launch/playtime summary.',
        'needs_environment': True,
        'fields': [],
        'execute': verify_export,
    },
]
