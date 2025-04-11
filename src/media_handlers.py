#!/usr/bin/env python3
"""
Media Handlers for Red Media Browser

This module contains functions for processing media URLs, 
particularly dealing with RedGIFs, Reddit, and other media providers.
It also handles downloading and caching of media files.
"""

import os
import re
import logging
import requests
import shutil
import time
import json
from urllib.parse import urlparse, quote

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot

from utils import (
    normalize_redgifs_url, ensure_json_url, get_cache_path_for_url,
    file_exists_in_cache, get_domain_cache_dir
)

# Set up logging
logger = logging.getLogger(__name__)

# Define registry for provider-specific handlers.
provider_handlers = {}

def register_handler(domain, handler):
    """Register a handler function for a specific domain."""
    provider_handlers[domain] = handler

# RedGIFS Specific Handlers
def extract_redgifs_url_from_reddit(json_data):
    """
    Extract a direct RedGIFs URL from Reddit JSON data.
    """
    try:
        post_listing = json_data[0]
        post_data = post_listing["data"]["children"][0]["data"]
        redgifs_url = post_data.get("url_overridden_by_dest") or post_data.get("url")
        
        # If we already have a redgifs URL, just return it
        if redgifs_url and "redgifs.com" in urlparse(redgifs_url).netloc:
            logger.debug(f"Found RedGIFs URL in post data: {redgifs_url}")
            return redgifs_url
            
        # Try to extract from secure_media or media
        secure_media = post_data.get("secure_media") or post_data.get("media")
        if (secure_media and "oembed" in secure_media):
            oembed_data = secure_media["oembed"]
            
            # Try to extract from thumbnail_url
            thumbnail_url = oembed_data.get("thumbnail_url")
            if thumbnail_url and "redgifs.com" in thumbnail_url:
                logger.debug(f"Extracted RedGIFs thumbnail URL: {thumbnail_url}")
                # This is likely a poster image, try to convert to video URL
                redgifs_id = None
                # Extract ID from poster image URL like media.redgifs.com/SociableGiftedCoyote-poster.jpg
                poster_match = re.search(r'([A-Za-z]+)-poster\.(jpg|jpeg|png)', thumbnail_url)
                if poster_match:
                    redgifs_id = poster_match.group(1)
                    logger.debug(f"Extracted RedGIFs ID from poster: {redgifs_id}")
                    return f"https://www.redgifs.com/watch/{redgifs_id.lower()}"
            
            # Try to extract from the HTML
            oembed_html = oembed_data.get("html", "")
            match = re.search(r'src="([^"]+)"', oembed_html)
            if match:
                candidate = match.group(1)
                if "redgifs.com" in urlparse(candidate).netloc:
                    logger.debug(f"Extracted RedGIFs iframe URL: {candidate}")
                    # Extract ID from iframe URL like redgifs.com/ifr/sociablegiftedcoyote
                    iframe_match = re.search(r'/ifr/([A-Za-z]+)', candidate)
                    if iframe_match:
                        redgifs_id = iframe_match.group(1)
                        logger.debug(f"Extracted RedGIFs ID from iframe: {redgifs_id}")
                        return f"https://www.redgifs.com/watch/{redgifs_id.lower()}"
                    return candidate
        
        # Check crossposted content
        if not redgifs_url and "crosspost_parent_list" in post_data:
            for cp in post_data["crosspost_parent_list"]:
                candidate = cp.get("url_overridden_by_dest") or cp.get("url")
                if candidate and "redgifs.com" in urlparse(candidate).netloc:
                    redgifs_url = candidate
                    break
                    
                # Also check embedded media in crossposts
                cp_media = cp.get("secure_media") or cp.get("media")
                if cp_media and "oembed" in cp_media and "html" in cp_media["oembed"]:
                    html = cp_media["oembed"]["html"]
                    match = re.search(r'src="([^"]+)"', html)
                    if match:
                        candidate = match.group(1)
                        if "redgifs.com" in urlparse(candidate).netloc:
                            redgifs_url = candidate
                            break
        
        if redgifs_url:
            logger.debug(f"Final extracted RedGIFs URL: {redgifs_url}")
        else:
            logger.error("Could not extract a RedGIFs URL from the post.")
        return redgifs_url
    except Exception as e:
        logger.exception(f"Error extracting RedGIFs URL from Reddit JSON: {e}")
        return None

def get_redgifs_mp4_url(url):
    """
    Attempts to extract an mp4 video URL for a RedGIFs post.
    """
    url = normalize_redgifs_url(url)
    logger.debug(f"Attempting to fetch mp4 URL from RedGIFs for: {url}")
    
    # Extract the RedGIFs ID from the URL
    redgifs_id = None
    numeric_id = None
    
    # Check for new numeric ID format
    if '/watch/' in url and url.split('/watch/')[-1].isdigit():
        numeric_id = url.split('/watch/')[-1]
        logger.debug(f"Extracted numeric RedGIFs ID: {numeric_id}")
    # Check for traditional text ID format
    elif '/watch/' in url or '/ifr/' in url:
        m = re.search(r'(?:watch|ifr)/([A-Za-z]+)', url)
        if m:
            redgifs_id = m.group(1)
            logger.debug(f"Extracted text RedGIFs ID: {redgifs_id}")
    else:
        # Try to extract from any URL format
        m = re.search(r'redgifs\.com/(?:[^/]+/)?([A-Za-z]+)(?:\?|$|#)', url)
        if m:
            redgifs_id = m.group(1)
            logger.debug(f"Extracted fallback RedGIFs ID: {redgifs_id}")
    
    if not redgifs_id and not numeric_id:
        logger.error(f"Could not extract RedGIFs ID from URL: {url}")
        return url
    
    # Headers for API requests
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Referer': 'https://www.redgifs.com/',
        'Accept': 'application/json'
    }
    
    # Try to get an access token first (needed for API v2)
    try:
        token_url = "https://api.redgifs.com/v2/auth/temporary"
        token_response = requests.get(token_url, headers=headers)
        if token_response.status_code == 200:
            token_data = token_response.json()
            access_token = token_data.get("token")
            if access_token:
                logger.debug("Successfully acquired RedGIFs API token")
                headers["Authorization"] = f"Bearer {access_token}"
    except Exception as e:
        logger.exception(f"Error getting RedGIFs token: {e}")
    
    # For numeric IDs, use a different API endpoint
    if numeric_id:
        try:
            api_url = f"https://api.redgifs.com/v2/gifs/{numeric_id}"
            logger.debug(f"Trying RedGIFs API v2 with numeric ID: {api_url}")
            
            response = requests.get(api_url, headers=headers, timeout=10)
            logger.debug(f"API v2 numeric ID response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                gif_data = data.get("gif", {})
                
                # Try HD URL first, then SD
                mp4_url = gif_data.get("urls", {}).get("hd")
                if not mp4_url:
                    mp4_url = gif_data.get("urls", {}).get("sd")
                
                if mp4_url:
                    logger.debug(f"Extracted mp4 URL from API v2 with numeric ID: {mp4_url}")
                    return mp4_url
                else:
                    logger.error(f"No mp4 URL found in API v2 response for numeric ID: {gif_data}")
            else:
                logger.error(f"API v2 call failed for numeric ID with status: {response.status_code}")
        except Exception as e:
            logger.exception(f"Exception calling API v2 with numeric ID: {e}")
            
        # If API v2 with numeric ID fails, try a direct construction
        direct_url = f"https://thumbs2.redgifs.com/{numeric_id}.mp4"
        logger.debug(f"Trying direct URL with numeric ID: {direct_url}")
        return direct_url
    
    # For text-based IDs, proceed with normal API calls
    else:
        # Try the RedGIFs API v2 first
        api_url = f"https://api.redgifs.com/v2/gifs/{redgifs_id}"
        logger.debug(f"Trying RedGIFs API v2 URL: {api_url}")
        
        try:
            response = requests.get(api_url, headers=headers, timeout=10)
            logger.debug(f"API v2 response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                gif_data = data.get("gif", {})
                
                # Try HD URL first
                mp4_url = gif_data.get("urls", {}).get("hd")
                if not mp4_url:
                    # Try SD URL next
                    mp4_url = gif_data.get("urls", {}).get("sd")
                
                if mp4_url:
                    logger.debug(f"Extracted mp4 URL from API v2: {mp4_url}")
                    return mp4_url
                else:
                    logger.error(f"No mp4 URL found in API v2 response: {gif_data}")
            else:
                logger.error(f"API v2 call failed with status: {response.status_code}")
        except Exception as e:
            logger.exception(f"Exception calling API v2: {e}")
        
        # Try the oEmbed API as a fallback
        api_url = "https://api.redgifs.com/v1/oembed?url=" + quote(url, safe='')
        logger.debug(f"Fallback: Fetching RedGIFs oEmbed API URL: {api_url}")
        
        try:
            response = requests.get(api_url, headers=headers, timeout=10)
            logger.debug(f"oEmbed API response status: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                logger.debug(f"oEmbed data: {data}")
                html_embed = data.get("html", "")
                match = re.search(r'src=[\'"]([^\'"]+\.mp4)[\'"]', html_embed)
                if match:
                    mp4_url = match.group(1)
                    logger.debug(f"Extracted mp4 URL from oEmbed: {mp4_url}")
                    return mp4_url
                else:
                    logger.error(f"No mp4 URL found in oEmbed HTML: {html_embed}")
            else:
                logger.error(f"Failed fetching oEmbed API, status: {response.status_code}")
        except Exception as e:
            logger.exception(f"Exception while calling oEmbed API: {e}")
        
        # If all else fails, try the legacy GFYcats API
        gfycats_url = f"https://api.redgifs.com/v1/gfycats/{redgifs_id}"
        logger.debug(f"Attempting legacy GFYCats API with URL: {gfycats_url}")
        
        try:
            response = requests.get(gfycats_url, headers=headers, timeout=10)
            logger.debug(f"GFYCats API response status: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                gfyItem = data.get("gfyItem", {})
                mp4_url = gfyItem.get("mp4Url", "")
                if not mp4_url and "urls" in gfyItem:
                    mp4_url = gfyItem["urls"].get("hd", "")
                    if not mp4_url:
                        mp4_url = gfyItem["urls"].get("sd", "")
                
                if mp4_url:
                    logger.debug(f"Extracted mp4 URL from GFYCats API: {mp4_url}")
                    return mp4_url
                else:
                    logger.error("No mp4 URL property found in GFYCats response.")
            else:
                logger.error(f"GFYCats API call failed with status: {response.status_code}")
        except Exception as e:
            logger.exception(f"Exception calling GFYCats API: {e}")
        
        # Final fallback - try a direct URL construction
        direct_url = f"https://thumbs2.redgifs.com/{redgifs_id}.mp4"
        logger.debug(f"All API calls failed, trying direct URL construction: {direct_url}")
        return direct_url

def redgifs_image_handler(url):
    """
    Special handling for i.redgifs.com image URLs.
    Preserves the file extension for proper media type detection.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        "Referer": "https://redgifs.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    
    # Get file extension from original URL (preserve it for type detection)
    ext = os.path.splitext(url.lower())[1]
    is_image_extension = ext in ['.jpg', '.jpeg', '.png', '.webp']
    
    try:
        resp = requests.get(url, stream=True, allow_redirects=True, headers=headers, timeout=10)
        ctype = resp.headers.get('Content-Type', '')
        
        # If content-type confirms it's an image, ensure we preserve that information
        is_image_content = 'image/' in ctype.lower()
        
        if 'text/html' in ctype.lower():
            logger.debug("Redgifs handler: received HTML, attempting extraction.")
            html_content = resp.text
            m = re.search(
                r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                html_content,
                re.IGNORECASE
            )
            if m:
                extracted_url = m.group(1)
                logger.debug(f"Redgifs handler: extracted image URL: {extracted_url}")
                
                # Preserve the image extension if we had one originally
                if is_image_extension and not any(extracted_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                    logger.debug(f"Preserving original image extension: {ext}")
                    extracted_url = f"{extracted_url}{ext}"
                
                return extracted_url
            else:
                logger.error("Redgifs handler: No og:image tag found.")
        elif is_image_content:
            # It's already an image and content type confirms it - keep URL as is
            logger.debug(f"Redgifs handler: URL confirmed as image via content-type: {ctype}")
            return url
    except Exception as e:
        logger.exception(f"Redgifs handler exception: {e}")
    
    # Return original URL as fallback
    return url

# Register the RedGIFs handler
register_handler("i.redgifs.com", redgifs_image_handler)
register_handler("media.redgifs.com", redgifs_image_handler)

def reddit_video_handler(url):
    """
    Process Reddit video URLs (v.redd.it).
    Extracts the direct MP4 URL from Reddit's video JSON data.
    """
    logger.debug(f"Processing Reddit video URL: {url}")
    
    # Check if it's already a direct MP4 URL
    if url.endswith('.mp4'):
        return url
    
    # These are the direct video URLs: structure is v.redd.it/[video_id]
    video_id = None
    parsed_url = urlparse(url)
    if parsed_url.netloc == 'v.redd.it':
        # Extract the video ID from path
        video_id = parsed_url.path.strip('/')
        logger.debug(f"Extracted Reddit video ID: {video_id}")
    
    if not video_id:
        logger.error(f"Could not extract video ID from Reddit URL: {url}")
        return url
    
    # Try to get the post JSON to extract fallback_url
    try:
        # First try to get the post data via Reddit API
        headers = {"User-Agent": "Mozilla/5.0 (compatible; red-media-browser/1.0)"}
        
        # We need to get the actual post URL first - v.redd.it is just a redirect
        # Try fetching with HEAD request to follow redirects to get the actual post
        session = requests.Session()
        try:
            head_response = session.head(url, headers=headers, timeout=10, allow_redirects=True)
            if head_response.url and "reddit.com" in head_response.url:
                post_url = head_response.url
                logger.debug(f"Redirected to post URL: {post_url}")
                
                # Convert to JSON endpoint
                json_url = ensure_json_url(post_url)
                logger.debug(f"Fetching JSON data from: {json_url}")
                
                response = session.get(json_url, headers=headers, timeout=10)
                response.raise_for_status()
                json_data = response.json()
                
                # Extract video URL from the JSON
                post_data = json_data[0]["data"]["children"][0]["data"]
                secure_media = post_data.get("secure_media") or post_data.get("media")
                
                if secure_media and "reddit_video" in secure_media:
                    reddit_video = secure_media["reddit_video"]
                    fallback_url = reddit_video.get("fallback_url")
                    
                    if fallback_url:
                        logger.debug(f"Successfully extracted fallback URL: {fallback_url}")
                        return fallback_url
            
        except Exception as head_e:
            logger.debug(f"Error following redirects: {head_e}")
        
        # Fallback method: try constructing a direct URL if we have the video ID
        fallback_url = f"https://v.redd.it/{video_id}/DASH_1080.mp4?source=fallback"
        logger.debug(f"Using constructed fallback URL: {fallback_url}")
        return fallback_url
        
    except Exception as e:
        logger.exception(f"Error processing Reddit video URL: {e}")
        # If all else fails, just return the original URL
        return url

# Register the Reddit video handler
register_handler("v.redd.it", reddit_video_handler)

def process_media_url(url):
    """
    Determine the media provider and delegate URL processing.
    """
    logger.debug(f"Processing media URL: {url}")
    
    # For all media types, first check if we already know what the processed URL would be
    # and if that processed URL is already in the cache
    
    # For RedGifs URLs
    if "redgifs.com" in url and not url.endswith('.mp4'):
        # Try to predict what the processed URL would be based on RedGifs ID patterns
        redgifs_id = None
        
        # Try to extract ID from the URL pattern
        if '/watch/' in url:
            m = re.search(r'/watch/([A-Za-z]+)', url)
            if m:
                redgifs_id = m.group(1)
                # Convert to proper case for media URLs (first letter capitalized for each word)
                redgifs_id_proper = ''.join(word.capitalize() for word in re.findall(r'[a-z]+', redgifs_id))
                predicted_url = f"https://media.redgifs.com/{redgifs_id_proper}.mp4"
                
                # Check if the predicted URL is in cache
                cache_path = get_cache_path_for_url(predicted_url)
                if cache_path and os.path.exists(cache_path):
                    logger.debug(f"Cache hit for predicted RedGifs URL: {predicted_url}")
                    return predicted_url
        
        # If cache prediction didn't work, proceed with normal processing
        logger.debug(f"Processing RedGIFs URL: {url}")
        processed_url = get_redgifs_mp4_url(url)
        logger.debug(f"Processed URL: {url} -> {processed_url}")
        return processed_url

    # Check if any registered handler can process this URL
    for domain, handler in provider_handlers.items():
        if domain in url:
            # For domain-specific handlers, check cache first with the original URL
            cache_path = get_cache_path_for_url(url)
            if cache_path and os.path.exists(cache_path):
                logger.debug(f"Cache hit for URL before handler processing: {url}")
                return url
                
            # If not in cache, let the handler process it
            new_url = handler(url)
            if new_url != url:
                logger.debug(f"Handler for {domain} modified URL to: {new_url}")
                return new_url

    # Process Reddit RedGifs crosspost links
    if "reddit.com" in url:
        # Check cache first with the original URL
        cache_path = get_cache_path_for_url(url)
        if cache_path and os.path.exists(cache_path):
            logger.debug(f"Cache hit for Reddit URL: {url}")
            return url
            
        json_url = ensure_json_url(url)
        logger.debug(f"Converted Reddit URL to JSON endpoint: {json_url}")
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; red-media-browser/1.0)"}
            response = requests.get(json_url, headers=headers, timeout=10)
            response.raise_for_status()
            reddit_json = response.json()
            extracted = extract_redgifs_url_from_reddit(reddit_json)
            if extracted:
                normalized = normalize_redgifs_url(extracted)
                mp4_url = get_redgifs_mp4_url(normalized)
                logger.debug(f"Returning MP4 URL after Reddit extraction: {mp4_url}")
                return mp4_url
            else:
                logger.error("Failed to extract redgifs URL from Reddit JSON.")
        except Exception as e:
            logger.exception(f"Error processing Reddit redgifs URL: {e}")
        return url

    # Convert gifv to mp4 for Imgur
    if url.endswith('.gifv'):
        return url.replace('.gifv', '.mp4')

    logger.debug(f"No provider-specific processing required for: {url}")
    return url

# Signal class for worker communication
class WorkerSignals(QObject):
    finished = pyqtSignal(str)  # Emits the downloaded file path
    progress = pyqtSignal(int)  # Emits download progress percentage
    error = pyqtSignal(str)     # Emits error message if download fails

# Asynchronous media downloader
class MediaDownloadWorker(QRunnable):
    """
    Worker for downloading media files asynchronously.
    Includes progress reporting and error handling.
    """
    def __init__(self, url):
        super().__init__()
        self.original_url = url
        self.processed_url = process_media_url(url)
        self.signals = WorkerSignals()
        logger.debug(f"MediaDownloadWorker initialized: original={self.original_url}, processed={self.processed_url}")
        
    @pyqtSlot()
    def run(self):
        """
        Entry point for the worker.
        Downloads the media file and reports progress.
        """
        try:
            # Skip empty URLs
            if not self.processed_url:
                logger.error("Empty URL provided to MediaDownloadWorker")
                self.signals.error.emit("Empty URL provided")
                return
                
            # Check if already cached
            cache_path = get_cache_path_for_url(self.processed_url)
            if cache_path and os.path.exists(cache_path):
                logger.debug(f"File already cached: {cache_path}")
                self.signals.finished.emit(cache_path)
                return
                
            # Download the file
            file_path = self.download_file(self.processed_url)
            self.signals.finished.emit(file_path)
        except Exception as e:
            logger.exception(f"Error downloading media: {e}")
            self.signals.error.emit(str(e))

    def download_file(self, url):
        """
        Download a file from a URL to the cache directory.
        
        Args:
            url: URL of the file to download
        
        Returns:
            Path to the downloaded file
        """
        # Skip empty URLs
        if not url:
            raise ValueError("Empty URL provided for download")
            
        # Get cache path
        cache_path = get_cache_path_for_url(url)
        if not cache_path:
            raise ValueError(f"Could not determine cache path for URL: {url}")
            
        # Create the directory if needed
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        
        # Set up headers for the request
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': '*/*'
        }
        
        # Add referrer for certain domains
        if "redgifs.com" in url:
            headers['Referer'] = 'https://www.redgifs.com/'
        elif "imgur.com" in url:
            headers['Referer'] = 'https://imgur.com/'

        # --- Initial Download Attempt ---
        logger.debug(f"Attempting download from: {url}")
        response = requests.get(url, stream=True, headers=headers, timeout=30, allow_redirects=True)
        logger.debug(f"Initial response status: {response.status_code}, Final URL: {response.url}")

        # --- Handle RedGifs Image Redirect ---
        # Check if an i.redgifs.com image URL redirected to a www.redgifs.com/watch page returning HTML
        original_domain = urlparse(url).netloc
        final_domain = urlparse(response.url).netloc
        content_type = response.headers.get('Content-Type', '').lower()

        if (original_domain == "i.redgifs.com" and
            final_domain == "www.redgifs.com" and
            "/watch/" in response.url and
            response.status_code == 200 and
            'text/html' in content_type):

            logger.debug("Detected i.redgifs.com image redirect to HTML watch page. Parsing for actual image URL.")
            html_content = response.text
            # Try extracting og:image meta tag
            og_image_match = re.search(
                r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                html_content,
                re.IGNORECASE
            )
            # Try extracting twitter:image meta tag as fallback
            twitter_image_match = re.search(
                 r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
                 html_content,
                 re.IGNORECASE
            )

            actual_image_url = None
            if og_image_match:
                actual_image_url = og_image_match.group(1)
                logger.debug(f"Found og:image URL: {actual_image_url}")
            elif twitter_image_match:
                 actual_image_url = twitter_image_match.group(1)
                 logger.debug(f"Found twitter:image URL: {actual_image_url}")
            else:
                logger.error("Could not find image URL (og:image or twitter:image) in redirected HTML.")
                raise Exception("Failed to extract actual image URL from RedGifs watch page HTML.")

            # --- Second Download Attempt (Actual Image) ---
            if actual_image_url:
                logger.debug(f"Attempting second download for actual image: {actual_image_url}")
                # Use same headers, maybe update Referer?
                headers['Referer'] = response.url # Referer is the watch page
                response = requests.get(actual_image_url, stream=True, headers=headers, timeout=30)
                logger.debug(f"Second download response status: {response.status_code}")
                # Update URL variable to reflect the actual downloaded content for later extension logic
                url = actual_image_url
                content_type = response.headers.get('Content-Type', '').lower() # Update content_type too

        # --- Process Final Response ---
        if response.status_code == 200:
            # Get content length for progress reporting (use final response)
            content_length = int(response.headers.get('Content-Length', 0))
            
            # Setup progress tracking
            bytes_downloaded = 0
            
            with open(cache_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:  # filter out keep-alive new chunks
                        f.write(chunk)
                        
                        # Update progress if content length is known
                        if content_length > 0:
                            bytes_downloaded += len(chunk)
                            progress = int(100 * bytes_downloaded / content_length)
                            self.signals.progress.emit(progress)
            
            # Special handling for RedGIFs content
            if "redgifs.com" in url:
                # Check content type to determine if it's an image or video
                content_type = response.headers.get('Content-Type', '').lower()
                # Use the URL *passed to download_file* to check the extension
                original_ext = os.path.splitext(url.lower())[1]
                is_image_url = original_ext in ['.jpg', '.jpeg', '.png', '.webp']
                is_image_content = 'image/' in content_type

                if is_image_url or is_image_content:
                    # If the URL looked like an image OR content type confirms it's an image
                    logger.debug(f"RedGifs image detected (URL: {is_image_url}, Content: {is_image_content}), ensuring correct extension.")

                    # Determine the correct extension
                    correct_ext = original_ext # Default to original URL extension if it was an image type
                    if not is_image_url: # If original URL didn't have image ext, use content type
                         if 'image/jpeg' in content_type:
                             correct_ext = '.jpg'
                         elif 'image/png' in content_type:
                             correct_ext = '.png'
                         elif 'image/webp' in content_type:
                             correct_ext = '.webp'
                         else:
                             correct_ext = '.jpg' # Fallback

                    # Ensure the cached file has the correct extension
                    current_ext = os.path.splitext(cache_path.lower())[1]
                    if current_ext != correct_ext:
                        base_path = os.path.splitext(cache_path)[0]
                        new_cache_path = base_path + correct_ext
                        try:
                            # Only move if the target doesn't already exist (avoid race conditions)
                            if not os.path.exists(new_cache_path):
                                shutil.move(cache_path, new_cache_path)
                                cache_path = new_cache_path
                                logger.debug(f"Renamed RedGifs image file to use correct extension: {cache_path}")
                            elif cache_path != new_cache_path:
                                # Target exists, likely another thread handled it, remove the duplicate
                                os.remove(cache_path)
                                cache_path = new_cache_path # Point to the existing correct file
                                logger.debug(f"Correctly named RedGifs image file already exists: {cache_path}")
                        except Exception as e:
                            logger.error(f"Error renaming file to use correct extension: {e}")

                # Only force .mp4 if the URL *didn't* look like an image initially
                elif not is_image_url and not cache_path.lower().endswith('.mp4'):
                    # For non-image URLs (likely videos), force .mp4 extension if needed
                    new_cache_path = os.path.splitext(cache_path)[0] + ".mp4"
                    try:
                         # Only move if the target doesn't already exist
                        if not os.path.exists(new_cache_path):
                            shutil.move(cache_path, new_cache_path)
                            cache_path = new_cache_path
                            logger.debug(f"Renamed RedGifs file to ensure .mp4 extension: {cache_path}")
                        elif cache_path != new_cache_path:
                            os.remove(cache_path)
                            cache_path = new_cache_path
                            logger.debug(f"Correctly named RedGifs video file already exists: {cache_path}")
                    except Exception as e:
                        logger.error(f"Error renaming file to add .mp4 extension: {e}")

            # Success
            return cache_path
        else:
            # Request failed
            logger.error(f"Failed to download {url}: HTTP status {response.status_code}")
            raise Exception(f"HTTP error {response.status_code}")
