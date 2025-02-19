#!/usr/bin/env python3
import sys
import os
import json
import shutil
import logging
import time
import html
import webbrowser
import requests
import re
import weakref
from urllib.parse import urlparse, unquote, quote, parse_qs, urljoin

import praw
import prawcore.exceptions
import vlc

# Basic Logging Configuration
logger = logging.getLogger()
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


# PyQt6 Imports
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QPushButton, QLineEdit, QWidget,
    QLabel, QTableWidget, QHeaderView, QHBoxLayout, QMessageBox, QSizePolicy, QInputDialog, QDialog
)
from PyQt6.QtCore import (
    QAbstractListModel, Qt, QSize, QThread, pyqtSignal, QThreadPool, QRunnable,
    pyqtSlot, QObject, QTimer
)
from PyQt6.QtGui import QPixmap, QPixmapCache, QMovie, QGuiApplication



# Import Configuration Manager
from red_config import load_config, get_new_refresh_token, update_config_with_new_token


# Load Configuration and Initialize Reddit API Client
# Use our external configuration module to load or create config.json.
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
config = load_config(config_path)

requested_scopes = ['identity', 'read', 'history', 'modconfig', 'modposts', 'mysubreddits', 'modcontributors','modlog']


# Global dictionary for storing moderation statuses (e.g., "approved" or "removed")
moderation_statuses = {}


try:
    reddit = praw.Reddit(
        client_id=config['client_id'],
        client_secret=config['client_secret'],
        redirect_uri=config['redirect_uri'],
        refresh_token=config['refresh_token'],
        user_agent=config['user_agent'],
        scopes=requested_scopes,
        log_request=2
    )

    logger.info("Successfully initialized Reddit API client.")
    logger.info(f"Requested Reddit API client scopes: {requested_scopes}")
    authorized_scopes = reddit.auth.scopes()
    logger.info(f"Reddit API client scopes: {authorized_scopes}")

    if set(requested_scopes).issubset(authorized_scopes):
        logger.info("All requested scopes are authorized.")
    else:
        logger.warning("Not all requested scopes are authorized. Initiating process to obtain new refresh token.")
        new_refresh_token = get_new_refresh_token(reddit, requested_scopes)
        if new_refresh_token:
            update_config_with_new_token(config, config_path, new_refresh_token)
            reddit = praw.Reddit(
                client_id=config['client_id'],
                client_secret=config['client_secret'],
                refresh_token=new_refresh_token,
                user_agent=config['user_agent'],
                scopes=requested_scopes,
                log_request=2
            )
            logger.info("Successfully re-initialized Reddit API client with new refresh token.")
        else:
            logger.error("Failed to obtain new refresh token. Exiting.")
            sys.exit(1)

    default_subreddit = config.get('default_subreddit', 'pics')
    logger.info(f"Default subreddit set to: {default_subreddit}")

except FileNotFoundError:
    logger.error(f"config.json not found at {config_path}. Please create a config file with your Reddit API credentials.")
    sys.exit(1)
except KeyError as e:
    logger.error(f"Missing key in config.json: {e}")
    sys.exit(1)
except Exception as e:
    logger.error(f"Error initializing Reddit API client: {e}")
    sys.exit(1)

# Helper Functions for Media URL Processing and RedGIFS
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

def schedule_media_download(url, callback, pool=None):
    """
    Schedules an asynchronous download for the given media URL.
    """
    processed_url = process_media_url(url)
    worker = ImageDownloadWorker(processed_url)
    if pool is None:
        pool = QThreadPool.globalInstance()
    worker.signals.finished.connect(lambda file_path, purl=processed_url: callback(file_path, purl))
    pool.start(worker)

def normalize_redgifs_url(url):
    """
    Normalize a RedGIFs URL.
    """
    logger.debug("Original RedGIFs URL: " + url)
    if "v3.redgifs.com/watch/" in url:
        url = url.replace("v3.redgifs.com/watch/", "www.redgifs.com/watch/")
        logger.debug("Normalized v3.redgifs URL to: " + url)
    if "redgifs.com/ifr/" in url:
        url = url.replace("/ifr/", "/watch/")
        logger.debug("Normalized iframe URL to: " + url)
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

def extract_redgifs_url_from_reddit(json_data):
    """
    Extract a direct RedGIFs URL from Reddit JSON data.
    """
    try:
        post_listing = json_data[0]
        post_data = post_listing["data"]["children"][0]["data"]
        redgifs_url = post_data.get("url_overridden_by_dest") or post_data.get("url")
        if redgifs_url and "redgifs.com" not in urlparse(redgifs_url).netloc:
            secure_media = post_data.get("secure_media")
            if secure_media and "oembed" in secure_media:
                oembed_html = secure_media["oembed"].get("html", "")
                match = re.search(r'src="([^"]+)"', oembed_html)
                if match:
                    candidate = match.group(1)
                    if "redgifs.com" in urlparse(candidate).netloc:
                        redgifs_url = candidate
        if not redgifs_url and "crosspost_parent_list" in post_data:
            for cp in post_data["crosspost_parent_list"]:
                candidate = cp.get("url_overridden_by_dest") or cp.get("url")
                if candidate and "redgifs.com" in urlparse(candidate).netloc:
                    redgifs_url = candidate
                    break
        if redgifs_url:
            logger.debug("Extracted RedGIFs URL from Reddit JSON: " + redgifs_url)
        else:
            logger.error("Could not extract a RedGIFs URL from the post.")
        return redgifs_url
    except Exception as e:
        logger.exception("Error extracting RedGIFs URL from Reddit JSON: " + str(e))
        return None

def get_redgifs_mp4_url(url):
    """
    Attempts to extract an mp4 video URL for a RedGIFs post.
    """
    url = normalize_redgifs_url(url)
    logger.debug("Attempting to fetch mp4 URL from RedGIFs for: " + url)
    
    api_url = "https://api.redgifs.com/v1/oembed?url=" + quote(url, safe='')
    logger.debug("Fetching RedGIFs oEmbed API URL: " + api_url)
    try:
        response = requests.get(api_url, timeout=10)
        logger.debug("oEmbed API response status: " + str(response.status_code))
        if response.status_code == 200:
            data = response.json()
            logger.debug("oEmbed data: " + str(data))
            html_embed = data.get("html", "")
            match = re.search(r'src=[\'"]([^\'"]+\.mp4)[\'"]', html_embed)
            if match:
                mp4_url = match.group(1)
                logger.debug("Extracted mp4 URL from oEmbed: " + mp4_url)
                return mp4_url
            else:
                logger.error("No mp4 URL found in oEmbed HTML: " + html_embed)
        else:
            logger.error("Failed fetching oEmbed API, status: " + str(response.status_code))
    except Exception as e:
        logger.exception("Exception while calling oEmbed API: " + str(e))
    
    m = re.search(r'(?:watch|ifr)/(\w+)', url)
    if m:
        gif_id = m.group(1)
        gfycats_url = f"https://api.redgifs.com/v1/gfycats/{gif_id}"
        logger.debug("Attempting GFYCats API with URL: " + gfycats_url)
        try:
            response = requests.get(gfycats_url, timeout=10)
            logger.debug("GFYCats API response status: " + str(response.status_code))
            if response.status_code == 200:
                data = response.json()
                logger.debug("GFYCats response data: " + str(data))
                gfyItem = data.get("gfyItem", {})
                mp4_url = gfyItem.get("mp4Url", "")
                if not mp4_url and "urls" in gfyItem:
                    mp4_url = gfyItem["urls"].get("hd", "")
                if mp4_url:
                    logger.debug("Extracted mp4 URL from GFYCats API: " + mp4_url)
                    return mp4_url
                else:
                    logger.error("No mp4 URL property found in GFYCats response.")
            else:
                logger.error("GFYCats API call failed with status: " + str(response.status_code))
        except Exception as e:
            logger.exception("Exception calling GFYCats API: " + str(e))
    else:
        logger.error("Could not extract RedGIFs ID from URL for GFYCats API call.")
    
    logger.debug("Returning original URL as fallback: " + url)
    return url  # fallback

# Define registry for provider-specific handlers.
provider_handlers = {}

def register_handler(domain, handler):
    provider_handlers[domain] = handler

def redgifs_image_handler(url):
    """
    Special handling for i.redgifs.com image URLs.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        "Referer": "https://redgifs.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        resp = requests.get(url, stream=True, allow_redirects=True, headers=headers, timeout=10)
        ctype = resp.headers.get('Content-Type', '')
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
                logger.debug("Redgifs handler: extracted image URL: " + extracted_url)
                return extracted_url
            else:
                logger.error("Redgifs handler: No og:image tag found.")
    except Exception as e:
        logger.exception("Redgifs handler exception: " + str(e))
    return url

register_handler("i.redgifs.com", redgifs_image_handler)

def process_media_url(url):
    """
    Determine the media provider and delegate processing.
    """
    logger.debug("Processing media URL: " + url)
    for domain, handler in provider_handlers.items():
        if domain in url:
            new_url = handler(url)
            if new_url != url:
                logger.debug(f"Handler for {domain} modified URL to: " + new_url)
                return new_url

    if "reddit.com/r/redgifs/comments/" in url:
        json_url = ensure_json_url(url)
        logger.debug("Converted Reddit URL to JSON endpoint: " + json_url)
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; red-image-browser/1.0)"}
            response = requests.get(json_url, headers=headers, timeout=10)
            response.raise_for_status()
            reddit_json = response.json()
            extracted = extract_redgifs_url_from_reddit(reddit_json)
            if extracted:
                normalized = normalize_redgifs_url(extracted)
                mp4_url = get_redgifs_mp4_url(normalized)
                logger.debug("Returning MP4 URL after Reddit extraction: " + mp4_url)
                return mp4_url
            else:
                logger.error("Failed to extract redgifs URL from Reddit JSON.")
        except Exception as e:
            logger.exception("Error processing Reddit redgifs URL: " + str(e))
        return url

    if (("redgifs.com/watch/" in url or "redgifs.com/ifr/" in url or "v3.redgifs.com/watch/" in url)
         and not url.endswith('.mp4')):
        return get_redgifs_mp4_url(url)

    logger.debug("No provider-specific processing required for: " + url)
    return url

# Asynchronous Image/Video Downloader Worker (Using QThreadPool & QRunnable)
class WorkerSignals(QObject):
    finished = pyqtSignal(str)  # Emits the downloaded file path

class ImageDownloadWorker(QRunnable):
    """
    Worker for downloading an image or video file asynchronously.
    """
    def __init__(self, url):
        super().__init__()
        self.url = url
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        file_path = self.download_file(self.url)
        self.signals.finished.emit(file_path)

    def download_file(self, url):
        if url.endswith('.gifv'):
            url = url.replace('.gifv', '.mp4')

        cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
        os.makedirs(cache_dir, exist_ok=True)

        domain = urlparse(url).netloc
        domain_dir = os.path.join(cache_dir, domain)
        os.makedirs(domain_dir, exist_ok=True)

        parsed_url = urlparse(url)
        path = unquote(parsed_url.path)
        filename = os.path.basename(path)
        if not filename:
            filename = "downloaded_media.mp4" if url.endswith('.mp4') else "downloaded_media"
        filename = filename.replace('?', '_').replace('&', '_').replace('=', '_')
        file_path = os.path.join(domain_dir, filename)

        if os.path.exists(file_path):
            logger.debug(f"File already cached: {file_path}")
            return file_path

        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            if "i.redgifs.com" in domain:
                headers["Referer"] = "https://redgifs.com/"
                headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"

            response = requests.get(url, stream=True, allow_redirects=False, headers=headers)
            redirect_count = 0
            while response.status_code in (301, 302, 303, 307, 308) and redirect_count < 5:
                redirect_location = response.headers.get('Location')
                # Use urljoin to convert potential relative URL to an absolute one
                redirect_url = urljoin(url, redirect_location)
                logger.debug(f"Redirecting to {redirect_url}")
                response = requests.get(redirect_url, stream=True, allow_redirects=False, headers=headers)
                url = redirect_url  # Update the base URL for potential further redirects
                redirect_count += 1

            ctype = response.headers.get('Content-Type', '')
            logger.debug(f"Downloading {url} - Final URL: {response.url} - Content-Type: {ctype}")
            if url.endswith('.mp4') or 'image' in ctype or 'video' in ctype:
                with open(file_path, 'wb') as local_file:
                    shutil.copyfileobj(response.raw, local_file)
                file_size = os.path.getsize(file_path)
                logger.debug(f"Downloaded {file_path} (size: {file_size} bytes)")
                return file_path
            else:
                logger.error(f"Invalid content type for URL {url}: {ctype}")
                return None
        except Exception as e:
            logger.exception(f"Failed to download {url}: {e}")
            return None


# Reddit Gallery Model and Snapshot Fetching
class RedditGalleryModel(QAbstractListModel):
    def __init__(self, name, is_user_mode=False, parent=None):
        """
        If is_user_mode is False, then name is treated as a subreddit.
        If is_user_mode is True, then name is treated as a redditor's username.
        """
        super().__init__(parent)
        self.is_user_mode = is_user_mode
        self.is_moderator = False  # Default moderator status
        self.snapshot = []         # Snapshot of submissions (up to 100)
        self.source_name = name
        if self.is_user_mode:
            self.user = reddit.redditor(name)
        else:
            self.subreddit = reddit.subreddit(name)

    def check_user_moderation_status(self):
        try:
            if self.is_user_mode:
                return False
            logger.debug("Performing one-time moderator status check")
            moderators = list(self.subreddit.moderator())
            user = reddit.user.me()
            logger.debug(f"Current user: {user.name}")
            logger.debug(f"Moderators in subreddit: {[mod.name for mod in moderators]}")
            self.is_moderator = any(mod.name.lower() == user.name.lower() for mod in moderators)
            logger.debug(f"Moderator status for current user: {self.is_moderator}")
            return self.is_moderator
        except prawcore.exceptions.PrawcoreException as e:
            logger.error(f"PRAW error while checking moderation status: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error while checking moderation status: {e}")
            return False

    def fetch_submissions(self, after=None, count=10):
        submissions = []
        new_after = None
        try:
            if self.is_user_mode:
                already_fetched = sum(len(page) for page in self.snapshot) if self.snapshot else 0
                user_params = {
                    'limit': count,
                    'count': already_fetched
                }
                if after:
                    user_params['after'] = after
                submissions = list(self.user.submissions.new(limit=count, params=user_params))
            else:
                if self.is_moderator:
                    already_fetched = sum(len(page) for page in self.snapshot) if self.snapshot else 0
                    params = {
                        'limit': count,
                        'raw_json': 1,
                        'sort': 'new',
                        'count': already_fetched
                    }
                    if after:
                        params['after'] = after
                    # Fetch new submissions and modqueue submissions
                    new_subs = list(self.subreddit.new(limit=count, params=params))
                    mod_subs = list(self.subreddit.mod.modqueue(limit=count))
                    # Fetch removed submissions from the mod log (using removelink action)
                    mod_log_entries = list(self.subreddit.mod.log(action="removelink", limit=count))
                    removed_fullnames = {entry.target_fullname for entry in mod_log_entries if entry.target_fullname.startswith("t3_")}
                    removed_subs = list(reddit.info(fullnames=list(removed_fullnames)))
                    # Merge submissions by unique id
                    merged = {s.id: s for s in new_subs + mod_subs + removed_subs}
                    submissions = list(merged.values())
                    submissions.sort(key=lambda s: s.created_utc, reverse=True)
                else:
                    already_fetched = sum(len(page) for page in self.snapshot) if self.snapshot else 0
                    params = {
                        'limit': count,
                        'raw_json': 1,
                        'sort': 'new',
                        'count': already_fetched
                    }
                    if after:
                        params['after'] = after
                    submissions = list(self.subreddit.new(limit=count, params=params))
            if submissions:
                new_after = submissions[-1].name
            else:
                new_after = None
        except Exception as e:
            logger.exception(f"Error fetching submissions: {e}")
        return submissions, new_after

    def fetch_snapshot(self, total=100, after=None):
        try:
            params = {'raw_json': 1, 'sort': 'new'}
            if after:
                params['after'] = after
            if self.is_user_mode:
                snapshot = list(self.user.submissions.new(limit=total, params=params))
            else:
                if self.is_moderator:
                    # Fetch new submissions, modqueue submissions, and removed submissions from mod log
                    new_subs = list(self.subreddit.new(limit=total, params=params))
                    mod_subs = list(self.subreddit.mod.modqueue(limit=total))
                    mod_log_entries = list(self.subreddit.mod.log(action="removelink", limit=total))
                    removed_fullnames = {entry.target_fullname for entry in mod_log_entries if entry.target_fullname.startswith("t3_")}
                    removed_subs = list(reddit.info(fullnames=list(removed_fullnames)))
                    
                    # Mark submissions as removed in our moderation_statuses dictionary.
                    for sub in removed_subs:
                        moderation_statuses[sub.id] = "removed"
                    
                    merged = {s.id: s for s in new_subs + mod_subs + removed_subs}
                    snapshot = list(merged.values())
                    snapshot.sort(key=lambda s: s.created_utc, reverse=True)
                    # Filter out objects without a title (e.g. comments)
                    snapshot = [s for s in snapshot if hasattr(s, 'title')]
                else:
                    snapshot = list(self.subreddit.new(limit=total, params=params))
                    # Ensure removed posts are not shown in non-mod view
                    snapshot = [s for s in snapshot if moderation_statuses.get(s.id) != "removed"]
            logger.debug(f"Fetched snapshot of {len(snapshot)} submissions.")
            return snapshot
        except Exception as e:
            logger.exception("Error fetching snapshot: " + str(e))
            return []

# Snapshot Fetcher (Asynchronous Fetching of the Full Snapshot)
class SnapshotFetcher(QThread):
    snapshotFetched = pyqtSignal(list)
    def __init__(self, model, total=100, after=None):
        super().__init__()
        self.model = model
        self.total = total
        self.after = after
    def run(self):
        snapshot = self.model.fetch_snapshot(total=self.total, after=self.after)
        self.snapshotFetched.emit(snapshot)

# ClickableLabel for handling clicks on labels
class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.clicked.emit()

# ThumbnailWidget to Display Each Submission
class ThumbnailWidget(QWidget):
    authorClicked = pyqtSignal(str)

    def __init__(self, images, title, source_url, submission,
                 subreddit_name, has_multiple_images, post_url, is_moderator):
        super().__init__()
        self.praw_submission = submission
        self.submission_id = submission.id
        self.images = images
        self.current_index = 0
        self.post_url = post_url
        self.layout = QVBoxLayout(self)

        self.titleLabel = ClickableLabel()
        self.titleLabel.setText(title)
        self.titleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.titleLabel.setFixedHeight(20)
        self.layout.addWidget(self.titleLabel)
        self.titleLabel.clicked.connect(self.open_post_url)

        self.infoLayout = QHBoxLayout()
        try:
            username = submission.author.name if submission.author else "unknown"
        except Exception:
            username = "unknown"
        self.authorLabel = ClickableLabel()
        self.authorLabel.setText(username)
        self.authorLabel.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.authorLabel.setFixedHeight(20)
        self.authorLabel.clicked.connect(lambda: self.authorClicked.emit(username))
        self.infoLayout.addWidget(self.authorLabel)

        self.postUrlLabel = QLabel(source_url)
        self.postUrlLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.postUrlLabel.setFixedHeight(20)
        self.infoLayout.addWidget(self.postUrlLabel)
        self.layout.addLayout(self.infoLayout)

        self.imageLabel = ClickableLabel()
        self.imageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.imageLabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Disable auto-scaling so that we control letterboxing via setScaledSize
        self.imageLabel.setScaledContents(False)
        self.layout.addWidget(self.imageLabel)
        self.imageLabel.clicked.connect(self.open_fullscreen_view)

        self.has_multiple_images = has_multiple_images
        if self.has_multiple_images:
            self.init_arrow_buttons()

        self.pixmap = None
        self.subreddit_name = subreddit_name
        self.is_moderator = is_moderator
        logger.debug(f"ThumbnailWidget initialized with is_moderator: {self.is_moderator}")

        if self.is_moderator:
            logger.debug("User is a moderator. Creating moderation buttons.")
            self.create_moderation_buttons()
            self.update_moderation_status_ui()  # Apply any saved state
        else:
            logger.debug("User is not a moderator.")

        if self.images:
            self.load_image_async(self.images[self.current_index])

    def update_moderation_status_ui(self):
        global moderation_statuses
        status = moderation_statuses.get(self.submission_id)
        if status == "approved":
            self.approve_button.setStyleSheet("background-color: green;")
            self.approve_button.setText("Approved")
        elif status == "removed":
            self.remove_button.setStyleSheet("background-color: red;")
            self.remove_button.setText("Removed")

    def open_post_url(self):
        full_url = "https://www.reddit.com" + self.post_url if self.post_url.startswith("/") else self.post_url
        logger.debug(f"Opening browser URL: {full_url}")
        webbrowser.open(full_url)


    def open_fullscreen_view(self):
        logger.debug("open_fullscreen_view triggered.")
        if hasattr(self, 'movie') and self.movie:
            logger.debug("FullScreenViewer will use QMovie.")
            viewer = FullScreenViewer(movie=self.movie)
        elif self.pixmap and not self.pixmap.isNull():
            logger.debug("FullScreenViewer will use QPixmap.")
            viewer = FullScreenViewer(pixmap=self.pixmap)
        else:
            logger.debug("No valid media (movie/pixmap) available for full screen view.")
            return  # No media available; do nothing.
        # Keep a reference so viewer isn’t garbage collected.
        self.fullscreen_viewer = viewer
        viewer.showFullScreen()

    def create_moderation_buttons(self):
        logger.debug("Creating moderation buttons.")
        self.moderation_layout = QHBoxLayout()
        self.approve_button = QPushButton("Approve", self)
        self.remove_button = QPushButton("Remove", self)
        
        self.approve_button.clicked.connect(self.approve_submission)
        self.remove_button.clicked.connect(self.remove_submission)
        
        self.moderation_layout.addWidget(self.approve_button)
        self.moderation_layout.addWidget(self.remove_button)
        self.layout.addLayout(self.moderation_layout)

    def init_arrow_buttons(self):
        if hasattr(self, 'leftArrowButton') and hasattr(self, 'rightArrowButton'):
            return
        self.arrowLayout = QHBoxLayout()
        self.arrowLayout.setSpacing(5)
        self.arrowLayout.setContentsMargins(0, 0, 0, 0)

        self.leftArrowButton = QPushButton("<")
        self.leftArrowButton.clicked.connect(self.show_previous_image)
        self.arrowLayout.addWidget(self.leftArrowButton)

        self.rightArrowButton = QPushButton(">")
        self.rightArrowButton.clicked.connect(self.show_next_image)
        self.arrowLayout.addWidget(self.rightArrowButton)

        self.leftArrowButton.setEnabled(len(self.images) > 1)
        self.rightArrowButton.setEnabled(len(self.images) > 1)
        self.layout.addLayout(self.arrowLayout)

    def load_image_async(self, url):
        processed_url = process_media_url(url)
        cached_pixmap = QPixmapCache.find(processed_url)
        if cached_pixmap:
            self.pixmap = cached_pixmap
            self.update_pixmap()
            return

        import weakref
        weak_self = weakref.ref(self)

        def safe_on_image_downloaded(file_path, purl):
            widget = weak_self()
            if widget is None:
                # The widget has been garbage collected.
                return
            try:
                widget.on_image_downloaded(file_path, purl)
            except RuntimeError as e:
                # Catch errors caused by the underlying C++ object being deleted.
                print("Caught RuntimeError in safe_on_image_downloaded:", e)

        schedule_media_download(
            url,
            safe_on_image_downloaded,
            pool=QThreadPool.globalInstance()
        )
    #------ safe on image downloaded above is tabbed under def load image async
    
    
    
    def resize_gif_first_frame(self, frame_number):
        """
        This slot is called for the very first frame of the GIF.
        We update the scale and then disconnect so that subsequent frames are not re-scaled.
        """
        self.update_movie_scale()
        try:
            self.movie.frameChanged.disconnect(self.resize_gif_first_frame)
        except Exception:
            # In case the signal is already disconnected.
            pass
    
    def on_image_downloaded(self, file_path, url):
        if file_path:
            if file_path.endswith('.mp4'):
                self.play_video(file_path)
                return
            elif file_path.lower().endswith('.gif'):
                self.movie = QMovie(file_path)
                # Connect only once for the very first frame.
                self.movie.frameChanged.connect(self.resize_gif_first_frame)
                self.imageLabel.setMovie(self.movie)
                self.movie.start()
                domain = os.path.basename(os.path.dirname(file_path))
                filename = os.path.basename(file_path)
                display_path = f"{domain}/{filename}"
                self.postUrlLabel.setText(display_path)
            else:
                pix = QPixmap(file_path)
                if not pix.isNull():
                    QPixmapCache.insert(url, pix)
                    self.pixmap = pix
                    self.update_pixmap()
                    domain = os.path.basename(os.path.dirname(file_path))
                    filename = os.path.basename(file_path)
                    display_path = f"{domain}/{filename}"
                    self.postUrlLabel.setText(display_path)
                else:
                    self.imageLabel.setText("Image not available")
        else:
            self.imageLabel.setText("Image not available")

    def update_pixmap(self):
        if self.pixmap and not self.pixmap.isNull():
            scaled_pixmap = self.pixmap.scaled(self.imageLabel.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.imageLabel.setPixmap(scaled_pixmap)
        else:
            self.imageLabel.clear()
            self.imageLabel.setText("Image not available")
            self.imageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def update_movie_scale(self):
        if self.movie:
            # Use the current pixmap's size for a reliable natural size
            current_pixmap = self.movie.currentPixmap()
            if current_pixmap.isNull():
                logger.warning("update_movie_scale: current pixmap is null.")
                return
            orig_width = current_pixmap.width()
            orig_height = current_pixmap.height()
            label_width = self.imageLabel.width()
            label_height = self.imageLabel.height()
            logger.debug("update_movie_scale: label dimensions: %d x %d", label_width, label_height)
            logger.debug("update_movie_scale: original movie dimensions: %d x %d", orig_width, orig_height)
            scale_factor = min(label_width / orig_width, label_height / orig_height)
            new_width = int(orig_width * scale_factor)
            new_height = int(orig_height * scale_factor)
            new_size = QSize(new_width, new_height)
            logger.debug("update_movie_scale: setting new scaled size: %s", new_size)
            self.movie.setScaledSize(new_size)
            
    def resizeEvent(self, event):
       super().resizeEvent(event)
       # Log the image label dimensions for debugging.
       size = self.imageLabel.size()
       logger.debug(f"resizeEvent: imageLabel size: {size.width()}x{size.height()}")
       
       # Update the QMovie scaling if a movie is active.
       if hasattr(self, 'movie') and self.movie:
           self.update_movie_scale()
       # Otherwise, if a static image is displayed, update the pixmap scaling.
       elif self.pixmap and not self.pixmap.isNull():
           scaled_pixmap = self.pixmap.scaled(
               self.imageLabel.size(),
               Qt.AspectRatioMode.KeepAspectRatio,
               Qt.TransformationMode.SmoothTransformation
           )
           self.imageLabel.setPixmap(scaled_pixmap)
       
       # If you’re using VLC playback, handle that as needed.
       if hasattr(self, 'vlc_player') and self.vlc_player:
           native_size = self.vlc_player.video_get_size(0)
           if native_size[0] > 0 and native_size[1] > 0:
               aspect_ratio_str = f"{native_size[0]}:{native_size[1]}"
               self.vlc_player.video_set_aspect_ratio(aspect_ratio_str)

    def show_next_image(self):
        if self.images:
            self.current_index = (self.current_index + 1) % len(self.images)
            self.load_image_async(self.images[self.current_index])

    def show_previous_image(self):
        if self.images:
            self.current_index = (self.current_index - 1) % len(self.images)
            self.load_image_async(self.images[self.current_index])


    def attach_vlc_event_handlers(self, no_hw):
        # Only attach basic logging events; custom restart/looping logic removed.
        event_manager = self.vlc_player.event_manager()
        event_manager.event_attach(vlc.EventType.MediaPlayerPlaying,
                                   lambda event: logger.debug("VLC: Media started playing"))
        event_manager.event_attach(vlc.EventType.MediaPlayerPaused,
                                   lambda event: logger.debug("VLC: Media paused"))
        event_manager.event_attach(vlc.EventType.MediaPlayerStopped,
                                   lambda event: logger.debug("VLC: Media stopped"))

    def play_video(self, video_url, no_hw=False):
        abs_video_path = os.path.abspath(video_url)
        self.current_video_path = abs_video_path
        logger.debug(f"VLC: Playing video from file: {abs_video_path}")

        if hasattr(self, 'imageLabel'):
            self.layout.removeWidget(self.imageLabel)
            self.imageLabel.hide()

        if hasattr(self, 'vlc_player'):
            logger.debug("VLC: Cleaning up existing player")
            self.vlc_player.stop()
            self.layout.removeWidget(self.vlc_widget)
            self.vlc_widget.deleteLater()
            del self.vlc_player

        instance_args = (['--loop', '--vout=directx', '--no-video-title-show', '--verbose=0']
                        if not no_hw else
                        ['--loop', '--no-hw-decoding', '--vout=directx', '--no-video-title-show', '--verbose=0'])
        logger.debug(f"VLC: Creating instance with args: {instance_args}")
        self.vlc_instance = vlc.Instance(*instance_args)
        self.vlc_player = self.vlc_instance.media_player_new()
        self.vlc_widget = QWidget(self)
        self.layout.addWidget(self.vlc_widget)
        self.vlc_widget.show()

        if sys.platform.startswith('win'):
            self.vlc_player.set_hwnd(self.vlc_widget.winId())
        elif sys.platform.startswith('linux'):
            self.vlc_player.set_xwindow(self.vlc_widget.winId())
        elif sys.platform.startswith('darwin'):
            self.vlc_player.set_nsobject(int(self.vlc_widget.winId()))

        media = self.vlc_instance.media_new(abs_video_path)
        # Removed auto-repeat option to avoid conflict:
        # media.add_option('input-repeat=-1')
        self.vlc_player.set_media(media)

        # (Custom event handlers for restarting/looping have been removed)

        self.vlc_player.play()
        self.vlc_player.audio_set_mute(True)
        self.vlc_player.video_set_scale(0)

        time.sleep(0.1)  # Consider non-blocking timer
        native_size = self.vlc_player.video_get_size(0)
        if native_size[0] > 0 and native_size[1] > 0:
            aspect_ratio_str = f"{native_size[0]}:{native_size[1]}"
            self.vlc_player.video_set_aspect_ratio(aspect_ratio_str)

        if self.is_moderator and hasattr(self, 'moderation_layout'):
            self.layout.removeItem(self.moderation_layout)
            self.layout.addLayout(self.moderation_layout)

    def approve_submission(self):
        self.praw_submission.mod.approve()
        global moderation_statuses
        moderation_statuses[self.submission_id] = "approved"
        logger.debug(f"Approved: {self.submission_id}")
        self.approve_button.setStyleSheet("background-color: green;")
        self.approve_button.setText("Approved")

    def remove_submission(self):
        try:
            self.praw_submission.mod.remove()
            global moderation_statuses
            moderation_statuses[self.submission_id] = "removed"
            logger.debug(f"Removed: {self.submission_id}")
            self.remove_button.setStyleSheet("background-color: red;")
            self.remove_button.setText("Removed")
        except prawcore.exceptions.Forbidden:
            logger.error(f"Forbidden: You do not have permission to remove submission {self.submission_id}")
        except Exception as e:
            logger.exception(f"Unexpected error while removing submission {self.submission_id}: {e}")



class BanUserDialog(QDialog):
    def __init__(self, username, subreddit, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ban User")
        self.result = None  # Will be "share" or "private"
        self.reason = ""
        
        # Main layout
        layout = QVBoxLayout(self)
        
        # Instruction label
        label = QLabel(f"Enter the reason for banning {username} from r/{subreddit}:", self)
        layout.addWidget(label)
        
        # Ban reason input
        self.reason_input = QLineEdit(self)
        layout.addWidget(self.reason_input)
        
        # Buttons layout
        button_layout = QHBoxLayout()
        self.share_button = QPushButton("Ban and Share Reason with User", self)
        self.private_button = QPushButton("Ban and Set Private Reason", self)
        self.cancel_button = QPushButton("Cancel", self)
        
        button_layout.addWidget(self.share_button)
        button_layout.addWidget(self.private_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)
        
        # Connections
        self.share_button.clicked.connect(self.share_clicked)
        self.private_button.clicked.connect(self.private_clicked)
        self.cancel_button.clicked.connect(self.reject)  # Simply close dialog
        
    def share_clicked(self):
        text = self.reason_input.text().strip()
        if not text:
            QMessageBox.warning(self, "Warning", "You must enter a ban reason.")
            return
        self.reason = text
        self.result = "share"
        self.accept()
    
    def private_clicked(self):
        text = self.reason_input.text().strip()
        if not text:
            QMessageBox.warning(self, "Warning", "You must enter a ban reason.")
            return
        self.reason = text
        self.result = "private"
        self.accept()


# Main Window and Gallery View Classes with Snapshot Pagination
class MainWindow(QMainWindow):
    def __init__(self, subreddit='pics'):
        super().__init__()
        self.setWindowTitle("Reddit Image and Video Gallery")
        self.items_per_page = 10
        self.central_widget = QWidget()
        self.layout = QVBoxLayout(self.central_widget)
        
        self.status_label = QLabel("Loading...")
        self.layout.addWidget(self.status_label)

        self.saved_subreddit_model = None
        self.saved_page = None

        # Input layouts for subreddit and user posts.
        input_layout = QHBoxLayout()
        subreddit_layout = QHBoxLayout()
        self.subreddit_input = QLineEdit(subreddit)
        self.subreddit_input.textChanged.connect(lambda: self.update_ban_button_visibility() if self.saved_subreddit_model else None)
        self.load_subreddit_button = QPushButton('Load Subreddit')
        subreddit_layout.addWidget(self.subreddit_input)
        subreddit_layout.addWidget(self.load_subreddit_button)
        
        self.back_button = QPushButton("Back to Subreddit")
        self.back_button.clicked.connect(self.load_default_subreddit)
        self.back_button.hide()
        subreddit_layout.addWidget(self.back_button)
        
        user_layout = QHBoxLayout()
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Enter username to load posts")
        self.load_user_button = QPushButton("Load User")
        user_layout.addWidget(self.user_input)
        user_layout.addWidget(self.load_user_button)
                
        # New Filter by Subreddit button for user posts view.
        self.filter_by_subreddit_button = QPushButton("Filter by Subreddit")
        self.filter_by_subreddit_button.clicked.connect(self.filter_user_posts)
        self.filter_by_subreddit_button.hide()
        subreddit_layout.addWidget(self.filter_by_subreddit_button)

        # Ban button; it will be visible only in user mode when filtering by a subreddit 
        # and if you are a moderator of that subreddit.
        self.ban_user_button = QPushButton()
        self.ban_user_button.clicked.connect(self.ban_user)
        self.ban_user_button.hide()
        subreddit_layout.addWidget(self.ban_user_button)


        
        input_layout.addLayout(subreddit_layout)
        input_layout.addStretch()
        input_layout.addLayout(user_layout)
        
        self.layout.addLayout(input_layout)
        
        self.table_widget = QTableWidget(2, 5, self)
        self.table_widget.setHorizontalHeaderLabels(['A', 'B', 'C', 'D', 'E'])
        self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_widget.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.layout.addWidget(self.table_widget)
        
        nav_layout = QHBoxLayout()
        self.prev_page_button = QPushButton('Previous Page')
        self.next_page_button = QPushButton('Next Page')
        self.download_100_button = QPushButton('Refresh Snapshot')
        nav_layout.addWidget(self.prev_page_button)
        nav_layout.addWidget(self.next_page_button)
        nav_layout.addWidget(self.download_100_button)
        self.layout.addLayout(nav_layout)
        
        self.setCentralWidget(self.central_widget)
        
        self.load_subreddit_button.clicked.connect(self.load_subreddit)
        self.subreddit_input.returnPressed.connect(self.load_subreddit)
        self.load_user_button.clicked.connect(lambda: self.load_user_posts(self.user_input.text().strip()))
        self.prev_page_button.clicked.connect(self.load_previous_page)
        self.next_page_button.clicked.connect(self.load_next_page)
        self.download_100_button.clicked.connect(self.fetch_snapshot_for_model)
        
        self.model = RedditGalleryModel(subreddit)
        self.model.is_moderator = self.model.check_user_moderation_status()
        logger.debug(f"MainWindow initialized with is_moderator: {self.model.is_moderator}")
        self.setMinimumSize(800, 600)
        self.center()
        
        self.threadpool = QThreadPool()
        self.after_cursor = None  # To track the "after" cursor for pagination
        
        # For snapshot mode, fetch the snapshot from the API once.
        self.fetch_snapshot_for_model()
    
    def update_status(self, message):
        full_message = message
        if hasattr(self, "paginated_pages") and self.paginated_pages:
            full_message = f"Page {self.current_page_index + 1}: " + message
        self.status_label.setText(full_message)
        logger.debug("Status updated: " + full_message)
    
    def fetch_snapshot_for_model(self, append=False, after=None):
        self.snapshot_fetcher = SnapshotFetcher(self.model, total=100, after=after)
        if append:
            self.snapshot_fetcher.snapshotFetched.connect(self.on_snapshot_appended)
        else:
            self.snapshot_fetcher.snapshotFetched.connect(self.on_snapshot_fetched)
        self.snapshot_fetcher.start()
    
    def on_snapshot_fetched(self, snapshot):
        self.model.snapshot = snapshot
        if snapshot:
            self.after_cursor = snapshot[-1].name  # Save the cursor from the last submission
        self.paginated_pages = [snapshot[i:i+self.items_per_page] for i in range(0, len(snapshot), self.items_per_page)]
        self.current_page_index = 0
        self.display_current_page_submissions_snapshot()
        total_pages = len(self.paginated_pages)
        self.update_status(f"Snapshot loaded. Showing page 1 of {total_pages}.")
    
    def on_snapshot_appended(self, snapshot):
        if snapshot:
            new_after = snapshot[-1].name if snapshot[-1].name else None
            logger.debug(f"Fetched {len(snapshot)} new posts. New after_cursor: {new_after}")
            # Always update after_cursor, regardless of equality.
            self.after_cursor = new_after
            # Append the new posts to the already loaded snapshot.
            self.model.snapshot.extend(snapshot)
            # Recalculate the paginated pages.
            self.paginated_pages = [
                self.model.snapshot[i:i + self.items_per_page]
                for i in range(0, len(self.model.snapshot), self.items_per_page)
            ]
            self.current_page_index += 1
            self.display_current_page_submissions_snapshot()
            self.update_status(f"Displaying snapshot page {self.current_page_index + 1} of {len(self.paginated_pages)}")
        else:
            self.update_status("No more older posts available.")
    
    def load_subreddit(self):
        subreddit_name = self.subreddit_input.text()
        self.update_status(f"Loading subreddit '{subreddit_name}' snapshot...")
        self.after_cursor = None  # Reset the cursor when reloading a different subreddit
        try:
            self.model = RedditGalleryModel(subreddit_name)
            self.model.is_moderator = self.model.check_user_moderation_status()
            logger.debug(f"Moderator status for subreddit '{subreddit_name}': {self.model.is_moderator}")
            self.paginated_pages = []
            self.current_page_index = 0
            self.table_widget.clearContents()
            self.fetch_snapshot_for_model()
            self.load_subreddit_button.setEnabled(True)
            self.subreddit_input.setEnabled(True)
            self.back_button.hide()
        except prawcore.exceptions.Redirect as e:
            error_msg = f"Subreddit '{subreddit_name}' does not exist."
            logger.error(error_msg)
            QMessageBox.critical(self, "Subreddit Error", error_msg)
            return
        except Exception as e:
            logger.error(f"Error loading subreddit: {str(e)}")
            return
    
    def load_user_posts(self, username):
        self.user_input.setText(username)
        self.after_cursor = None  # Reset the cursor for user posts as well.
        try:
            new_model = RedditGalleryModel(username, is_user_mode=True)
            _ = new_model.user.id  
        except Exception as e:
            logger.error(f"Error loading posts for user {username}: {e}")
            self.update_status(f"Error: User '{username}' does not exist or cannot be loaded.")
            return

        # Save the currently loaded subreddit model.
        self.saved_subreddit_model = self.model  
        self.saved_page = self.current_page_index
        self.update_status(f"Loading posts from user '{username}' snapshot...")
        
        # Switch model to user mode.
        self.model = new_model
        self.model.is_moderator = self.model.check_user_moderation_status()
        self.paginated_pages = []
        self.current_page_index = 0
        self.table_widget.clearContents()
        self.load_subreddit_button.setEnabled(False)
        
        # Enable the subreddit input (for filtering) and show our added buttons.
        self.subreddit_input.setEnabled(True)  
        self.back_button.show()
        self.filter_by_subreddit_button.show()
        
        # Pre-fill the subreddit input with the subreddit you came from.
        if self.saved_subreddit_model:
            self.subreddit_input.setText(self.saved_subreddit_model.source_name)
        self.fetch_snapshot_for_model()

        # Update ban button visibility and text for the new user.
        self.update_ban_button_visibility()
    
    
    def filter_user_posts(self):
        """
        Filters the current user's posts to display only posts from the specified subreddit.
        If the input is empty, it resets the filter to show all posts.
        """
        filter_subreddit = self.subreddit_input.text().strip()
        if filter_subreddit == "":
            # Reset filter: show all user posts.
            self.paginated_pages = [
                self.model.snapshot[i:i+self.items_per_page]
                for i in range(0, len(self.model.snapshot), self.items_per_page)
            ]
            self.current_page_index = 0
            self.display_current_page_submissions_snapshot()
            self.update_status(f"Showing all posts by {self.model.user.name}.")
            self.ban_user_button.hide()  # Hide the ban button when no filter is set.
            return

        # Filter the snapshot to only those submissions in the specified subreddit.
        filtered_snapshot = [
            s for s in self.model.snapshot 
            if s.subreddit.display_name.lower() == filter_subreddit.lower()
        ]
        if not filtered_snapshot:
            self.update_status(f"No posts found in subreddit r/{filter_subreddit} by user {self.model.user.name}.")
        else:
            self.paginated_pages = [
                filtered_snapshot[i:i+self.items_per_page]
                for i in range(0, len(filtered_snapshot), self.items_per_page)
            ]
            self.current_page_index = 0
            self.display_current_page_submissions_snapshot()
            self.update_status(f"Filtered posts for user {self.model.user.name} in subreddit r/{filter_subreddit}.")
        
        # Update the ban button visibility since we are in user mode with a filter.
        self.update_ban_button_visibility()


    def update_ban_button_visibility(self):
        """
        Show the ban button only when:
        - We are in user mode (i.e. a saved_subreddit_model exists)
        - The saved subreddit model indicates the current account is a moderator
        - A subreddit filter is applied (i.e. the subreddit_input text is nonempty)
        """
        if self.saved_subreddit_model and self.saved_subreddit_model.is_moderator:
            filter_subreddit = self.subreddit_input.text().strip()
            if filter_subreddit:
                self.ban_user_button.setText(f"Ban {self.model.user.name} from r/{filter_subreddit}")
                self.ban_user_button.show()
                # Re-enable the ban button for the new user view.
                self.ban_user_button.setEnabled(True)
            else:
                self.ban_user_button.hide()
        else:
            self.ban_user_button.hide()

    def ban_user(self):
        """
        Uses the Reddit API to ban the user from the filtered subreddit,
        after prompting for a ban reason and letting the moderator choose
        to share the reason with the user or keep it private (or cancel the operation).
        """
        filter_subreddit = self.subreddit_input.text().strip()
        username = self.model.user.name

        # Create and show the custom ban dialog
        dialog = BanUserDialog(username, filter_subreddit, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            ban_reason = dialog.reason
            action = dialog.result  # "share" or "private"
            try:
                target_subreddit = reddit.subreddit(filter_subreddit)
                if action == "share":
                    target_subreddit.banned.add(username, ban_reason=ban_reason, ban_message=ban_reason, note=ban_reason)
                elif action == "private":
                    target_subreddit.banned.add(username, ban_reason=ban_reason, note=ban_reason)
                QMessageBox.information(self, "User Banned", f"{username} has been banned from r/{filter_subreddit}.")
                self.ban_user_button.setEnabled(False)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to ban {username} from r/{filter_subreddit}.\nError: {str(e)}")
        else:
            # Dialog was canceled – no further action is taken.
            return

    
    
    def load_default_subreddit(self):
        if self.saved_subreddit_model:
            # Restore the previously saved subreddit model and page index.
            self.model = self.saved_subreddit_model
            self.current_page_index = self.saved_page
            # Rebuild paginated pages using the already fetched snapshot.
            if self.model.snapshot:
                self.paginated_pages = [
                    self.model.snapshot[i:i+self.items_per_page] 
                    for i in range(0, len(self.model.snapshot), self.items_per_page)
                ]
                self.display_current_page_submissions_snapshot()
                self.update_status(f"Returning to subreddit '{self.model.source_name}' snapshot, page {self.current_page_index + 1}.")
            else:
                self.fetch_snapshot_for_model()
            self.back_button.hide()
            self.filter_by_subreddit_button.hide()  # Hide filter button in subreddit view.
            self.load_subreddit_button.setEnabled(True)
            self.subreddit_input.setEnabled(True)
            self.saved_subreddit_model = None
            self.saved_page = None
        else:
            subreddit_name = self.subreddit_input.text() or default_subreddit
            self.update_status(f"Loading subreddit '{subreddit_name}' snapshot...")
            self.model = RedditGalleryModel(subreddit_name)
            self.model.is_moderator = self.model.check_user_moderation_status()
            self.fetch_snapshot_for_model()
            self.load_subreddit_button.setEnabled(True)
            self.subreddit_input.setEnabled(True)
            self.back_button.hide()
            self.filter_by_subreddit_button.hide()
    
    def center(self):
        screen = QGuiApplication.primaryScreen()
        if screen:
            screen_geometry = screen.availableGeometry()
            frame_geometry = self.frameGeometry()
            frame_geometry.moveCenter(screen_geometry.center())
            self.move(frame_geometry.topLeft())
    
    def display_current_page_submissions_snapshot(self):
        if not hasattr(self, "paginated_pages") or not self.paginated_pages:
            logger.info("No snapshot submissions to display on this page.")
            return
        try:
            current_page = self.paginated_pages[self.current_page_index]
        except IndexError:
            logger.info("No submissions to display on this page.")
            return

        logger.debug(f"Displaying snapshot page {self.current_page_index + 1} with {len(current_page)} submissions.")
        self.table_widget.clearContents()
        row = 0
        col = 0

        for submission in current_page:
            if row >= self.table_widget.rowCount():
                break

            title = submission.title
            image_urls = extract_image_urls(submission)
            source_url = image_urls[0] if image_urls else submission.url

            thumb_widget = ThumbnailWidget(
                images=image_urls,
                title=title,
                source_url=source_url,
                submission=submission,
                subreddit_name=submission.subreddit.display_name if hasattr(submission, 'subreddit') else "",
                has_multiple_images=len(image_urls) > 1,
                post_url=submission.permalink,
                is_moderator=self.model.is_moderator
            )
            thumb_widget.authorClicked.connect(self.load_user_posts)
            self.table_widget.setCellWidget(row, col, thumb_widget)
            col += 1
            if col >= self.table_widget.columnCount():
                col = 0
                row += 1

        self.update_navigation_buttons()
    
    def update_navigation_buttons(self):
        self.prev_page_button.setEnabled(self.current_page_index > 0)
        if hasattr(self, "paginated_pages") and self.paginated_pages:
            # Enable next page if there is another page OR an after_cursor is available for fetching more posts.
            if self.current_page_index < len(self.paginated_pages) - 1 or self.after_cursor:
                self.next_page_button.setEnabled(True)
            else:
                self.next_page_button.setEnabled(False)
        else:
            self.next_page_button.setEnabled(False)
    
    def load_next_page(self):
        if hasattr(self, "paginated_pages") and self.paginated_pages:
            if self.current_page_index + 1 < len(self.paginated_pages):
                self.current_page_index += 1
                self.display_current_page_submissions_snapshot()
                self.update_status(f"Displaying snapshot page {self.current_page_index + 1} of {len(self.paginated_pages)}")
            else:
                # At the last page: fetch next 100 posts if available
                if self.after_cursor:
                    self.update_status("Fetching older posts...")
                    self.fetch_snapshot_for_model(append=True, after=self.after_cursor)
                else:
                    self.update_status("No older posts available.")
    
    def load_previous_page(self):
        if hasattr(self, "paginated_pages") and self.paginated_pages:
            if self.current_page_index > 0:
                self.current_page_index -= 1
                self.display_current_page_submissions_snapshot()
                self.update_status(f"Displaying snapshot page {self.current_page_index + 1} of {len(self.paginated_pages)}")


class FullScreenViewer(QDialog):
    def __init__(self, pixmap=None, movie=None, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.movie = movie
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)  # Updated flags
        self.setStyleSheet("background-color: black;")
        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        if self.movie:
            self.label.setMovie(self.movie)
            self.movie.start()
        elif self.pixmap:
            self.label.setPixmap(self.pixmap.scaled(
                QGuiApplication.primaryScreen().availableGeometry().size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
    
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)
    
    def mousePressEvent(self, event):
        self.close()
        super().mousePressEvent(event)




if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyleSheet("""
        QMainWindow, QWidget {
            background-color: #121212;
            color: white;
        }
        QPushButton {
            color: white;
            background-color: #1e1e1e;
            border: 1px solid #333333;
            padding: 5px;
        }
        QLineEdit {
            color: white;
            background-color: #1e1e1e;
            border: 1px solid #333333;
            padding: 5px;
        }
        QLabel {
            color: white;
        }
        QMessageBox {
            color: white;
            background-color: #121212;
        }
        QTableWidget {
            background-color: #1e1e1e;
            color: white;
            gridline-color: #333333;
        }
        QHeaderView::section {
            background-color: #1e1e1e;
            color: white;
            border: 1px solid #333333;
        }
    """)
    main_win = MainWindow(subreddit=default_subreddit)
    main_win.show()
    main_win.update()
    QApplication.processEvents()
    sys.exit(app.exec())