"""Simp URL Fetcher — NiceGUI web application."""

import sys
import signal
import subprocess
import os
import json
import shutil
import threading
import time
import re
from urllib.parse import urlparse
import html as html_mod
from datetime import datetime
from pathlib import Path
from typing import Optional

from nicegui import ui, app

# Load .env into environment (must happen before Settings.load())
from dotenv import load_dotenv
load_dotenv()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from core.models import Settings
from core.extractor import ScanResult, extract_from_html, extract_from_folder, merge_results
from core.sorter import process_folder, sort_model_folder
from core.renamer import rename_files
from core.exporter import export_all_formats, export_json, import_json
from core.registry import ModelRegistry
from core.downloader import run_download, retry_failed_urls
from core.crawler import crawl_thread, validate_cookie_json

settings = Settings.load()
registry = ModelRegistry(os.path.join(PROJECT_DIR, 'models_registry.json'))

VERSION = "0.0.1-dev"
RESTART_MARKER = os.path.join(PROJECT_DIR, '.restart.rqd')

# In-memory state for import/export tab
current_results: dict = {}
last_merged: Optional[ScanResult] = None
last_output_paths: dict = {}

# Cancel events for downloads (model_name -> threading.Event)
_cancel_events: dict[str, threading.Event] = {}

# Crawler state
_crawl_in_progress = False
_crawl_cancel = threading.Event()
_crawler_log: list[str] = []

# ── Logging ───────────────────────────────────────────────────────────

log_messages: list[dict] = []  # Each: {'timestamp': str, 'level': str, 'message': str}

def log(msg: str, level: str = 'INFO'):
    log_messages.append({
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'level': level.upper(),
        'message': msg
    })
    # Also print to console so the user sees output in the terminal
    print(f'[{level}] {msg}')


# ── Startup Logs ───────────────────────────────────────────────────────

log('=== Simp URL Fetcher startup ===', level='SYS')
log(f'Project directory: {PROJECT_DIR}', level='SYS')
log(f'Settings loaded: input_dir={settings.input_dir or "not set"}', level='SYS')
log(f'Update branch: {settings.update_branch or "default"}', level='SYS')
log(f'Models registry: {len(registry.models)} model(s) registered', level='SYS')
log(f'Output directory: {settings.output_dir}', level='SYS')


# ── Update system ─────────────────────────────────────────────────────

_update_state = {
    'checked': False,
    'update_available': False,
    'local_sha': '',
    'remote_sha': '',
    'local_branch': '',
    'error': '',
}


def _run_git(args: list[str]) -> str:
    """Run a git command, return stdout stripped."""
    import subprocess
    result = subprocess.run(
        ['git'] + args,
        capture_output=True, text=True,
        cwd=PROJECT_DIR, timeout=15,
    )
    return result.stdout.strip()


def _check_for_updates() -> dict:
    """Check if a newer commit exists on the remote branch.
    Safe to call from any thread. Returns the update state dict."""
    try:
        local_sha = _run_git(['rev-parse', 'HEAD'])
        branch = settings.update_branch or _run_git(['rev-parse', '--abbrev-ref', 'HEAD'])
        # Fetch remote ref for the configured branch
        remote_ref = _run_git(['ls-remote', 'origin', f'refs/heads/{branch}'])
        if not remote_ref:
            # Fallback: try remote HEAD (default branch, e.g. main)
            remote_ref = _run_git(['ls-remote', 'origin', 'HEAD'])
        remote_sha = remote_ref.split()[0] if remote_ref else ''
        dirty = bool(_run_git(['status', '--porcelain']))

        update_available = bool(remote_sha) and remote_sha != local_sha

        # If update available, fetch and get the commit log
        commit_log = ''
        if update_available:
            _run_git(['fetch', 'origin', branch])
            commit_log = _run_git(
                ['log', f'{local_sha}..{remote_sha}', '--oneline', '--no-decorate']
            )

        _update_state.update({
            'checked': True,
            'update_available': update_available,
            'local_sha': local_sha[:12],
            'remote_sha': remote_sha[:12] if remote_sha else '',
            'local_branch': branch,
            'dirty': dirty,
            'commit_log': commit_log,
            'error': '',
        })
    except Exception as e:
        _update_state.update({
            'checked': True,
            'error': str(e),
        })
    return _update_state


def _perform_update() -> str:
    """Pull latest changes, preserving local modifications.
    Returns status message."""
    try:
        # 1. Stash any local changes
        dirt = _run_git(['status', '--porcelain'])
        stashed = False
        if dirt:
            _run_git(['stash', 'push', '-m', 'auto-stash before update'])
            stashed = True

        # 2. Pull
        pull_out = _run_git(['pull', 'origin', settings.update_branch or 'main'])
        print(f'[git pull] {pull_out}')
        if 'Already up to date' in pull_out:
            msg = 'Already up to date.'
        elif 'Updating' in pull_out or 'Fast-forward' in pull_out:
            msg = f'Updated: {pull_out[:60]}'
        else:
            msg = pull_out[:100] if pull_out else 'Pull completed (unknown result).'

        # 3. Pop stash if it was applied
        if stashed:
            pop_out = _run_git(['stash', 'pop'])
            if 'CONFLICT' in pop_out:
                msg += ' ⚠ Stash conflicts — resolve manually.'

        log(f'Update: {msg}', level='SYS')
        return msg
    except Exception as e:
        log(f'Update failed: {e}', level='ERROR')
        return f'Update failed: {e}'


def _request_restart():
    """Write restart marker and shut down the server.
    The parent launcher (start.py) will detect the marker and restart.
    Exits non-zero to trigger systemd Restart=on-failure."""
    log('Restart requested — writing marker...', level='SYS')
    try:
        with open(RESTART_MARKER, 'w') as f:
            f.write('restart')
        print(f'\n  Restart marker written: {RESTART_MARKER}')
    except Exception as e:
        print(f'  Failed to write restart marker: {e}')
        log(f'Restart marker failed: {e}', level='ERROR')
    print('  Shutting down server...')
    ui.notify('Restarting...', type='info')
    time.sleep(0.5)
    app.shutdown()
    os._exit(1)  # non-zero exit to trigger systemd Restart=on-failure


# ── Helpers ───────────────────────────────────────────────────────────

def _fmt_size(b: int) -> str:
    if b < 1024:
        return f'{b}B'
    elif b < 1024**2:
        return f'{b/1024:.1f}KB'
    elif b < 1024**3:
        return f'{b/1024**2:.1f}MB'
    else:
        return f'{b/1024**3:.2f}GB'


def _fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f'{int(seconds)}s'
    elif seconds < 3600:
        return f'{int(seconds//60)}m {int(seconds%60)}s'
    else:
        return f'{int(seconds//3600)}h {int((seconds%3600)//60)}m'


def _fmt_speed(bps: float) -> str:
    if bps <= 0:
        return '—'
    if bps < 1024:
        return f'{bps:.0f} B/s'
    elif bps < 1024 ** 2:
        return f'{bps / 1024:.0f} KB/s'
    else:
        return f'{bps / 1024 ** 2:.1f} MB/s'


def scan_output_dir(model_name: str) -> dict:
    """Scan the output directory for a model and return stats about downloaded content."""
    out_dir = os.path.join(PROJECT_DIR, settings.output_dir, model_name)
    result = {
        'exists': os.path.isdir(out_dir),
        'post_folders': 0,
        'total_files': 0,
        'total_bytes': 0,
    }
    if not result['exists']:
        return result
    try:
        entries = sorted(os.listdir(out_dir))
    except PermissionError:
        return result
    for entry in entries:
        entry_path = os.path.join(out_dir, entry)
        if os.path.isdir(entry_path):
            result['post_folders'] += 1
            try:
                for root, dirs, files in os.walk(entry_path):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            result['total_bytes'] += os.path.getsize(fp)
                            result['total_files'] += 1
                        except OSError:
                            pass
            except PermissionError:
                pass
    return result


# ── Tab: Extract ──────────────────────────────────────────────────

def build_extract_tab():
    selected_files: list[str] = []
    last_filter_text = ['']
    _loose_files: list[str] = []

    def scan_input_files(folder_path: str) -> list[str]:
        files = []
        if not os.path.isdir(folder_path):
            return files
        for fname in os.listdir(folder_path):
            if fname.endswith('.html'):
                files.append(os.path.join(folder_path, fname))
        for root, dirs, fnames in os.walk(folder_path):
            for f in fnames:
                if f.endswith('.html') and os.path.join(root, f) not in files:
                    files.append(os.path.join(root, f))
        return files

    async def handle_upload(e):
        if not e.file or not e.file.name:
            return
        input_dir = settings.input_dir
        if not input_dir or not os.path.isdir(input_dir):
            ui.notify('Please set an input folder in Settings first', type='warning')
            return
        dest = os.path.join(input_dir, e.file.name)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            await e.file.save(dest)
            ui.notify(f'Uploaded: {e.file.name}', type='positive')
            scan_to_list()
        except Exception as ex:
            ui.notify(f'Upload failed: {ex}', type='negative')

    def scan_to_list():
        file_list_container.clear()
        folder = settings.input_dir
        if not folder or not os.path.isdir(folder):
            return
        html_files = scan_input_files(folder)
        ft = last_filter_text[0].strip().lower()
        if ft:
            html_files = [f for f in html_files if ft in os.path.basename(f).lower()]

        # Group by subdirectory: archive folders first, then loose files
        loose = []
        archived = {}  # dir_name -> [files]
        for fp in html_files:
            rel = os.path.relpath(fp, folder) if fp.startswith(folder) else fp
            if os.sep in rel:
                arch_dir = rel.split(os.sep)[0]
                archived.setdefault(arch_dir, []).append(fp)
            else:
                loose.append(fp)
        _loose_files.clear()
        _loose_files.extend(loose)

        if not html_files:
            with file_list_container:
                ui.label('No HTML files found.').classes('text-gray-400')
            return

        # Archive folders sorted by name
        for arch_name in sorted(archived.keys()):
            arch_files = archived[arch_name]
            n = len(arch_files)
            any_selected = any(f in selected_files for f in arch_files)
            with file_list_container:
                ui.label(f'{"☑" if any_selected else "☐"}  📁 {arch_name} ({n} file(s))') \
                    .classes('cursor-pointer') \
                    .on('click', lambda files=arch_files: _toggle_archived(files))

        # Loose files
        for fp in loose:
            checked = '☑' if fp in selected_files else '☐'
            lbl = os.path.relpath(fp, folder) if fp.startswith(folder) else fp
            with file_list_container:
                ui.label(f'{checked}  {lbl}') \
                    .classes('cursor-pointer') \
                    .on('click', lambda f=fp: _toggle_file(f))
    # end scan_to_list

    def _toggle_archived(files):
        all_selected = all(f in selected_files for f in files)
        if all_selected:
            for f in files:
                if f in selected_files:
                    selected_files.remove(f)
        else:
            for f in files:
                if f not in selected_files:
                    selected_files.append(f)
        scan_to_list()

    def _toggle_file(fp):
        if fp in selected_files:
            selected_files.remove(fp)
        else:
            selected_files.append(fp)
        scan_to_list()

    ui.label('URL Extraction').classes('text-xl font-bold')

    # ── Input folder row ──
    with ui.row().classes('w-full items-center'):
        ui.label('Input folder:').classes('text-sm w-24 shrink-0')
        folder_input = ui.input(value=settings.input_dir, placeholder='/path/to/html/files') \
            .props('size=40').classes('flex-grow')

    # ── Model name row ──
    with ui.row().classes('w-full items-center'):
        ui.label('Model name:').classes('text-sm w-24 shrink-0')
        model_name_input = ui.input(value='', placeholder='e.g. rincosplay') \
            .classes('flex-grow')

    # ── Upload row ──
    with ui.row().classes('w-full'):
        from nicegui.elements.upload import Upload
        upload = Upload(
            label='Upload HTML files',
            on_upload=handle_upload,
            multiple=True,
        ).props('accept=.html,.htm').classes('w-full')

    # ── Action buttons row ──
    with ui.row().classes('w-full items-center gap-1 flex-wrap'):
        ui.button('Scan', icon='refresh', on_click=scan_to_list).props('outline')
        ui.button('☑ All', on_click=lambda: (
            selected_files.clear(),
            [selected_files.append(f) for f in scan_input_files(settings.input_dir)] if settings.input_dir else None,
            scan_to_list()
        )[-1]).props('outline')
        ui.button('☐ None', on_click=lambda: selected_files.clear() or scan_to_list()).props('outline')
        ui.button('📄 Loose', on_click=lambda: (
            selected_files.clear(),
            [selected_files.append(f) for f in _loose_files],
            scan_to_list()
        )[-1]).props('outline')
        ui.button('🗑 Delete Selected', icon='delete') \
            .props('color=negative').classes('') \
            .on('click', lambda: do_confirm_delete())
        archive_check = ui.checkbox('Archive', value=True).classes('text-xs')

    # ── Filter row (own row, full width) ──
    with ui.row().classes('w-full items-center'):
        ui.label('Filter files:').classes('text-xs text-gray-400 w-20 shrink-0')
        filter_input = ui.input(placeholder='Type to filter by filename...') \
            .props('dense outlined').classes('flex-grow')
        def on_filter_change():
            last_filter_text[0] = filter_input.value or ''
            scan_to_list()
        filter_input.on('keyup', on_filter_change)
    file_list_container = ui.column().classes(
        'w-full text-xs bg-gray-900 p-2 rounded max-h-60 overflow-auto font-mono'
    )
    scan_to_list()  # populate file list on render

    def do_confirm_delete():
        if not selected_files:
            ui.notify('No files selected', type='warning')
            return
        with ui.dialog() as dialog, ui.card().classes('bg-gray-800 w-96'):
            ui.label(f'Delete {len(selected_files)} file(s)?').classes('text-lg font-bold')
            ui.separator()
            for fp in selected_files[:10]:
                ui.label(f'  {os.path.basename(fp)}').classes('text-xs font-mono text-gray-300')
            if len(selected_files) > 10:
                ui.label(f'  ... and {len(selected_files) - 10} more').classes('text-xs font-mono text-gray-500')
            ui.label('This cannot be undone.').classes('text-xs text-gray-500 mt-1')
            confirm_check = ui.checkbox('Yes, I am sure — delete these files').classes('text-sm text-red-400 mt-2')
            with ui.row().classes('w-full justify-end gap-2 mt-2'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Delete', icon='delete', on_click=lambda: (
                    do_delete_selected(), dialog.close()
                )).props('flat color=negative') \
                    .bind_enabled_from(confirm_check, 'value')
        dialog.open()

    def do_delete_selected():
        deleted = 0
        failed = 0
        for fp in selected_files:
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
                    deleted += 1
            except Exception:
                failed += 1
        selected_files.clear()
        log(f'Deleted {deleted} HTML file(s) from input', level='INFO')
        if failed:
            ui.notify(f'{failed} file(s) failed to delete', type='negative')
            log(f'{failed} file(s) delete failed', level='ERROR')
        scan_to_list()

    def do_extract():
        folder = settings.input_dir
        if not folder or not os.path.isdir(folder):
            ui.notify('Please set a valid input folder in Settings', type='warning')
            return
        model_name = model_name_input.value.strip()
        if not model_name:
            ui.notify('Please enter a model name', type='warning')
            return
        mode = 'normal'
        if archive_check.value:
            arch_dir = os.path.join(folder, model_name)
            os.makedirs(arch_dir, exist_ok=True)
        log(f'Starting extraction for {model_name} from {folder}', level='SYS')
        try:
            results = []
            for fp in selected_files:
                sr = extract_from_html(fp)
                results.append(sr)
                if archive_check.value:
                    try:
                        shutil.move(fp, os.path.join(arch_dir, os.path.basename(fp)))
                    except Exception as e:
                        log(f'Failed to archive {fp}: {e}', level='WARN')
            merged = merge_results(results, model_name)
            log(f'Extraction complete: {merged.total_posts} posts, {merged.total_urls} URLs')
            data_dir = os.path.join(PROJECT_DIR, settings.models_data_dir)
            os.makedirs(data_dir, exist_ok=True)
            paths = export_all_formats(merged, data_dir, model_name)
            registry.register_extraction(model_name, 'simpcity', merged.total_urls, merged.total_posts, file_paths=paths)
            log(f'Registered {model_name} in registry', level='SYS')
            ui.notify(f'Extraction complete: {merged.total_posts} posts, {merged.total_urls} URLs', type='positive')
            selected_files.clear()
            scan_to_list()
        except Exception as e:
            log(f'Extraction failed: {e}', level='ERROR')
            ui.notify(f'Extraction error: {e}', type='negative')
            import traceback
            traceback.print_exc()

    ui.button('Extract URLs', icon='auto_awesome', on_click=do_extract) \
        .props('size=md color=primary').classes('mt-2')

    # Allow scanning with Enter on folder input
    folder_input.on('keydown.enter', scan_to_list)
    folder_input.on('change', lambda: setattr(settings, 'input_dir', folder_input.value) or settings.save())


# ── Tab: Import / Export ──────────────────────────────────────────────

def build_import_export_tab():
    ui.label('Import / Export').classes('text-xl font-bold')
    ui.separator()

    # ── Export section ────────────────────────────────────────
    ui.label('Export').classes('text-md font-bold mt-2')

    with ui.row().classes('w-full items-center gap-2'):
        model_select = ui.select(
            options=list(registry.models.keys()),
            label='Model',
            value=None,
            with_input=True,
        ).classes('min-w-[200px]')
        format_sel = ui.select(
            options=['JSON (posts.json)', 'All URLs (all_urls.txt)'],
            label='Format',
            value='JSON (posts.json)',
        ).classes('min-w-[160px]')

        def do_export():
            mname = model_select.value
            if not mname:
                ui.notify('Select a model first', type='warning')
                return
            entry = registry.models.get(mname)
            if not entry:
                ui.notify(f'Model "{mname}" not found in registry', type='negative')
                return

            modes = entry.get('modes', {})
            normal = modes.get('normal', {})
            fp = normal.get('file_paths', {})
            fmt = format_sel.value or 'JSON (posts.json)'

            if fmt.startswith('JSON'):
                path = fp.get('json', '')
                label = 'JSON'
            else:
                path = fp.get('all_urls', '')
                label = 'All URLs TXT'

            if not path or not os.path.exists(path):
                ui.notify(f'No {label} file found for {mname} — run a normal scan first', type='warning')
                return

            log(f'Exported {label}: {path}')
            ui.download(path)

        ui.button('Export', icon='file_download', on_click=do_export).props('outline')

    # ── Import section ────────────────────────────────────────
    ui.separator().classes('my-4')
    ui.label('Import JSON').classes('text-md font-bold mt-2')
    ui.label('Upload a posts.json file to reconstruct a model. TXT files (all_urls and per-post) are automatically generated.').classes('text-xs text-gray-400 mb-2')

    import_status = ui.label('').classes('text-sm mt-1')

    async def handle_upload(e):
        if not e.file:
            ui.notify('No file received', type='warning')
            return
        content = await e.file.read()
        try:
            text = content.decode('utf-8')
            data = json.loads(text)

            # Validate basic structure
            if not isinstance(data, dict) or 'posts' not in data:
                ui.notify('Invalid format: expected a JSON object with a "posts" array', type='negative')
                return
            if 'model_name' not in data:
                data['model_name'] = e.file.name.replace('.json', '').replace('_posts', '').replace('_all_urls', '')
                ui.notify(f'No model_name in JSON — using "{data["model_name"]}"', type='warning')

            # Reconstruct ScanResult directly (import_json() expects filepath not dict, so bypass it)
            result = ScanResult.from_dict(data)
            if not result or not result.posts:
                ui.notify('Import failed: no posts found', type='negative')
                return

            global last_merged, last_output_paths
            last_merged = result
            mname = result.model_name or data.get('model_name', 'imported')
            data_dir = os.path.join(PROJECT_DIR, settings.models_data_dir)
            paths = export_all_formats(result, data_dir, mname)

            # Also register in registry so it appears in Export list
            registry.register_extraction(mname, result.forum_source or 'imported',
                                         result.total_urls, result.total_posts,
                                         file_paths=paths)
            # Refresh model dropdown
            model_select.options = list(registry.models.keys())

            last_output_paths = paths
            msg = f'Imported: {result.total_posts} posts, {result.total_urls} URLs'
            import_status.text = msg
            ui.notify(msg, type='positive')
            log(f'Imported JSON: {result.total_posts} posts, {result.total_urls} URLs — files in {os.path.dirname(paths["json"])}')

        except json.JSONDecodeError as ex:
            ui.notify(f'Invalid JSON: {ex}', type='negative')
        except Exception as ex:
            ui.notify(f'Import error: {ex}', type='negative')
            import traceback
            traceback.print_exc()

    from nicegui.elements.upload import Upload as Ug
    Ug(label='Upload posts.json file', on_upload=handle_upload).props('accept=.json').classes('w-full')

    # Show paths from last import
    @ui.refreshable
    def _show_last_paths():
        if last_output_paths:
            p = last_output_paths
            ui.separator().classes('my-2')
            ui.label('Last import output:').classes('text-xs text-gray-400')
            for key, label in [('all_urls', 'All URLs'), ('json', 'JSON'), ('posts_dir', 'Posts directory')]:
                path = p.get(key, '')
                if path:
                    ui.label(f'  {label}: {os.path.basename(path)}').classes('text-xs font-mono text-gray-500')

    _show_last_paths()


# ── Tab: Sort / Deduplicate ───────────────────────────────────────────

def build_sort_tab():
    ui.label('Sort & Deduplicate').classes('text-xl font-bold')
    ui.separator()

    # Resolve paths
    output_base = settings.output_dir or os.path.join(PROJECT_DIR, 'output')
    dest_base = settings.sort_dir or os.path.expanduser('~/Downloads')
    if not os.path.isabs(dest_base):
        dest_base = os.path.join(PROJECT_DIR, dest_base)

    # Model folder selector
    model_selector = ui.select(
        options=[],
        label='Model folder (from output/)',
        value=None,
        with_input=True,
    ).classes('w-full')

    def refresh_model_list():
        models = []
        if os.path.isdir(output_base):
            models = sorted([
                d for d in os.listdir(output_base)
                if os.path.isdir(os.path.join(output_base, d))
            ])
        model_selector.options = models
        model_selector.value = models[0] if models else None

    refresh_model_list()

    with ui.row().classes('w-full items-center gap-2 mt-1'):
        ui.button('Refresh list', icon='refresh', on_click=refresh_model_list).props('outline')
        ui.label(f'Source: {output_base}/').classes('text-xs text-gray-400')
        ui.label(f'Dest: {dest_base}/').classes('text-xs text-gray-400')

    action_sel = ui.select(
        options=['copy', 'move'],
        label='Action',
        value='copy',
    ).classes('w-full')
    ui.label('Use Copy for testing, Move for final run.').classes('text-xs text-gray-400')

    dedup_check = ui.checkbox('Remove duplicates (quality-aware)', value=True).classes('text-xs')

    # Status
    progress_label = ui.label('').classes('text-sm')
    result_label = ui.label('').classes('text-sm')

    # ── Progress bar ──
    sort_progress_bar = ui.linear_progress(value=0).props('size=24px color=orange track-color=gray-700').classes('w-full mt-1').set_visibility(False)

    # ── Verbose log output (newest at top) ──
    sort_log_entries: list[str] = []

    def _add_sort_log(msg: str):
        ts = time.strftime('%H:%M:%S')
        entry = f'[{ts}] {msg}'
        sort_log_entries.insert(0, entry)  # newest at top
        if len(sort_log_entries) > 200:
            sort_log_entries.pop()  # keep bounded
        if sort_log_textarea is not None:
            sort_log_textarea.set_value('\n'.join(sort_log_entries))
        log(msg, level='INFO')

    sort_log_textarea = ui.textarea().classes('w-full font-mono text-xs').props('rows=12 readonly')
    ui.label('Verbose log shows each file as it is processed.').classes('text-xs text-gray-400')

    # ── Controls row ──
    sort_btn = ui.button('Sort', icon='sort').props('color=warning').classes('mt-2')
    cancel_sort_btn = ui.button('Cancel', icon='cancel').props('color=negative outline').set_visibility(False)

    _sort_cancel = threading.Event()
    _sort_in_progress = [False]

    def _sort_worker(model_name, action, dedup):
        _add_sort_log(f'Starting sort: {model_name} ({action})')
        _add_sort_log(f'  Source: {os.path.join(output_base, model_name)}')
        _add_sort_log(f'  Dest:   {os.path.join(dest_base, model_name)}')

        try:
            def on_progress(current, total, fname):
                if _sort_cancel.is_set():
                    return
                _add_sort_log(f'[{current}/{total}] {fname}')
                if total > 0:
                    sort_progress_bar.set_value(current / total)
                    sort_progress_bar.set_visibility(True)
                # Update status with percentage
                pct = int(current / total * 100) if total else 0
                progress_label.text = f'Sorting {model_name}... {pct}% ({current}/{total})'

            result = sort_model_folder(
                model_name=model_name,
                source_base=output_base,
                dest_base=dest_base,
                action=action,
                dedup=dedup,
                progress_callback=on_progress,
            )

            if _sort_cancel.is_set():
                _add_sort_log('↺ Cancelled by user')
                return

            parts = []
            total_files = result.get('total_files', 0)
            total_proc = result.get('total_processed', 0)
            parts.append(f'Total: {total_files} files ({total_proc} processed)')
            img_t = result.get('images_total', result.get('images_moved', 0))
            vid_t = result.get('videos_total', result.get('videos_moved', 0))
            unk_t = result.get('unknown_total', result.get('unknown_moved', 0))
            parts.append(f'Images: {img_t}')
            parts.append(f'Videos: {vid_t}')
            parts.append(f'Unknown: {unk_t}')
            _add_sort_log(f'Images: {img_t}  |  Videos: {vid_t}  |  Unknown: {unk_t}')
            if result.get('duplicates_removed'):
                parts.append(f'Duplicates: {result["duplicates_removed"]}')
                _add_sort_log(f'Duplicates removed: {result["duplicates_removed"]}')
            if result.get('already_exist'):
                parts.append(f'Already present: {result["already_exist"]}')
                _add_sort_log(f'Already present: {result["already_exist"]}')
            if result.get('errors'):
                for e in result['errors']:
                    _add_sort_log(f'⚠ {e}')
            _add_sort_log(f'✓ Sort complete.')
            result_label.text = ' | '.join(parts) if parts else 'Nothing to sort.'
            _sort_in_progress[0] = False
        except Exception as e:
            _add_sort_log(f'✗ Sort failed: {e}')
            _sort_in_progress[0] = False

    def _start_sort():
        model_name = model_selector.value
        if not model_name:
            ui.notify('Select a model folder first', type='warning')
            return

        action = action_sel.value
        dedup = dedup_check.value

        _sort_cancel.clear()
        _sort_in_progress[0] = True
        progress_label.text = f'Sorting {model_name}...'
        result_label.text = ''
        sort_log_entries.clear()
        if sort_log_textarea is not None:
            sort_log_textarea.set_value('')
        sort_btn.disable()
        cancel_sort_btn.set_visibility(True)

        t = threading.Thread(target=_sort_worker, args=(model_name, action, dedup), daemon=True)
        t.start()

    def _cancel_sort():
        _sort_cancel.set()
        cancel_sort_btn.disable()

    # Poll for completion
    def _poll_sort():
        if not _sort_in_progress[0]:
            if sort_btn.enabled is False:
                sort_btn.enable()
                cancel_sort_btn.set_visibility(False)
                cancel_sort_btn.enable()
                progress_label.text = 'Idle'
                sort_progress_bar.set_visibility(False)

    ui.timer(0.3, _poll_sort, active=True)

    sort_btn.on('click', _start_sort)
    cancel_sort_btn.on('click', _cancel_sort)


# ── Tab: Rename ───────────────────────────────────────────────────────

def build_rename_tab():
    ui.label('Sequential File Renamer').classes('text-xl font-bold')
    ui.separator()

    # Resolve destination base (same as Sort tab)
    dest_base = settings.sort_dir or os.path.expanduser('~/Downloads')
    if not os.path.isabs(dest_base):
        dest_base = os.path.join(PROJECT_DIR, dest_base)

    # Folder picker — discover subdirectories under dest_base
    with ui.row().classes('w-full items-center gap-2'):
        base_input = ui.input('Base name:', placeholder='e.g. Rincospl_Images').classes('flex-grow')
        use_root_check = ui.checkbox('Use folder name as base', value=True)
    status_label = ui.label('').classes('text-sm')

    folder_selector = ui.select(
        options=[],
        label='Select folder (from sorted output)',
        value=None,
        with_input=True,
        on_change=lambda e: _on_folder_change(e.value),
    ).classes('w-full')

    def _on_folder_change(new_value: str):
        """Handle folder selection change — update file count + auto-set base name."""
        update_file_count(new_value)
        if use_root_check.value and new_value:
            root = new_value.split('/')[0]
            base_input.set_value(root)

    use_root_check.on('change', lambda: (
        base_input.set_value(folder_selector.value.split('/')[0] if use_root_check.value and folder_selector.value else ''),
        base_input.disable() if use_root_check.value else base_input.enable()
    ))

    file_count_label = ui.label('').classes('text-xs text-gray-400')

    def refresh_folder_list():
        entries = []
        if os.path.isdir(dest_base):
            # Collect all model dirs and their images/videos subdirs
            for entry in sorted(os.listdir(dest_base)):
                entry_path = os.path.join(dest_base, entry)
                if not os.path.isdir(entry_path):
                    continue
                # Skip hidden dirs
                if entry.startswith('.'):
                    continue
                # Add the model folder itself
                entries.append(entry)
                # Add images/videos subdirs if they exist
                for sub in ('images', 'videos'):
                    sub_path = os.path.join(entry_path, sub)
                    if os.path.isdir(sub_path):
                        entries.append(f'{entry}/{sub}')
        folder_selector.options = entries
        if entries:
            folder_selector.value = entries[0]
        else:
            folder_selector.value = None
            file_count_label.text = 'No folders found — sort some models first.'

    def update_file_count(folder_rel):
        if not folder_rel:
            file_count_label.text = ''
            return
        full = os.path.join(dest_base, folder_rel)
        if not os.path.isdir(full):
            file_count_label.text = ''
            return
        files = [f for f in os.listdir(full) if os.path.isfile(os.path.join(full, f))]
        # Also count files in subdirectories
        subdirs = [d for d in os.listdir(full) if os.path.isdir(os.path.join(full, d))]
        sub_files = 0
        for sd in subdirs:
            sd_path = os.path.join(full, sd)
            sub_files += len([f for f in os.listdir(sd_path) if os.path.isfile(os.path.join(sd_path, f))])
        total = len(files) + sub_files
        sub_info = f' (+{sub_files} in subdirs)' if sub_files else ''
        file_count_label.text = f'Files: {total}{sub_info}'

    refresh_folder_list()

    with ui.row().classes('w-full items-center gap-2 mt-1'):
        ui.button('Refresh', icon='refresh', on_click=refresh_folder_list).props('outline').classes('')
        ui.label(f'Base: {dest_base}/').classes('text-xs text-gray-400')

    # ── Progress bar ──
    rename_progress_bar = ui.linear_progress(value=0).props('size=24px color=positive track-color=gray-700').classes('w-full mt-1').set_visibility(False)

    # ── Verbose log output (newest at top) ──
    rename_log_entries: list[str] = []

    def _add_rename_log(msg: str):
        ts = time.strftime('%H:%M:%S')
        entry = f'[{ts}] {msg}'
        rename_log_entries.insert(0, entry)  # newest at top
        if len(rename_log_entries) > 200:
            rename_log_entries.pop()  # keep bounded
        if rename_log_textarea is not None:
            rename_log_textarea.set_value('\n'.join(rename_log_entries))
        log(msg, level='INFO')

    rename_log_textarea = ui.textarea().classes('w-full font-mono text-xs').props('rows=10 readonly')
    ui.label('Verbose log shows each file as it is renamed.').classes('text-xs text-gray-400')

    def do_rename():
        folder_rel = folder_selector.value
        if not folder_rel:
            ui.notify('Select a folder first', type='warning')
            return
        base = base_input.value.strip()
        if not base:
            ui.notify('Enter a base name', type='warning')
            return

        folder = os.path.join(dest_base, folder_rel)
        if not os.path.isdir(folder):
            ui.notify(f'Folder not found: {folder}', type='negative')
            return

        rename_log_entries.clear()
        if rename_log_textarea is not None:
            rename_log_textarea.set_value('')
        _add_rename_log(f'Starting rename: {folder_rel} → base "{base}"')

        # Find target folders: if root selected, process subdirs too
        target_folders = [folder]
        if os.path.isdir(folder):
            subdirs = sorted([d for d in os.listdir(folder)
                              if os.path.isdir(os.path.join(folder, d))])
            direct_files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
            if not direct_files and subdirs:
                # Root folder with subdirs but no direct files → process subdirs
                target_folders = [os.path.join(folder, sd) for sd in subdirs]
                _add_rename_log(f'Found subdirectories: {", ".join(subdirs)} — processing each')

        # Pre-count total files across all target folders for progress bar
        try:
            _rename_total = 0
            for tf in target_folders:
                if os.path.isdir(tf):
                    _rename_total += len([f for f in os.listdir(tf) if os.path.isfile(os.path.join(tf, f))])
        except Exception:
            _rename_total = 0
        _rename_done = [0]
        rename_progress_bar.set_value(0)
        rename_progress_bar.set_visibility(True)

        try:
            _combined_result = {'renamed_count': 0, 'skipped_count': 0, 'errors': []}

            def on_progress(fname, action, detail=None):
                _rename_done[0] += 1
                if _rename_total > 0:
                    rename_progress_bar.set_value(_rename_done[0] / _rename_total)
                    status_label.text = f'Renaming... {int(_rename_done[0] / _rename_total * 100)}% ({_rename_done[0]}/{_rename_total})'
                if action == 'rename':
                    _add_rename_log(f'  → {fname} renamed to {detail}')
                elif action == 'skip':
                    _add_rename_log(f'  · {fname} (already matches pattern, skipped)')
                elif action == 'error':
                    _add_rename_log(f'  ⚠ {fname}: {detail}')

            for tf in target_folders:
                tf_name = os.path.basename(tf) if len(target_folders) > 1 else folder_rel
                if len(target_folders) > 1:
                    _add_rename_log(f'--- Processing: {folder_rel}/{tf_name} ---')
                result = rename_files(tf, base, progress_callback=on_progress)
                _combined_result['renamed_count'] += result.get('renamed_count', 0)
                _combined_result['skipped_count'] += result.get('skipped_count', 0)
                _combined_result['errors'].extend(result.get('errors', []))

            rename_progress_bar.set_value(1.0)
            ui.timer(0.5, lambda: rename_progress_bar.set_visibility(False), once=True)
            parts = []
            rn = _combined_result['renamed_count']
            sk = _combined_result['skipped_count']
            if rn:
                parts.append(f'Renamed: {rn} files')
            if sk:
                parts.append(f'Skipped (already match pattern): {sk}')
            status_label.text = ' | '.join(parts) if parts else 'No files to rename.'
            if _combined_result['errors']:
                for e in _combined_result['errors']:
                    _add_rename_log(f'⚠ {e}')
            _add_rename_log(f'✓ Rename complete.')
            if rn:
                ui.notify(f'Renamed {rn} files', type='positive')
            update_file_count(folder_rel)
        except Exception as e:
            rename_progress_bar.set_visibility(False)
            _add_rename_log(f'✗ Rename failed: {e}')
            ui.notify(f'Rename failed: {e}', type='negative')

    ui.button('Rename Files', icon='drive_file_rename_outline', on_click=do_rename).props('color=positive').classes('mt-2')
    ui.separator().classes('my-4')
    ui.label('Files are renamed sequentially as: {base}_{number}.{ext}. Already-matching files are skipped — safe to re-run.').classes('text-xs text-gray-400')


# ── Tab: Crawler ─────────────────────────────────────────────────────

_log_textarea = None  # ref for crawler log output
_crawler_log = []  # list of log strings
_crawler_log_len = [0]  # for change detection in timer


def build_crawler_tab():
    global _log_textarea

    if not settings.cookies_json or not settings.cookies_json.strip():
        ui.label('Crawler requires cookies').classes('text-xl font-bold text-orange-400')
        ui.separator()
        ui.label('Go to Settings → Cookies to paste your EditThisCookie JSON export.').classes('text-sm')
        ui.label('You must be logged into SimpCity in your browser first.').classes('text-sm')
        ui.label(
            'DO NOT use your main SimpCity account. Account can be flagged as a bot and be permanently banned!'
        ).classes('text-sm font-bold text-red-400 mt-2')
        return

    ui.label('Crawler').classes('text-xl font-bold')
    ui.separator()

    ui.label('Paste a SimpCity thread URL to download all its HTML pages into the input folder.').classes('text-sm text-gray-400')
    ui.label('Files are saved loose in the input directory, ready for extraction.').classes('text-sm text-gray-400')

    url_input = ui.input(
        'Thread URL',
        placeholder='https://simpcity.cr/threads/<slug>.<thread-id>/',
    ).classes('w-full mt-2').props('clearable')

    status_label = ui.label('').classes('text-sm mt-1')
    fetch_btn = ui.button('Start Fetching', icon='cloud_download').props('color=positive').classes('mt-1')
    cancel_btn = ui.button('Cancel', icon='cancel', color='red').props('disabled').classes('mt-1 ml-2')

    # ── Log output area ──
    with ui.row().classes('w-full mt-2 items-center gap-2'):
        ui.label('Output Log').classes('text-sm font-bold')
        log_badge = ui.label('').classes('text-xs text-blue-400')
    _log_textarea = ui.textarea().classes('w-full font-mono text-xs').props('rows=12 readonly')

    # Poller: updates textarea from main thread only when _crawler_log changes
    def _poll_crawler_log():
        if _log_textarea is not None and len(_crawler_log) != _crawler_log_len[0]:
            _crawler_log_len[0] = len(_crawler_log)
            _log_textarea.set_value('\n'.join(_crawler_log))
    ui.timer(0.3, _poll_crawler_log, active=True)

    def _refresh_crawler_log():
        """Force a UI refresh — called on clear or initial display."""
        if _log_textarea is not None:
            _log_textarea.set_value('\n'.join(_crawler_log))

    def add_crawler_log(msg: str):
        """Append a log entry from any thread. The UI timer picks it up."""
        _crawler_log.insert(0, msg)  # newest at top
        if len(_crawler_log) > 500:
            _crawler_log.pop()

    # Seed with any existing logs (e.g. from a previous session)
    _refresh_crawler_log()

    def _crawl_worker(url: str):
        nonlocal status_label, fetch_btn, cancel_btn
        _crawl_cancel.clear()
        add_crawler_log(f'=== Starting crawl: {url} ===')

        result = crawl_thread(
            url=url,
            cookies_json=settings.cookies_json,
            input_dir=os.path.join(PROJECT_DIR, settings.input_dir),
            request_delay=settings.request_delay,
            user_agent=settings.user_agent,
            max_pages=50,
            cancel_check=lambda: _crawl_cancel.is_set(),
            log_callback=add_crawler_log,
        )

        if 'error' in result:
            add_crawler_log(f'ERROR: {result["error"]}')
            ui.notify(f'Crawl failed: {result["error"]}', type='negative')
        elif _crawl_cancel.is_set():
            add_crawler_log('=== Cancelled by user ===')
        else:
            add_crawler_log(
                f'=== Done: {result["pages"]}/{result["total"]} pages, '
                f'{len(result.get("errors", []))} errors ==='
            )
            if result.get('errors'):
                for err in result['errors']:
                    add_crawler_log(f'  ERR: {err}')

        global _crawl_in_progress
        _crawl_in_progress = False
        fetch_btn.enable()
        cancel_btn.disable()
        status_label.text = 'Idle'
        log_badge.text = ''

    def _start_crawl():
        global _crawl_in_progress
        url = url_input.value.strip() if url_input else ''
        if not url:
            ui.notify('Enter a SimpCity thread URL', type='warning')
            return
        from core.crawler import parse_thread_id
        if not parse_thread_id(url):
            ui.notify('Could not parse thread ID from URL', type='negative')
            return
        if _crawl_in_progress:
            ui.notify('Crawl already in progress', type='warning')
            return

        _crawl_in_progress = True
        fetch_btn.disable()
        cancel_btn.enable()
        status_label.text = 'Crawling...'
        log_badge.text = '⏳ Running...'
        _crawler_log.clear()
        _crawler_log_len[0] = 0
        _refresh_crawler_log()

        t = threading.Thread(target=_crawl_worker, args=(url,), daemon=True)
        t.start()

    def _cancel_crawl():
        _crawl_cancel.set()
        cancel_btn.disable()

    fetch_btn.on('click', _start_crawl)
    cancel_btn.on('click', _cancel_crawl)


# ── Tab: Settings ─────────────────────────────────────────────────────

def build_settings_tab():
    ui.label('Settings').classes('text-xl font-bold')
    ui.separator()

    # ── Defaults ──
    ui.label('Defaults').classes('text-md font-bold mt-2')
    title_input = ui.input('Site title (shown on landing page & tab)', value=settings.site_title).classes('w-full')
    inp_dir = ui.input('Input directory', value=settings.input_dir or '').classes('w-full')
    data_dir = ui.input('Models data directory (TXT/JSON)', value=settings.models_data_dir or '').classes('w-full')
    out_dir = ui.input('Output directory (media downloads)', value=settings.output_dir or '').classes('w-full')
    sort_dir = ui.input('Default sort folder', value=settings.sort_dir or '').classes('w-full')
    mode_sel = ui.select(options=['normal', 'reverse', 'no_filter'],
                          label='Default extraction mode',
                          value=settings.extraction_mode or 'normal').classes('w-full')
    concurrency_input = ui.number(label='Max concurrent downloads',
                               value=settings.max_concurrent_downloads,
                               min=1, max=10, step=1).classes('w-full')
    speed_input = ui.number(label='Max Download Speed (MB/s)',
                             value=settings.max_speed_mbps,
                             min=0, max=10000, step=1).classes('w-full')
    ui.label('0 = unlimited').classes('text-xs text-gray-400 -mt-2 mb-2')
    streaming_toggle = ui.checkbox('Pre-resolve next batch while downloading (faster)',
                                    value=settings.streaming_resolve).classes('w-full mt-1')

    def do_save():
        settings.site_title = title_input.value or ''
        settings.input_dir = inp_dir.value
        settings.models_data_dir = data_dir.value
        settings.output_dir = out_dir.value
        settings.sort_dir = sort_dir.value
        settings.extraction_mode = mode_sel.value
        settings.max_concurrent_downloads = int(concurrency_input.value)
        settings.max_speed_mbps = int(speed_input.value)
        settings.streaming_resolve = streaming_toggle.value
        settings.save()
        ui.notify('Settings saved!', type='positive')
        log('Settings saved', level='SYS')

    ui.button('Save Settings', icon='save', on_click=do_save).props('color=positive').classes('mt-2')

    # ── Updates & Restart ──
    ui.separator().classes('my-4')
    ui.label('Updates & Restart').classes('text-md font-bold mt-2')

    update_status = ui.label('').classes('text-sm')
    update_detail = ui.label('').classes('text-xs text-gray-400')
    update_commits = ui.label('').classes(
        'text-xs font-mono text-yellow-400 whitespace-pre-wrap max-w-lg'
    )

    # Branch selector
    def _get_branches() -> list[str]:
        try:
            out = _run_git(['branch', '-a', '--format', '%(refname:short)'])
            branches = set()
            for b in out.strip().split('\n'):
                b = b.strip()
                if not b:
                    continue
                if b.startswith('origin/'):
                    b = b[7:]
                branches.add(b)
            result = sorted(branches, key=lambda x: (x != 'main', x != 'dev', x))
            return result if result else ['main', 'dev']
        except Exception:
            return ['main', 'dev']

    _available_branches = _get_branches()
    _default_branch = settings.update_branch or _available_branches[0]
    if _default_branch not in _available_branches:
        _default_branch = _available_branches[0]
        settings.update_branch = _default_branch
        settings.save()

    branch_select = ui.select(
        _available_branches,
        value=_default_branch,
        label='Update branch',
    ).classes('w-48 mt-2')

    def _on_branch_change():
        settings.update_branch = branch_select.value
        settings.save()
        log(f'Update branch changed to: {branch_select.value}', level='INFO')
        ui.notify(f'Update branch set to {branch_select.value}', type='info')

    branch_select.on('change', _on_branch_change)

    def _do_manual_check():
        update_status.text = 'Checking for updates...'
        update_detail.text = ''
        update_commits.text = ''
        ui.notify('Checking for updates...', type='info')
        state = _check_for_updates()
        if state.get('error'):
            update_status.text = f'⚠ Check failed: {state["error"]}'
            update_detail.text = ''
        elif state.get('update_available'):
            update_status.text = f'✓ Update available on {state["local_branch"]}'
            update_detail.text = f'{state["local_sha"]} → {state["remote_sha"]}'
            cl = state.get('commit_log', '')
            if cl:
                update_commits.text = cl[:500]
            ui.notify('Update available!', type='warning')
        else:
            update_status.text = '✓ Up to date'
            update_detail.text = f'Branch: {state.get("local_branch", "?")} @ {state.get("local_sha", "?")}'
            ui.notify('Up to date', type='positive')

    def _do_update():
        msg = _perform_update()
        ui.notify(msg, type='positive' if 'failed' not in msg.lower() else 'negative')
        update_status.text = msg[:80]
        if not msg.startswith('Update failed'):
            ui.timer(2.0, _request_restart, active=True, once=True)

    with ui.row().classes('gap-2'):
        ui.button('Check for Updates', icon='refresh', on_click=_do_manual_check).props('outline')
        ui.button('Update & Restart', icon='system_update_alt', on_click=_do_update).props('color=warning')

    # URL Filters section
    ui.separator().classes('my-4')
    ui.label('URL Filters').classes('text-md font-bold')
    ui.label('Edit pattern lists used by the extractor. Changes take effect on the next scan.').classes('text-sm mb-2')
    ui.label('Keep patterns (url_patterns.json) — URLs matching any line are INCLUDED in extraction.').classes('text-xs text-green-400')
    ui.label('Skip patterns (skip_url.json) — URLs matching any line are EXCLUDED (thumbnails, avatars, social links).').classes('text-xs text-orange-400')

    def make_saver(fname):
        def saver(txt):
            try:
                arr = json.loads(txt)
                if not isinstance(arr, list):
                    raise ValueError('Must be a JSON array')
                with open(fname, 'w') as f:
                    json.dump(arr, f, indent=2)
                ui.notify(f'{os.path.basename(fname)} saved ({len(arr)} entries)', type='positive')
                log(f'Filter file saved: {fname}', level='SYS')
            except Exception as ex:
                ui.notify(f'Invalid JSON: {ex}', type='negative')
        return saver

    incl_path = os.path.join(PROJECT_DIR, 'url_patterns.json')
    incl_text = ''
    if os.path.exists(incl_path):
        with open(incl_path) as f:
            incl_text = f.read()
    else:
        incl_text = '[]'
    ui.label('Keep patterns — url_patterns.json').classes('text-xs')
    incl_edit = ui.textarea(value=incl_text).props('w-full font-mono text-xs rows=6').classes('w-full')
    ui.button('Save url_patterns.json', on_click=lambda: make_saver(incl_path)(incl_edit.value)) \
        .props('outline')

    excl_path = os.path.join(PROJECT_DIR, 'skip_url.json')
    excl_text = ''
    if os.path.exists(excl_path):
        with open(excl_path) as f:
            excl_text = f.read()
    else:
        excl_text = '[]'
    ui.label('Skip patterns — skip_url.json').classes('text-xs')
    excl_edit = ui.textarea(value=excl_text).props('w-full font-mono text-xs rows=6').classes('w-full')
    ui.button('Save skip_url.json', on_click=lambda: make_saver(excl_path)(excl_edit.value)) \
        .props('outline')

    # ── Cookies, API & Crawler Settings ──
    ui.separator().classes('my-4')
    ui.label('Cookies, API Keys & Crawler Settings').classes('text-md font-bold')
    ui.label(
        'DO NOT use your main SimpCity account. Account can be flagged as a bot and be permanently banned!'
    ).classes('text-sm font-bold text-red-400')

    # ── SimpCity Cookies ──
    ui.label('SimpCity Cookies').classes('text-sm font-bold mt-2')

    cookie_status = ui.label('').classes('text-sm mb-1')
    cookies_visible = [False]

    def _update_cookie_status():
        if settings.cookies_json:
            try:
                data = json.loads(settings.cookies_json)
                if isinstance(data, list):
                    domains = set(e.get('domain', '?') for e in data if isinstance(e, dict))
                    cookie_status.text = f'Cookies saved: {len(data)} entries for {len(domains)} domain(s)'
                    cookie_status.classes('text-green-400', replace=False)
                    return
            except Exception:
                pass
            cookie_status.text = 'Cookies saved (invalid JSON format — re-paste)'
            cookie_status.classes('text-red-400', replace=False)
        else:
            cookie_status.text = 'No cookies saved'
            cookie_status.classes('text-gray-400', replace=False)

    _update_cookie_status()

    cookie_editor = ui.textarea(
        value='',
        placeholder='Paste EditThisCookie JSON array here... '
                    '[{"domain":"simpcity.cr","name":"xf_user","value":"...", ...}]',
    ).classes('w-full font-mono text-xs').props('rows=8')

    def _toggle_cookies():
        if cookies_visible[0]:
            cookie_editor.set_value('')
            cookies_visible[0] = False
            toggle_btn.text = 'Show saved cookies'
        else:
            cookie_editor.set_value(settings.cookies_json or '')
            cookies_visible[0] = True
            toggle_btn.text = 'Hide cookies'

    def do_save_cookies():
        raw = cookie_editor.value
        if not raw or not raw.strip():
            ui.notify('Nothing to save — paste cookies first', type='warning')
            return
        result = validate_cookie_json(raw)
        if isinstance(result, str):
            ui.notify(f'Cookie error: {result}', type='negative')
            return
        settings.cookies_json = raw.strip()
        settings.save()
        _update_cookie_status()
        ui.notify(f'Cookies saved ({len(result)} entries)', type='positive')
        cookie_editor.set_value('')
        cookies_visible[0] = False
        toggle_btn.text = 'Show saved cookies'

    def _clear_cookies():
        settings.cookies_json = ''
        settings.save()
        _update_cookie_status()
        cookie_editor.set_value('')
        cookies_visible[0] = False
        toggle_btn.text = 'Show saved cookies'
        ui.notify('Cookies cleared', type='positive')

    with ui.row().classes('gap-2'):
        toggle_btn = ui.button('Show saved cookies', icon='visibility', on_click=_toggle_cookies).props('flat')
        ui.button('Save Cookies', icon='save', color='positive', on_click=do_save_cookies)
        ui.button('Clear Cookies', icon='delete', color='red',
                  on_click=_clear_cookies).props('flat')

    # ── Crawler Settings ──
    ui.label('Crawler Settings').classes('text-sm font-bold mt-4')

    delay_input = ui.number(
        'Request Delay (seconds)',
        value=settings.request_delay,
        min=1.0, max=30.0, step=0.5,
    ).classes('w-64 mt-2')

    ua_input = ui.textarea(
        'User-Agent',
        value=settings.user_agent,
    ).classes('w-full font-mono text-xs mt-2').props('rows=3')

    def do_save_crawler():
        settings.request_delay = float(delay_input.value)
        settings.user_agent = ua_input.value.strip()
        settings.save()
        ui.notify('Crawler settings saved', type='positive')

    ui.button('Save Crawler Settings', icon='save', on_click=do_save_crawler).props('flat')

    # ── Pixeldrain API Key ──
    ui.label('Pixeldrain API Key').classes('text-sm font-bold mt-4')
    ui.label('API key from your account settings (pixeldrain.com/settings). '
             'Free-tier keys bypass hotlink/CAPTCHA gates.').classes('text-sm mb-2')
    pixeldrain_key_input = ui.input(
        'Pixeldrain API Key',
        value=settings.pixeldrain_api_key,
        password=True,
        password_toggle_button=True,
    ).classes('w-full').props('clearable')

    def _save_pixeldrain_key():
        settings.pixeldrain_api_key = (pixeldrain_key_input.value or '').strip()
        settings.save()
        ui.notify('Pixeldrain API key saved', type='positive')
        log('Pixeldrain API key saved', level='SYS')

    ui.button('Save Pixeldrain Key', icon='save', on_click=_save_pixeldrain_key).props('flat')

    # ── Pixeldrain Cookie ──
    ui.label('Pixeldrain Cookie').classes('text-sm font-bold mt-4')
    ui.label('Export your pd_auth_key session cookie from the browser as EditThisCookie JSON '
             'and paste below. Required for list ZIP downloads.').classes('text-sm mb-2')

    pd_cookie_status = ui.label('').classes('text-sm mb-1')
    pd_cookies_visible = [False]

    def _update_pd_cookie_status():
        if settings.pixeldrain_cookies_json:
            try:
                data = json.loads(settings.pixeldrain_cookies_json)
                if isinstance(data, list):
                    entries = len([c for c in data if isinstance(c, dict) and c.get('name') == 'pd_auth_key'])
                    pd_cookie_status.text = f'Pixeldrain cookie saved: {entries} pd_auth_key entry(s)'
                    pd_cookie_status.classes('text-green-400', replace=False)
                    return
            except Exception:
                pass
            pd_cookie_status.text = 'Pixeldrain cookie saved (invalid JSON — re-paste)'
            pd_cookie_status.classes('text-red-400', replace=False)
        else:
            pd_cookie_status.text = 'No pixeldrain cookie saved'
            pd_cookie_status.classes('text-gray-400', replace=False)

    _update_pd_cookie_status()

    pd_cookie_editor = ui.textarea(
        value='',
        placeholder='Paste EditThisCookie JSON array for pixeldrain.com... '
                    '[{"domain":".pixeldrain.com","name":"pd_auth_key","value":"...", ...}]',
    ).classes('w-full font-mono text-xs').props('rows=6')

    def _toggle_pd_cookies():
        if pd_cookies_visible[0]:
            pd_cookie_editor.set_value('')
            pd_cookies_visible[0] = False
            pd_toggle_btn.text = 'Show saved cookie'
        else:
            pd_cookie_editor.set_value(settings.pixeldrain_cookies_json or '')
            pd_cookies_visible[0] = True
            pd_toggle_btn.text = 'Hide cookie'

    def do_save_pd_cookies():
        raw = pd_cookie_editor.value
        if not raw or not raw.strip():
            ui.notify('Nothing to save — paste cookies first', type='warning')
            return
        try:
            data = json.loads(raw.strip())
            if not isinstance(data, list) or len(data) == 0:
                raise ValueError('Must be a non-empty JSON array')
            # Validate it has pd_auth_key
            has_pd = any(
                isinstance(c, dict) and c.get('name') == 'pd_auth_key'
                for c in data
            )
            if not has_pd:
                ui.notify('Warning: no pd_auth_key found in the JSON array', type='warning')
        except Exception as e:
            ui.notify(f'Invalid JSON: {e}', type='negative')
            return
        settings.pixeldrain_cookies_json = raw.strip()
        settings.save()
        _update_pd_cookie_status()
        ui.notify('Pixeldrain cookies saved', type='positive')
        pd_cookie_editor.set_value('')
        pd_cookies_visible[0] = False
        pd_toggle_btn.text = 'Show saved cookie'

    def _clear_pd_cookies():
        settings.pixeldrain_cookies_json = ''
        settings.save()
        _update_pd_cookie_status()
        pd_cookie_editor.set_value('')
        pd_cookies_visible[0] = False
        pd_toggle_btn.text = 'Show saved cookie'
        ui.notify('Pixeldrain cookies cleared', type='positive')

    with ui.row().classes('gap-2'):
        pd_toggle_btn = ui.button('Show saved cookie', icon='visibility', on_click=_toggle_pd_cookies).props('flat')
        ui.button('Save Pixeldrain Cookie', icon='save', color='positive', on_click=do_save_pd_cookies)
        ui.button('Clear Pixeldrain Cookie', icon='delete', color='red',
                  on_click=_clear_pd_cookies).props('flat')



# ── Tab: Models ──────────────────────────────────────────────────────

def _show_cancel_dialog(model_name: str):
    """Show cancel dialog with option to keep or delete downloaded files."""
    _cancel_file_path = os.path.join(PROJECT_DIR, settings.output_dir, model_name, '.cancel')

    def _write_cancel_file():
        """Write cancel file so the downloader picks it up even after page refresh."""
        try:
            os.makedirs(os.path.dirname(_cancel_file_path), exist_ok=True)
            with open(_cancel_file_path, 'w') as f:
                f.write('cancel')
        except Exception:
            pass

    def _do_cancel_and_clean():
        ev = _cancel_events.get(model_name)
        if ev:
            ev.set()
        _write_cancel_file()
        # Retry rmtree with short delays to let in-flight downloads finish
        out_dir = os.path.join(PROJECT_DIR, settings.output_dir, model_name)
        for _attempt in range(5):
            if os.path.isdir(out_dir):
                try:
                    time.sleep(0.5)
                    shutil.rmtree(out_dir, ignore_errors=True)
                    if not os.path.isdir(out_dir):
                        break
                except Exception:
                    time.sleep(0.5)
        if os.path.isdir(out_dir):
            try:
                shutil.rmtree(out_dir, ignore_errors=True)
            except Exception:
                pass
        log(f'Deleted output for {model_name} after cancel', level='INFO')
        ui.notify(f'Download cancelled for {model_name} — files cleaned', type='warning')

    def _do_cancel_keep():
        ev = _cancel_events.get(model_name)
        if ev:
            ev.set()
        _write_cancel_file()
        ui.notify(f'Download cancelled for {model_name} — files kept', type='warning')

    with ui.dialog() as dialog, ui.card().classes('bg-gray-800 w-96'):
        ui.label(f'Cancel download for {model_name}?').classes('text-lg font-bold')
        ui.separator()
        ui.label('What should happen to already-downloaded files?').classes('text-sm text-gray-300')
        with ui.row().classes('w-full justify-end gap-2 mt-4'):
            ui.button('Keep files', icon='save', on_click=lambda: (_do_cancel_keep(), dialog.close())).props('flat')
            ui.button('Delete files', icon='delete_sweep', on_click=lambda: (_do_cancel_and_clean(), dialog.close())).props('color=negative')
            ui.button('Resume', on_click=dialog.close).props('flat')
    dialog.open()


def _show_retry_dialog(model_name: str, fail_path: str, on_refresh=None):
    """Dialog to view and retry selected failed downloads."""
    if not os.path.exists(fail_path):
        ui.notify(f'Failed downloads file not found: {fail_path}', type='negative')
        return

    # Parse the fail file
    entries = []
    try:
        with open(fail_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split('\t')
                url = parts[0]
                page = 0
                post = 0
                reason = ''
                for part in parts[1:]:
                    if part.startswith('Page'):
                        page = int(part[4:])
                    elif part.startswith('Post'):
                        post = int(part[4:])
                    else:
                        reason = part
                entries.append({'url': url, 'page': page, 'post': post, 'reason': reason})
    except Exception as e:
        ui.notify(f'Failed to parse {fail_path}: {e}', type='negative')
        return

    if not entries:
        ui.notify('No failed entries found', type='warning')
        return

    # Build the dialog
    with ui.dialog() as dialog, ui.card().classes('bg-gray-800 max-w-2xl w-full max-h-[80vh]'):
        ui.label(f'Retry Failed Downloads — {model_name}').classes('text-lg font-bold')
        ui.label(f'File: {os.path.basename(fail_path)}').classes('text-xs text-gray-400')
        ui.separator()

        selected_urls: set[str] = set()
        status_label = ui.label(f'Select URLs to retry ({len(entries)} total)').classes('text-sm mt-1')

        scroll = ui.scroll_area().classes('w-full max-h-60')
        with scroll:
            for entry in entries:
                url_short = entry['url'][:80] + '...' if len(entry['url']) > 80 else entry['url']
                reason = entry.get('reason', '')
                loc = f'Page{entry["page"]} Post{entry["post"]}' if entry['page'] or entry['post'] else ''
                with ui.row().classes('w-full items-center gap-1'):
                    chk = ui.checkbox(text='').classes('shrink-0')
                    chk.on('change', lambda e, u=entry['url'], total=len(entries): (
                        selected_urls.add(u) if e.value else selected_urls.discard(u),
                        setattr(status_label, 'text', f'{len(selected_urls)}/{total} selected'),
                    ))
                    ui.label(f'{url_short}').classes('text-xs font-mono flex-grow text-red-300')
                    if loc:
                        ui.label(f'[{loc}]').classes('text-xs text-gray-400 shrink-0')
                    if reason:
                        ui.label(f'({reason})').classes('text-xs text-gray-500 shrink-0')

        ui.separator()
        retry_status = ui.label('').classes('text-sm')

        def _select_all():
            selected_urls.clear()
            selected_urls.update(e['url'] for e in entries)
            status_label.text = f'{len(selected_urls)}/{len(entries)} selected'

        def _select_none():
            selected_urls.clear()
            status_label.text = f'0/{len(entries)} selected'

        def _do_retry():
            if not selected_urls:
                ui.notify('No URLs selected', type='warning')
                return
            # Build failed_entries list for the selected URLs
            selected = [e for e in entries if e['url'] in selected_urls]
            failed_entries = [(e['url'], e['page'], e['post']) for e in selected]

            ui.notify(f'Retrying {len(failed_entries)} URL(s) — check Logs tab', type='info')
            log(f'Retry started for {model_name}: {len(failed_entries)} URL(s)')
            dialog.close()

            # Poll for completion from a thread (thread-safe via closure)
            _retry_result: list = [None]

            def _run_retry():
                out_base = os.path.join(PROJECT_DIR, settings.output_dir)
                result = retry_failed_urls(
                    failed_entries,
                    model_name,
                    output_base=out_base,
                    max_concurrent=settings.max_concurrent_downloads,
                    log_callback=log,
                )
                _retry_result[0] = result

            t = threading.Thread(target=_run_retry, daemon=True)
            t.start()

            def _check_retry():
                if _retry_result[0] is None:
                    return
                result = _retry_result[0]
                succeeded = result.get('succeeded', 0)
                failed = result.get('failed', 0)
                ui.notify(f'Retry: {succeeded} OK, {failed} FAIL',
                          type='positive' if failed == 0 else 'warning')
                log(f'Retry complete for {model_name}: {succeeded} OK, {failed} FAIL')
                if on_refresh:
                    on_refresh()
                _check_timer.deactivate()

            _check_timer = ui.timer(0.3, _check_retry, active=True)

        with ui.row().classes('w-full gap-2 mt-2'):
            ui.button('Select All', on_click=_select_all).props('outline size=sm')
            ui.button('Select None', on_click=_select_none).props('outline size=sm')
            ui.space()
            ui.button('Cancel', on_click=dialog.close).props('flat')
            ui.button('Retry Selected', icon='refresh', color='warning',
                      on_click=lambda: (_do_retry(), dialog.close()))

    dialog.open()


def build_models_tab():
    def do_clear_registry():
        with ui.dialog() as dialog, ui.card().classes('bg-gray-800 w-96'):
            ui.label('Clear entire registry?').classes('text-lg font-bold')
            ui.separator()
            ui.label('This removes all model entries from the registry. It does NOT delete downloaded files.').classes('text-sm text-gray-300')
            with ui.row().classes('w-full justify-end mt-4'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                def do_clear():
                    registry.clear()
                    dialog.close()
                    model_list.refresh()
                    ui.notify('Registry cleared', type='warning')
                ui.button('Clear Registry', icon='delete_sweep').props('color=negative').on('click', do_clear)
        dialog.open()

    with ui.row().classes('w-full items-center gap-2 mb-2'):
        status_text = ui.label('').classes('text-sm text-gray-400 flex-grow')
        ui.button('Clear Registry', icon='delete_sweep').props('outline color=negative').on('click', do_clear_registry)
        ui.button('Refresh', icon='refresh').props('outline').on('click', lambda: model_list.refresh())

    # Auto-refresh every 10 seconds to catch download progress changes
    _last_registry_size = [0]
    def auto_refresh_models():
        if len(registry.models) != _last_registry_size[0]:
            _last_registry_size[0] = len(registry.models)
            model_list.refresh()
    ui.timer(10, auto_refresh_models, active=True)

    @ui.refreshable
    def model_list():
        if not registry.models:
            ui.label('No models processed yet. Run extraction first.').classes('text-gray-400 py-8')
            return

        for name, entry in registry.models.items():
            forum = entry.get('forum', '')
            modes = entry.get('modes', {})
            urls = entry.get('total_urls', 0)
            posts = entry.get('total_posts', 0)
            extracted = entry.get('extracted_at', '')

            # Toggle state for collapsible card
            if not hasattr(model_list, '_expanded'):
                model_list._expanded = {}
            if name not in model_list._expanded:
                model_list._expanded[name] = False
            is_expanded = model_list._expanded[name]

            with ui.card().classes('w-full mb-2 bg-gray-800'):
                # ══ Header row (always visible) ══
                with ui.row().classes('w-full items-center'):
                    def toggle_expand(mname=name):
                        model_list._expanded[mname] = not model_list._expanded.get(mname, False)
                        if hasattr(model_list, '_content_refs') and mname in model_list._content_refs:
                            model_list._content_refs[mname].set_visibility(model_list._expanded[mname])
                    expand_text = 'Collapse' if is_expanded else 'Expand'
                    expand_icon = 'expand_more' if is_expanded else 'chevron_right'
                    ui.button(expand_text, icon=expand_icon, on_click=toggle_expand) \
                        .props('flat size=sm').classes('text-gray-400')
                    ui.label(name).classes('text-lg font-bold flex-grow')
                    if forum:
                        ui.label(forum).classes('text-sm text-gray-400')
                    # Remove model button (always visible)
                    def make_remove_handler(mname):
                        def remove():
                            with ui.dialog() as dialog, ui.card().classes('bg-gray-800 w-96'):
                                ui.label(f'Remove {mname}').classes('text-lg font-bold')
                                ui.separator()
                                ui.label(
                                    'Remove from registry and delete all output files and extracted data?'
                                ).classes('text-sm text-gray-300')
                                out_path = os.path.join(PROJECT_DIR, settings.output_dir, mname)
                                has_files = os.path.isdir(out_path)
                                with ui.row().classes('w-full justify-end gap-2 mt-4'):
                                    ui.button('Cancel', on_click=dialog.close).props('flat')
                                    if has_files:
                                        def do_remove_files():
                                            try:
                                                shutil.rmtree(out_path)
                                                log(f'Deleted output folder for {mname}', level='SYS')
                                            except Exception:
                                                pass
                                            registry.remove(mname)
                                            dialog.close()
                                            model_list.refresh()
                                            ui.notify(f'{mname} removed with files', type='warning')
                                        ui.button('Remove & Delete Files', icon='delete_forever') \
                                            .props('color=negative') \
                                            .on('click', do_remove_files)
                                    def do_remove_registry():
                                        registry.remove(mname)
                                        dialog.close()
                                        model_list.refresh()
                                        ui.notify(f'{mname} removed from registry', type='warning')
                                    ui.button('Remove from Registry Only', icon='delete') \
                                        .props('color=warning') \
                                        .on('click', do_remove_registry)
                            dialog.open()
                        return remove
                    ui.button(icon='delete', on_click=make_remove_handler(name)) \
                        .props('flat dense round size=sm color=negative')

                # ══ Collapsible body ══
                with ui.column().classes('w-full') as body_container:
                    if not hasattr(model_list, '_content_refs'):
                        model_list._content_refs = {}
                    model_list._content_refs[name] = body_container
                    if not is_expanded:
                        body_container.set_visibility(False)

                    # Stats grid
                    with ui.row().classes('w-full gap-x-6 gap-y-1 text-sm'):
                        ui.label(f'URLs: {urls}').classes('font-mono')
                        ui.label(f'Posts: {posts}').classes('font-mono')
                        ui.label(f'Extracted: {extracted}').classes('text-gray-400')

                    # ── Download progress indicator (only shown when no live panel exists) ──
                    dl_info = entry.get('download')
                    normal_json_exists = any(
                        modes.get(mk, {}).get('file_paths', {}).get('json', '')
                        for mk in ('normal', 'reverse', 'no_filter')
                    )
                    if dl_info and dl_info.get('status') and dl_info['status'] != 'complete' and not normal_json_exists:
                        ts = dl_info.get('updated_at', '')
                        fc = dl_info.get('total_files', 0)
                        fb = dl_info.get('total_bytes', 0)
                        fl = dl_info.get('failed_count', 0)
                        sk = dl_info.get('skipped_count', 0)
                        sz = _fmt_size(fb)
                        fl_str = f', {fl} failed' if fl else ''
                        sk_str = f', {sk} skipped' if sk else ''
                        with ui.row().classes('w-full gap-2 mt-1 text-xs text-yellow-400'):
                            ui.label(f'⏳ {dl_info["status"]}: {fc} files, {sz}{fl_str}{sk_str} (updated {ts})')

                    # ── On-disk content summary ──
                    out_stats = scan_output_dir(name)
                    if out_stats['exists'] and out_stats['total_files'] > 0:
                        sz = _fmt_size(out_stats['total_bytes'])
                        with ui.row().classes('w-full gap-x-4 gap-y-1 mt-1 p-2 rounded text-sm items-center').style('background: rgba(34,197,94,0.1); border: 1px solid rgba(34,197,94,0.25)'):
                            ui.label('✓ Downloaded').classes('text-green-400 font-bold')
                            with ui.column().classes('gap-0'):
                                ui.label(f'Posts: {out_stats["post_folders"]}').classes('text-gray-200 font-mono')
                                ui.label(f'Files: {out_stats["total_files"]}').classes('text-gray-200 font-mono')
                                ui.label(f'Size: {sz}').classes('text-gray-300 font-mono')

                    # Download button for normal mode
                    normal_data = modes.get('normal', {})
                    normal_fp = normal_data.get('file_paths', {})
                    normal_json = normal_fp.get('json', '')

                    if normal_json and os.path.exists(normal_json):
                        with ui.row().classes('w-full gap-2 mt-1 items-center'):
                            dl_btn = ui.button('Download', icon='download',
                                               ).props('color=positive').classes('')
                            cancel_btn = ui.button('Cancel', icon='cancel',
                                                   ).props('color=negative outline').classes('').set_visibility(False)
                            dl_status = ui.label('').classes('text-xs flex-grow')
                            check_logs_badge = ui.label('').classes(
                                'text-xs text-blue-400 bg-blue-900/30 px-2 py-0.5 rounded shrink-0'
                            ).set_visibility(False)
                            # ── Inline download status after page reload (no live panel) ──
                            _dl_entry = registry.get(name)
                            if _dl_entry and _dl_entry.get('download'):
                                _dl_info = _dl_entry['download']
                                _dl_status = _dl_info.get('status', '')
                                if _dl_status and _dl_status != 'complete':
                                    cancel_btn.set_visibility(True)
                                    dl_btn.disable()
                                    dl_status.set_text(f'⏳ {_dl_status}')
                                    _cancel_events[name] = threading.Event()
                                    def _inline_poll():
                                        _e = registry.get(name)
                                        if not _e or not _e.get('download'):
                                            return
                                        _i = _e['download']
                                        _s = _i.get('status', '')
                                        if not _s or _s == 'complete':
                                            dl_status.set_text('✓ Download complete — refresh page')
                                            cancel_btn.set_visibility(False)
                                            dl_btn.enable()
                                            _cancel_events.pop(name, None)
                                            if _inline_timer:
                                                _inline_timer.deactivate()
                                            return
                                        dl_status.set_text(f'⏳ {_s}')
                                    _inline_timer = ui.timer(2.0, _inline_poll, active=True)

                        def make_download_handler(mname, json_p, status_label, model_list_ref, logs_badge=None):
                            _status_queue: list = []
                            _dl_cancel_event = threading.Event()

                            def _poll_status():
                                while _status_queue:
                                    action, args = _status_queue.pop(0)
                                    if action == 'text':
                                        status_label.set_text(args)
                                    elif action == 'classes':
                                        status_label.classes(args)
                                    elif action == 'style':
                                        status_label.style(args)
                                    elif action == 'btn_disable':
                                        dl_btn.disable()
                                    elif action == 'btn_enable':
                                        dl_btn.enable()
                                    elif action == 'cancel_show':
                                        cancel_btn.set_visibility(True)
                                    elif action == 'cancel_hide':
                                        cancel_btn.set_visibility(False)
                                    elif action == 'notify':
                                        ui.notify(args, type='positive', close_button='OK')
                                    elif action == 'refresh':
                                        if model_list_ref:
                                            model_list_ref.refresh()
                                    elif action == 'stop_poll':
                                        if _poll_timer:
                                            _poll_timer.deactivate()
                                    elif action == 'cancel_prompt':
                                        _show_cancel_dialog(mname)
                                    elif action == 'checklogs_show':
                                        if logs_badge is not None:
                                            logs_badge.set_text('Check Logs')
                                            logs_badge.set_visibility(True)
                                    elif action == 'checklogs_hide':
                                        if logs_badge is not None:
                                            logs_badge.set_text('')
                                            logs_badge.set_visibility(False)

                            _poll_timer = ui.timer(0.1, _poll_status, active=True)

                            def handler():
                                _completed = [0]
                                _failed_count = [0]
                                _skipped_count = [0]
                                _total_bytes_val = [0]
                                _start_time = [0.0]
                                _total_urls = [0]
                                _last_registry_save = [0.0]
                                _speed_samples: list = []  # [(timestamp, total_bytes), ...]
                                _agg_samples: list = []  # [(timestamp, agg_downloaded_bytes), ...]
                                _active_files: list[dict] = []
                                _active_files_lock = threading.Lock()
                                _last_active_save = [0.0]
                                _all_urls_list = []

                                def _file_progress(url, filename, downloaded, total_bytes, speed):
                                    with _active_files_lock:
                                        for f in _active_files:
                                            if f['url'] == url:
                                                f['downloaded'] = downloaded
                                                f['total_bytes'] = total_bytes
                                                f['speed'] = speed
                                                f['status'] = 'downloading'
                                                break
                                        else:
                                            host = urlparse(url).netloc if url else ''
                                            _active_files.append({
                                                'url': url,
                                                'filename': filename,
                                                'host': host,
                                                'downloaded': downloaded,
                                                'total_bytes': total_bytes,
                                                'speed': speed,
                                                'status': 'downloading',
                                            })
                                        # Compute aggregate bytes across all active files
                                        _agg_bytes = sum(f.get('downloaded', 0) for f in _active_files)
                                    # Track aggregate bytes over time for total speed calculation
                                    _agg_samples.append((time.time(), _agg_bytes))
                                    # Keep samples within a 5s window
                                    _cutoff_agg = time.time() - 5
                                    while _agg_samples and _agg_samples[0][0] < _cutoff_agg:
                                        _agg_samples.pop(0)
                                    _agg_speed = 0
                                    if len(_agg_samples) >= 2:
                                        _dt = _agg_samples[-1][0] - _agg_samples[0][0]
                                        _db = _agg_samples[-1][1] - _agg_samples[0][1]
                                        _agg_speed = _db / _dt if _dt > 0 else 0
                                    # Throttled save to registry (0.5s)
                                    _now = time.time()
                                    if _now - _last_active_save[0] >= 0.5:
                                        _last_active_save[0] = _now
                                        with _active_files_lock:
                                            _af_copy = list(_active_files)
                                        registry.set_download_progress(
                                            mname, _completed[0], _total_bytes_val[0] + _agg_bytes,
                                            _failed_count[0], _skipped_count[0],
                                            status=f'{_completed[0] + _failed_count[0] + _skipped_count[0]}/{_total_urls[0]}',
                                            total_urls=_total_urls[0],
                                            speed_bps=_agg_speed,
                                            current_file=filename,
                                            current_file_size=total_bytes,
                                            active_files=_af_copy,
                                        )
                                try:
                                    with open(json_p) as _jf:
                                        _jdata = json.load(_jf)
                                    for _p in _jdata.get('posts', []):
                                        _all_urls_list.extend(_p.get('urls', []))
                                except Exception:
                                    pass

                                def pc(phase, **kw):
                                    if phase == 'start':
                                        _start_time[0] = time.time()
                                        _total_urls[0] = kw.get('total_urls', 0)
                                        total_posts = kw.get('total_posts', '?')
                                        _status_queue.append(('text', f'→ Download started: {total_posts} posts, {_total_urls[0]} URLs total'))
                                        log(f'Download starting for {mname}: {total_posts} posts, {_total_urls[0]} URLs')
                                        registry.set_download_progress(mname, 0, 0, 0, 0, status=f'Starting ({_total_urls[0]} URLs)', total_urls=_total_urls[0])
                                    elif phase == 'resolving':
                                        t = kw.get('total', '?')
                                        _status_queue.append(('text', f'Resolving {t} URLs...'))
                                        log(f'Resolving for {mname}...')
                                        registry.set_download_progress(mname, 0, 0, 0, 0, status=f'Resolving {t} URLs', total_urls=_total_urls[0])
                                    elif phase == 'resolve_progress':
                                        r = kw.get('resolved', 0)
                                        t = kw.get('total', 0)
                                        eta = kw.get('eta', 0)
                                        oc = kw.get('overall_completed', 0)
                                        ot = kw.get('overall_total', t)
                                        oeta = kw.get('overall_eta', 0)
                                        if oc > 0 and oeta > 0:
                                            _status_queue.append(('text', f'Resolving {r}/{t} — overall [{oc}/{ot}] ETA {_fmt_eta(oeta)}'))
                                        else:
                                            _status_queue.append(('text', f'Resolving {r}/{t} (ETA {_fmt_eta(eta)})'))
                                        registry.set_download_progress(mname, 0, 0, 0, 0,
                                            status=f'Resolving {r}/{t}' + (f' ETA {_fmt_eta(eta)}' if eta else ''),
                                            total_urls=_total_urls[0])
                                    elif phase == 'resolved':
                                        okc = kw.get('ok', 0)
                                        fld = kw.get('failed', 0)
                                        log(f'Resolved: {okc} OK, {fld} failed')
                                        registry.set_download_progress(mname, 0, 0, 0, 0,
                                            status=f'Resolved: {okc} OK, {fld} failed',
                                            total_urls=_total_urls[0])
                                    elif phase == 'file':
                                        oc = kw.get('overall_completed', 0)
                                        ot = kw.get('overall_total', _total_urls[0])
                                        # Accept updated overall_total from ZIP handler (gallery expansion)
                                        if kw.get('overall_total') is not None and ot != _total_urls[0]:
                                            _total_urls[0] = ot
                                        oeta = kw.get('overall_eta', 0)
                                        fn = kw.get('filename', '')
                                        strategy = kw.get('strategy', '')
                                        url = kw.get('url', '')
                                        ok = kw.get('ok', True)
                                        size = kw.get('size', 0)
                                        skipped = kw.get('skipped', False)
                                        # Remove completed file from active list
                                        if url:
                                            with _active_files_lock:
                                                _active_files[:] = [f for f in _active_files if f['url'] != url]
                                        if skipped:
                                            _skipped_count[0] += 1
                                        elif ok:
                                            _completed[0] += 1
                                            _total_bytes_val[0] += size
                                        else:
                                            _failed_count[0] += 1
                                        # Instantaneous speed over last 5s window
                                        _speed_samples.append((time.time(), _total_bytes_val[0]))
                                        _cutoff = time.time() - 5
                                        _speed_samples[:] = [(t, b) for t, b in _speed_samples if t >= _cutoff]
                                        if len(_speed_samples) >= 2:
                                            _dt = _speed_samples[-1][0] - _speed_samples[0][0]
                                            _db = _speed_samples[-1][1] - _speed_samples[0][1]
                                            speed_bps = _db / _dt if _dt > 0 else 0
                                        else:
                                            speed_bps = 0
                                        _elapsed = time.time() - _start_time[0]
                                        if oeta and oeta > 0:
                                            eta_display = f'ETA {_fmt_eta(oeta)}'
                                        else:
                                            rate = oc / _elapsed if _elapsed > 0 else 0
                                            remaining = ot - oc
                                            loc_eta = remaining / rate if rate > 0 else 0
                                            eta_display = f'ETA {_fmt_eta(loc_eta)}'
                                        status_text = f'[{oc}/{ot}] {eta_display}'
                                        fn_display = ''
                                        if fn:
                                            _base, _ext = os.path.splitext(fn)
                                            fn_display = _base[:40 - len(_ext) - 3] + '...' + _ext if len(fn) > 40 else fn
                                            status_text += f'  —  {fn_display}'
                                        _status_queue.append(('text', status_text))
                                        log(f'Download progress: {oc}/{ot} — {eta_display} — {fn}')
                                        _total_urls[0] = ot
                                        # Mark URL consumed
                                        if url and _all_urls_list:
                                            try:
                                                idx = _all_urls_list.index(url)
                                                _all_urls_list[idx] = '__CONSUMED__'
                                            except ValueError:
                                                pass
                                        # Build queue of next unconsumed files + host breakdown
                                        _queue = []
                                        _host_counts: dict[str, int] = {}
                                        for _uq in _all_urls_list:
                                            if _uq == '__CONSUMED__':
                                                continue
                                            # Count hosts for breakdown
                                            _uq_host = urlparse(_uq).netloc
                                            if _uq_host:
                                                _host_counts[_uq_host] = _host_counts.get(_uq_host, 0) + 1
                                            # Build display queue (first 10 items)
                                            if len(_queue) < 10:
                                                _uq_fn = os.path.basename(_uq)
                                                if not _uq_fn:
                                                    _uq_fn = _uq[:40]
                                                _base, _ext = os.path.splitext(_uq_fn)
                                                _qtxt = _base[:35 - len(_ext) - 3] + '...' + _ext if len(_uq_fn) > 35 else _uq_fn
                                                _queue.append(_qtxt)
                                        _now = time.time()
                                        if _now - _last_registry_save[0] >= 1.0 or oc == ot:
                                            with _active_files_lock:
                                                _af_copy = list(_active_files)
                                            registry.set_download_progress(
                                                mname, _completed[0], _total_bytes_val[0],
                                                _failed_count[0], _skipped_count[0],
                                                status=f'{oc}/{ot} · {_completed[0]} files · {_fmt_size(_total_bytes_val[0])}',
                                                total_urls=_total_urls[0],
                                                speed_bps=speed_bps,
                                                current_file=fn_display if fn else '',
                                                current_file_size=size,
                                                current_file_strategy=strategy if not skipped else '',
                                                active_queue=_queue,
                                                active_files=_af_copy,
                                                host_queue=_host_counts)
                                            _last_registry_save[0] = _now
                                    elif phase == 'complete':
                                        okc = kw.get('completed', 0)
                                        fld = kw.get('failed', 0)
                                        skp = kw.get('skipped', 0)
                                        tbytes = kw.get('total_bytes', 0)
                                        ot = kw.get('overall_total', okc + fld)
                                        total_ok = okc + skp
                                        skp_str = f', {skp} skipped' if skp else ''
                                        _status_queue.append(('text', f'✓ Download complete: {okc} OK, {fld} failed{skp_str} ({_fmt_size(tbytes)})'))
                                        _status_queue.append(('btn_enable', None))
                                        _status_queue.append(('cancel_hide', None))
                                        _status_queue.append(('stop_poll', None))
                                        _status_queue.append(('checklogs_hide', None))
                                        _status_queue.append(('notify', f'Download complete: {okc} files ({_fmt_size(tbytes)})'))
                                        _status_queue.append(('refresh', None))
                                        log(f'Download finished for {mname}: {okc} OK, {fld} failed{skp_str}, {_fmt_size(tbytes)}')
                                        registry.register_download(mname, okc + fld + skp, total_ok, tbytes, fld, skp)

                                _cancel_events[mname] = _dl_cancel_event
                                out_base = os.path.join(PROJECT_DIR, settings.output_dir)
                                _status_queue.append(('btn_disable', None))
                                _status_queue.append(('cancel_show', None))

                                def _run():
                                    try:
                                        from core.downloader import run_download
                                        stats = run_download(json_p, mname, out_base, progress_callback=pc, log_callback=log, max_concurrent=settings.max_concurrent_downloads, max_speed_bps=settings.max_speed_mbps * 1024 * 1024 if settings.max_speed_mbps > 0 else 0, cancel_event=_dl_cancel_event, cancel_file_path=os.path.join(out_base, mname, '.cancel'), pixeldrain_api_key=settings.pixeldrain_api_key, pixeldrain_cookies_json=settings.pixeldrain_cookies_json, file_progress_callback=_file_progress, streaming_resolve=settings.streaming_resolve, models_data_dir=os.path.join(PROJECT_DIR, settings.models_data_dir))
                                    except Exception as ex:
                                        _status_queue.append(('text', f'Error: {ex}'))
                                        _status_queue.append(('classes', 'text-red'))
                                        log(f'Download error for {mname}: {ex}', level='ERROR')
                                        import traceback
                                        traceback.print_exc()
                                    finally:
                                        _cancel_events.pop(mname, None)
                                        try:
                                            registry.set_download_progress(mname,
                                                _completed[0] if _completed else 0,
                                                _total_bytes_val[0] if _total_bytes_val else 0,
                                                _failed_count[0] if _failed_count else 0,
                                                _skipped_count[0] if _skipped_count else 0,
                                                status='complete',
                                                total_urls=_total_urls[0] if _total_urls else 0)
                                        except Exception:
                                            pass
                                        _status_queue.append(('btn_enable', None))
                                        _status_queue.append(('cancel_hide', None))
                                        _status_queue.append(('checklogs_hide', None))
                                        _status_queue.append(('stop_poll', None))
                                        _cf = os.path.join(out_base, mname, '.cancel')
                                        if os.path.exists(_cf):
                                            try:
                                                os.remove(_cf)
                                            except Exception:
                                                pass

                                t = threading.Thread(target=_run, daemon=True)
                                t.start()

                            return handler

                        _handler = make_download_handler(name, normal_json, dl_status, model_list, check_logs_badge)
                        dl_btn.on('click', _handler)
                        cancel_btn.on('click', lambda e, n=name: _show_cancel_dialog(n))
                    # Per-mode files
                    mode_labels = {'normal': 'Normal', 'reverse': 'Reverse', 'no_filter': 'No Filter'}
                    mode_order = ['normal', 'reverse', 'no_filter']
                    all_files_for_delete: list[tuple[str, str]] = []

                    for mode_key in mode_order:
                        mode_data = modes.get(mode_key)
                        if not mode_data:
                            continue
                        fp = mode_data.get('file_paths', {})
                        txt_p = fp.get('all_urls', '')
                        json_p = fp.get('json', '')
                        posts_dir = fp.get('posts_dir', '')
                        ts = mode_data.get('extracted_at', '')

                        available = []
                        if txt_p and os.path.exists(txt_p):
                            available.append(('txt', txt_p))
                            all_files_for_delete.append(('all_urls.txt', txt_p))
                        if json_p and os.path.exists(json_p):
                            available.append(('json', json_p))
                            all_files_for_delete.append(('posts.json', json_p))
                        if posts_dir and os.path.isdir(posts_dir):
                            available.append(('posts', posts_dir))
                            all_files_for_delete.append(('posts/', posts_dir))

                        if not available:
                            continue

                        label = mode_labels.get(mode_key, mode_key.capitalize())
                        ui.label(f'— {label} ({ts}) —').classes('text-xs text-gray-500 mt-1')

                        with ui.row().classes('w-full gap-2'):
                            def make_dl_handler(p, name_text):
                                def handler():
                                    if not os.path.exists(p):
                                        ui.notify(f'{name_text} not found on disk', type='negative')
                                        return
                                    ui.download(p)
                                return handler

                            for kind, path in available:
                                if kind == 'txt':
                                    btn_text = os.path.basename(path)
                                    ui.button(f'📄 {btn_text}', icon='text_snippet') \
                                        .props('outline').classes('') \
                                        .on('click', make_dl_handler(path, btn_text))
                                elif kind == 'json':
                                    btn_text = os.path.basename(path)
                                    ui.button(f'📄 {btn_text}', icon='data_object') \
                                        .props('outline').classes('') \
                                        .on('click', make_dl_handler(path, btn_text))
                                elif kind == 'posts':
                                    ui.label(f'📁 posts/').classes('text-xs text-gray-400')

                    # Failed downloads files (checked in models_data dir first, then output dir)
                    models_data_path = os.path.join(PROJECT_DIR, settings.models_data_dir, name)
                    out_dir = os.path.join(PROJECT_DIR, settings.output_dir, name)
                    check_dirs = []
                    if os.path.isdir(models_data_path):
                        check_dirs.append(models_data_path)
                    if os.path.isdir(out_dir):
                        check_dirs.append(out_dir)
                    fail_files = []
                    fail_dir = ''
                    for d in check_dirs:
                        ff = sorted(
                            [f for f in os.listdir(d)
                             if f.startswith('failed_downloads_') and f.endswith('.txt')],
                            reverse=True
                        )
                        if ff:
                            fail_files = ff
                            fail_dir = d
                            break
                    if fail_files:
                        latest_fail = os.path.join(fail_dir, fail_files[0])
                        with ui.row().classes('w-full gap-2 items-center'):
                            ui.button(f'⚠️ {fail_files[0]}', icon='error_outline') \
                                .props('outline color=negative').classes('') \
                                .on('click', lambda p=latest_fail: ui.download(p) if os.path.exists(p) else None)
                            ui.button('🔄 Retry Failed', icon='refresh', color='warning') \
                                .on('click', lambda n=name, f=latest_fail: _show_retry_dialog(n, f, on_refresh=lambda: model_list.refresh()))

                    # Delete buttons
                    def make_delete_handler(model_name, files_list):
                        def handler():
                            ui.notify(f'Delete {model_name} output files? See dialog.', type='warning')
                            with ui.dialog() as dialog, ui.card().classes('bg-gray-800 w-96'):
                                ui.label(f'Delete output files for {model_name}?').classes('text-lg font-bold')
                                ui.separator()
                                deleted = []
                                failed = []
                                for label, p in files_list:
                                    if p and os.path.exists(p):
                                        try:
                                            if os.path.isdir(p):
                                                shutil.rmtree(p)
                                            else:
                                                os.remove(p)
                                            deleted.append(label)
                                        except Exception as ex:
                                            failed.append(f'{label}: {ex}')
                                if deleted:
                                    ui.label(f'Removed: {", ".join(deleted)}').classes('text-green')
                                if failed:
                                    ui.label(f'Failed: {", ".join(failed)}').classes('text-red')
                                if not deleted and not failed:
                                    ui.label('No files to delete.').classes('text-gray-400')
                                if deleted:
                                    log(f'Deleted {model_name} files: {", ".join(deleted)}', level='INFO')
                                if failed:
                                    log(f'Delete errors for {model_name}: {", ".join(failed)}', level='ERROR')
                                registry.remove(model_name)
                                log(f'Removed {model_name} from registry', level='SYS')
                                model_list.refresh()
                                info_overview.refresh()
                                with ui.row().classes('w-full justify-end'):
                                    ui.button('Close', on_click=dialog.close).props('flat')
                            dialog.open()
                        return handler

                    if all_files_for_delete:
                        with ui.row().classes('w-full gap-2 mt-1'):
                            ui.button('🗑 Delete All Output Files', icon='delete') \
                                .props('color=negative') \
                                .classes('') \
                                .on('click', make_delete_handler(name, all_files_for_delete))

                    def make_delete_input_handler(model_files):
                        def handler():
                            dirs_to_del = set()
                            for mode_key in mode_order:
                                mode_data = modes.get(mode_key)
                                if not mode_data:
                                    continue
                                fpp = mode_data.get('file_paths', {})
                                for kind in ('json', 'all_urls', 'posts_dir'):
                                    p = fpp.get(kind, '')
                                    if p:
                                        d = os.path.dirname(p) if kind != 'posts_dir' else p
                                        if os.path.isdir(d):
                                            dirs_to_del.add(d)
                            if not dirs_to_del:
                                ui.notify('No input folders to delete.', type='info')
                                return
                            with ui.dialog() as dialog, ui.card().classes('bg-gray-800 w-96'):
                                ui.label('Delete input data?').classes('text-lg font-bold')
                                ui.separator()
                                for d in sorted(dirs_to_del):
                                    ui.label(f'  📁 {os.path.basename(d)}').classes('text-gray-300')
                                deleted = []
                                failed = []
                                for d in dirs_to_del:
                                    try:
                                        shutil.rmtree(d)
                                        deleted.append(os.path.basename(d))
                                    except Exception as ex:
                                        failed.append(f'{os.path.basename(d)}: {ex}')
                                if deleted:
                                    ui.label(f'Removed: {", ".join(deleted)}').classes('text-green')
                                if failed:
                                    ui.label(f'Failed: {", ".join(failed)}').classes('text-red')
                                if not deleted and not failed:
                                    ui.label('No folders to delete.').classes('text-gray-400')
                                registry.remove(model_name)
                                log(f'Removed {name} from registry', level='SYS')
                                model_list.refresh()
                                with ui.row().classes('w-full justify-end'):
                                    ui.button('Close', on_click=dialog.close).props('flat')
                            dialog.open()
                        return handler

                    if all_files_for_delete:
                        with ui.row().classes('w-full gap-2'):
                            ui.button('🗑 Delete Input Data', icon='folder_off') \
                                .props('color=negative') \
                                .classes('') \
                                .on('click', make_delete_input_handler(name))

    model_list()


# ── Tab: Logs ──────────────────────────────────────────────────────

def build_logs_tab():
    with ui.card().classes('w-full'):
        ui.label('Application Logs').classes('text-xl font-bold')
        ui.separator()

        filter_all = {'SYS': True, 'INFO': True, 'WARN': True, 'ERROR': True}

        with ui.row().classes('items-center gap-2'):
            ui.label('Filter:').classes('text-sm')
            sys_btn = ui.button('SYS', icon='settings').props('flat outline size=sm') \
                .classes('text-blue')
            info_btn = ui.button('Info', icon='info').props('flat outline size=sm')
            warn_btn = ui.button('Warn', icon='warning').props('flat outline size=sm') \
                .classes('text-yellow')
            error_btn = ui.button('Error', icon='error').props('flat outline size=sm') \
                .classes('text-red')
            ui.space()
            clear_btn = ui.button('Clear', icon='delete_sweep').props('outline')
            refresh_btn = ui.button('Refresh', icon='refresh').props('outline')

        def update_filter_style():
            for btn, level, is_active in [
                (sys_btn, 'SYS', filter_all['SYS']),
                (info_btn, 'INFO', filter_all['INFO']),
                (warn_btn, 'WARN', filter_all['WARN']),
                (error_btn, 'ERROR', filter_all['ERROR']),
            ]:
                btn.style('opacity: 1.0' if is_active else 'opacity: 0.35')

        def toggle_level(level):
            def fn():
                filter_all[level] = not filter_all[level]
                update_filter_style()
                render_logs.refresh()
            return fn

        sys_btn.on('click', toggle_level('SYS'))
        info_btn.on('click', toggle_level('INFO'))
        warn_btn.on('click', toggle_level('WARN'))
        error_btn.on('click', toggle_level('ERROR'))
        update_filter_style()

        def do_clear():
            log_messages.clear()
            render_logs.refresh()

        clear_btn.on('click', do_clear)

        @ui.refreshable
        def render_logs():
            filtered = [e for e in log_messages if filter_all.get(e.get('level', 'INFO'), True)]
            if not filtered:
                ui.label('No log entries match the current filter.').classes('text-gray-500 text-sm py-4')
                return

            for entry in reversed(filtered[-200:]):
                ts = entry.get('timestamp', '')
                lvl = entry.get('level', 'INFO')
                msg = entry.get('message', '')

                if lvl == 'ERROR':
                    color = 'text-red'
                    badge = 'ERROR'
                elif lvl == 'WARN':
                    color = 'text-yellow'
                    badge = 'WARN'
                elif lvl == 'SYS':
                    color = 'text-blue-400'
                    badge = 'SYS'
                else:
                    color = 'text-gray-300'
                    badge = 'INFO'

                with ui.row().classes('w-full items-start gap-1 text-xs font-mono border-b border-gray-700 py-0.5'):
                    ui.label(ts).classes('text-gray-500 w-16 shrink-0')
                    ui.label(badge).classes(f'{color} w-10 shrink-0 font-bold')
                    ui.label(msg).classes(f'{color} flex-grow')

        render_logs()
        refresh_btn.on('click', render_logs.refresh)

        _last_log_count = [0]
        def auto_refresh():
            if len(log_messages) != _last_log_count[0]:
                _last_log_count[0] = len(log_messages)
                render_logs.refresh()
        ui.timer(2.0, auto_refresh, active=True)


# ── Tab: Info ─────────────────────────────────────────────────────────

@ui.refreshable
def info_overview():
    """Refreshable overview card showing current registry stats."""
    model_count = len(registry.models)
    total_urls = sum(e.get('total_urls', 0) for e in registry.models.values())
    total_posts = sum(e.get('total_posts', 0) for e in registry.models.values())
    dl_models = sum(1 for e in registry.models.values() if e.get('download', {}).get('status') == 'complete')

    with ui.card().classes('w-full bg-gray-800'):
        ui.label('Overview').classes('text-lg font-bold')
        ui.separator()
        with ui.row().classes('w-full gap-x-8 gap-y-1'):
            ui.label(f'Models: {model_count}').classes('font-mono')
            ui.label(f'Total URLs: {total_urls}').classes('font-mono')
            ui.label(f'Total Posts: {total_posts}').classes('font-mono')
            ui.label(f'Downloaded: {dl_models}/{model_count}').classes('font-mono text-green' if dl_models == model_count else 'font-mono')

def build_info_tab():
    title = settings.site_title or 'Simp URL Fetcher'
    ui.label(title).classes('text-2xl font-bold mb-4')
    ui.label('Extract, manage and download media from scraper-collected HTML pages.').classes('text-gray-300')

    # ── Update banner (info only — install via Settings) ──
    update_banner = ui.card().classes('w-full bg-yellow-900/30 border border-yellow-600').set_visibility(False)
    with update_banner:
        with ui.row().classes('w-full items-start gap-2 p-1'):
            ui.icon('system_update').classes('text-yellow-400 mt-0.5')
            ui.label('An update is available.').classes('text-yellow-400 font-bold shrink-0')
            update_info = ui.label('').classes('text-sm text-yellow-200 flex-grow')
            ui.label('Install via Settings → Updates & Restart').classes('text-xs text-gray-400 mt-1')

    def _apply_update_state(state: dict):
        if state.get('checked') and state.get('update_available'):
            update_info.text = (
                f'{state.get("local_branch", "?")}: '
                f'{state.get("local_sha", "?")} → {state.get("remote_sha", "?")}'
            )
            update_banner.set_visibility(True)
        elif state.get('error'):
            update_info.text = f'Check failed: {state["error"]}'
            update_banner.set_visibility(False)
        else:
            update_banner.set_visibility(False)

    # Apply initial state if already checked
    _apply_update_state(_update_state)

    # Poll until check completes (if startup check is still running)
    def _poll_update():
        if _update_state.get('checked'):
            _apply_update_state(_update_state)
            _poll_timer.deactivate()
    _poll_timer = ui.timer(0.5, _poll_update, active=True)

    info_overview()
    # Auto-refresh overview every 5s so stats stay current when navigating back from other tabs
    ui.timer(5.0, info_overview.refresh, active=True)

    with ui.card().classes('w-full bg-gray-800 mt-2'):
        ui.label('How to use:').classes('text-lg font-bold')
        ui.separator()

        ui.label('1. Extraction').classes('text-md font-bold mt-2 text-blue-400')
        ui.label('Upload or place HTML files in the input folder, select them in the Extract tab, and click "EXTRACT URLS".')

        ui.label('2. Models').classes('text-md font-bold mt-2 text-blue-400')
        ui.label('The Models tab lists all processed models with extracted URLs and download status. Click "Download" to start fetching media files.')

        ui.label('3. Monitoring').classes('text-md font-bold mt-2 text-blue-400')
        ui.label('Watch live progress in the Models tab (ETA per download batch). The Logs tab shows a detailed stream of all operations with filterable levels.')

    with ui.card().classes('w-full bg-gray-800 mt-2'):
        ui.label('Download Pipeline').classes('text-lg font-bold')
        ui.separator()
        ui.label('• Streaming pre-resolution — the next batch of URLs resolves while the current batch downloads, eliminating wait gaps between batches.').classes('text-sm text-gray-300')
        ui.label('• Host queue breakdown — the Downloads tab shows pending files grouped by host, so you can see which hosts are rate-limited or backed up.').classes('text-sm text-gray-300')
        ui.label('• Aggregate speed tracking — total download speed across all active downloads, calculated from a rolling 5-second window.').classes('text-sm text-gray-300')
        ui.label('• Speed throttle — set max_speed_mbps in Settings (0 = unlimited) to cap per-file download speed in MB/s.').classes('text-sm text-gray-300')
        ui.label('• Failure context — all failed URLs are recorded in failed_downloads.txt with Mode{mode}, model_name, page, and post for debugging.').classes('text-sm text-gray-300')
        ui.label('').classes('text-xs')
        ui.label('Pixeldrain').classes('text-md font-bold text-blue-400')
        ui.separator()
        ui.label('• Paste your full Pixeldrain cookie JSON (EditThisCookie export) in Settings → Host API Keys — same UX as the SimpCity cookie field.').classes('text-sm text-gray-300')
        ui.label('  The field is a hidden password-style input. Export cookies via EditThisCookie from your browser while logged into Pixeldrain.').classes('text-sm text-gray-300')
        ui.label('  Single files with CAPTCHA protection are flagged and skipped (no programmatic bypass without a subscription).').classes('text-sm text-gray-300')

    # ── Cookie Instructions ──
    with ui.card().classes('w-full bg-gray-800 mt-2'):
        ui.label('SimpCity Crawler — Getting Cookies').classes('text-lg font-bold')
        ui.separator()
        ui.label('The Crawler tab needs your SimpCity session cookies to access threads.').classes('text-sm mb-2')
        ui.label('How to export cookies with EditThisCookie:').classes('text-sm font-bold mt-2')
        steps = [
            ('1', 'Install the EditThisCookie extension in your browser (Chrome/Firefox).'),
            ('2', 'Log into SimpCity in your browser.'),
            ('3', 'Click the EditThisCookie icon (cookie icon in toolbar) → click the export button (⎆).'),
            ('4', 'The full cookie JSON is copied to your clipboard.'),
            ('5', 'Go to Settings → Cookies & Settings, paste the JSON, and click Save Cookies.'),
        ]
        for num, text in steps:
            with ui.row().classes('items-start gap-2'):
                ui.label(num).classes('text-xs font-bold text-blue-400 bg-gray-700 rounded-full px-2 py-0.5 mt-0.5')
                ui.label(text).classes('text-sm text-gray-300')
        ui.label('The Crawler tab activates automatically once cookies are saved.').classes('text-sm text-green-400 mt-1')
        ui.label('Important: Cookies expire after some time. Re-export if the crawler stops working.').classes('text-sm text-orange-400')

    # ── URL Filter Patterns ──
    with ui.card().classes('w-full bg-gray-800 mt-2'):
        ui.label('URL Filter Patterns').classes('text-lg font-bold')
        ui.separator()
        ui.label('Two JSON files control which URLs the extractor picks up from forum HTML pages:').classes('text-sm mb-2')
        with ui.row().classes('w-full gap-2 mt-1'):
            ui.label('• url_patterns.json (keep)').classes('text-green font-bold')
            ui.label('URLs matching any line are INCLUDED during normal extraction. Add new hosts/domains here. 55 entries default.').classes('text-gray-300')
        with ui.row().classes('w-full gap-2'):
            ui.label('• skip_url.json (skip)').classes('text-orange font-bold')
            ui.label('URLs matching any line are EXCLUDED — used to filter out thumbnails, avatars, social links, forum assets. 19 entries default.').classes('text-gray-300')
        ui.label('Edit in Settings → URL Filters. Entries are regex substrings (not exact URLs). Changes apply on the next extraction run.').classes('text-xs text-gray-400 mt-1')
        ui.label('These files live in the project root and are version-controlled (committed as templates).').classes('text-xs text-gray-400')

    # ── Supported Hosts & URL Processing ──
    with ui.card().classes('w-full bg-gray-800 mt-2'):
        ui.label('Supported Hosts (Extraction)').classes('text-lg font-bold')
        ui.separator()
        ui.label('URLs from these hosts are captured during extraction when found in forum HTML pages:').classes('text-sm mb-2')
        hosts = [
            'bunkr.cr/.ru/.ac/.ci/.fi/.media/.black/.ph/.pk/.red/.si/.site/.sk/.ws/.ax/.bz/.cat',
            'bunkrrr.org, bunkrr.su',
            'cyberdrop.me, cyberdrop.cr, cyberfile.me, cyberfile.su',
            'cyberfiles-static.b-cdn.net',
            'pixl.li, jpg4.su, jpg5.su, jpg6.su, jpg7.cr, jpg.church',
            'pixeldrain.com, saint2.su, selti-delivery.ru',
            'anonfiles.com, gofile.io, imgbox.com, giphy.com, mega.nz, mega.co.nz',
            'host.church, 1fichier.com',
            'media.redgifs.com, redgifs.com/',
            'media.imagepond.net/media/, www.imagebam.com/image',
            'i.gyazo.com, i.imgur.com',
            'drive.google.com/drive/folders, e-hentai.org/g/',
            'cdn.camwhores.tv/contents/, gotanynudes.com/',
            'simpcity.cr/attachments/, celebforum.to/attachments/',
            'forums.socialmediagirls.com/attachments',
        ]
        for h in hosts:
            ui.label(f'• {h}').classes('text-xs font-mono text-gray-300')
        ui.label('').classes('text-xs')
        ui.label('Download strategies: Bunkr ✓  Cyberdrop/Cyberfile ✓  Pixl/JPEG hosts ✓  Pixeldrain ✓  Saint2 ✓').classes('text-xs text-green-400')
        ui.label('Gofile ✓  Imgbox ✓  RedGifs ✓  Imagebam ✓  Gyazo ✓  Imgur ✓  Mega.nz/co.nz ✓').classes('text-xs text-green-400')
        ui.label('Anonfiles, 1fichier, Giphy, Google Drive, E-hentai, Gotanynudes — captured during extraction but').classes('text-xs text-gray-400')
        ui.label('no dedicated download strategy (URLs may still resolve if they are direct media links).').classes('text-xs text-gray-400')

    with ui.card().classes('w-full bg-gray-800 mt-2'):
        ui.label('URL Resolution & Download').classes('text-lg font-bold')
        ui.separator()
        ui.label('During download, each URL is resolved through a pipeline:').classes('text-sm mb-1')
        ui.label('1. oEmbed API — checked first for rich media info (RedGifs, Imgur, etc.)').classes('text-xs text-gray-300')
        ui.label('2. Direct fetch — if oEmbed fails, the URL is fetched directly to check availability').classes('text-xs text-gray-300')
        ui.label('3. Browser fallback — if direct fetch fails, a headless browser attempts to scrape the media URL').classes('text-xs text-gray-300')
        ui.label('4. Retry pass — all unresolved URLs are retried once in bulk before being logged as failed').classes('text-xs text-gray-300 mt-1')
        ui.label('Files are downloaded in parallel batches (6 workers). Progress and ETA are shown live in the Models tab.').classes('text-xs text-gray-300 mt-1')

    # ── Version footer ──
    ui.separator().classes('my-2')
    with ui.row().classes('w-full justify-between items-center'):
        ui.label(f'v{VERSION}').classes('text-xs text-gray-500')
        ui.label('Simp URL Fetcher').classes('text-xs text-gray-600')


# ── Download Tab ────────────────────────────────────────────────────────

def _cancel_download_immediate(model_name: str):
    """Write cancel file immediately + set in-memory event. No dialog needed."""
    cancel_file = os.path.join(PROJECT_DIR, settings.output_dir, model_name, '.cancel')
    try:
        os.makedirs(os.path.dirname(cancel_file), exist_ok=True)
        with open(cancel_file, 'w') as f:
            f.write('cancel')
        log(f'Cancel file written for {model_name}', level='WARN')
    except Exception as e:
        log(f'Failed to write cancel file: {e}', 'WARN')
    ev = _cancel_events.get(model_name)
    if ev:
        ev.set()
    ui.notify(f'Cancel requested for {model_name}', type='warning')


def build_download_tab():
    """Show active downloads — redesigned layout matching user spec."""
    # ── Persistent cancel bar (rebuilt only when active model list changes) ──
    cancel_bar = ui.row().classes('w-full gap-2 mb-2 flex-wrap items-center')
    _last_active_models: list[str] = []

    # ── Data section (rebuilt every 2s) ──
    dl_container = ui.column().classes('w-full gap-4')

    def _update_cancel_bar():
        nonlocal _last_active_models
        active_models = []
        for name, entry in registry.models.items():
            dl_info = entry.get('download')
            if dl_info and dl_info.get('status', '') not in ('', 'complete'):
                active_models.append(name)

        if active_models == _last_active_models:
            return  # no change — keep buttons stable
        _last_active_models = list(active_models)
        cancel_bar.clear()
        if active_models:
            ui.label('Active Downloads:').classes('text-sm font-bold text-gray-300 mr-2')
            for mn in active_models:
                ui.button(f'✕ {mn}', icon='cancel',
                          on_click=lambda _e, n=mn: _cancel_download_immediate(n)) \
                    .props('color=negative outline size=sm')

    def _render():
        dl_container.clear()
        active = False
        for name, entry in registry.models.items():
            dl_info = entry.get('download')
            if not dl_info:
                continue
            dl_status = dl_info.get('status', '')
            if not dl_status or dl_status == 'complete':
                continue
            active = True

            _completed = dl_info.get('total_files', 0)
            _failed = dl_info.get('failed_count', 0)
            _skipped = dl_info.get('skipped_count', 0)
            _total_bytes = dl_info.get('total_bytes', 0)
            _total_urls = dl_info.get('total_urls', 0)
            _speed_bps = dl_info.get('speed_bps', 0)
            _status_text = dl_info.get('status', '')
            _host_queue = dl_info.get('host_queue', {})
            _active_files = dl_info.get('active_files', [])

            with dl_container, ui.card().classes('w-full bg-gray-800 p-3'):
                # ── Header: Model | Total URLs | Speed | ETA ──
                with ui.row().classes('w-full items-center gap-3'):
                    ui.label(name).classes('text-lg font-bold')
                    ui.label(f'Total URLs: {_total_urls}').classes('text-sm font-mono text-gray-400')
                    if _speed_bps > 0:
                        ui.label(_fmt_speed(_speed_bps)).classes('text-xs font-mono text-cyan-400')
                    # ETA
                    _total_done = _completed + _failed + _skipped
                    if _speed_bps > 0 and _total_urls > 0 and _total_done < _total_urls:
                        _remaining = _total_urls - _total_done
                        _avg_size = _total_bytes / _total_done if _total_done > 0 else 0
                        _eta_s = (_remaining * _avg_size) / _speed_bps if _avg_size > 0 else 9999
                        ui.label(f'ETA {_fmt_eta(_eta_s)}').classes('text-xs font-mono text-orange-400')
                    else:
                        ui.label('—').classes('text-xs font-mono text-gray-500')
                    ui.space()
                    # Cancel button inside card too as fallback
                    ui.button('Cancel', icon='cancel',
                              on_click=lambda _e, mn=name: _cancel_download_immediate(mn)) \
                        .props('color=negative outline size=sm')

                # ── Stats line: X OK | Y Fail | Z Skipped ──
                with ui.row().classes('w-full text-sm gap-3 mt-1'):
                    ui.label(f'{_completed} OK').classes('font-mono text-green-400')
                    if _failed:
                        ui.label(f'{_failed} Fail').classes('font-mono text-red-400')
                    if _skipped:
                        ui.label(f'{_skipped} Skipped').classes('font-mono text-gray-400')

                # ── Total downloaded size ──
                ui.label(f'{_fmt_size(_total_bytes)} Total downloaded').classes(
                    'text-xs font-mono text-gray-300')

                # ── Status section ──
                with ui.row().classes('w-full text-xs items-start gap-1 mt-1'):
                    ui.label('Status:').classes('text-gray-500 font-bold')
                    ui.label(_status_text).classes('text-yellow-400/90 font-mono')

                # ── Separator ──
                ui.separator()

                # ── Main progress bar ──
                if _total_urls > 0:
                    _pct = min(_total_done / _total_urls, 1.0)
                    with ui.row().classes('w-full gap-2 items-center'):
                        ui.linear_progress(value=_pct, show_value=False) \
                            .props('size=24px color=green-600 track-color=gray-700') \
                            .classes('flex-1')
                        ui.label(f'{int(_pct * 100)}%').classes(
                            'text-sm font-mono text-gray-300 w-12 text-right')

                # ── Separator ──
                ui.separator()

                # ── Host queue ──
                ui.label('URLs in queue per host').classes('text-xs text-gray-400 font-bold')
                if _host_queue:
                    _sorted_hosts = sorted(_host_queue.items(), key=lambda x: -x[1])
                    for _h_idx, (_host, _hcount) in enumerate(_sorted_hosts):
                        with ui.row().classes('w-full items-center gap-1 py-0.5 px-1 rounded').style(
                                'background: rgba(6,182,212,0.06)' if _h_idx % 2 == 0 else ''):
                            ui.label(_host).classes('text-xs font-mono truncate flex-1 min-w-0 text-gray-400')
                            ui.label(str(_hcount)).classes(
                                'text-xs font-mono w-16 shrink-0 text-right text-yellow-200/90')
                else:
                    ui.label('Collecting host data...').classes('text-xs text-gray-500 italic py-1')

                # ── Separator ──
                ui.separator()

                # ── Downloads table ──
                ui.label('Downloads').classes('text-xs text-gray-400 font-bold')
                with ui.column().classes('w-full gap-0.5'):
                    # Column header
                    with ui.row().classes('w-full items-center gap-1 text-xs text-gray-500 font-bold px-1 mb-1'):
                        ui.label('Filename').classes('flex-1 min-w-0')
                        ui.label('Type').classes('w-12 shrink-0')
                        ui.label('Host').classes('w-22 shrink-0')
                        ui.label('Size').classes('w-24 shrink-0 text-right')
                        ui.label('Speed').classes('w-16 shrink-0 text-right')
                        ui.label('Progress').classes('w-28 shrink-0 text-right')
                    if _active_files:
                        for af_idx, af in enumerate(_active_files[:10]):
                            fn = af.get('filename', '?')
                            host = af.get('host', '')
                            dl = af.get('downloaded', 0)
                            total = af.get('total_bytes', 0)
                            spd = af.get('speed', 0.0)
                            pct_val = min(dl / total, 1.0) if total > 0 else 0
                            pct_str = f'{int(pct_val * 100)}%' if total > 0 else '?'
                            size_str = _fmt_size(total) if total > 0 else _fmt_size(dl)
                            speed_str = _fmt_speed(spd) if spd > 0 else '—'
                            fn_display = fn[:28] + '…' if len(fn) > 29 else fn
                            _ext = os.path.splitext(fn)[1].lower()[:6] if '.' in fn else ''
                            with ui.row().classes('w-full items-center gap-1 py-0.5 px-1 rounded').style(
                                    'background: rgba(6,182,212,0.06)' if af_idx % 2 == 0 else ''):
                                ui.label(fn_display).classes(
                                    'text-xs font-mono truncate flex-1 min-w-0 text-yellow-200/90')
                                ui.label(_ext).classes('text-xs font-mono text-gray-500 w-12 shrink-0')
                                ui.label(host).classes('text-xs font-mono text-gray-400 w-22 shrink-0 truncate')
                                ui.label(size_str).classes('text-xs font-mono w-24 shrink-0 text-right text-gray-300')
                                ui.label(speed_str).classes(
                                    'text-xs font-mono w-16 shrink-0 text-right text-cyan-400/80')
                                with ui.row().classes('w-28 shrink-0 gap-1 items-center justify-end'):
                                    bar_color = 'cyan-500' if pct_val < 1.0 else 'green-500'
                                    ui.linear_progress(value=pct_val, show_value=False) \
                                        .props(f'size=14px color={bar_color} track-color=gray-700') \
                                        .classes('w-14')
                                    ui.label(pct_str).classes('text-xs font-mono text-gray-300 w-8 text-right')
                    else:
                        ui.label('Preparing downloads...').classes(
                            'text-xs text-gray-500 italic py-2 px-1')

        if not active:
            with dl_container:
                ui.label('Start a download from the Models tab.').classes(
                    'text-gray-400 py-8 text-center text-lg')

    _update_cancel_bar()
    _render()
    # Update data every 2s; cancel bar only on model-list change
    ui.timer(2.0, _render, active=True)
    ui.timer(2.0, _update_cancel_bar, active=True)


# ── Main Page ─────────────────────────────────────────────────────────

def main_page():
    ui.dark_mode().enable()
    ui.query('body').classes('bg-gray-950')
    title = settings.site_title or 'Simp URL Fetcher'
    ui.label(title).classes('text-2xl font-bold mb-4')

    with ui.tabs().classes('w-full') as tabs:
        tab_info = ui.tab('Info', icon='info')
        tab_logs = ui.tab('Logs', icon='terminal')
        tab_settings = ui.tab('Settings', icon='settings')
        tab_crawler = ui.tab('Crawler', icon='cloud_download')
        tab_extract = ui.tab('Extract', icon='auto_awesome')
        tab_models = ui.tab('Models', icon='folder')
        tab_download = ui.tab('Download', icon='download')
        tab_sort = ui.tab('Sort', icon='sort')
        tab_rename = ui.tab('Rename', icon='drive_file_rename_outline')
        tab_imex = ui.tab('Import/Export', icon='import_export')

    with ui.tab_panels(tabs, value=tab_info).classes('w-full'):
        with ui.tab_panel(tab_info):
            build_info_tab()
        with ui.tab_panel(tab_logs):
            build_logs_tab()
        with ui.tab_panel(tab_settings):
            build_settings_tab()
        with ui.tab_panel(tab_crawler):
            build_crawler_tab()
        with ui.tab_panel(tab_extract):
            build_extract_tab()
        with ui.tab_panel(tab_models):
            build_models_tab()
        with ui.tab_panel(tab_download):
            build_download_tab()
        with ui.tab_panel(tab_sort):
            build_sort_tab()
        with ui.tab_panel(tab_rename):
            build_rename_tab()
        with ui.tab_panel(tab_imex):
            build_import_export_tab()


# ── Graceful Shutdown ─────────────────────────────────────────────────

def _shutdown_cleanup() -> None:
    """Cancel all running operations on shutdown."""
    log('Shutting down gracefully...', level='SYS')
    # Cancel all downloads
    for ev in _cancel_events.values():
        ev.set()
    _cancel_events.clear()
    # Cancel crawler
    _crawl_cancel.set()


# ── Entry Point ───────────────────────────────────────────────────────

def start_app():
    # Mark any stale in-progress downloads as complete (survives server restart)
    for mname in list(registry.models.keys()):
        entry = registry.get(mname)
        if entry and entry.get('download', {}).get('status', '') not in ('', 'complete'):
            entry['download']['status'] = 'complete'
            log(f'Marked stale download for {mname} as complete', level='SYS')
    registry.save()

    # Register shutdown handler (runs when server stops for any reason)
    app.on_shutdown(_shutdown_cleanup)

    # Register signal handler for Ctrl+C — overrides uvicorn's handler to
    # ensure clean shutdown message and cancel all operations
    def _handle_signal(signum, frame):
        _shutdown_cleanup()
        print(f'\nReceived signal {signum}, shutting down...')
        app.shutdown()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, _handle_signal)

    # Auto-check for updates on startup (non-blocking)
    threading.Thread(target=lambda: (
        time.sleep(3),
        _check_for_updates(),
        log(f'Startup update check: '
            f'{"update available" if _update_state.get("update_available") else "up to date"}'
            f' on {_update_state.get("local_branch", "?")}',
            level='SYS'),
    ), daemon=True).start()

    ui.page('/')(main_page)
    ui.run(
        host='0.0.0.0',
        port=8080,
        title=settings.site_title or 'Simp URL Fetcher',
        favicon='📥',
        storage_secret='simp-url-fetcher-secret',
        reload=False,
    )


if __name__ == '__main__':
    start_app()
