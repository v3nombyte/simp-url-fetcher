"""Auto-extract downloaded archives (ZIP/RAR) if they contain media files.

After a file is downloaded, if it's a .zip or .rar archive, this module
extracts it and checks whether the contents are images or videos. If yes,
the archive is deleted and the extracted media files remain in the
download folder. If no media content is found, the archive is left as-is.
"""

import os
import shutil
import tempfile
import zipfile
import subprocess
import logging

log = logging.getLogger(__name__)

# Extensions we know how to extract
ARCHIVE_EXTENSIONS = {'.zip', '.rar'}

# Media extensions (mirrors sorter.py constants)
MEDIA_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff',
    '.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v',
}


def _has_media_in_dir(dirpath: str) -> bool:
    """Check if a directory contains any image/video files."""
    for root, _dirs, files in os.walk(dirpath):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in MEDIA_EXTENSIONS:
                return True
    return False


def _extract_zip(archive_path: str, extract_dir: str) -> bool:
    """Extract a ZIP archive. Returns True on success."""
    try:
        with zipfile.ZipFile(archive_path, 'r') as zf:
            zf.extractall(extract_dir)
        return True
    except (zipfile.BadZipFile, OSError, RuntimeError) as e:
        log.warning('Failed to extract ZIP %s: %s', os.path.basename(archive_path), e)
        return False


def _extract_rar(archive_path: str, extract_dir: str) -> bool:
    """Extract a RAR archive via unrar or 7z subprocess."""
    # Try unrar first, then 7z
    for cmd_template in [
        ['unrar', 'x', '-y', archive_path],
        ['7z', 'x', '-y', archive_path],
    ]:
        try:
            result = subprocess.run(
                cmd_template,
                cwd=extract_dir,
                capture_output=True,
                timeout=120,
            )
            if result.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    log.warning('No RAR extractor found (tried unrar, 7z) for %s',
                os.path.basename(archive_path))
    return False


def try_extract_archive(archive_path: str, target_dir: str) -> bool:
    """Try to extract a ZIP or RAR archive if it contains media files.

    Args:
        archive_path: Full path to the archive file.
        target_dir: Directory where extracted files should be placed.

    Returns:
        True if archive was successfully extracted and media content found
        (archive deleted). False if archive was left untouched (no media,
        extraction failed, or unknown format).
    """
    ext = os.path.splitext(archive_path)[1].lower()

    if ext not in ARCHIVE_EXTENSIONS:
        return False

    if not os.path.isfile(archive_path):
        return False

    # Extract to a temporary directory
    tmp_dir = tempfile.mkdtemp(prefix='simp_extract_')
    try:
        if ext == '.zip':
            ok = _extract_zip(archive_path, tmp_dir)
        elif ext == '.rar':
            ok = _extract_rar(archive_path, tmp_dir)
        else:
            return False

        if not ok:
            return False

        # Check if extracted content includes media files
        if not _has_media_in_dir(tmp_dir):
            return False  # Leave archive as-is, no media content

        # Move media files to target_dir
        media_count = 0
        for root, _dirs, files in os.walk(tmp_dir):
            for f in files:
                ext_f = os.path.splitext(f)[1].lower()
                if ext_f in MEDIA_EXTENSIONS:
                    src = os.path.join(root, f)
                    dst = os.path.join(target_dir, f)
                    # Avoid name collisions
                    if os.path.exists(dst):
                        base, ext2 = os.path.splitext(f)
                        counter = 1
                        while os.path.exists(dst):
                            dst = os.path.join(target_dir, f'{base}_{counter}{ext2}')
                            counter += 1
                    try:
                        shutil.move(src, dst)
                        media_count += 1
                    except OSError:
                        pass

        if media_count > 0:
            # Delete the archive — we have the extracted files
            try:
                os.remove(archive_path)
            except OSError:
                pass
            log.info('Extracted %s → %d media files, archive deleted',
                     os.path.basename(archive_path), media_count)
            return True

        return False

    finally:
        # Clean up temp directory
        shutil.rmtree(tmp_dir, ignore_errors=True)
