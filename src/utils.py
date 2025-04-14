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
from urllib.parse import urlparse, unquote, quote, parse_qs, urljoin
import shutil
import requests
import json
import time
import threading
import html # Ensure html is imported for unescaping
from urllib.parse import urlparse # Import urlparse for URL checking
from praw.models import Redditor, Subreddit # For type checking in filtering

# Basic Logging Configuration
logger = logging.getLogger(__name__)

# --- Metadata Cache Globals ---
_submission_index = None
_index_lock = threading.Lock()
_index_path = None

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
    # Replace problematic characters
    return filename.replace('?', '_').replace('&', '_').replace('=', '_')
    
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

def extract_image_urls(submission):
    """
    Given a submission object (PRAW or SimpleNamespace/dict), returns a list of image URLs.
    Handles regular posts, gallery posts, and crossposts.
    """
    submission_id_str = getattr(submission, 'id', 'N/A')
    logger.debug(f"Extracting image URLs for submission ID: {submission_id_str}")

    # --- Check for Crosspost First ---
    crosspost_parent_list = getattr(submission, 'crosspost_parent_list', None)
    if crosspost_parent_list and isinstance(crosspost_parent_list, list) and len(crosspost_parent_list) > 0:
        parent_data = crosspost_parent_list[0] # This is expected to be a dictionary
        logger.debug(f"Processing {submission_id_str} as crosspost.") # Log keys: {list(parent_data.keys())}") # Keys can be verbose

        # Check parent for gallery
        parent_is_gallery = parent_data.get('is_gallery', False)
        parent_media_metadata = parent_data.get('media_metadata', None)

        if parent_is_gallery and parent_media_metadata and isinstance(parent_media_metadata, dict):
            try:
                urls = [html.unescape(media['s']['u'])
                        for media in parent_media_metadata.values()
                        if isinstance(media, dict) and 's' in media and isinstance(media['s'], dict) and 'u' in media['s']]
                if urls:
                    logger.debug(f"Extracted {len(urls)} gallery URLs from crosspost parent {submission_id_str}.")
                    return urls
                else:
                     logger.warning(f"Crosspost parent gallery detected but no valid URLs found in media_metadata for {submission_id_str}")
            except Exception as e:
                 logger.error(f"Error processing crosspost parent gallery metadata for {submission_id_str}: {e}")

        # Check parent for direct URL (if not a gallery or gallery extraction failed)
        parent_url = parent_data.get('url', None)
        if parent_url:
            # Basic check if the URL itself looks like an image
            parsed_url = urlparse(parent_url)
            if any(parent_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']):
                 logger.debug(f"Using direct image URL from crosspost parent {submission_id_str}: {parent_url}")
                 return [parent_url]
            # Consider adding domain checks for imgur, etc. if needed later
            logger.debug(f"Crosspost parent URL found for {submission_id_str}, but not a direct image link: {parent_url}. Falling back.")
        else:
             logger.debug(f"No gallery or direct URL found in crosspost parent for {submission_id_str}")

    # --- If Not Crosspost (or crosspost processing failed/didn't find media) ---
    logger.debug(f"Processing {submission_id_str} as regular post (or fallback from crosspost)")

    # Check main submission for gallery (using getattr for safety)
    is_gallery = getattr(submission, 'is_gallery', False)
    media_metadata = getattr(submission, 'media_metadata', None)

    if is_gallery and media_metadata:
        try:
            # Ensure media_metadata is dict-like
            if not isinstance(media_metadata, dict):
                 logger.warning(f"media_metadata is not a dict for {submission_id_str}, type: {type(media_metadata)}")
                 # Attempt to fallback to URL if possible
                 url = getattr(submission, 'url', None)
                 if url:
                      logger.debug(f"Falling back to direct URL for non-dict media_metadata: {url}")
                      return [url]
                 else:
                      logger.error(f"Cannot extract gallery URLs (media_metadata not dict) and no fallback URL for {submission_id_str}")
                      return []

            urls = [html.unescape(media['s']['u'])
                    for media in media_metadata.values()
                    if isinstance(media, dict) and 's' in media and isinstance(media['s'], dict) and 'u' in media['s']]
            if urls:
                logger.debug(f"Extracted {len(urls)} gallery URLs from main submission {submission_id_str}.")
                return urls
            else:
                 logger.warning(f"Main submission gallery detected but no valid URLs found in media_metadata for {submission_id_str}")
                 # Fall through to check direct URL as fallback
        except Exception as e:
             logger.error(f"Error processing main submission gallery metadata for {submission_id_str}: {e}")
             # Fall through to check direct URL as fallback

    # Fallback to direct URL on main submission
    url = getattr(submission, 'url', None)
    if url:
        # Basic check if the URL itself looks like an image before returning
        parsed_url = urlparse(url)
        if any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']):
            logger.debug(f"Using direct image URL from main submission {submission_id_str}: {url}")
            return [url]
        else:
            # If the direct URL isn't an image, maybe it's a video or something else?
            # The ThumbnailWidget might handle this, but extract_image_urls should ideally return image URLs.
            # For now, let's return it, but log a warning.
            logger.warning(f"Direct URL from main submission {submission_id_str} is not an image link: {url}. Returning anyway.")
            return [url]


    # If absolutely no URL found
    logger.error(f"Could not extract any image URL for submission {submission_id_str}")
    return []

def is_image_file(file_path):
    """Check if the file is an image based on extension."""
    image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
    ext = os.path.splitext(file_path.lower())[1]
    return ext in image_extensions

def is_video_file(file_path):
    """Check if the file is a video based on extension."""
    video_extensions = ['.mp4', '.webm', '.avi', '.mov', '.mkv', '.flv']
    ext = os.path.splitext(file_path.lower())[1]
    
    # Special case for RedGifs URLs that may not have proper extensions
    if 'redgifs.com' in file_path.lower() and any(domain in file_path.lower() for domain in ['media.redgifs.com', 'thumbs2.redgifs.com']):
        return True
        
    return ext in video_extensions

def is_animated_image(file_path):
    """Check if the file is an animated image (gif, etc)."""
    return file_path.lower().endswith('.gif')  # Could be extended for other formats

def get_media_type(file_path):
    """Determine the media type of a file."""
    # Special case for RedGifs content - check extension first
    if 'redgifs.com' in file_path.lower():
        # Check file extension to determine if it's an image or video
        ext = os.path.splitext(file_path.lower())[1]
        if ext in ['.jpg', '.jpeg', '.png', '.webp']:
            logger.debug(f"RedGifs image detected: {file_path}")
            return "image"
        elif ext in ['.gif']:
            logger.debug(f"RedGifs animated image detected: {file_path}")
            return "animated_image"
        elif ext in ['.mp4', '.webm', '']:  # Empty extension might be a video
            logger.debug(f"RedGifs video detected: {file_path}")
            return "video"
    
    # Normal file type detection
    if is_image_file(file_path):
        if is_animated_image(file_path):
            return "animated_image"
        return "image"
    elif is_video_file(file_path):
        return "video"
    
    # If we get here, try to determine by checking the actual file
    if os.path.exists(file_path):
        try:
            # Check file signature/magic bytes for common media types
            with open(file_path, 'rb') as f:
                header = f.read(12)  # Read first 12 bytes for signature detection
                
                # Check for MP4 signature
                if header.startswith(b'\x00\x00\x00\x18\x66\x74\x79\x70') or \
                   header.startswith(b'\x00\x00\x00\x20\x66\x74\x79\x70'):
                    return "video"
                    
                # JPEG signature
                if header.startswith(b'\xff\xd8\xff'):
                    return "image"
                    
                # PNG signature
                if header.startswith(b'\x89\x50\x4e\x47\x0d\x0a\x1a\x0a'):
                    return "image"
                    
                # GIF signature (and check if it's animated)
                if header.startswith(b'GIF87a') or header.startswith(b'GIF89a'):
                    # We'd need more complex logic to check if it's animated
                    # Just assume GIF is animated for now
                    return "animated_image"
        except Exception as e:
            logger.error(f"Error determining file type from contents: {e}")
    
    # Default if all else fails
    return "unknown"

def file_exists_in_cache(url):
    """Check if a file exists in the cache based on its URL."""
    cache_path = get_cache_path_for_url(url)
    return os.path.exists(cache_path) if cache_path else False

def get_cache_path_for_url(url):
    """Get the cache file path for a URL."""
    try:
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        if not domain:
            return None
            
        domain_dir = get_domain_cache_dir(domain)
        path = unquote(parsed_url.path)
        filename = os.path.basename(path)
        
        # Special handling for RedGifs domains
        if "redgifs.com" in domain: # Changed from elif to if
            original_ext = os.path.splitext(filename)[1].lower()
            # If it already has a valid media extension, keep it.
            if original_ext in ['.jpg', '.jpeg', '.png', 'gif', '.webp', '.mp4', '.webm']:
                pass # Keep filename as is
            # Handle watch/ifr URLs -> should become .mp4
            elif "/watch/" in url or "/ifr/" in url:
                 match = re.search(r'(?:watch|ifr)/([A-Za-z0-9]+)', url) # Allow numeric IDs too
                 if match:
                     redgifs_id = match.group(1)
                     filename = f"{redgifs_id}.mp4" # Force .mp4 for watch/ifr pages
                 else: # Fallback hash if ID extraction fails
                     import hashlib
                     url_hash = hashlib.md5(url.encode()).hexdigest()
                     filename = f"redgif_watch_hash_{url_hash}.mp4"
            # Handle URLs like i.redgifs.com/i/xyz (no extension) -> use hash, no assumed extension
            elif not original_ext and "i.redgifs.com" in domain:
                 import hashlib
                 url_hash = hashlib.md5(url.encode()).hexdigest()
                 logger.warning(f"RedGifs URL has no extension, using hash: {url}")
                 filename = f"redgif_noext_hash_{url_hash}" # Don't assume extension
            # Fallback for other unexpected RedGifs URLs - use hash, don't assume extension
            else:
                 import hashlib
                 url_hash = hashlib.md5(url.encode()).hexdigest()
                 logger.warning(f"Unhandled RedGifs URL format for cache path, using hash: {url}")
                 # Preserve original extension if it exists, otherwise no extension
                 filename = f"redgif_fallback_hash_{url_hash}{original_ext}"

        # Handle URLs without a filename (e.g., root path '/') AFTER domain-specific logic
        if not filename or filename == '/':
            extension = ""
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
                extension = ".mp4"  # Default to .mp4 for RedGifs
                
            filename = f"downloaded_media{extension}"
        
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
    if len(base_id) < 6: # Ensure we have enough characters for subdirs
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
                json.dump(_submission_index, f, indent=2) # Use indent for readability

            # Rename temporary file to actual index file (atomic on most systems)
            os.replace(temp_path, index_path)
            logger.debug(f"Saved submission index with {len(_submission_index)} entries.")
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
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2) # Use indent for readability
        os.replace(temp_path, metadata_path)
        return True
    except Exception as e:
        logger.exception(f"Error writing metadata file {metadata_path}: {e}")
        # Clean up temp file if it exists
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return False

def _filter_submission_data(submission):
    """
    Filters a PRAW Submission object's attributes for caching.
    Removes internal PRAW objects, comments, and simplifies complex objects.
    """
    if not submission:
        return {}

    # Attributes to explicitly exclude
    exclude_keys = {
        'comments', '_reddit', '_mod', '_fetched', '_info_params',
        'comment_limit', 'comment_sort', # Related to comments
        # Potentially others depending on PRAW version and usage
    }

    # Attributes to simplify (store identifier instead of object)
    simplify_keys = {
        'author': lambda obj: getattr(obj, 'name', None) if isinstance(obj, Redditor) else str(obj),
        'subreddit': lambda obj: getattr(obj, 'display_name', None) if isinstance(obj, Subreddit) else str(obj),
        # Add others if needed, e.g., 'approved_by', 'banned_by'
    }

    data = {}
    # Use vars() or __dict__ cautiously, prefer iterating known attributes if possible
    # PRAW objects might not have a clean __dict__
    # Let's try iterating dir() and getattr, filtering as we go
    for attr in dir(submission):
        if attr.startswith('_') or attr in exclude_keys:
            continue # Skip private/internal and excluded keys

        try:
            value = getattr(submission, attr)

            # Skip methods
            if callable(value):
                continue

            # Simplify complex objects
            if attr in simplify_keys:
                data[attr] = simplify_keys[attr](value)
            # Basic types that are safe for JSON
            elif isinstance(value, (str, int, float, bool, list, dict, type(None))):
                 # Basic check for list/dict contents (optional, can be slow)
                 # if isinstance(value, (list, dict)):
                 #     try:
                 #         json.dumps(value) # Quick check if serializable
                 #     except TypeError:
                 #         logger.warning(f"Skipping non-serializable attribute '{attr}' in submission {submission.id}")
                 #         continue
                 data[attr] = value
            # Log other types we might be missing
            # else:
            #     logger.debug(f"Skipping attribute '{attr}' of type {type(value)} for submission {submission.id}")

        except Exception as e:
            # Handle potential errors accessing attributes (e.g., prawcore exceptions)
            logger.warning(f"Could not access attribute '{attr}' for submission {submission.id}: {e}")
            continue

    # Ensure essential fields are present even if getattr failed (shouldn't happen often)
    essential = ['id', 'name', 'title', 'permalink', 'url']
    for key in essential:
        if key not in data:
            try:
                data[key] = getattr(submission, key, None)
            except: # Catch broad exception as fallback
                 data[key] = None

    return data


def update_metadata_cache(submission, media_cache_path, final_media_url):
    """
    Updates the metadata cache for a given submission.
    Writes the filtered submission data to its JSON file and updates the index.
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
            # Check 'removed' or if 'banned_by' is set (indicating removal)
            initial_mod_status = "removed"
        # Add more checks if needed, e.g., spam status
        # elif getattr(submission, 'spam', False):
        #     initial_mod_status = "spam" # Or maybe just "removed"?
    except Exception as e:
        logger.warning(f"Could not determine initial mod status for {submission_id}: {e}")

    # Only add moderation_status if it's determined (approved/removed)
    # Otherwise, leave it out or set to None, indicating neutral/unknown initial state
    if initial_mod_status:
         metadata['moderation_status'] = initial_mod_status
    elif 'moderation_status' in metadata:
         # Ensure we don't carry over an old status if the new check is neutral
         del metadata['moderation_status']


    # Write the individual metadata file
    if not write_metadata_file(metadata_path, metadata):
        logger.error(f"Failed to write metadata file for submission {submission_id}.")
        return False # Stop if writing the main data fails

    # Update the index
    index = load_submission_index() # Load current index (might be cached)
    # Store relative path from cache_dir for portability
    relative_metadata_path = os.path.relpath(metadata_path, get_cache_dir())
    
    # Use posix path separators for consistency across OS
    relative_metadata_path = relative_metadata_path.replace(os.sep, '/') 
    
    index[submission_id] = relative_metadata_path
    # No need to set _submission_index globally here, save_submission_index reads it

    # Save the updated index
    save_submission_index()
    logger.debug(f"Updated metadata cache and index for submission {submission_id}")
    return True

def clear_metadata_cache():
    """Deletes all cached metadata JSON files and the index."""
    metadata_dir = get_metadata_dir()
    index_path = _get_index_path()
    
    logger.info("Clearing metadata cache...")
    try:
        if os.path.exists(metadata_dir):
            shutil.rmtree(metadata_dir)
            logger.debug(f"Removed metadata directory: {metadata_dir}")
        # Recreate the base metadata directory
        ensure_directory(metadata_dir)
        
        if os.path.exists(index_path):
            os.remove(index_path)
            logger.debug(f"Removed submission index file: {index_path}")
            
        # Clear the in-memory cache
        global _submission_index
        with _index_lock:
             _submission_index = {}
             
        logger.info("Metadata cache cleared.")
        return True
    except Exception as e:
        logger.exception(f"Error clearing metadata cache: {e}")
        return False

def clear_full_cache():
    """Deletes all cached media files AND metadata."""
    cache_dir = get_cache_dir()
    logger.info("Clearing full cache (media and metadata)...")
    try:
        # List contents *before* deleting the main dir
        items = os.listdir(cache_dir)
        for item in items:
            item_path = os.path.join(cache_dir, item)
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    logger.debug(f"Removed directory: {item_path}")
                else:
                    os.remove(item_path)
                    logger.debug(f"Removed file: {item_path}")
            except Exception as item_e:
                 logger.error(f"Error removing cache item {item_path}: {item_e}")
                 
        # Ensure cache dir exists after clearing
        ensure_directory(cache_dir)
        
        # Clear the in-memory index cache
        global _submission_index
        with _index_lock:
             _submission_index = {}
             
        logger.info("Full cache cleared.")
        return True
    except Exception as e:
        logger.exception(f"Error clearing full cache: {e}")
        return False
