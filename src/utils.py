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

# Basic Logging Configuration
logger = logging.getLogger(__name__)

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
    Given a submission, returns a list of image URLs.
    """
    if (hasattr(submission, 'is_gallery') and submission.is_gallery and
        hasattr(submission, 'media_metadata') and submission.media_metadata):
        return [html.unescape(media['s']['u'])
                for media in submission.media_metadata.values()
                if 's' in media and 'u' in media['s']]
    else:
        return [submission.url]

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
    # Special case for RedGifs videos - they should always be treated as videos
    if 'redgifs.com' in file_path.lower():
        # All RedGifs content should be treated as video regardless of extension
        logger.debug(f"RedGifs media forced to be detected as video: {file_path}")
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
        
        # Special handling for RedGifs URLs that might not have a clear filename
        if "redgifs.com" in domain:
            # Extract ID from URL patterns like redgifs.com/watch/someid or media.redgifs.com/SomeId.mp4
            if "media.redgifs.com" in domain and filename.endswith(".mp4"):
                # For direct media URLs, just use the filename as is
                pass
            elif "/watch/" in url:
                match = re.search(r'/watch/([A-Za-z]+)', url)
                if match:
                    redgifs_id = match.group(1)
                    filename = f"{redgifs_id}.mp4"  # Explicitly add .mp4 extension
            elif not filename or not filename.endswith(".mp4"):
                # Try to extract ID from the path
                match = re.search(r'([A-Za-z]+)(?:\.mp4)?$', path)
                if match:
                    redgifs_id = match.group(1)
                    filename = f"{redgifs_id}.mp4"  # Explicitly add .mp4 extension
                else:
                    # Fallback for other RedGifs URL patterns
                    import hashlib
                    url_hash = hashlib.md5(url.encode()).hexdigest()
                    filename = f"redgif_{url_hash}.mp4"  # Add .mp4 extension
        
        if not filename:
            # Handle URLs without a filename
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