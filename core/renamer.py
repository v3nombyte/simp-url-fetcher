"""Sequential file renamer.

Renames all files in a folder to base_name_1.ext, base_name_2.ext, ...
Skips files already matching the naming pattern. Safe to re-run.
"""

import os
import re


def rename_files(folder: str, base_name: str, progress_callback=None) -> dict:
    """
    Rename all files in a folder sequentially.

    Args:
        folder: Path to folder containing files.
        base_name: Base name for the new filenames.
        progress_callback: Optional callable(filename, action) where
                          action is 'rename', 'skip', or 'error'.

    Returns:
        dict with renamed_count, skipped_count, errors.
    """
    stats = {"renamed_count": 0, "skipped_count": 0, "errors": []}

    if not os.path.isdir(folder):
        stats["errors"].append(f"Folder not found: {folder}")
        return stats

    files = [f for f in os.listdir(folder)
             if os.path.isfile(os.path.join(folder, f))]

    if not files:
        return stats

    # Find existing numbers already used
    pattern = rf"^{re.escape(base_name)}_(\d+)\.[^.]+$"
    used_numbers = set()
    for f in files:
        m = re.match(pattern, f)
        if m:
            used_numbers.add(int(m.group(1)))

    for file in sorted(files):
        full_path = os.path.join(folder, file)

        # Skip files already matching pattern
        if re.match(pattern, file):
            stats["skipped_count"] += 1
            if progress_callback:
                progress_callback(file, 'skip')
            continue

        ext = os.path.splitext(file)[1]
        next_num = 1
        while next_num in used_numbers:
            next_num += 1

        new_name = f"{base_name}_{next_num}{ext}"
        new_path = os.path.join(folder, new_name)

        # Safety: if new_name exists, skip forward
        while os.path.exists(new_path):
            next_num += 1
            new_name = f"{base_name}_{next_num}{ext}"
            new_path = os.path.join(folder, new_name)

        try:
            os.rename(full_path, new_path)
            used_numbers.add(next_num)
            stats["renamed_count"] += 1
            if progress_callback:
                progress_callback(file, 'rename', new_name)
        except OSError as e:
            stats["errors"].append(f"Cannot rename {file}: {e}")
            if progress_callback:
                progress_callback(file, 'error', str(e))

    return stats
