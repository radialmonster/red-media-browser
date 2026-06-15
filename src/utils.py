#!/usr/bin/env python3
"""
Utility Functions for Red Media Browser

This module contains utility functions for processing URLs, handling media paths,
and other helper functions used throughout the application.
"""

import os
import re
import logging
import html
import shutil
import json
import time
import threading
import hashlib

from urllib.parse import urlparse, unquote, quote, parse_qs
from PyQt6.QtGui import QImage
from praw.models import Redditor, Subreddit
from constants import REPORT_CACHE_TTL_SECONDS

# Basic Logging Configuration
logger = logging.getLogger(__name__)

# --- Media File Extension Constants ---
IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
VIDEO_EXTENSIONS = ['.mp4', '.webm', '.avi', '.mov', '.mkv', '.flv']
ANIMATED_IMAGE_EXTENSIONS = ['.gif']
IMAGE_VISUAL_HASH_DISTANCE_THRESHOLD = 6
IMAGE_VISUAL_HASH_MIN_BITS = 8
IMAGE_VISUAL_HASH_MAX_BITS = 56
NON_ACTIONABLE_AUTHOR_NAMES = {"", "[deleted]", "unknown", "none"}

# Directory structure constants
MIN_ID_LENGTH_FOR_SUBDIRS = 6

# --- File Cache Preload Globals ---
_file_cache_set = None
_file_cache_lock = threading.Lock()

def preload_file_cache():
    """
    Recursively scan the media cache directory and build a set of all cached file paths (relative to cache dir).
    Should be called once at program startup.
    """
    global _file_cache_set
    cache_dir = get_cache_dir()
    file_set = set()
    for root, dirs, files in os.walk(cache_dir):
        for fname in files:
            # Exclude metadata and index files
            if fname.endswith('.json') or fname == 'submission_index.json':
                continue
            # Store relative path from cache_dir for fast lookup
            try:
                rel_path = os.path.relpath(os.path.join(root, fname), cache_dir)
                file_set.add(rel_path.replace(os.sep, '/'))  # Use posix separators
            except ValueError as e:
                # Handle case where paths are on different drives (Windows)
                logger.warning(f"Could not create relative path for {fname}: {e}")
                # Use a normalized fallback that maintains consistency
                abs_path = os.path.join(root, fname)
                # Create a pseudo-relative path using the filename and parent dir
                fallback_path = f"external/{os.path.basename(root)}/{fname}"
                file_set.add(fallback_path.replace(os.sep, '/'))
    with _file_cache_lock:
        _file_cache_set = file_set
    logger.info(f"Preloaded file cache with {len(_file_cache_set)} media files.")

def force_repair_cache_index():
    """Force a complete cache repair regardless of apparent consistency."""
    return repair_cache_index(force_repair=True)

def repair_cache_index(force_repair=False):
    """
    Scan all cached media files and ensure there is a metadata file and index entry for each.
    If missing, create a minimal metadata file and update the index.

    Args:
        force_repair (bool): If True, always run repair. If False, only repair if issues detected.
    """
    cache_dir = get_cache_dir()
    metadata_dir = get_metadata_dir()
    index = load_submission_index()

    # Quick check: if we don't have many cached files, repair is fast anyway
    global _file_cache_set
    if _file_cache_set is None:
        logger.warning("File cache not preloaded. Preloading now for repair.")
        preload_file_cache()

    num_cached_files = len(_file_cache_set)
    num_index_entries = len(index)

    # Only run repair if forced, or if there's a significant mismatch suggesting missing entries
    if not force_repair:
        # Be more lenient with the threshold - many index entries are metadata-only (text posts, failed downloads)
        # Only trigger repair if we have significantly MORE media files than index entries (missing metadata)
        # If index entries > media files, that's normal (text posts, failed downloads, etc.)

        if num_index_entries >= num_cached_files:
            # More index entries than files is normal, only repair if ratio is extreme
            ratio = num_index_entries / max(num_cached_files, 1)
            if ratio < 5.0:  # Allow up to 5x more index entries than media files
                logger.debug(f"Cache appears consistent ({num_cached_files} files, {num_index_entries} index entries, ratio {ratio:.1f}x). Skipping repair.")
                return
            else:
                logger.info(f"Excessive index entries detected ({num_cached_files} files vs {num_index_entries} index entries, ratio {ratio:.1f}x). Running cleanup repair...")
        else:
            # More files than index entries suggests missing metadata
            variance_threshold = max(10, num_cached_files * 0.05)  # 5% or at least 10 files
            missing_entries = num_cached_files - num_index_entries
            if missing_entries < variance_threshold:
                logger.debug(f"Cache appears consistent ({num_cached_files} files, {num_index_entries} index entries, {missing_entries} missing). Skipping repair.")
                return
            else:
                logger.info(f"Missing index entries detected ({missing_entries} files without metadata). Running repair...")

    logger.info("Starting cache repair/index warming...")
    repaired = 0
    migrated = 0
    report_backfilled = 0

    # Build a set of all cache paths from metadata for O(1) lookup
    logger.info("Building cache path lookup from metadata...")
    existing_cache_paths = set()

    for sub_id, meta_rel in index.items():
        meta_path = os.path.join(cache_dir, meta_rel.replace('/', os.sep))
        if os.path.exists(meta_path):
            try:
                meta = read_metadata_file(meta_path)
                if not meta:
                    continue

                legacy_match = (
                    meta.get('cache_path') and
                    meta.get('media_url') and
                    not _normalize_metadata_media_assets(meta)
                )
                # Backfill the dedicated report-freshness field onto legacy metadata
                # that recorded reports before `report_last_checked_utc` existed. Report
                # reads already fall back to `last_checked_utc`, so behavior is unchanged
                # either way, but writing the dedicated field here makes legacy records
                # unambiguous for offline/external tooling. Only seed it when reports were
                # actually cached (`report_count` present) and a legacy timestamp exists.
                needs_report_backfill = (
                    'report_count' in meta
                    and meta.get('report_last_checked_utc') is None
                    and meta.get('last_checked_utc') is not None
                )
                if legacy_match:
                    meta['media_assets'] = [{
                        'requested_url': None,
                        'media_url': meta.get('media_url'),
                        'cache_path': meta.get('cache_path'),
                        'last_checked_utc': meta.get('last_checked_utc'),
                    }]
                if needs_report_backfill:
                    meta['report_last_checked_utc'] = meta['last_checked_utc']

                if legacy_match or needs_report_backfill:
                    if write_metadata_file(meta_path, meta):
                        if legacy_match:
                            migrated += 1
                        if needs_report_backfill:
                            report_backfilled += 1
                    else:
                        logger.warning(f"Failed to backfill metadata fields for: {meta_path}")

                for normalized_path in _iter_metadata_cache_paths(meta):
                    existing_cache_paths.add(normalized_path)
            except Exception:
                continue

    logger.info(f"Found {len(existing_cache_paths)} existing cache paths in metadata")
    logger.info("Checking for missing metadata entries...")

    for rel_path in _file_cache_set:
        abs_path = os.path.join(cache_dir, rel_path)
        normalized_abs_path = os.path.normpath(abs_path)

        if normalized_abs_path in existing_cache_paths:
            continue  # Already indexed

        # Not found, create a new metadata file and index entry
        fname = os.path.basename(rel_path)
        base_id = os.path.splitext(fname)[0]

        # Ensure the synthetic ID is long enough for standard directory structure
        if len(base_id) < MIN_ID_LENGTH_FOR_SUBDIRS:
            # Pad with hash to ensure consistent length
            hash_suffix = hashlib.md5(rel_path.encode()).hexdigest()[:8]
            base_id = f"{base_id}_{hash_suffix}"

        submission_id = f"cachefile_{base_id}"
        meta_path = get_metadata_file_path(submission_id)
        minimal_metadata = {
            "id": submission_id,
            "cache_path": abs_path,
            "media_url": None,
            "media_assets": [],
            "title": f"Recovered cached file {fname}",
            "last_checked_utc": time.time(),
        }
        if write_metadata_file(meta_path, minimal_metadata):
            try:
                rel_meta_path = os.path.relpath(meta_path, cache_dir).replace(os.sep, '/')
            except ValueError:
                # Handle case where paths are on different drives (Windows)
                rel_meta_path = meta_path.replace(os.sep, '/')
            index[submission_id] = rel_meta_path
            repaired += 1

    if repaired > 0:
        try:
            save_submission_index()
            logger.info(
                f"Cache repair complete. Added {repaired} missing metadata/index entries, "
                f"migrated {migrated} legacy metadata files, and backfilled "
                f"report_last_checked_utc on {report_backfilled} records."
            )
        except Exception as e:
            logger.error(f"Cache repair failed to save index after adding {repaired} entries: {e}")
    elif migrated > 0 or report_backfilled > 0:
        logger.info(
            f"Cache repair complete. Migrated {migrated} legacy metadata files and "
            f"backfilled report_last_checked_utc on {report_backfilled} records."
        )
    else:
        logger.info("Cache repair complete. No missing entries found.")

def file_in_cache_preloaded(rel_path):
    """
    Check if a file (relative to cache dir, posix style) is in the preloaded file cache set.
    """
    global _file_cache_set
    with _file_cache_lock:
        if _file_cache_set is None:
            logger.warning("File cache set not preloaded. Call preload_file_cache() first.")
            return False
        return rel_path in _file_cache_set

def register_cached_file_path(file_path):
    """
    Add a newly created cache file to the in-memory file cache set.
    This keeps `file_exists_in_cache()` accurate after downloads complete.
    """
    global _file_cache_set
    with _file_cache_lock:
        if _file_cache_set is None:
            return

        cache_dir = get_cache_dir()
        try:
            rel_path = os.path.relpath(file_path, cache_dir).replace(os.sep, '/')
        except ValueError:
            rel_path = file_path.replace(os.sep, '/')

        _file_cache_set.add(rel_path)


def _normalize_cache_path(cache_path):
    """Normalize a stored cache path for stable comparisons across metadata formats."""
    if not cache_path:
        return None

    normalized_path = cache_path.replace('\\', '/').replace('/', os.sep)
    return os.path.normpath(normalized_path)

# --- Metadata Cache Globals ---
_submission_index = None
_index_lock = threading.Lock()
_index_path = None
_metadata_lock = threading.Lock()
_removal_log_lock = threading.Lock()
_media_usage_index_lock = threading.Lock()

def ensure_directory(directory):
    """Ensure that the specified directory exists."""
    os.makedirs(directory, exist_ok=True)
    return directory

def get_cache_dir():
    """Return the application's cache directory."""
    cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
    return ensure_directory(cache_dir)

def get_removal_log_path():
    """Return the persistent per-subreddit removal log path."""
    return os.path.join(get_cache_dir(), "moderation_removals.json")

def get_media_usage_index_path():
    """Return the persistent reverse index of cached media usage."""
    return os.path.join(get_cache_dir(), "media_usage_index.json")

def calculate_file_sha256(file_path):
    """Return the sha256 hex digest for a cached media file."""
    if not file_path or not os.path.exists(file_path):
        return None

    digest = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                if chunk:
                    digest.update(chunk)
        return digest.hexdigest()
    except Exception as e:
        logger.exception(f"Could not hash cached media file {file_path}: {e}")
        return None

def calculate_image_visual_hash(file_path):
    """Return a 64-bit dHash for an image file as 16 hex characters."""
    if not file_path or not os.path.exists(file_path):
        return None

    try:
        image = QImage(file_path)
        if image.isNull():
            return None

        scaled = image.convertToFormat(QImage.Format.Format_Grayscale8).scaled(9, 8)
        if scaled.isNull() or scaled.width() < 9 or scaled.height() < 8:
            return None

        value = 0
        for y in range(8):
            for x in range(8):
                left = scaled.pixelColor(x, y).value()
                right = scaled.pixelColor(x + 1, y).value()
                value = (value << 1) | (1 if left > right else 0)

        return f"{value:016x}"
    except Exception as e:
        logger.exception(f"Could not calculate visual hash for image {file_path}: {e}")
        return None

def _visual_hash_distance(hash_a, hash_b):
    """Return Hamming distance between two 64-bit visual hashes."""
    if not hash_a or not hash_b:
        return None
    try:
        return (int(str(hash_a), 16) ^ int(str(hash_b), 16)).bit_count()
    except Exception:
        return None

def _is_useful_image_visual_hash(visual_hash):
    """Return whether an image dHash has enough signal for duplicate matching."""
    if not visual_hash:
        return False
    try:
        bit_count = int(str(visual_hash), 16).bit_count()
        return IMAGE_VISUAL_HASH_MIN_BITS <= bit_count <= IMAGE_VISUAL_HASH_MAX_BITS
    except Exception:
        return False

def load_media_usage_index():
    """Load media usage grouped by exact media hash or fallback URL."""
    path = get_media_usage_index_path()
    with _media_usage_index_lock:
        if not os.path.exists(path):
            return {"version": 1, "updated_at_utc": None, "items": {}}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"version": 1, "updated_at_utc": None, "items": {}}
            if not isinstance(data.get("items"), dict):
                data["items"] = {}
            if not isinstance(data.get("visual_items"), dict):
                data["visual_items"] = {}
            data.setdefault("version", 1)
            data.setdefault("updated_at_utc", None)
            return data
        except json.JSONDecodeError:
            logger.error(f"Error decoding media usage index file: {path}")
            return {"version": 1, "updated_at_utc": None, "items": {}}
        except Exception as e:
            logger.exception(f"Error loading media usage index: {e}")
            return {"version": 1, "updated_at_utc": None, "items": {}}

def save_media_usage_index(index_data):
    """Persist the media usage index atomically."""
    if not isinstance(index_data, dict):
        return False

    path = get_media_usage_index_path()
    temp_path = path + ".tmp"
    index_data.setdefault("version", 1)
    index_data.setdefault("items", {})
    index_data.setdefault("visual_items", {})
    index_data["updated_at_utc"] = time.time()

    with _media_usage_index_lock:
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(index_data, f, indent=2)
            try:
                os.replace(temp_path, path)
            except OSError as e:
                logger.warning(f"os.replace failed for media usage index, using fallback copy: {e}")
                shutil.copy2(temp_path, path)
                os.remove(temp_path)
            return True
        except Exception as e:
            logger.exception(f"Error saving media usage index: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return False

def load_removal_log():
    """Load removal history grouped by subreddit and author."""
    path = get_removal_log_path()
    with _removal_log_lock:
        if not os.path.exists(path):
            return {}

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.exception(f"Error loading removal log {path}: {e}")
            return {}

def save_removal_log(removal_log):
    """Persist removal history atomically."""
    path = get_removal_log_path()
    ensure_directory(os.path.dirname(path))
    tmp_path = f"{path}.tmp"
    with _removal_log_lock:
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(removal_log, f, indent=2, sort_keys=True)
            os.replace(tmp_path, path)
            return True
        except Exception as e:
            logger.exception(f"Error saving removal log {path}: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return False

def _submission_author_name(submission):
    author = getattr(submission, 'author', None)
    if author is None:
        return "[deleted]"
    if hasattr(author, 'name'):
        return author.name or "[deleted]"
    return str(author) or "[deleted]"

def _submission_subreddit_name(submission):
    subreddit = getattr(submission, 'subreddit', None)
    if subreddit is None:
        return "unknown"
    if hasattr(subreddit, 'display_name'):
        return subreddit.display_name or "unknown"
    return str(subreddit) or "unknown"

def build_removal_log_entry(submission, subreddit_name=None):
    """Create a serializable removal-log entry for a submission."""
    submission_id = getattr(submission, 'id', None)
    return {
        "submission_id": submission_id,
        "fullname": getattr(submission, 'fullname', None) or (f"t3_{submission_id}" if submission_id else None),
        "title": getattr(submission, 'title', '') or '',
        "author": _submission_author_name(submission),
        "subreddit": subreddit_name or _submission_subreddit_name(submission),
        "permalink": getattr(submission, 'permalink', '') or '',
        "url": getattr(submission, 'url', '') or '',
        "score": getattr(submission, 'score', None),
        "num_comments": getattr(submission, 'num_comments', None),
        "removed_at_utc": time.time(),
    }

def _submission_id_from_fullname(fullname):
    fullname = str(fullname or "")
    if fullname.startswith("t3_"):
        return fullname[3:]
    return fullname or None

def _normalize_removal_log_entry(entry):
    normalized_entry = dict(entry)
    fullname = normalized_entry.get("fullname")
    submission_id = normalized_entry.get("submission_id") or _submission_id_from_fullname(fullname)
    if submission_id and not fullname:
        fullname = f"t3_{submission_id}"

    normalized_entry["submission_id"] = submission_id
    normalized_entry["fullname"] = fullname
    normalized_entry["author"] = normalized_entry.get("author") or "[deleted]"
    normalized_entry["subreddit"] = normalized_entry.get("subreddit") or "unknown"
    normalized_entry.setdefault("removed_at_utc", time.time())
    normalized_entry.setdefault("title", "")
    normalized_entry.setdefault("permalink", "")
    normalized_entry.setdefault("url", "")
    return normalized_entry

def _remove_post_from_subreddit_log(subreddit_log, submission_id=None, fullname=None):
    changed = False
    for author, author_log in list(subreddit_log.items()):
        posts = author_log.get("posts", [])
        filtered_posts = []
        for post in posts:
            post_submission_id = post.get("submission_id")
            post_fullname = post.get("fullname")
            if (
                (submission_id and post_submission_id == submission_id) or
                (fullname and post_fullname == fullname)
            ):
                changed = True
                continue
            filtered_posts.append(post)

        if filtered_posts:
            filtered_posts.sort(key=lambda post: post.get("removed_at_utc") or 0, reverse=True)
            author_log["posts"] = filtered_posts
            author_log["count"] = len(filtered_posts)
            author_log["last_removed_at_utc"] = filtered_posts[0].get("removed_at_utc")
        else:
            del subreddit_log[author]

    return changed

def _upsert_removed_entry(removal_log, entry):
    entry = _normalize_removal_log_entry(entry)
    submission_id = entry.get("submission_id")
    fullname = entry.get("fullname")
    author = entry.get("author") or "[deleted]"
    subreddit = (entry.get("subreddit") or "unknown").lower()

    if not submission_id and not fullname:
        logger.warning("Cannot record removal without submission id or fullname.")
        return 0

    subreddit_log = removal_log.setdefault(subreddit, {})
    _remove_post_from_subreddit_log(
        subreddit_log,
        submission_id=submission_id,
        fullname=fullname,
    )

    author_log = subreddit_log.setdefault(author, {"count": 0, "posts": []})
    posts = list(author_log.get("posts", []))
    posts.append(entry)
    posts.sort(key=lambda post: post.get("removed_at_utc") or 0, reverse=True)

    author_log["posts"] = posts
    author_log["count"] = len(posts)
    author_log["last_removed_at_utc"] = posts[0].get("removed_at_utc") if posts else None
    return author_log["count"]

def record_removed_submission(submission, subreddit_name=None):
    """Record one removed submission and return the user's current removal count."""
    entry = build_removal_log_entry(submission, subreddit_name=subreddit_name)
    removal_log = load_removal_log()
    removal_count = _upsert_removed_entry(removal_log, entry)

    if save_removal_log(removal_log):
        logger.info(
            "removal_log_recorded subreddit=%s author=%s count=%s submission_id=%s title=%s",
            (entry.get("subreddit") or "unknown").lower(),
            entry.get("author") or "[deleted]",
            removal_count,
            entry.get("submission_id"),
            entry.get("title", ""),
        )

    return removal_count

def record_removed_log_entry(entry):
    """Record one removal entry already extracted from a moderator log."""
    removal_log = load_removal_log()
    removal_count = _upsert_removed_entry(removal_log, entry)
    if save_removal_log(removal_log):
        normalized_entry = _normalize_removal_log_entry(entry)
        logger.info(
            "removal_log_backfill_recorded subreddit=%s author=%s count=%s fullname=%s mod=%s",
            (normalized_entry.get("subreddit") or "unknown").lower(),
            normalized_entry.get("author") or "[deleted]",
            removal_count,
            normalized_entry.get("fullname"),
            normalized_entry.get("moderator", ""),
        )
    return removal_count

def remove_approved_submission_from_removal_log(submission_id=None, fullname=None):
    """Remove an approved submission from removal history."""
    if not submission_id and not fullname:
        return False

    removal_log = load_removal_log()
    changed = False

    for subreddit, subreddit_log in list(removal_log.items()):
        changed = _remove_post_from_subreddit_log(
            subreddit_log,
            submission_id=submission_id,
            fullname=fullname,
        ) or changed
        if not subreddit_log:
            del removal_log[subreddit]

    if changed and save_removal_log(removal_log):
        logger.info(
            "removal_log_entry_removed submission_id=%s fullname=%s",
            submission_id,
            fullname,
        )

    return changed

def build_removal_log_entry_from_mod_action(action):
    """Create a removal-log entry from a PRAW ModAction-like object or dict."""
    getter = action.get if isinstance(action, dict) else lambda key, default=None: getattr(action, key, default)
    fullname = getter("target_fullname")
    return {
        "submission_id": _submission_id_from_fullname(fullname),
        "fullname": fullname,
        "title": getter("target_title") or "",
        "author": str(getter("target_author") or "[deleted]"),
        "subreddit": str(getter("subreddit") or "").strip() or "unknown",
        "permalink": getter("target_permalink") or "",
        "url": getter("target_url") or "",
        "removed_at_utc": float(getter("created_utc") or time.time()),
        "moderator": str(getter("mod") or ""),
        "mod_action_id": str(getter("id") or ""),
        "mod_action_details": str(getter("details") or ""),
    }

def backfill_removal_log_from_mod_actions(actions, allowed_removal_moderators=None):
    """
    Replay moderator log actions into the local removal log.

    Only removelink actions from allowed_removal_moderators are counted.
    Approvelink actions remove matching posts regardless of approver.
    """
    allowed = {
        str(name).strip().lower()
        for name in (allowed_removal_moderators or [])
        if str(name).strip()
    }
    removal_log = load_removal_log()
    stats = {
        "actions_seen": 0,
        "removals_recorded": 0,
        "removals_skipped_by_moderator": 0,
        "approvals_applied": 0,
    }

    sorted_actions = sorted(
        list(actions or []),
        key=lambda action: float(
            (action.get("created_utc") if isinstance(action, dict) else getattr(action, "created_utc", 0))
            or 0
        ),
    )

    for action in sorted_actions:
        getter = action.get if isinstance(action, dict) else lambda key, default=None: getattr(action, key, default)
        action_name = str(getter("action") or "")
        fullname = getter("target_fullname")
        if not fullname:
            continue

        stats["actions_seen"] += 1
        if action_name == "removelink":
            moderator = str(getter("mod") or "").strip().lower()
            if allowed and moderator not in allowed:
                stats["removals_skipped_by_moderator"] += 1
                continue
            _upsert_removed_entry(
                removal_log,
                build_removal_log_entry_from_mod_action(action),
            )
            stats["removals_recorded"] += 1
        elif action_name == "approvelink":
            subreddit = str(getter("subreddit") or "").strip().lower()
            subreddit_log = removal_log.get(subreddit)
            if subreddit_log and _remove_post_from_subreddit_log(
                subreddit_log,
                submission_id=_submission_id_from_fullname(fullname),
                fullname=fullname,
            ):
                stats["approvals_applied"] += 1
                if not subreddit_log:
                    del removal_log[subreddit]

    save_removal_log(removal_log)
    logger.info(
        "removal_log_backfill_done actions=%s removals_recorded=%s skipped_by_moderator=%s approvals_applied=%s",
        stats["actions_seen"],
        stats["removals_recorded"],
        stats["removals_skipped_by_moderator"],
        stats["approvals_applied"],
    )
    return stats

def get_removed_user_summaries(subreddit_name):
    """Return users sorted by most currently removed posts for one subreddit."""
    if not subreddit_name:
        return []

    removal_log = load_removal_log()
    subreddit_log = removal_log.get(subreddit_name.lower(), {})
    summaries = []
    for author, author_log in subreddit_log.items():
        posts = list(author_log.get("posts", []))
        count = len(posts)
        if count <= 0:
            continue
        posts.sort(key=lambda post: post.get("removed_at_utc") or 0, reverse=True)
        summaries.append({
            "author": author,
            "count": count,
            "last_removed_at_utc": posts[0].get("removed_at_utc"),
            "posts": posts,
        })

    summaries.sort(
        key=lambda item: (
            item.get("count", 0),
            item.get("last_removed_at_utc") or 0,
        ),
        reverse=True,
    )
    return summaries

def get_domain_cache_dir(domain):
    """Return the cache directory for a specific domain."""
    domain_dir = os.path.join(get_cache_dir(), domain)
    return ensure_directory(domain_dir)

def clean_filename(filename):
    """Clean a filename to make it safe for the filesystem."""
    if not filename:
        return "unknown_file"
    # Replace problematic characters for cross-platform filesystem safety
    unsafe_chars = '<>:"|?*\\/'
    for char in unsafe_chars:
        filename = filename.replace(char, '_')
    # Also handle query parameters and other URL artifacts
    filename = filename.replace('&', '_').replace('=', '_')
    # Remove or replace any remaining control characters
    filename = ''.join(c if ord(c) >= 32 else '_' for c in filename)
    # Ensure it's not too long (max 255 chars for most filesystems)
    if len(filename) > 200:  # Leave room for extensions
        filename = filename[:200]
    return filename
    
def normalize_redgifs_url(url):
    """
    Normalize a RedGIFs URL to a standard format.
    """
    logger.debug(f"Original RedGIFs URL: {url}")
    if "v3.redgifs.com/watch/" in url:
        url = url.replace("v3.redgifs.com/watch/", "www.redgifs.com/watch/")
        logger.debug(f"Normalized v3.redgifs URL to: {url}")
    if "redgifs.com/ifr/" in url:
        url = url.replace("/ifr/", "/watch/")
        logger.debug(f"Normalized iframe URL to: {url}")
    # Also handle mobile URLs
    if "m.redgifs.com" in url:
        url = url.replace("m.redgifs.com", "www.redgifs.com")
        logger.debug(f"Normalized mobile URL to: {url}")
    return url

def ensure_json_url(url):
    """
    Convert a Reddit post URL to its JSON equivalent.
    """
    if not url.endswith(".json"):
        if url.endswith("/"):
            url = url[:-1]
        url = url + ".json"
    return url

def _extract_gallery_urls(media_metadata, submission_id_str, source_type):
    """Helper function to extract URLs from gallery metadata."""
    if not isinstance(media_metadata, dict):
        logger.warning(f"media_metadata is not a dict for {submission_id_str} ({source_type}), type: {type(media_metadata)}")
        return None

    try:
        urls = [
            html.unescape(media['s']['u'])
            for media in media_metadata.values()
            if isinstance(media, dict) and 's' in media and isinstance(media['s'], dict) and 'u' in media['s']
        ]
        if urls:
            logger.debug(f"Extracted {len(urls)} gallery URLs from {source_type} {submission_id_str}.")
            return urls
        logger.warning(f"{source_type.title()} gallery detected but no valid URLs found in media_metadata for {submission_id_str}")
    except Exception as e:
        logger.error(f"Error processing {source_type} gallery metadata for {submission_id_str}: {e}")

    return None

def _try_direct_url(data_source, submission_id_str, source_type):
    """Helper function to extract direct URL from a data source."""
    url = data_source.get('url') if hasattr(data_source, 'get') else getattr(data_source, 'url', None)
    if url:
        if any(url.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
            logger.debug(f"Using direct image URL from {source_type} {submission_id_str}: {url}")
            return [url]
        logger.debug(f"{source_type.title()} URL found for {submission_id_str}, but not a direct image link: {url}")
        return [url]  # Return anyway for further processing
    return None

def extract_image_urls(submission):
    """
    Given a submission object (PRAW or SimpleNamespace/dict), returns a list of image URLs.
    Handles regular posts, gallery posts, and crossposts.
    """
    submission_id_str = getattr(submission, 'id', 'N/A')
    logger.debug(f"Extracting image URLs for submission ID: {submission_id_str}")

    # Check for crosspost first
    crosspost_parent_list = getattr(submission, 'crosspost_parent_list', None)
    if crosspost_parent_list and isinstance(crosspost_parent_list, list) and len(crosspost_parent_list) > 0:
        parent_data = crosspost_parent_list[0]
        logger.debug(f"Processing {submission_id_str} as crosspost.")

        # Try gallery first
        if parent_data.get('is_gallery') and parent_data.get('media_metadata'):
            gallery_urls = _extract_gallery_urls(parent_data.get('media_metadata'), submission_id_str, "crosspost parent")
            if gallery_urls:
                return gallery_urls

        # Try direct URL
        direct_urls = _try_direct_url(parent_data, submission_id_str, "crosspost parent")
        if direct_urls:
            return direct_urls

        logger.debug(f"No gallery or direct URL found in crosspost parent for {submission_id_str}")

    # Process main submission
    logger.debug(f"Processing {submission_id_str} as regular post (or fallback from crosspost)")

    # Try gallery
    if getattr(submission, 'is_gallery', False) and getattr(submission, 'media_metadata', None):
        gallery_urls = _extract_gallery_urls(getattr(submission, 'media_metadata'), submission_id_str, "main submission")
        if gallery_urls:
            return gallery_urls

        # Gallery failed, try direct URL fallback
        url = getattr(submission, 'url', None)
        if url:
            logger.debug(f"Falling back to direct URL for gallery failure: {url}")
            return [url]

    # Try direct URL
    direct_urls = _try_direct_url(submission, submission_id_str, "main submission")
    if direct_urls:
        return direct_urls

    logger.error(f"Could not extract any image URL for submission {submission_id_str}")
    return []

def is_image_file(file_path):
    """Check if the file is an image based on extension."""
    ext = os.path.splitext(file_path.lower())[1]
    return ext in IMAGE_EXTENSIONS

def is_video_file(file_path):
    """Check if the file is a video based on extension."""
    ext = os.path.splitext(file_path.lower())[1]

    # Special case for RedGifs URLs that may not have proper extensions
    if 'redgifs.com' in file_path.lower() and any(domain in file_path.lower() for domain in ['media.redgifs.com', 'thumbs2.redgifs.com']):
        return True

    return ext in VIDEO_EXTENSIONS

def is_animated_image(file_path):
    """Check if the file is an animated image (gif, etc)."""
    ext = os.path.splitext(file_path.lower())[1]
    return ext in ANIMATED_IMAGE_EXTENSIONS

def _detect_redgifs_media_type(file_path):
    """Helper function to detect media type for RedGifs URLs."""
    ext = os.path.splitext(file_path.lower())[1]

    if ext in IMAGE_EXTENSIONS and ext not in ANIMATED_IMAGE_EXTENSIONS:
        logger.debug(f"RedGifs image detected: {file_path}")
        return "image"
    elif ext in ANIMATED_IMAGE_EXTENSIONS:
        logger.debug(f"RedGifs animated image detected: {file_path}")
        return "animated_image"
    elif ext in VIDEO_EXTENSIONS or ext == '':  # Empty extension might be a video
        logger.debug(f"RedGifs video detected: {file_path}")
        return "video"

    return None

def _detect_media_type_by_signature(file_path):
    """Helper function to detect media type by file signature/magic bytes."""
    try:
        with open(file_path, 'rb') as f:
            header = f.read(16)  # Read first 16 bytes for signature detection

            # Check for MP4 signature (ftyp box)
            if len(header) >= 8 and header[4:8] == b'ftyp':
                return "video"

            # WebM signature (matroska container)
            if header.startswith(b'\x1a\x45\xdf\xa3'):
                return "video"

            # JPEG signature
            if header.startswith(b'\xff\xd8\xff'):
                return "image"

            # PNG signature
            if header.startswith(b'\x89\x50\x4e\x47\x0d\x0a\x1a\x0a'):
                return "image"

            # WebP signature
            if len(header) >= 12 and header[0:4] == b'RIFF' and header[8:12] == b'WEBP':
                return "image"

            # BMP signature
            if header.startswith(b'BM'):
                return "image"

            # GIF signature (and check if it's animated)
            if header.startswith(b'GIF87a') or header.startswith(b'GIF89a'):
                # We'd need more complex logic to check if it's animated
                # Just assume GIF is animated for now
                return "animated_image"

    except Exception as e:
        logger.error(f"Error determining file type from contents: {e}")

    return None

def get_media_type(file_path):
    """Determine the media type of a file."""
    # Special case for RedGifs content - check extension first
    if 'redgifs.com' in file_path.lower():
        redgifs_type = _detect_redgifs_media_type(file_path)
        if redgifs_type:
            return redgifs_type

    # Normal file type detection by extension
    if is_image_file(file_path):
        return "animated_image" if is_animated_image(file_path) else "image"

    if is_video_file(file_path):
        return "video"

    # If extension detection fails, try file signature detection
    if os.path.exists(file_path):
        signature_type = _detect_media_type_by_signature(file_path)
        if signature_type:
            return signature_type

    # Default if all else fails
    return "unknown"

def file_exists_in_cache(url):
    """Check if a file exists in the cache based on its URL, using preloaded file cache if available."""
    existing_cache_path = get_existing_cache_path_for_url(url)
    if existing_cache_path:
        return True

    if not get_cache_path_for_url(url):
        logger.debug(f"file_exists_in_cache: No cache path for URL: {url}")
        return False

    return False

def _cache_path_in_preloaded_set(cache_path):
    """Return True when an absolute cache path is present in the preloaded media set."""
    cache_dir = get_cache_dir()
    try:
        rel_path = os.path.relpath(cache_path, cache_dir).replace(os.sep, '/')
    except ValueError:
        rel_path = cache_path.replace(os.sep, '/')

    return file_in_cache_preloaded(rel_path)

def _iter_equivalent_cache_paths(cache_path):
    """Yield cache paths that may hold the same media after content-type extension correction."""
    if not cache_path:
        return

    yield cache_path

    base_path, ext = os.path.splitext(cache_path)
    ext = ext.lower()
    equivalent_exts = {
        ".jpeg": (".jpg",),
        ".jpg": (".jpeg",),
    }.get(ext, ())

    for equivalent_ext in equivalent_exts:
        yield f"{base_path}{equivalent_ext}"

def get_existing_cache_path_for_url(url):
    """Return the existing cache path for a URL, including equivalent content-type extensions."""
    cache_path = get_cache_path_for_url(url)
    if not cache_path:
        logger.debug(f"get_existing_cache_path_for_url: No cache path for URL: {url}")
        return None

    global _file_cache_set
    for candidate_path in _iter_equivalent_cache_paths(cache_path):
        if _file_cache_set is not None:
            if _cache_path_in_preloaded_set(candidate_path):
                logger.debug(f"get_existing_cache_path_for_url: Cache HIT for {candidate_path}")
                return candidate_path
            continue

        if os.path.exists(candidate_path):
            logger.debug(f"get_existing_cache_path_for_url: Disk HIT for {candidate_path}")
            return candidate_path

    logger.debug(f"get_existing_cache_path_for_url: Cache MISS for {cache_path}")
    return None

def _normalize_url_for_caching(url):
    """Helper function to normalize URL by removing query parameters for media files."""
    try:
        parsed_url = urlparse(url)
        path = unquote(parsed_url.path)
        filename = os.path.basename(path)

        # If the URL has a query string and looks like an image/video, ignore the query for cache path
        all_media_extensions = IMAGE_EXTENSIONS + VIDEO_EXTENSIONS
        if parsed_url.query and any(filename.lower().endswith(ext) for ext in all_media_extensions):
            url_no_query = url.split('?', 1)[0]
            parsed_url = urlparse(url_no_query)
            path = unquote(parsed_url.path)
            filename = os.path.basename(path)

        return parsed_url, path, filename
    except Exception:
        return None, None, None

def _get_query_format_extension(url, domain):
    """Return the real media extension requested by known CDN query parameters."""
    if "redd.it" not in domain:
        return None

    try:
        query_values = parse_qs(urlparse(url).query)
    except Exception:
        return None

    requested_format = (query_values.get("format") or [""])[0].lower()
    return {
        "jpg": ".jpg",
        "jpeg": ".jpg",
        "pjpg": ".jpg",
        "png": ".png",
        "webp": ".webp",
        "gif": ".gif",
        "mp4": ".mp4",
        "webm": ".webm",
    }.get(requested_format)

def _handle_redgifs_filename(url, domain, filename):
    """Helper function to handle RedGifs-specific filename logic."""
    original_ext = os.path.splitext(filename)[1].lower()
    all_media_extensions = IMAGE_EXTENSIONS + VIDEO_EXTENSIONS

    # If already has a valid media extension, keep as is
    if original_ext in all_media_extensions:
        return filename

    # Handle watch/ifr URLs
    if "/watch/" in url or "/ifr/" in url:
        match = re.search(r'(?:watch|ifr)/([A-Za-z0-9]+)', url)
        if match:
            redgifs_id = match.group(1)
            return f"{redgifs_id}.mp4"
        else:
            url_hash = hashlib.md5(url.encode()).hexdigest()
            return f"redgif_watch_hash_{url_hash}.mp4"

    # Handle i.redgifs.com URLs without extension
    if not original_ext and "i.redgifs.com" in domain:
        url_hash = hashlib.md5(url.encode()).hexdigest()
        logger.warning(f"RedGifs URL has no extension, using hash: {url}")
        return f"redgif_noext_hash_{url_hash}"

    # Fallback for unhandled RedGifs formats
    url_hash = hashlib.md5(url.encode()).hexdigest()
    logger.warning(f"Unhandled RedGifs URL format for cache path, using hash: {url}")
    return f"redgif_fallback_hash_{url_hash}{original_ext}"

def _handle_missing_filename(url, domain):
    """Helper function to generate filename when URL has no filename."""
    if url.endswith('.mp4'):
        extension = ".mp4"
    elif url.endswith('.jpg') or url.endswith('.jpeg'):
        extension = ".jpg"
    elif url.endswith('.png'):
        extension = ".png"
    elif url.endswith('.gif'):
        extension = ".gif"
    elif url.endswith('.webm'):
        extension = ".webm"
    elif "redgifs.com" in domain:
        extension = ".mp4"
    else:
        extension = ""

    return f"downloaded_media{extension}"

def get_cache_path_for_url(url):
    """
    Get the cache file path for a URL.
    Handles special cases for RedGifs and ensures a safe, unique filename.
    Strips query parameters for image/video URLs to ensure consistent cache hits.
    """
    try:
        # Parse and normalize URL
        parsed_url, path, filename = _normalize_url_for_caching(url)
        if not parsed_url:
            return None

        domain = parsed_url.netloc
        if not domain:
            return None

        domain_dir = get_domain_cache_dir(domain)

        # Special handling for RedGifs domains
        if "redgifs.com" in domain:
            filename = _handle_redgifs_filename(url, domain, filename)

        # Handle URLs without a filename (e.g., root path '/')
        elif not filename or filename == '/':
            filename = _handle_missing_filename(url, domain)

        query_format_ext = _get_query_format_extension(url, domain)
        if query_format_ext:
            filename_base, filename_ext = os.path.splitext(filename)
            if filename_ext.lower() != query_format_ext:
                filename = f"{filename_base}{query_format_ext}"

        filename = clean_filename(filename)
        return os.path.join(domain_dir, filename)
    except Exception as e:
        logger.exception(f"Error determining cache path for URL {url}: {e}")
        return None

# --- Metadata Cache Functions ---

def get_metadata_dir():
    """Return the directory for storing metadata JSON files."""
    metadata_dir = os.path.join(get_cache_dir(), 'metadata')
    return ensure_directory(metadata_dir)

def get_metadata_file_path(submission_id):
    """
    Generate the structured path for a submission's metadata JSON file.
    Example: cache/metadata/t3/ab/cd/ef/t3_abcdef.json
    """
    if not submission_id or not isinstance(submission_id, str):
        logger.error(f"Invalid submission_id provided: {submission_id}")
        return None
        
    # Remove prefix like 't3_' if present for directory structure
    base_id = submission_id.split('_')[-1]
    if len(base_id) < MIN_ID_LENGTH_FOR_SUBDIRS: # Ensure we have enough characters for subdirs
        logger.warning(f"Submission ID too short for standard directory structure: {submission_id}")
        # Use a fallback structure or just place it directly? For now, place directly under prefix.
        prefix = submission_id.split('_')[0] if '_' in submission_id else 'unknown'
        subdir = os.path.join(get_metadata_dir(), prefix)
    else:
        # Use parts of the ID for subdirectories: e.g., /t3/ab/cd/ef/
        prefix = submission_id.split('_')[0] if '_' in submission_id else 'unknown'
        subdir = os.path.join(get_metadata_dir(), prefix, base_id[0:2], base_id[2:4], base_id[4:6])

    ensure_directory(subdir)
    return os.path.join(subdir, f"{submission_id}.json")

def _get_index_path():
    """Get the path to the submission index file."""
    global _index_path
    if _index_path is None:
        _index_path = os.path.join(get_cache_dir(), 'submission_index.json')
    return _index_path

def load_submission_index(force_reload=False):
    """
    Load the submission index from JSON file.
    Uses a cached version unless force_reload is True.
    Thread-safe access to the global index cache.
    """
    global _submission_index
    index_path = _get_index_path()

    with _index_lock:
        if _submission_index is not None and not force_reload:
            return _submission_index

        if os.path.exists(index_path):
            try:
                with open(index_path, 'r', encoding='utf-8') as f:
                    _submission_index = json.load(f)
                logger.debug(f"Loaded submission index with {len(_submission_index)} entries.")
                return _submission_index
            except json.JSONDecodeError:
                logger.error(f"Error decoding submission index file: {index_path}. Starting fresh.")
                _submission_index = {}
                return _submission_index
            except Exception as e:
                logger.exception(f"Error loading submission index: {e}")
                _submission_index = {} # Fallback to empty dict on error
                return _submission_index
        else:
            logger.debug("Submission index file not found. Initializing empty index.")
            _submission_index = {}
            return _submission_index

def save_submission_index():
    """
    Save the current submission index to JSON file.
    Thread-safe. Writes to a temporary file first.
    """
    global _submission_index
    index_path = _get_index_path()
    temp_path = index_path + ".tmp"

    with _index_lock:
        if _submission_index is None:
            logger.warning("Attempted to save submission index, but it's not loaded.")
            return # Or maybe load it first? For now, just return.

        try:
            # Write to temporary file
            with open(temp_path, 'w', encoding='utf-8') as f:
                # Create a copy to avoid "dictionary changed size during iteration" error
                # since other threads might be modifying the global dict
                index_copy = _submission_index.copy()
                json.dump(index_copy, f, indent=2) # Use indent for readability

            # Rename temporary file to actual index file (atomic on most systems)
            try:
                os.replace(temp_path, index_path)
                logger.debug(f"Saved submission index with {len(_submission_index)} entries.")
            except OSError as e:
                # Fallback to copy + delete if replace fails
                logger.warning(f"os.replace failed, using fallback copy method: {e}")
                shutil.copy2(temp_path, index_path)
                os.remove(temp_path)
                logger.debug(f"Saved submission index with {len(_submission_index)} entries (fallback method).")
        except Exception as e:
            logger.exception(f"Error saving submission index: {e}")
            # Clean up temp file if it exists
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass # Ignore error during cleanup

def read_metadata_file(metadata_path):
    """Read and parse a specific metadata JSON file."""
    if not metadata_path or not os.path.exists(metadata_path):
        return None
    try:
        with open(metadata_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.error(f"Error decoding metadata file: {metadata_path}")
        return None # Or raise? For now return None
    except Exception as e:
        logger.exception(f"Error reading metadata file {metadata_path}: {e}")
        return None

def write_metadata_file(metadata_path, metadata):
    """Write metadata to a specific JSON file."""
    if not metadata_path or not metadata:
        logger.error("Missing metadata_path or metadata for writing.")
        return False
    try:
        # Ensure directory exists (should be handled by get_metadata_file_path, but double check)
        os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
        temp_path = metadata_path + ".tmp"
        with _metadata_lock:
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, indent=2)  # Use indent for readability
                try:
                    os.replace(temp_path, metadata_path)
                except OSError as e:
                    # Fallback to copy + delete if replace fails
                    logger.warning(f"os.replace failed for {metadata_path}, using fallback copy method: {e}")
                    shutil.copy2(temp_path, metadata_path)
                    os.remove(temp_path)
                return True
            except Exception as e:
                logger.exception(f"Error writing metadata file {metadata_path}: {e}")
                # Clean up temp file if it exists and writing failed
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                return False
    except Exception as e:
        logger.exception(f"Error preparing to write metadata file {metadata_path}: {e}")
        return False


def update_report_metadata_cache(submission, report_count, report_reasons):
    """
    Persist report data for a submission so rebuilt widgets can reuse it.
    """
    if not submission or not hasattr(submission, 'id'):
        logger.error("Invalid submission object provided to update_report_metadata_cache.")
        return False

    submission_id = submission.id
    metadata_path = get_metadata_file_path(submission_id)
    if not metadata_path:
        logger.error(f"Could not determine metadata path for submission {submission_id}.")
        return False

    existing_metadata = read_metadata_file(metadata_path) or {}
    metadata = dict(existing_metadata)
    metadata.update(_filter_submission_data(submission))
    metadata['report_count'] = max(0, int(report_count or 0))
    metadata['report_reasons'] = [str(reason) for reason in (report_reasons or [])]
    report_checked_utc = time.time()
    metadata['report_last_checked_utc'] = report_checked_utc

    if existing_metadata:
        compare_keys = ['report_count', 'report_reasons']
        existing_last_checked = float(
            existing_metadata.get('report_last_checked_utc')
            or existing_metadata.get('last_checked_utc', 0)
            or 0
        )
        if (
            all(existing_metadata.get(key) == metadata.get(key) for key in compare_keys)
            and time.time() - existing_last_checked < REPORT_CACHE_TTL_SECONDS
        ):
            logger.debug(
                "Report metadata for submission %s is already up to date; no update needed.",
                submission_id,
            )
            return True

    if not write_metadata_file(metadata_path, metadata):
        logger.error(f"Failed to write report metadata for submission {submission_id}.")
        return False

    index = load_submission_index()
    try:
        relative_metadata_path = os.path.relpath(metadata_path, get_cache_dir())
    except ValueError:
        relative_metadata_path = metadata_path
    relative_metadata_path = relative_metadata_path.replace(os.sep, '/')
    index[submission_id] = relative_metadata_path
    save_submission_index()
    logger.debug("Updated report metadata cache and index for submission %s", submission_id)
    return True

def _filter_submission_data(submission):
    """
    Filter a PRAW Submission object's attributes for caching.

    Removes internal PRAW objects, comments, and simplifies complex objects
    (e.g., author and subreddit are stored as their names).
    Only JSON-serializable types are included.
    """
    if not submission:
        return {}

    exclude_keys = {
        'comments', '_reddit', '_mod', '_fetched', '_info_params',
        'comment_limit', 'comment_sort',
    }

    simplify_keys = {
        'author': lambda obj: getattr(obj, 'name', None) if isinstance(obj, Redditor) else str(obj),
        'subreddit': lambda obj: getattr(obj, 'display_name', None) if isinstance(obj, Subreddit) else str(obj),
    }

    data = {}
    for attr in dir(submission):
        if attr.startswith('_') or attr in exclude_keys:
            continue

        try:
            value = getattr(submission, attr)
            if callable(value):
                continue
            if attr in simplify_keys:
                data[attr] = simplify_keys[attr](value)
            elif isinstance(value, (str, int, float, bool, list, dict, type(None))):
                data[attr] = value
        except Exception as e:
            logger.warning(f"Could not access attribute '{attr}' for submission {getattr(submission, 'id', 'N/A')}: {e}")
            continue

    # Ensure essential fields are present
    essential = ['id', 'name', 'title', 'permalink', 'url']
    for key in essential:
        if key not in data:
            try:
                data[key] = getattr(submission, key, None)
            except Exception:
                data[key] = None

    return data


def _normalize_metadata_media_assets(metadata):
    """Return a normalized per-asset cache list from metadata."""
    assets = metadata.get('media_assets')
    if not isinstance(assets, list):
        return []

    normalized_assets = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue

        cache_path = asset.get('cache_path')
        media_url = asset.get('media_url')
        if not cache_path or not media_url:
            continue

        normalized_assets.append({
            'requested_url': asset.get('requested_url'),
            'media_url': media_url,
            'cache_path': cache_path,
            'media_type': asset.get('media_type'),
            'media_sha256': asset.get('media_sha256'),
            'image_visual_hash': asset.get('image_visual_hash'),
            'visual_hash_type': asset.get('visual_hash_type'),
            'last_checked_utc': asset.get('last_checked_utc'),
        })

    return normalized_assets


def _media_usage_group_key(asset):
    """Return the reverse-index key for a media asset."""
    media_sha256 = asset.get('media_sha256')
    if media_sha256:
        return f"sha256:{media_sha256}"

    media_url = asset.get('media_url')
    if media_url:
        return f"url:{media_url}"

    return None


def _is_indexable_media_asset(asset):
    """Return whether a cached asset appears to be real image/video media."""
    cache_path = asset.get('cache_path') if isinstance(asset, dict) else None
    if not cache_path or not os.path.exists(cache_path):
        return False

    return get_media_type(cache_path) in {"image", "animated_image", "video"}


def _build_media_usage_record(metadata, asset):
    """Build one media usage index post record from submission metadata."""
    if not isinstance(metadata, dict) or not isinstance(asset, dict):
        return None

    submission_id = metadata.get('id')
    if not submission_id:
        return None

    author = str(metadata.get('author') or "").strip()
    if author.lower() in NON_ACTIONABLE_AUTHOR_NAMES:
        author = "[deleted]"

    return {
        "submission_id": submission_id,
        "fullname": metadata.get('name') or f"t3_{submission_id}",
        "author": author,
        "subreddit": metadata.get('subreddit') or "",
        "title": metadata.get('title') or "",
        "permalink": metadata.get('permalink') or "",
        "created_utc": metadata.get('created_utc'),
        "media_url": asset.get('media_url'),
        "requested_url": asset.get('requested_url'),
        "cache_path": asset.get('cache_path'),
        "media_type": asset.get('media_type'),
        "media_sha256": asset.get('media_sha256'),
        "image_visual_hash": asset.get('image_visual_hash'),
        "visual_hash_type": asset.get('visual_hash_type'),
        "last_seen_utc": time.time(),
    }


def _remove_submission_from_media_usage_index(index_data, submission_id):
    """Remove stale records for one submission from a media usage index."""
    items = index_data.setdefault("items", {})
    empty_keys = []
    for media_key, group in items.items():
        posts = group.get("posts")
        if not isinstance(posts, list):
            empty_keys.append(media_key)
            continue
        filtered_posts = [
            post for post in posts
            if str(post.get("submission_id") or "") != str(submission_id)
        ]
        if filtered_posts:
            group["posts"] = filtered_posts
            group["post_count"] = len(filtered_posts)
            group["author_count"] = len({
                str(post.get("author") or "").strip().lower()
                for post in filtered_posts
                if str(post.get("author") or "").strip()
            })
        else:
            empty_keys.append(media_key)

    for media_key in empty_keys:
        items.pop(media_key, None)


def _add_media_usage_record(index_data, media_key, record):
    """Add or replace one post record in the media usage index."""
    if not media_key or not record:
        return

    items = index_data.setdefault("items", {})
    group = items.setdefault(media_key, {
        "media_key": media_key,
        "match_type": "sha256" if media_key.startswith("sha256:") else "url",
        "media_sha256": record.get("media_sha256"),
        "image_visual_hash": record.get("image_visual_hash"),
        "visual_hash_type": record.get("visual_hash_type"),
        "media_url": record.get("media_url"),
        "cache_path": record.get("cache_path"),
        "posts": [],
    })

    group["media_sha256"] = group.get("media_sha256") or record.get("media_sha256")
    group["image_visual_hash"] = group.get("image_visual_hash") or record.get("image_visual_hash")
    group["visual_hash_type"] = group.get("visual_hash_type") or record.get("visual_hash_type")
    group["media_url"] = group.get("media_url") or record.get("media_url")
    group["cache_path"] = group.get("cache_path") or record.get("cache_path")

    submission_id = str(record.get("submission_id") or "")
    posts = [
        post for post in group.get("posts", [])
        if str(post.get("submission_id") or "") != submission_id
    ]
    posts.append(record)
    posts.sort(key=lambda post: float(post.get("created_utc") or 0), reverse=True)
    group["posts"] = posts
    group["post_count"] = len(posts)
    group["author_count"] = len({
        str(post.get("author") or "").strip().lower()
        for post in posts
        if str(post.get("author") or "").strip()
    })


def _unique_posts_for_media_group(posts, normalized_subreddit=""):
    """Return filtered, de-duplicated post records for a duplicate media group."""
    filtered_posts = []
    seen_submission_ids = set()
    for post in posts or []:
        if not isinstance(post, dict):
            continue
        if normalized_subreddit and str(post.get("subreddit") or "").strip().lower() != normalized_subreddit:
            continue
        submission_id = str(post.get("submission_id") or "")
        if submission_id and submission_id in seen_submission_ids:
            continue
        seen_submission_ids.add(submission_id)
        filtered_posts.append(post)
    return filtered_posts


def _author_count_for_posts(posts):
    """Return the number of distinct actionable authors in a post list."""
    return len({
        str(post.get("author") or "").strip().lower()
        for post in posts or []
        if str(post.get("author") or "").strip().lower() not in NON_ACTIONABLE_AUTHOR_NAMES
    })


def _build_visual_image_duplicate_groups(index_data, distance_threshold=IMAGE_VISUAL_HASH_DISTANCE_THRESHOLD):
    """Build likely-similar image duplicate groups from indexed image visual hashes."""
    items = index_data.get("items", {})
    visual_assets = []

    for media_key, group in items.items():
        posts = group.get("posts")
        if not isinstance(posts, list):
            continue

        visual_hash = group.get("image_visual_hash")
        if not visual_hash:
            for post in posts:
                visual_hash = post.get("image_visual_hash")
                if visual_hash:
                    break
        if not visual_hash:
            continue
        if not _is_useful_image_visual_hash(visual_hash):
            continue

        media_type = group.get("media_type") or (posts[0].get("media_type") if posts else None)
        if media_type not in {"image", "animated_image"}:
            continue

        visual_assets.append({
            "media_key": media_key,
            "visual_hash": visual_hash,
            "media_sha256": group.get("media_sha256"),
            "media_url": group.get("media_url"),
            "cache_path": group.get("cache_path"),
            "posts": posts,
        })

    count = len(visual_assets)
    if count < 2:
        return {}

    parents = list(range(count))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    visual_hash_ints = []
    for asset in visual_assets:
        try:
            visual_hash_ints.append(int(asset["visual_hash"], 16))
        except Exception:
            visual_hash_ints.append(None)

    for left in range(count):
        left_hash = visual_hash_ints[left]
        if left_hash is None:
            continue
        for right in range(left + 1, count):
            right_hash = visual_hash_ints[right]
            if right_hash is None:
                continue
            if (left_hash ^ right_hash).bit_count() <= distance_threshold:
                union(left, right)

    clusters = {}
    for index, asset in enumerate(visual_assets):
        clusters.setdefault(find(index), []).append(asset)

    visual_items = {}
    for cluster_index, assets in clusters.items():
        media_keys = {asset["media_key"] for asset in assets}
        sha_values = {
            asset.get("media_sha256")
            for asset in assets
            if asset.get("media_sha256")
        }
        if len(media_keys) < 2 and len(sha_values) < 2:
            continue

        posts = []
        for asset in assets:
            posts.extend(asset.get("posts") or [])
        unique_posts = _unique_posts_for_media_group(posts)
        author_count = _author_count_for_posts(unique_posts)
        if len(unique_posts) < 2 or author_count < 2:
            continue

        representative_hash = assets[0].get("visual_hash")
        media_key = f"visual_image:{representative_hash}:{cluster_index}"
        visual_items[media_key] = {
            "media_key": media_key,
            "match_type": "visual_image",
            "visual_hash_type": "dhash64",
            "visual_distance_threshold": distance_threshold,
            "image_visual_hash": representative_hash,
            "media_url": assets[0].get("media_url"),
            "cache_path": assets[0].get("cache_path"),
            "posts": unique_posts,
            "post_count": len(unique_posts),
            "author_count": author_count,
        }

    return visual_items


def update_media_usage_index_for_metadata(metadata):
    """Update the reverse media usage index for one submission metadata record."""
    if not isinstance(metadata, dict) or not metadata.get('id'):
        return False

    index_data = load_media_usage_index()
    _remove_submission_from_media_usage_index(index_data, metadata.get('id'))

    for asset in _normalize_metadata_media_assets(metadata):
        if not _is_indexable_media_asset(asset):
            continue
        media_key = _media_usage_group_key(asset)
        record = _build_media_usage_record(metadata, asset)
        _add_media_usage_record(index_data, media_key, record)

    return save_media_usage_index(index_data)


def rebuild_media_usage_index(compute_missing_hashes=True):
    """
    Rebuild the reverse media usage index from all submission metadata.

    When compute_missing_hashes is true, existing metadata is also backfilled
    with sha256 hashes for cached media files.
    """
    cache_dir = get_cache_dir()
    submission_index = load_submission_index(force_reload=True)
    media_usage_index = {"version": 1, "updated_at_utc": None, "items": {}}
    stats = {
        "metadata_records": 0,
        "media_assets": 0,
        "hashed_files": 0,
        "visual_hashed_images": 0,
        "updated_metadata_records": 0,
        "indexed_assets": 0,
        "missing_files": 0,
        "duplicate_groups": 0,
        "visual_duplicate_groups": 0,
    }

    for submission_id, metadata_rel_path in list(submission_index.items()):
        metadata_path = os.path.join(cache_dir, metadata_rel_path.replace('/', os.sep))
        metadata = read_metadata_file(metadata_path)
        if not isinstance(metadata, dict):
            continue

        stats["metadata_records"] += 1
        metadata_changed = False
        assets = _normalize_metadata_media_assets(metadata)

        if not assets and metadata.get('cache_path') and metadata.get('media_url'):
            assets = [{
                'requested_url': None,
                'media_url': metadata.get('media_url'),
                'cache_path': metadata.get('cache_path'),
                'media_type': metadata.get('media_type'),
                'media_sha256': metadata.get('media_sha256'),
                'image_visual_hash': metadata.get('image_visual_hash'),
                'visual_hash_type': metadata.get('visual_hash_type'),
                'last_checked_utc': metadata.get('last_checked_utc'),
            }]
            metadata['media_assets'] = assets
            metadata_changed = True

        updated_assets = []
        for asset in assets:
            stats["media_assets"] += 1
            updated_asset = dict(asset)
            cache_path = updated_asset.get('cache_path')
            media_type = get_media_type(cache_path) if cache_path and os.path.exists(cache_path) else None
            if media_type and updated_asset.get('media_type') != media_type:
                updated_asset['media_type'] = media_type
                metadata_changed = True
            is_indexable_media = _is_indexable_media_asset(updated_asset)
            if cache_path and os.path.exists(cache_path):
                if compute_missing_hashes and not updated_asset.get('media_sha256'):
                    if is_indexable_media:
                        media_sha256 = calculate_file_sha256(cache_path)
                        if media_sha256:
                            updated_asset['media_sha256'] = media_sha256
                            stats["hashed_files"] += 1
                            metadata_changed = True
                if media_type in {"image", "animated_image"} and not updated_asset.get('image_visual_hash'):
                    image_visual_hash = calculate_image_visual_hash(cache_path)
                    if image_visual_hash:
                        updated_asset['image_visual_hash'] = image_visual_hash
                        updated_asset['visual_hash_type'] = "dhash64"
                        stats["visual_hashed_images"] += 1
                        metadata_changed = True
            elif cache_path:
                stats["missing_files"] += 1

            updated_assets.append(updated_asset)
            if not is_indexable_media:
                continue

            media_key = _media_usage_group_key(updated_asset)
            record = _build_media_usage_record(metadata, updated_asset)
            if media_key and record:
                _add_media_usage_record(media_usage_index, media_key, record)
                stats["indexed_assets"] += 1

        if updated_assets:
            metadata['media_assets'] = updated_assets
            last_asset = updated_assets[-1]
            metadata['media_sha256'] = last_asset.get('media_sha256')
            metadata['image_visual_hash'] = last_asset.get('image_visual_hash')
            metadata['visual_hash_type'] = last_asset.get('visual_hash_type')

        if metadata_changed and write_metadata_file(metadata_path, metadata):
            stats["updated_metadata_records"] += 1

    media_usage_index["visual_items"] = _build_visual_image_duplicate_groups(media_usage_index)
    save_media_usage_index(media_usage_index)
    stats["duplicate_groups"] = len(get_duplicate_media_usage_groups())
    stats["visual_duplicate_groups"] = len(media_usage_index.get("visual_items") or {})
    return stats


def get_duplicate_media_usage_groups(require_multiple_authors=True, subreddit_name=None):
    """Return media usage groups that represent duplicate media usage."""
    index_data = load_media_usage_index()
    items = {}
    items.update(index_data.get("items", {}))
    items.update(index_data.get("visual_items", {}))
    groups = []
    normalized_subreddit = str(subreddit_name or "").strip().lower()

    for media_key, group in items.items():
        posts = group.get("posts")
        if not isinstance(posts, list):
            continue

        filtered_posts = _unique_posts_for_media_group(posts, normalized_subreddit)

        if len(filtered_posts) < 2:
            continue

        author_count = _author_count_for_posts(filtered_posts)
        if require_multiple_authors and author_count < 2:
            continue

        groups.append({
            "media_key": media_key,
            "match_type": group.get("match_type") or ("sha256" if media_key.startswith("sha256:") else "url"),
            "media_sha256": group.get("media_sha256"),
            "image_visual_hash": group.get("image_visual_hash"),
            "visual_hash_type": group.get("visual_hash_type"),
            "visual_distance_threshold": group.get("visual_distance_threshold"),
            "media_url": group.get("media_url"),
            "cache_path": group.get("cache_path"),
            "posts": filtered_posts,
            "post_count": len(filtered_posts),
            "author_count": author_count,
        })

    groups.sort(
        key=lambda item: (
            1 if item.get("match_type") == "sha256" else 0,
            item["author_count"],
            item["post_count"],
        ),
        reverse=True,
    )
    return groups


def _iter_metadata_cache_paths(metadata):
    """Yield every cache path referenced by a metadata record."""
    if not isinstance(metadata, dict):
        return

    legacy_cache_path = metadata.get('cache_path')
    normalized_legacy_path = _normalize_cache_path(legacy_cache_path)
    if normalized_legacy_path:
        yield normalized_legacy_path

    for asset in _normalize_metadata_media_assets(metadata):
        normalized_asset_path = _normalize_cache_path(asset.get('cache_path'))
        if normalized_asset_path:
            yield normalized_asset_path


def get_cached_submission_media_match(metadata, requested_url=None):
    """
    Return a cached media match from submission metadata.

    When `requested_url` is provided, prefer an exact per-asset match before
    falling back to the legacy submission-level `cache_path`/`media_url` pair.
    """
    if not isinstance(metadata, dict):
        return None

    for asset in _normalize_metadata_media_assets(metadata):
        cache_path = asset.get('cache_path')
        if not cache_path or not os.path.exists(cache_path):
            continue

        media_url = asset.get('media_url')
        asset_requested_url = asset.get('requested_url')
        if not requested_url or requested_url in {asset_requested_url, media_url}:
            return cache_path, media_url, asset_requested_url

    cache_path = metadata.get('cache_path')
    if cache_path and os.path.exists(cache_path):
        return cache_path, metadata.get('media_url'), None

    return None


def update_metadata_cache(submission, media_cache_path, final_media_url, original_media_url=None):
    """
    Updates the metadata cache for a given submission.
    Writes the filtered submission data to its JSON file and updates the index,
    but only if the metadata is missing or has changed.
    """
    if not submission or not hasattr(submission, 'id'):
        logger.error("Invalid submission object provided to update_metadata_cache.")
        return False

    submission_id = submission.id
    metadata_path = get_metadata_file_path(submission_id)
    if not metadata_path:
        logger.error(f"Could not determine metadata path for submission {submission_id}.")
        return False

    existing_metadata = {}
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, 'r', encoding='utf-8') as f:
                existing_metadata = json.load(f)
        except Exception:
            existing_metadata = {}

    # Filter the submission data and preserve existing derived/cache-only fields.
    metadata = dict(existing_metadata)
    metadata.update(_filter_submission_data(submission))

    # Add/Update our custom fields
    metadata['cache_path'] = media_cache_path # Absolute path to media
    metadata['media_url'] = final_media_url # The URL that was actually downloaded
    metadata['last_checked_utc'] = time.time()
    media_type = get_media_type(media_cache_path)
    media_sha256 = calculate_file_sha256(media_cache_path) if media_type in {"image", "animated_image", "video"} else None
    image_visual_hash = calculate_image_visual_hash(media_cache_path) if media_type in {"image", "animated_image"} else None
    if media_sha256:
        metadata['media_sha256'] = media_sha256
    if media_type:
        metadata['media_type'] = media_type
    if image_visual_hash:
        metadata['image_visual_hash'] = image_visual_hash
        metadata['visual_hash_type'] = "dhash64"

    existing_assets = _normalize_metadata_media_assets(existing_metadata)
    asset_urls = {url for url in (original_media_url, final_media_url) if url}
    updated_assets = []
    asset_updated = False

    for asset in existing_assets:
        if asset_urls and asset_urls.intersection(
            {asset.get('requested_url'), asset.get('media_url')}
        ):
            updated_asset = dict(asset)
            updated_asset['requested_url'] = original_media_url or asset.get('requested_url')
            updated_asset['media_url'] = final_media_url
            updated_asset['cache_path'] = media_cache_path
            updated_asset['media_type'] = media_type
            updated_asset['media_sha256'] = media_sha256 or asset.get('media_sha256')
            updated_asset['image_visual_hash'] = image_visual_hash or asset.get('image_visual_hash')
            updated_asset['visual_hash_type'] = "dhash64" if (image_visual_hash or asset.get('image_visual_hash')) else asset.get('visual_hash_type')
            updated_asset['last_checked_utc'] = metadata['last_checked_utc']
            updated_assets.append(updated_asset)
            asset_updated = True
        else:
            updated_assets.append(asset)

    if not asset_updated and final_media_url:
        updated_assets.append({
            'requested_url': original_media_url,
            'media_url': final_media_url,
            'cache_path': media_cache_path,
            'media_type': media_type,
            'media_sha256': media_sha256,
            'image_visual_hash': image_visual_hash,
            'visual_hash_type': "dhash64" if image_visual_hash else None,
            'last_checked_utc': metadata['last_checked_utc'],
        })

    metadata['media_assets'] = updated_assets

    # Determine initial moderation status from PRAW object attributes
    initial_mod_status = None
    try:
        if getattr(submission, 'approved', False):
            initial_mod_status = "approved"
        elif getattr(submission, 'removed', False) or getattr(submission, 'banned_by', None) is not None:
            initial_mod_status = "removed"
    except Exception as e:
        logger.warning(f"Could not determine initial mod status for {submission_id}: {e}")

    if initial_mod_status:
        metadata['moderation_status'] = initial_mod_status

    # Check if metadata file exists and is unchanged
    needs_update = True
    if existing_metadata:
        try:
            # Only update if something important has changed
            compare_keys = [
                'cache_path', 'media_url', 'media_type', 'media_sha256', 'image_visual_hash',
                'visual_hash_type', 'media_assets', 'id', 'title', 'score',
                'num_comments', 'moderation_status', 'report_count', 'report_reasons',
                'approved', 'removed'
            ]
            # Allow for missing keys in either dict (consider them as different)
            if all(
                existing_metadata.get(k) == metadata.get(k)
                for k in compare_keys
                if k in existing_metadata or k in metadata
            ):
                needs_update = False
        except Exception:
            needs_update = True

    if needs_update:
        if not write_metadata_file(metadata_path, metadata):
            logger.error(f"Failed to write metadata file for submission {submission_id}.")
            return False

        # Update the index
        index = load_submission_index() # Load current index (might be cached)
        try:
            relative_metadata_path = os.path.relpath(metadata_path, get_cache_dir())
        except ValueError:
            # Handle case where paths are on different drives (Windows)
            relative_metadata_path = metadata_path
        relative_metadata_path = relative_metadata_path.replace(os.sep, '/')
        index[submission_id] = relative_metadata_path
        save_submission_index()
        logger.debug(f"Updated metadata cache and index for submission {submission_id}")
    else:
        logger.debug(f"Metadata for submission {submission_id} is up to date; no update needed.")

    try:
        update_media_usage_index_for_metadata(metadata)
    except Exception as e:
        logger.exception(f"Failed to update media usage index for submission {submission_id}: {e}")
    return True

