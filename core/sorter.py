"""Media sorter with quality-aware deduplication.

Sorts downloaded media from an output model folder into a destination
(Downloads) with images/ and videos/ subfolders. Deduplication is
quality-aware: when SHA-256 or perceptual hashes match, the higher-resolution
/ larger file is kept.

In COPY mode: source files are NEVER touched. Dedup only decides which files
to copy (skipping lower-quality duplicates).

In MOVE mode: source files ARE deleted — lower-quality duplicates are removed
before the best quality file is moved.
"""

import os
import shutil
import hashlib
import subprocess
import tempfile
from PIL import Image
import imagehash


VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}
IMAGE_HASH_THRESHOLD = 5  # Perceptual hash distance


def is_video(filepath: str) -> bool:
    return os.path.splitext(filepath)[1].lower() in VIDEO_EXTENSIONS


def is_image(filepath: str) -> bool:
    return os.path.splitext(filepath)[1].lower() in IMAGE_EXTENSIONS


def file_sha256(filepath: str, chunk_size: int = 8192) -> str | None:
    """Compute SHA-256 hash of a file."""
    try:
        h = hashlib.sha256()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(chunk_size), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def image_resolution(filepath: str) -> tuple[int, int]:
    """Return (width, height) of an image."""
    try:
        with Image.open(filepath) as img:
            return img.size
    except Exception:
        return (0, 0)


def image_phash(filepath: str):
    """Perceptual hash of an image."""
    try:
        img = Image.open(filepath).convert("RGB")
        return imagehash.phash(img)
    except Exception:
        return None


def video_phash(filepath: str):
    """Perceptual hash of a video (frame at 1s)."""
    try:
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run(
            ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
             '-ss', '00:00:01', '-i', filepath, '-frames:v', '1', tmp_path],
            check=True, capture_output=True
        )
        h = image_phash(tmp_path)
        os.unlink(tmp_path)
        return h
    except Exception:
        return None


def video_resolution(filepath: str) -> tuple[int, int]:
    """Get video resolution via ffprobe."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height',
             '-of', 'csv=p=0', filepath],
            capture_output=True, text=True, timeout=15
        )
        parts = result.stdout.strip().split(',')
        if len(parts) == 2:
            return (int(parts[0]), int(parts[1]))
    except Exception:
        pass
    return (0, 0)


def file_quality_score(filepath: str) -> float:
    """
    Score a media file by quality. Higher = better.
    Uses resolution area + file size.
    """
    size = os.path.getsize(filepath)
    if is_image(filepath):
        w, h = image_resolution(filepath)
    elif is_video(filepath):
        w, h = video_resolution(filepath)
    else:
        return float(size)
    area = w * h
    return float(area) + (size / 1000.0)


def unique_path(path: str) -> str:
    """If path exists, append _1, _2, etc."""
    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(path):
        path = f"{base}_{counter}{ext}"
        counter += 1
    return path


def _dedup_and_transfer(filepath: str, dest_path: str, seen_sha: dict,
                        image_perceptual: dict, video_perceptual: dict,
                        action: str, stats: dict) -> bool:
    """
    Dedup a single file and copy/move it.

    Returns True if the file should be transferred (not a duplicate), False if
    it was skipped (either because a better copy exists, or it's already at
    dest).

    COPY mode: source files are NEVER deleted. The dedup only skips lower-
    quality duplicates — they remain in source untouched.

    MOVE mode: lower-quality duplicates are deleted from source; the best
    quality copy is moved.
    """
    fname = os.path.basename(filepath)

    # ── File existence check ──
    if not os.path.exists(filepath):
        stats["errors"].append(f"File not found: {fname}")
        return False

    # ── SHA-256 exact dedup ──
    h = file_sha256(filepath)
    if h is None:
        stats["errors"].append(f"Cannot hash: {fname}")
        return False

    if h in seen_sha:
        existing = seen_sha[h]
        if not os.path.exists(existing):
            # Previous best was moved/deleted — current file wins
            seen_sha[h] = filepath
        elif file_quality_score(filepath) > file_quality_score(existing):
            # New file is better — replace
            if action == "move":
                try:
                    os.remove(existing)
                    stats["duplicates_removed"] += 1
                except OSError as e:
                    stats["errors"].append(f"Cannot remove {existing}: {e}")
            seen_sha[h] = filepath
        else:
            # Existing is better — skip/delete new
            if action == "move":
                try:
                    os.remove(filepath)
                    stats["duplicates_removed"] += 1
                except OSError as e:
                    stats["errors"].append(f"Cannot remove {filepath}: {e}")
            return False  # Don't transfer
    else:
        seen_sha[h] = filepath

    if not os.path.exists(filepath):
        return False

    # ── Perceptual image dedup ──
    if is_image(filepath):
        ph = image_phash(filepath)
        if ph:
            for existing_ph, existing_path in list(image_perceptual.items()):
                if not os.path.exists(existing_path):
                    # Previous best was moved/deleted — forget it
                    del image_perceptual[existing_ph]
                    continue
                if ph - existing_ph <= IMAGE_HASH_THRESHOLD:
                    if file_quality_score(filepath) > file_quality_score(existing_path):
                        if action == "move":
                            try:
                                os.remove(existing_path)
                                stats["duplicates_removed"] += 1
                            except OSError as e:
                                stats["errors"].append(f"Cannot remove {existing_path}: {e}")
                        image_perceptual[existing_ph] = filepath
                    else:
                        if action == "move":
                            try:
                                os.remove(filepath)
                                stats["duplicates_removed"] += 1
                            except OSError as e:
                                stats["errors"].append(f"Cannot remove {filepath}: {e}")
                        return False
                    break
            else:
                image_perceptual[ph] = filepath
        if not os.path.exists(filepath):
            return False

    # ── Perceptual video dedup ──
    if is_video(filepath):
        ph = video_phash(filepath)
        if ph:
            for existing_ph, existing_path in list(video_perceptual.items()):
                if not os.path.exists(existing_path):
                    del video_perceptual[existing_ph]
                    continue
                if ph - existing_ph <= IMAGE_HASH_THRESHOLD:
                    if file_quality_score(filepath) > file_quality_score(existing_path):
                        if action == "move":
                            try:
                                os.remove(existing_path)
                                stats["duplicates_removed"] += 1
                            except OSError as e:
                                stats["errors"].append(f"Cannot remove {existing_path}: {e}")
                        video_perceptual[existing_ph] = filepath
                    else:
                        if action == "move":
                            try:
                                os.remove(filepath)
                                stats["duplicates_removed"] += 1
                            except OSError as e:
                                stats["errors"].append(f"Cannot remove {filepath}: {e}")
                        return False
                    break
            else:
                video_perceptual[ph] = filepath
        if not os.path.exists(filepath):
            return False

    # ── Check destination ──
    if os.path.exists(dest_path):
        dest_hash = file_sha256(dest_path)
        if dest_hash == h:
            stats["already_exist"] += 1
            return False
        dest_path = unique_path(dest_path)

    # ── Do transfer ──
    try:
        if action == "move":
            shutil.move(filepath, dest_path)
        else:
            shutil.copy2(filepath, dest_path)
        return True
    except OSError as e:
        stats["errors"].append(f"Cannot {action} {fname}: {e}")
        return False


def sort_model_folder(model_name: str, source_base: str, dest_base: str,
                      action: str = "copy", dedup: bool = True,
                      progress_callback=None) -> dict:
    """
    Sort media from a model's output folder into dest_base/{model}/images/ and
    .../videos/, with quality-aware deduplication.

    Args:
        model_name: Name of the model folder (e.g. 'Rincospl').
        source_base: Base directory where model folders live (e.g. 'output/').
        dest_base: Base directory where sorted folders go (e.g. '~/Downloads').
        action: 'copy' or 'move'. In copy mode source is never modified.
        dedup: Whether to deduplicate via SHA-256 + perceptual hashing.
        progress_callback: Optional callable(processed, total, filename).

    Returns:
        dict with keys: images_moved, videos_moved, unknown_moved,
                        duplicates_removed, total_files, total_processed,
                        errors (list), already_exist (int).
    """
    stats = {
        "images_moved": 0,
        "videos_moved": 0,
        "unknown_moved": 0,
        "images_total": 0,
        "videos_total": 0,
        "unknown_total": 0,
        "unknown_extensions": set(),  # e.g. {'.txt', '.nfo'}
        "duplicates_removed": 0,
        "already_exist": 0,
        "total_files": 0,
        "total_processed": 0,
        "errors": [],
    }

    source_dir = os.path.join(source_base, model_name)
    if not os.path.isdir(source_dir):
        stats["errors"].append(f"Source folder not found: {source_dir}")
        return stats

    # Destination directories
    dest_model = os.path.join(dest_base, model_name)
    dest_images = os.path.join(dest_model, "images")
    dest_videos = os.path.join(dest_model, "videos")
    dest_unknown = os.path.join(dest_model, "unknown")
    os.makedirs(dest_images, exist_ok=True)
    os.makedirs(dest_videos, exist_ok=True)
    os.makedirs(dest_unknown, exist_ok=True)

    # ── Collect ALL files from source (not just media) ──
    all_files = []  # Each entry: (filepath, category)
    for root, dirs, files in os.walk(source_dir, topdown=True):
        for f in sorted(files):
            fpath = os.path.join(root, f)
            fext = os.path.splitext(f)[1].lower()
            if fext in IMAGE_EXTENSIONS:
                all_files.append((fpath, "image"))
                stats["images_total"] += 1
            elif fext in VIDEO_EXTENSIONS:
                all_files.append((fpath, "video"))
                stats["videos_total"] += 1
            else:
                all_files.append((fpath, "unknown"))
                stats["unknown_total"] += 1

    stats["total_files"] = len(all_files)
    if stats["total_files"] == 0:
        stats["errors"].append(f"No files found in {source_dir}")
        return stats

    # ── Dedup tracking (images/videos only) ──
    seen_sha = {}             # sha256 -> best source filepath
    image_perceptual = {}     # phash -> best source filepath
    video_perceptual = {}     # phash -> best source filepath

    processed = 0
    transferred = 0

    for filepath, category in all_files:
        processed += 1
        fname = os.path.basename(filepath)
        ext = os.path.splitext(fname)[1].lower()
        if progress_callback:
            progress_callback(processed, stats["total_files"], fname)

        if not os.path.exists(filepath):
            stats["errors"].append(f"File not found: {fname}")
            continue

        if category == "image":
            dest_dir = dest_images
            ok = _dedup_and_transfer(
                filepath, os.path.join(dest_dir, fname), seen_sha,
                image_perceptual, video_perceptual,
                action, stats,
            )
            if ok:
                transferred += 1
                stats["images_moved"] += 1

        elif category == "video":
            dest_dir = dest_videos
            ok = _dedup_and_transfer(
                filepath, os.path.join(dest_dir, fname), seen_sha,
                image_perceptual, video_perceptual,
                action, stats,
            )
            if ok:
                transferred += 1
                stats["videos_moved"] += 1

        else:  # unknown
            stats["unknown_extensions"].add(ext)
            # No dedup for unknowns — just copy/move to unknown/ folder
            dest = os.path.join(dest_unknown, fname)
            if os.path.exists(dest):
                dest = unique_path(dest)
            try:
                if action == "move":
                    shutil.move(filepath, dest)
                else:
                    shutil.copy2(filepath, dest)
                transferred += 1
                stats["unknown_moved"] += 1
            except OSError as e:
                stats["errors"].append(f"Cannot {action} {fname}: {e}")

    stats["total_processed"] = processed

    # ── Clean up empty subdirectories in move mode ──
    if action == "move":
        for root, dirs, files in os.walk(source_dir, topdown=False):
            try:
                if root != source_dir and not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass
        # Remove model folder itself if empty
        try:
            if os.path.isdir(source_dir) and not os.listdir(source_dir):
                os.rmdir(source_dir)
        except OSError:
            pass

    return stats



# ── Legacy wrapper (in-place clips/pics sorting) ──
def process_folder(folder_path: str, remove_duplicates: bool = True,
                   progress_callback=None) -> dict:
    """Legacy in-place sorter — sorts into clips/ and pics/ inside folder."""
    stats = {
        "videos_moved": 0,
        "images_moved": 0,
        "duplicates_removed": 0,
        "errors": [],
    }

    if not os.path.isdir(folder_path):
        stats["errors"].append(f"Folder not found: {folder_path}")
        return stats

    clips_path = os.path.join(folder_path, "clips")
    pics_path = os.path.join(folder_path, "pics")
    os.makedirs(clips_path, exist_ok=True)
    os.makedirs(pics_path, exist_ok=True)

    seen_sha = {}
    image_perceptual = {}
    video_perceptual = {}

    all_files = []
    for root, dirs, files in os.walk(folder_path, topdown=True):
        dirs[:] = [d for d in dirs if d not in ("clips", "pics")]
        if root == clips_path or root == pics_path:
            continue
        for f in files:
            fpath = os.path.join(root, f)
            if is_video(fpath) or is_image(fpath):
                all_files.append(fpath)

    for filepath in all_files:
        fname = os.path.basename(filepath)
        if progress_callback:
            progress_callback(1, 1, fname)

        if is_video(filepath):
            dest_dir = clips_path
        elif is_image(filepath):
            dest_dir = pics_path
        else:
            continue

        dest = os.path.join(dest_dir, fname)
        dest = unique_path(dest)

        ok = _dedup_and_transfer(
            filepath, dest, seen_sha,
            image_perceptual, video_perceptual,
            "move", stats,
        )
        if ok:
            if is_video(filepath):
                stats["videos_moved"] += 1
            else:
                stats["images_moved"] += 1

    for root, dirs, files in os.walk(folder_path, topdown=False):
        if root in (clips_path, pics_path):
            continue
        if root == folder_path:
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass

    return stats
