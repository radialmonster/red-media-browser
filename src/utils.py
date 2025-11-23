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
from praw.models import Redditor, Subreddit

# Basic Logging Configuration
logger = logging.getLogger(__name__)

# --- Media File Extension Constants ---
IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
VIDEO_EXTENSIONS = ['.mp4', '.webm', '.avi', '.mov', '.mkv', '.flv']
ANIMATED_IMAGE_EXTENSIONS = ['.gif']

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

    # Build a set of all cache paths from metadata for O(1) lookup
    logger.info("Building cache path lookup from metadata...")
    existing_cache_paths = set()

    for sub_id, meta_rel in index.items():
        meta_path = os.path.join(cache_dir, meta_rel.replace('/', os.sep))
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    # Quick check first
                    content = f.read(1024)
                    if '"cache_path"' in content:
                        f.seek(0)
                        try:
                            meta = json.load(f)
                            cache_path = meta.get('cache_path')
                            if cache_path:
                                # Normalize path separators for consistent comparison
                                normalized_path = cache_path.replace('\\', '/').replace('/', os.sep)
                                existing_cache_paths.add(os.path.normpath(normalized_path))
                        except json.JSONDecodeError:
                            continue
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
            logger.info(f"Cache repair complete. Added {repaired} missing metadata/index entries.")
        except Exception as e:
            logger.error(f"Cache repair failed to save index after adding {repaired} entries: {e}")
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

# --- Metadata Cache Globals ---
_submission_index = None
_index_lock = threading.Lock()
_index_path = None
_metadata_lock = threading.Lock()

def ensure_directory(directory):
    """Ensure that the specified directory exists."""
    os.makedirs(directory, exist_ok=True)
    return directory

def get_cache_dir():
    """Return the application's cache directory."""
    cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
    return ensure_directory(cache_dir)

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
    cache_path = get_cache_path_for_url(url)
    if not cache_path:
        logger.debug(f"file_exists_in_cache: No cache path for URL: {url}")
        return False
    cache_dir = get_cache_dir()
    try:
        rel_path = os.path.relpath(cache_path, cache_dir).replace(os.sep, '/')
    except ValueError:
        # Handle case where paths are on different drives (Windows)
        rel_path = cache_path.replace(os.sep, '/')
    # Prefer preloaded set if available
    global _file_cache_set
    if _file_cache_set is not None:
        in_cache = file_in_cache_preloaded(rel_path)
        if in_cache:
            logger.debug(f"file_exists_in_cache: Cache HIT for {rel_path}")
        else:
            logger.debug(f"file_exists_in_cache: Cache MISS for {rel_path}")
        return in_cache
    # Fallback to disk check
    exists = os.path.exists(cache_path)
    if exists:
        logger.debug(f"file_exists_in_cache: Disk HIT for {cache_path}")
    else:
        logger.debug(f"file_exists_in_cache: Disk MISS for {cache_path}")
    return exists

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


def update_metadata_cache(submission, media_cache_path, final_media_url):
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

    # Filter the submission data
    metadata = _filter_submission_data(submission)

    # Add/Update our custom fields
    metadata['cache_path'] = media_cache_path # Absolute path to media
    metadata['media_url'] = final_media_url # The URL that was actually downloaded
    metadata['last_checked_utc'] = time.time()

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
    elif 'moderation_status' in metadata:
         del metadata['moderation_status']

    # Check if metadata file exists and is unchanged
    needs_update = True
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, 'r', encoding='utf-8') as f:
                existing = json.load(f)
            # Only update if something important has changed
            compare_keys = ['cache_path', 'media_url', 'id', 'title', 'score', 'num_comments', 'moderation_status']
            # Allow for missing keys in either dict (consider them as different)
            if all(existing.get(k) == metadata.get(k) for k in compare_keys if k in existing or k in metadata):
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
    return True

