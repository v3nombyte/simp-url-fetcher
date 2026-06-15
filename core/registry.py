"""Persistent registry of processed models."""

import os
import json
import threading
from datetime import datetime


class ModelRegistry:
    """Tracks extracted models and their processing status across sessions."""

    def __init__(self, path: str):
        self.path = path
        self.models: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    self.models = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.models = {}

    def save(self):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.models, f, indent=2)
        os.replace(tmp, self.path)  # atomic on Linux

    def register_extraction(self, model_name: str, forum: str, total_urls: int,
                            total_posts: int, file_paths: dict | None = None,
                            mode: str = 'normal'):
        """Register or update a model extraction for a specific mode.

        Stores file paths per mode so all scan types are visible in the UI.
        Top-level fields reflect the most recently registered mode.
        """
        entry = self.models.get(model_name, {})
        entry['model_name'] = model_name
        entry['forum'] = forum or ''

        if 'modes' not in entry:
            entry['modes'] = {}

        entry['modes'][mode] = {
            'total_urls': total_urls,
            'total_posts': total_posts,
            'extracted_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'file_paths': file_paths or {},
        }

        # Top-level summary reflects the latest mode registered
        entry['total_urls'] = total_urls
        entry['total_posts'] = total_posts
        entry['extracted_at'] = entry['modes'][mode]['extracted_at']

        # Preserve legacy single file_paths key for fallback
        entry['file_paths'] = file_paths or entry.get('file_paths', {})

        entry.setdefault('sorted', False)
        entry.setdefault('sorted_at', '')

        self.models[model_name] = entry
        self.save()

    def get_modes(self, model_name: str) -> dict:
        """Return the per-mode dict for a model, or empty dict."""
        entry = self.models.get(model_name)
        if entry:
            return entry.get('modes', {})
        return {}

    def update_file_sizes(self, model_name: str):
        """Refresh download sizes by stat'ing stored file paths."""
        entry = self.models.get(model_name)
        if not entry:
            return
        paths = entry.get('file_paths', {})
        all_urls = paths.get('all_urls', '')
        json_file = paths.get('json', '')
        posts_dir = paths.get('posts_dir', '')

        sizes = {}
        if all_urls and os.path.exists(all_urls):
            sizes['all_urls_size'] = os.path.getsize(all_urls)
        if json_file and os.path.exists(json_file):
            sizes['json_size'] = os.path.getsize(json_file)

        posts_size = 0
        if posts_dir and os.path.isdir(posts_dir):
            for fname in os.listdir(posts_dir):
                fp = os.path.join(posts_dir, fname)
                if os.path.isfile(fp):
                    posts_size += os.path.getsize(fp)
        sizes['posts_size'] = posts_size
        sizes['total_size'] = sum(sizes.values())

        entry['file_sizes'] = sizes
        self.save()

    def register_download(self, model_name: str, total_urls: int, total_files: int,
                          total_bytes: int, failed_count: int, skipped_count: int,
                          resolve_failed_count: int = 0,
                          resolve_failed_file: str = ''):
        """Register completed download results for a model."""
        with self._lock:
            entry = self.models.get(model_name)
            if not entry:
                entry = {'model_name': model_name}
            entry['download'] = {
                'model_name': model_name,
                'total_urls': total_urls,
                'total_files': total_files,
                'total_bytes': total_bytes,
                'failed_count': failed_count,
                'skipped_count': skipped_count,
                'resolve_failed_count': resolve_failed_count,
                'resolve_failed_file': resolve_failed_file,
                'status': 'complete',
                'completed_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
            }
            self.models[model_name] = entry
            self.save()

    def set_download_progress(self, model_name: str, total_files: int,
                               total_bytes: int, failed_count: int,
                               skipped_count: int, status: str = '',
                               total_urls: int = 0,
                               speed_bps: float = 0,
                               current_file: str = '',
                               current_file_size: int = 0,
                               current_file_strategy: str = '',
                               active_queue: list | None = None,
                               active_files: list | None = None,
                               host_queue: dict | None = None,
                               resolve_failed_count: int = 0,
                               resolve_failed_file: str = '',
                               pending_hosts: dict | None = None):
        """Save in-progress download status (survives page refresh during download)."""
        with self._lock:
            entry = self.models.get(model_name)
            if not entry:
                entry = {'model_name': model_name}
            entry['download'] = {
                'total_files': total_files,
                'total_bytes': total_bytes,
                'failed_count': failed_count,
                'skipped_count': skipped_count,
                'resolve_failed_count': resolve_failed_count,
                'resolve_failed_file': resolve_failed_file,
                'total_urls': total_urls,
                'status': status,
                'speed_bps': speed_bps,
                'current_file': current_file,
                'current_file_size': current_file_size,
                'current_file_strategy': current_file_strategy,
                'active_queue': active_queue or [],
                'active_files': active_files or [],
                'host_queue': host_queue or {},
                'pending_hosts': pending_hosts or {},
                'updated_at': datetime.now().strftime('%H:%M:%S'),
            }
            self.models[model_name] = entry
            self.save()

    def get_all(self) -> list[dict]:
        """Return all entries sorted by extraction time (newest first)."""
        entries = list(self.models.values())
        entries.sort(key=lambda e: e.get('extracted_at', ''), reverse=True)
        return entries

    def get(self, model_name: str) -> dict | None:
        return self.models.get(model_name)

    def remove(self, model_name: str):
        self.models.pop(model_name, None)
        self.save()

    def clear(self):
        """Remove all models from the registry."""
        self.models.clear()
        self.save()


def format_size(bytes_val: int) -> str:
    """Human-readable file size."""
    if bytes_val < 1024:
        return f'{bytes_val} B'
    elif bytes_val < 1024 ** 2:
        return f'{bytes_val / 1024:.1f} KB'
    elif bytes_val < 1024 ** 3:
        return f'{bytes_val / 1024 ** 2:.1f} MB'
    else:
        return f'{bytes_val / 1024 ** 3:.2f} GB'
