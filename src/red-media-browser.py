#!/usr/bin/env python3
"""
Red Media Browser - Media Browser Application for Reddit

This is the main application file that initializes the GUI and connects all components.
It handles Reddit authentication, creates the main window, and manages the application flow.
"""

import os
import sys
import logging
import json
import time
from typing import List, Optional, Dict, Any

import praw
# Import praw.models.Subreddit for type checking
from praw.models import Subreddit as PrawSubreddit
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QScrollArea, QMessageBox,
    QComboBox, QProgressBar, QSplitter, QMenu, QStatusBar, QTabWidget,
    QGridLayout
)
from PyQt6.QtCore import Qt, QSize, QThreadPool, QThread, pyqtSignal, QTimer, QMutex, QRunnable, QMutexLocker
from PyQt6.QtGui import QAction, QPixmapCache

from red_config import load_config, get_new_refresh_token, update_config_with_new_token
# Import specific workers and functions
from reddit_api import RedditGalleryModel, SnapshotFetcher, ModeratedSubredditsFetcher, BanWorker
from ui_components import ThumbnailWidget, BanUserDialog
from utils import get_cache_dir, ensure_directory, extract_image_urls
from media_handlers import process_media_url, MediaDownloadWorker, WorkerSignals

# Import constants
from constants import (
    PIXMAP_CACHE_SIZE_MB, POSTS_FETCH_LIMIT, MOD_LOG_FETCH_LIMIT,
    UI_UPDATE_DELAY_MS, MOD_STATUS_DELAY_MS, THREAD_TERMINATION_TIMEOUT_MS
)

# Configure logging
logging.basicConfig(
    level=logging.DEBUG, # Changed to DEBUG for more detailed startup info
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Set QPixmapCache size to cache more thumbnails
QPixmapCache.setCacheLimit(PIXMAP_CACHE_SIZE_MB * 1024)

# --- Background Thread for Mod Log Fetching ---
class ModLogFetcher(QThread):
    """
    Worker thread for asynchronous fetching of moderator logs.
    """
    modLogsReady = pyqtSignal(dict)
    progressUpdate = pyqtSignal(str)

    def __init__(self, reddit_instance: praw.Reddit, moderated_subreddits: List[Dict[str, str]]) -> None:
        super().__init__()
        self.reddit_instance = reddit_instance
        self.moderated_subreddits = moderated_subreddits
        self.prefetched_mod_logs: Dict[str, List[Dict[str, Optional[str]]]] = {}

    def run(self) -> None:
        total_subs = len(self.moderated_subreddits)
        logger.info(f"Starting background fetch for mod logs of {total_subs} subreddits...")
        self.progressUpdate.emit(f"Fetching mod logs (0/{total_subs})...")

        for i, subreddit_info in enumerate(self.moderated_subreddits):
            sub_name = subreddit_info['name']
            display_name = subreddit_info['display_name']
            logger.debug(f"Fetching mod log for r/{display_name} ({i+1}/{total_subs})")
            try:
                # Process entries iteratively to avoid loading all into memory at once
                log_generator = self.reddit_instance.subreddit(sub_name).mod.log(action="removelink", limit=MOD_LOG_FETCH_LIMIT)
                # Store only necessary info efficiently
                processed_entries = []
                entry_count = 0
                for entry in log_generator:
                    if entry.target_fullname and entry.target_fullname.startswith('t3_'):
                        # Safely extract author name
                        author_name = None
                        if entry.target_author:
                            try:
                                author_name = str(entry.target_author).lower()
                            except (AttributeError, TypeError):
                                logger.debug(f"Could not convert target_author to string: {entry.target_author}")
                                author_name = None
                        processed_entries.append({
                            'author': author_name,
                            'fullname': entry.target_fullname
                        })
                    entry_count += 1
                self.prefetched_mod_logs[sub_name] = processed_entries
                logger.debug(f"Processed {entry_count} log entries for r/{display_name}, stored {len(self.prefetched_mod_logs[sub_name])} relevant entries.")
            except Exception as e:
                logger.exception(f"Error fetching mod log for r/{display_name}: {e}")
                self.prefetched_mod_logs[sub_name] = [] # Store empty list on error

            # Update progress
            self.progressUpdate.emit(f"Fetching mod logs ({i+1}/{total_subs})...")

        logger.info(f"Finished fetching mod logs for {total_subs} subreddits.")
        self.modLogsReady.emit(self.prefetched_mod_logs)

# --- Background Thread for Reports Fetching ---
class ReportsFetcher(QThread):
    """
    Worker thread for asynchronous fetching of mod reports.
    """
    reportsFetched = pyqtSignal(list)
    errorOccurred = pyqtSignal(str)

    def __init__(self, subreddit) -> None:
        super().__init__()
        self.subreddit = subreddit

    def run(self) -> None:
        try:
            # Process reports iteratively to avoid memory spike
            reports = []
            for report in self.subreddit.mod.reports(limit=POSTS_FETCH_LIMIT):
                reports.append(report)
                # Optionally limit memory usage by processing in batches
                if len(reports) >= POSTS_FETCH_LIMIT:
                    break
            self.reportsFetched.emit(reports)
        except Exception as e:
            logger.exception(f"Error fetching reports in worker: {e}")
            self.errorOccurred.emit(str(e))

# --- Background Thread for Removed Posts Fetching ---
class RemovedPostsFetcher(QThread):
    """
    Worker thread for asynchronous fetching of removed posts via mod log.
    """
    removedPostsFetched = pyqtSignal(list)
    errorOccurred = pyqtSignal(str)

    def __init__(self, subreddit) -> None:
        super().__init__()
        self.subreddit = subreddit

    def run(self) -> None:
        try:
            # Process mod log iteratively to avoid memory spikes
            fullnames = []
            for entry in self.subreddit.mod.log(action="removelink", limit=POSTS_FETCH_LIMIT):
                if (hasattr(entry, "target_fullname") and entry.target_fullname and
                    entry.target_fullname.startswith("t3_")):
                    fullnames.append(entry.target_fullname)
                    # Limit memory usage
                    if len(fullnames) >= POSTS_FETCH_LIMIT:
                        break

            if fullnames:
                # Process submissions in smaller batches if list is very large
                removed = []
                batch_size = 100  # Reddit API limit for info() requests
                for i in range(0, len(fullnames), batch_size):
                    batch = fullnames[i:i + batch_size]
                    batch_submissions = list(self.subreddit._reddit.info(fullnames=batch))
                    removed.extend(batch_submissions)
            else:
                removed = []
            self.removedPostsFetched.emit(removed)
        except Exception as e:
            logger.exception(f"Error fetching removed posts in worker: {e}")
            self.errorOccurred.emit(str(e))

# --- Background Thread for Filtering ---
class FilterWorker(QThread):
    """
    Worker thread for filtering posts by subreddit.
    """
    filteringComplete = pyqtSignal(list)

    def __init__(self, snapshot: List[Any], subreddit_name_lower: str) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.subreddit_name_lower = subreddit_name_lower

    def run(self) -> None:
        logger.debug(f"FilterWorker started for r/{self.subreddit_name_lower} with {len(self.snapshot)} posts.")
        filtered_snapshot = []
        try:
            # Perform the filtering
            for post in self.snapshot:
                 # Safely get subreddit name, handling both PRAW objects and cached strings/SimpleNamespace
                 subreddit_name = "unknown"
                 subreddit_attr = getattr(post, 'subreddit', None)
                 if isinstance(subreddit_attr, PrawSubreddit):
                     subreddit_name = getattr(subreddit_attr, 'display_name', 'unknown')
                 elif isinstance(subreddit_attr, str):
                     subreddit_name = subreddit_attr
                 elif hasattr(subreddit_attr, 'display_name'): # Handle SimpleNamespace case
                      subreddit_name = getattr(subreddit_attr, 'display_name', 'unknown')

                 if subreddit_name.lower() == self.subreddit_name_lower:
                     filtered_snapshot.append(post)

            logger.debug(f"FilterWorker finished. Found {len(filtered_snapshot)} posts.")
        except Exception as e:
            logger.exception(f"Error during background filtering: {e}")
            filtered_snapshot = []
        finally:
            self.filteringComplete.emit(filtered_snapshot)


class MediaPrefetchWorker(QRunnable):
    """Worker for prefetching media files in the background."""

    def __init__(self, main_window, submissions_to_prefetch):
        super().__init__()
        self.main_window = main_window
        self.submissions_to_prefetch = submissions_to_prefetch
        self.signals = WorkerSignals()

    def run(self):
        """Prefetch media files for given submissions."""
        try:
            for submission in self.submissions_to_prefetch:
                if not hasattr(submission, 'id'):
                    continue

                # Extract image URLs for this submission
                image_urls = extract_image_urls(submission)

                # Prefetch each media file
                for url in image_urls:
                    try:
                        # Check if already being prefetched
                        with QMutexLocker(self.main_window.prefetch_mutex):
                            if url in self.main_window.prefetched_media:
                                continue
                            # Mark as being prefetched
                            self.main_window.prefetched_media[url] = {
                                'status': 'prefetching',
                                'started_at': time.time()
                            }

                        # Process URL to get actual media URL
                        processed_url = process_media_url(url)
                        if processed_url and processed_url != url:
                            # Check if already cached
                            import os
                            from utils import get_cache_path
                            cache_path = get_cache_path(processed_url)

                            if not os.path.exists(cache_path):
                                # Start media download
                                worker = MediaDownloadWorker(processed_url, submission)
                                QThreadPool.globalInstance().start(worker)
                                logger.info(f"Prefetching media (not cached): {processed_url}")
                            else:
                                # Already cached
                                with QMutexLocker(self.main_window.prefetch_mutex):
                                    self.main_window.prefetched_media[url]['status'] = 'cached'
                                logger.debug(f"Media already cached, skipping: {processed_url}")

                    except Exception as e:
                        logger.debug(f"Error prefetching media {url}: {e}")
                        with QMutexLocker(self.main_window.prefetch_mutex):
                            if url in self.main_window.prefetched_media:
                                self.main_window.prefetched_media[url]['status'] = 'error'
                        continue

        except Exception as e:
            logger.exception(f"Error in media prefetch worker: {e}")
            self.signals.error.emit(str(e), None)
        finally:
            self.signals.finished.emit("prefetch_complete", None)


class RedMediaBrowser(QMainWindow):
    """
    The main application window for Red Media Browser.
    Handles layout, navigation, and Reddit API integration.
    """

    def __init__(self) -> None:
        super().__init__()

        # Initialize class variables
        self.reddit = None
        self.current_model = None
        self.current_after = None
        self.current_snapshot = []
        self.snapshot_page_size = 10
        self.snapshot_offset = 0
        self.thumbnail_widgets = []
        self.is_loading_posts = False
        self.selected_author = None

        # For back navigation
        self.previous_subreddit = None
        self.previous_offset = 0
        self.back_button = None

        # For moderated subreddits
        self.moderated_subreddits = []
        self.mod_subreddits_fetched = False
        self.prefetched_mod_logs = {}
        self.mod_logs_ready = False
        self.mod_log_fetcher_thread = None # To keep a reference
        self.filter_worker_thread = None # To keep reference to filter worker
        self.ban_worker = None # To keep reference to ban worker
        self.active_workers = [] # Keep track of active workers
        self.workers_mutex = QMutex() # Thread safety for active_workers list

        # Prefetch system for media only (post data already fetched at startup)
        self.prefetch_enabled = True
        self.prefetch_pages_ahead = 1  # Prefetch 1 page ahead
        self.prefetch_pages_behind = 1  # Prefetch 1 page behind
        self.prefetched_media = {}  # url -> prefetch_status
        self.prefetch_workers = []  # Track active prefetch workers
        self.prefetch_mutex = QMutex()  # Thread safety for prefetch data

        # Set up the UI
        self.init_ui()

        # Initialize Reddit API connection
        self.init_reddit()

        # Set up the global thread pool
        QThreadPool.globalInstance().setMaxThreadCount(10)

    def init_ui(self) -> None:
        """Initialize the user interface components."""
        self.setWindowTitle("Red Media Browser")
        self.setMinimumSize(1024, 768)

        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Main layout
        main_layout = QVBoxLayout(central_widget)

        # Navigation panel
        nav_layout = QHBoxLayout()

        # Moderated subreddits dropdown button (moved to far left)
        self.mod_subreddits_button = QPushButton("My Mod Subreddits")
        self.mod_subreddits_button.setToolTip("View and select from subreddits you moderate")
        self.mod_subreddits_button.clicked.connect(self.show_mod_subreddits_menu)
        nav_layout.addWidget(self.mod_subreddits_button)

        # Subreddit/User selector
        self.source_type_combo = QComboBox()
        self.source_type_combo.addItems(["Subreddit", "User"])
        self.source_type_combo.setFixedWidth(100)
        self.source_type_combo.currentIndexChanged.connect(self.on_source_type_changed)
        nav_layout.addWidget(self.source_type_combo)

        # Input field for subreddit/user
        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText("Enter subreddit name...")
        self.source_input.returnPressed.connect(self.load_content)
        nav_layout.addWidget(self.source_input)

        # Navigation buttons
        self.load_button = QPushButton("Load")
        self.load_button.clicked.connect(self.load_content)
        nav_layout.addWidget(self.load_button)

        # Back to subreddit button (initially hidden)
        self.back_button = QPushButton("Back to Subreddit")
        self.back_button.clicked.connect(self.go_back_to_subreddit)
        self.back_button.setVisible(False)  # Initially hidden
        nav_layout.addWidget(self.back_button)

        # Filter by subreddit button (initially hidden)
        self.filter_button = QPushButton("Filter by Subreddit")
        self.filter_button.clicked.connect(self.toggle_subreddit_filter)
        self.filter_button.setVisible(False)  # Initially hidden
        self.is_filtered = False  # Track if we're currently filtering
        nav_layout.addWidget(self.filter_button)

        self.prev_button = QPushButton("Previous Page")
        self.prev_button.clicked.connect(self.show_previous_page)
        self.prev_button.setEnabled(False)
        nav_layout.addWidget(self.prev_button)

        self.next_button = QPushButton("Next Page")
        self.next_button.clicked.connect(self.show_next_page)
        self.next_button.setEnabled(False)
        nav_layout.addWidget(self.next_button)

        # --- Mod-only Buttons ---
        self.view_reports_button = QPushButton("View Reports")
        self.view_reports_button.setToolTip("View posts in the mod reports queue")
        self.view_reports_button.clicked.connect(self.view_reports)
        self.view_reports_button.setVisible(False)
        nav_layout.addWidget(self.view_reports_button)

        self.view_removed_button = QPushButton("View Removed")
        self.view_removed_button.setToolTip("View posts that have been removed")
        self.view_removed_button.clicked.connect(self.view_removed)
        self.view_removed_button.setVisible(False)
        nav_layout.addWidget(self.view_removed_button)

        # --- Fetch Next 500 Button ---
        self.fetch_next_500_button = QPushButton("Fetch Next 500")
        self.fetch_next_500_button.setToolTip("Fetch the next 500 posts and add them to the list")
        self.fetch_next_500_button.clicked.connect(self.fetch_next_500)
        self.fetch_next_500_button.setEnabled(False)
        nav_layout.addWidget(self.fetch_next_500_button)

        main_layout.addLayout(nav_layout)

        # Status displays
        status_layout = QHBoxLayout()

        # Current source label
        self.source_label = QLabel("No content loaded")
        self.source_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        status_layout.addWidget(self.source_label, stretch=1)

        # Moderator status
        self.mod_status_label = QLabel("Not a moderator")
        self.mod_status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_layout.addWidget(self.mod_status_label)

        main_layout.addLayout(status_layout)

        # Loading indicator
        self.loading_bar = QProgressBar()
        self.loading_bar.setTextVisible(False)
        self.loading_bar.setRange(0, 0)  # Indeterminate progress
        self.loading_bar.hide()
        main_layout.addWidget(self.loading_bar)

        # Content area in a scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # Content widget to hold thumbnails in a grid layout
        self.content_widget = QWidget()
        self.content_layout = QGridLayout(self.content_widget)
        self.content_layout.setSpacing(10)
        self.content_layout.setContentsMargins(10, 10, 10, 10)

        # Configure grid layout to enforce equal cell sizes regardless of content
        for col in range(5):
            self.content_layout.setColumnStretch(col, 1)
            self.content_layout.setColumnMinimumWidth(col, 150)

        for row in range(2):
            self.content_layout.setRowStretch(row, 1)
            self.content_layout.setRowMinimumHeight(row, 250)

        self.scroll_area.setWidget(self.content_widget)

        main_layout.addWidget(self.scroll_area, stretch=1)

        # Status bar at bottom
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.mod_log_status_label = QLabel("") # Label for mod log status
        self.statusBar.addPermanentWidget(self.mod_log_status_label)

        # Set initial values
        self.on_source_type_changed(0)  # Default to subreddit mode

    def init_reddit(self) -> None:
        """Initialize Reddit API connection."""
        try:
            # Load config with error handling
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
            if not os.path.exists(config_path):
                QMessageBox.critical(self, "Configuration Error",
                                   f"Configuration file not found: {config_path}\nPlease ensure config.json exists.")
                return

            try:
                config = load_config(config_path)
            except (json.JSONDecodeError, KeyError) as e:
                QMessageBox.critical(self, "Configuration Error",
                                   f"Invalid configuration file: {str(e)}\nPlease check config.json format.")
                return

            # Validate required config keys
            required_keys = ["client_id", "client_secret", "refresh_token", "redirect_uri", "user_agent"]
            missing_keys = [key for key in required_keys if key not in config or not config[key]]
            if missing_keys:
                QMessageBox.critical(self, "Configuration Error",
                                   f"Missing required configuration: {', '.join(missing_keys)}")
                return

            # Set up Reddit instance with timeout
            try:
                self.reddit = praw.Reddit(
                    client_id=config["client_id"],
                    client_secret=config["client_secret"],
                    refresh_token=config["refresh_token"],
                    redirect_uri=config["redirect_uri"],
                    user_agent=config["user_agent"],
                    timeout=30  # Add timeout to prevent hanging
                )
            except Exception as praw_error:
                logger.exception(f"Failed to initialize PRAW Reddit instance: {praw_error}")
                QMessageBox.critical(self, "Reddit API Error",
                                   f"Failed to initialize Reddit API connection: {str(praw_error)}")
                return
            
            # Store VLC path from config (if available)
            self.vlc_path = config.get("vlc_path", "")
            if self.vlc_path and os.path.exists(self.vlc_path):
                logger.info(f"Using custom VLC path from config: {self.vlc_path}")
            else:
                self.vlc_path = ""
                logger.info("Using default VLC paths")

            # Verify credentials work with better error handling
            try:
                user = self.reddit.user.me()
                if user is None:
                    raise Exception("Reddit user authentication returned None")

                username = getattr(user, 'name', None)
                if not username:
                    raise Exception("Unable to retrieve username from Reddit API")

                logger.info(f"Authenticated as {username}")
                self.statusBar.showMessage(f"Authenticated as {username}")

                # Fetch moderated subreddits in background
                self.fetch_moderated_subreddits()

                # Load default subreddit if specified
                if config.get("default_subreddit"):
                    try:
                        self.source_input.setText(config["default_subreddit"])
                        QTimer.singleShot(100, self.load_content)  # Defer to avoid blocking startup
                    except Exception as load_error:
                        logger.warning(f"Failed to load default subreddit: {load_error}")

            except Exception as e:
                error_str = str(e).lower()
                logger.exception(f"Authentication failed: {e}")

                # Handle different types of authentication errors
                if any(phrase in error_str for phrase in ["invalid_grant", "invalid refresh token", "401", "unauthorized"]):
                    self.statusBar.showMessage("Authentication token expired. Please refresh token.")
                    self.handle_invalid_token(config, config_path)
                elif any(phrase in error_str for phrase in ["403", "forbidden", "insufficient scope"]):
                    self.statusBar.showMessage("Insufficient permissions. Please check Reddit app scopes.")
                elif any(phrase in error_str for phrase in ["timeout", "connection", "network"]):
                    self.statusBar.showMessage("Network error. Please check internet connection and try again.")
                else:
                    self.statusBar.showMessage(f"Authentication failed: {str(e)}")

                # Don't proceed with app initialization on auth failure
                return

        except Exception as e:
            logger.exception(f"Error initializing Reddit API: {e}")
            QMessageBox.critical(self, "Error", f"Failed to initialize Reddit API: {str(e)}")

    def fetch_moderated_subreddits(self) -> None:
        """Fetch the list of subreddits moderated by the current user."""
        self.mod_subreddits_button.setEnabled(False)
        self.mod_subreddits_button.setText("Loading Mod Subreddits...")

        # Create a thread to fetch moderated subreddits
        self.mod_subreddits_fetcher = ModeratedSubredditsFetcher(self.reddit)
        self.mod_subreddits_fetcher.subredditsFetched.connect(self.on_mod_subreddits_fetched, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.mod_subreddits_fetcher)  # Track worker for proper cleanup
        self.mod_subreddits_fetcher.start()

    def on_mod_subreddits_fetched(self, mod_subreddits: List[Dict[str, str]]) -> None:
        """Handle the fetched list of moderated subreddits and start mod log fetching."""
        self.moderated_subreddits = mod_subreddits
        self.mod_subreddits_fetched = True

        # Clean up the mod subreddits fetcher
        if hasattr(self, 'mod_subreddits_fetcher'):
            self.cleanup_worker(self.mod_subreddits_fetcher)

        # Update button to show count
        count = len(mod_subreddits)
        self.mod_subreddits_button.setText(f"My Mod Subreddits ({count})")
        self.mod_subreddits_button.setEnabled(True)

        if count == 0:
            self.mod_subreddits_button.setToolTip("You don't moderate any subreddits")
        else:
            self.mod_subreddits_button.setToolTip(f"You moderate {count} subreddits")

        # If user moderates subreddits, start fetching mod logs in the background
        if count > 0 and not self.mod_logs_ready and (self.mod_log_fetcher_thread is None or not self.mod_log_fetcher_thread.isRunning()):
            logger.info("Moderated subreddits found. Starting background mod log fetch.")
            self.mod_log_fetcher_thread = ModLogFetcher(self.reddit, self.moderated_subreddits)
            self.mod_log_fetcher_thread.modLogsReady.connect(self.on_mod_logs_ready, Qt.ConnectionType.QueuedConnection)
            self.mod_log_fetcher_thread.progressUpdate.connect(self.update_mod_log_status, Qt.ConnectionType.QueuedConnection)
            self.add_worker(self.mod_log_fetcher_thread)  # Track worker for proper cleanup
            self.mod_log_fetcher_thread.start()
        elif count == 0:
            self.mod_log_status_label.setText("No mod logs to fetch.")
            self.mod_logs_ready = True # Mark as ready even if empty

    def on_mod_logs_ready(self, prefetched_logs: Dict[str, List[Dict[str, Optional[str]]]]) -> None:
        """Handle the fetched moderator logs."""
        logger.info("Background mod log fetching complete.")
        self.prefetched_mod_logs = prefetched_logs
        self.mod_logs_ready = True
        self.mod_log_status_label.setText("Mod logs loaded.")
        # Optionally hide the progress label after a delay
        QTimer.singleShot(MOD_STATUS_DELAY_MS, self._update_mod_log_status_delayed)

        # Clean up the worker
        if hasattr(self, 'mod_log_fetcher_thread'):
            self.cleanup_worker(self.mod_log_fetcher_thread)

    def _update_mod_log_status_delayed(self) -> None:
        """Update mod log status after delay, avoiding lambda circular reference."""
        if self.mod_logs_ready:
            self.mod_log_status_label.setText("Mod logs loaded.")

    def update_mod_log_status(self, status_message: str) -> None:
        """Update the status bar with mod log fetching progress."""
        self.mod_log_status_label.setText(status_message)

    def show_mod_subreddits_menu(self) -> None:
        """Show a dropdown menu of moderated subreddits."""
        if not self.mod_subreddits_fetched:
            self.fetch_moderated_subreddits()
            return

        if not self.moderated_subreddits:
            self.statusBar.showMessage("You don't moderate any subreddits.")
            return

        # Create a menu of moderated subreddits
        menu = QMenu(self)

        # Add a header/title (as a disabled action)
        title_action = QAction("Select a Subreddit:", self)
        title_action.setEnabled(False)
        menu.addAction(title_action)
        menu.addSeparator()

        # Add each moderated subreddit
        for subreddit in self.moderated_subreddits:
            display_name = subreddit["display_name"]
            subscribers = subreddit.get("subscribers", 0)

            # Format menu item text with subscriber count if available
            if subscribers:
                text = f"{display_name} ({subscribers:,} subscribers)"
            else:
                text = display_name

            action = QAction(text, self)
            action.setData(display_name)  # Store the subreddit name as data
            menu.addAction(action)

        # Show the menu below the button
        action = menu.exec(self.mod_subreddits_button.mapToGlobal(
            self.mod_subreddits_button.rect().bottomLeft()))

        # Handle menu selection
        if action and action.isEnabled():
            subreddit_name = action.data()
            if subreddit_name:
                # Set the input field and load the subreddit
                self.source_type_combo.setCurrentIndex(0)  # Switch to Subreddit mode
                self.source_input.setText(subreddit_name)
                self.load_content()

    def handle_invalid_token(self, config: Dict[str, str], config_path: str) -> None:
        """Handle invalid refresh token by requesting a new one."""
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText("Your Reddit authentication token has expired.")
        msg.setInformativeText("Would you like to request a new authentication token?")
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        ret = msg.exec()

        if ret == QMessageBox.StandardButton.Yes:
            # Create a temporary Reddit instance without the refresh token
            temp_reddit = praw.Reddit(
                client_id=config["client_id"],
                client_secret=config["client_secret"],
                redirect_uri=config["redirect_uri"],
                user_agent=config["user_agent"]
            )

            # Request scopes needed for the application
            requested_scopes = ['identity', 'read', 'mysubreddits', 'history']

            # Ask user about moderation scopes via GUI dialog
            mod_scopes_msg = QMessageBox()
            mod_scopes_msg.setIcon(QMessageBox.Icon.Question)
            mod_scopes_msg.setWindowTitle("Moderation Scopes")
            mod_scopes_msg.setText("Do you want to request moderation scopes as well?")
            mod_scopes_msg.setInformativeText("This allows the application to perform moderation actions like banning users and viewing mod logs.")
            mod_scopes_msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            mod_scopes_ret = mod_scopes_msg.exec()

            if mod_scopes_ret == QMessageBox.StandardButton.Yes:
                requested_scopes.extend(['modcontributors', 'modconfig', 'modflair', 'modlog', 'modposts', 'modwiki'])

            # Get a new refresh token
            new_token = get_new_refresh_token(temp_reddit, requested_scopes)
            if new_token:
                update_config_with_new_token(config, config_path, new_token)
                QMessageBox.information(self, "Success", "Authentication successful. Please restart the application.")
                self.close()
            else:
                QMessageBox.critical(self, "Error", "Failed to obtain a new authentication token.")

    def on_source_type_changed(self, index: int) -> None:
        """Handle change between subreddit and user mode."""
        if index == 0:  # Subreddit mode
            self.source_input.setPlaceholderText("Enter subreddit name...")
            # Only show mod subreddits button in subreddit mode
            self.mod_subreddits_button.setVisible(True)
        else:  # User mode
            self.source_input.setPlaceholderText("Enter username...")
            # Hide mod subreddits button in user mode
            self.mod_subreddits_button.setVisible(False)

    def load_content(self) -> None:
        """Load content from the specified subreddit or user."""
        source = self.source_input.text().strip()
        if not source:
            self.statusBar.showMessage("Please enter a subreddit name or username.")
            return

        # Show loading indicator
        self.loading_bar.show()
        self.is_loading_posts = True
        self.load_button.setEnabled(False)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)

        # Clear any previous content
        self.clear_content()

        # Determine if we're in subreddit or user mode
        is_user_mode = self.source_type_combo.currentIndex() == 1
        source_type = "User" if is_user_mode else "Subreddit"

        # Hide the back button ONLY when manually loading new content that's not author navigation
        if not hasattr(self, 'is_author_navigation') or not self.is_author_navigation:
            self.back_button.setVisible(False)
            self.previous_subreddit = None
            self.previous_offset = 0

        was_author_navigation = False
        if hasattr(self, 'is_author_navigation') and self.is_author_navigation:
            was_author_navigation = True
        self.is_author_navigation = False # Reset flag after checking

        # Update the status label
        self.source_label.setText(f"Loading {source_type}: {source}")

        # Create a new gallery model, passing the pre-fetched logs and status
        self.current_model = RedditGalleryModel(
            source,
            is_user_mode=is_user_mode,
            reddit_instance=self.reddit,
            prefetched_mod_logs=self.prefetched_mod_logs,
            mod_logs_ready=self.mod_logs_ready
        )

        # Check if user is a moderator (only in subreddit mode)
        if not is_user_mode:
            is_mod = self.current_model.check_user_moderation_status()
            mod_status = "Moderator" if is_mod else "Not a moderator"
            self.mod_status_label.setText(mod_status)
        else:
            self.mod_status_label.setText("User mode")

            if was_author_navigation and self.previous_subreddit:
                logger.debug(f"Ensuring back button remains visible for r/{self.previous_subreddit}")
                self.back_button.setText(f"Back to r/{self.previous_subreddit}")
                self.back_button.setVisible(True)

        # Cleanup is handled by clear_content() called within display_current_page()
        # self.stop_all_thumbnail_media() # Removed redundant call

        # Start a thread to fetch the snapshot
        self.fetch_snapshot()

    def fetch_snapshot(self) -> None:
        """Fetch a snapshot of submissions asynchronously."""
        if not self.current_model:
            return

        # Clean up any existing snapshot fetcher before starting new one
        if hasattr(self, 'snapshot_fetcher') and self.snapshot_fetcher is not None:
            if self.snapshot_fetcher.isRunning():
                logger.debug("Terminating previous snapshot fetcher before starting new one")
                self.snapshot_fetcher.terminate()
                if not self.snapshot_fetcher.wait(THREAD_TERMINATION_TIMEOUT_MS):
                    logger.warning("Previous snapshot fetcher did not terminate gracefully")
            self.cleanup_worker(self.snapshot_fetcher)

        # Create a thread to fetch the snapshot
        self.snapshot_fetcher = SnapshotFetcher(self.current_model)
        self.snapshot_fetcher.snapshotFetched.connect(self.on_snapshot_fetched, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.snapshot_fetcher)  # Track worker for proper cleanup
        self.snapshot_fetcher.start()

    def on_snapshot_fetched(self, snapshot: List[Any]) -> None:
        """Handle the fetched snapshot of submissions."""
        self.current_snapshot = snapshot
        self.snapshot_offset = 0

        # Clean up the snapshot fetcher
        if hasattr(self, 'snapshot_fetcher'):
            self.cleanup_worker(self.snapshot_fetcher)

        # Reset pagination controls
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(len(self.current_snapshot) >= self.snapshot_page_size)

        # Enable/disable mod-only buttons and "Fetch Next 500"
        is_mod = False
        if self.current_model and not self.current_model.is_user_mode:
            is_mod = self.current_model.check_user_moderation_status()
        self.view_reports_button.setVisible(is_mod)
        self.view_removed_button.setVisible(is_mod)
        self.fetch_next_500_button.setEnabled(len(self.current_snapshot) > 0)

        # Update status
        self.is_loading_posts = False
        self.load_button.setEnabled(True)
        self.loading_bar.hide()

        source_type = "User" if self.current_model.is_user_mode else "Subreddit"
        source = self.source_input.text().strip()
        self.source_label.setText(f"{source_type}: {source} - {len(self.current_snapshot)} posts")

        # Show the first page
        self.display_current_page()

    def display_current_page(self) -> None:
        """Display the current page of submissions in a 5x2 grid."""
        # Clear any existing content
        self.clear_content()

        # Get the current page of submissions
        start = self.snapshot_offset
        end = min(start + self.snapshot_page_size, len(self.current_snapshot))
        current_page = self.current_snapshot[start:end]

        if not current_page:
            label = QLabel("No posts found")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.content_layout.addWidget(label, 0, 0, 1, 5)  # span across all columns
            return

        # Create thumbnail widgets for each submission and place them in the grid
        for i, submission in enumerate(current_page):
            try:
                row = i // 5
                col = i % 5
                self.add_submission_widget(submission, row, col)
            except Exception as e:
                # Log the error but try to continue displaying other posts
                submission_id_str = getattr(submission, 'id', 'unknown ID')
                logger.exception(f"Error creating widget for submission {submission_id_str}: {e}")
                try:
                    # Attempt to add an error placeholder
                    error_widget = QLabel(f"Error loading post:\n{submission_id_str}")
                    error_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    error_widget.setStyleSheet("background-color: #ffdddd; border: 1px solid #ffaaaa;")
                    self.content_layout.addWidget(error_widget, row, col)
                    self.thumbnail_widgets.append(error_widget) # Add placeholder to list for cleanup
                except Exception as placeholder_e:
                     logger.error(f"Failed to add error placeholder: {placeholder_e}")


        # Update status
        self.statusBar.showMessage(f"Showing posts {start+1} to {end} of {len(self.current_snapshot)}")

        # Start prefetching media for upcoming pages after a short delay
        # This allows current page media to load first
        # Only prefetch if we're not in the middle of loading posts
        if not self.is_loading_posts:
            QTimer.singleShot(2000, self.start_media_prefetch)  # 2 second delay

        # Clean up old prefetch data periodically
        self.cleanup_prefetch_data()

    def add_submission_widget(self, submission, row=None, col=None):
        """Add a thumbnail widget for a submission."""
        submission_id_str = getattr(submission, 'id', 'unknown ID')
        try:
            # Extract submission info
            title = getattr(submission, 'title', 'No Title')
            permalink = getattr(submission, 'permalink', '')

            # Get subreddit name robustly
            subreddit_name = "unknown"
            subreddit_attr = getattr(submission, 'subreddit', None)
            if isinstance(subreddit_attr, PrawSubreddit):
                subreddit_name = getattr(subreddit_attr, 'display_name', 'unknown')
            elif isinstance(subreddit_attr, str):
                subreddit_name = subreddit_attr
            elif hasattr(subreddit_attr, 'display_name'):
                subreddit_name = getattr(subreddit_attr, 'display_name', 'unknown')
            elif subreddit_attr is not None:
                logger.warning(f"Unexpected type for subreddit attribute: {type(subreddit_attr)} for submission {submission_id_str}")

            # Gallery vs. single image
            is_gallery = getattr(submission, 'is_gallery', False)
            gallery_data = getattr(submission, 'gallery_data', None)
            media_metadata = getattr(submission, 'media_metadata', None)
            has_multiple_images = is_gallery and (gallery_data or media_metadata)

            # Source URL
            source_url = getattr(submission, 'url', '')
            if has_multiple_images:
                source_url = "Gallery post"

            # Get image URLs
            image_urls = extract_image_urls(submission)
            if not image_urls:
                logger.warning(f"No images found for submission ID {submission_id_str}")
                return

            # Moderator check
            can_moderate_this_post = (
                self.current_model and
                hasattr(self.current_model, 'moderated_subreddit_names') and
                subreddit_name.lower() in self.current_model.moderated_subreddit_names
            )

            # Create and add thumbnail widget
            thumbnail = ThumbnailWidget(
                images=image_urls,
                title=title,
                source_url=source_url,
                submission=submission,
                subreddit_name=subreddit_name,
                has_multiple_images=has_multiple_images,
                post_url=permalink,
                is_moderator=can_moderate_this_post,
                reddit_instance=self.reddit,
                vlc_path=self.vlc_path  # Pass the VLC path from config
            )
            thumbnail.authorClicked.connect(self.on_author_clicked)
            if row is not None and col is not None:
                self.content_layout.addWidget(thumbnail, row, col)
            else:
                self.content_layout.addWidget(thumbnail)
            self.thumbnail_widgets.append(thumbnail)

        except Exception as e:
            logger.exception(f"Error adding widget for submission {submission_id_str}: {e}")
            if row is not None and col is not None:
                error_widget = QLabel(f"Error:\n{submission_id_str}")
                error_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
                error_widget.setStyleSheet("background-color: #ffdddd; border: 1px solid #ffaaaa;")
                self.content_layout.addWidget(error_widget, row, col)
                self.thumbnail_widgets.append(error_widget)

    def clear_content(self) -> None:
        """Remove all thumbnail widgets and clean up their resources."""
        if not self.thumbnail_widgets:
            return

        # Create copy to avoid modification during iteration, but keep original list until cleanup is complete
        widgets_to_cleanup = self.thumbnail_widgets[:]
        cleanup_success = True

        for widget in widgets_to_cleanup:
            try:
                # Clean up media if method exists
                cleanup = getattr(widget, "cleanup_current_media", None)
                if callable(cleanup):
                    try:
                        cleanup()
                    except Exception as e:
                        logger.debug(f"Error during media cleanup in clear_content: {e}")
                        cleanup_success = False

                # Remove widget from layout and schedule for deletion
                self.content_layout.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()

            except Exception as e:
                logger.error(f"Error during widget cleanup: {e}")
                cleanup_success = False

        # Only clear the original list after all cleanup attempts are complete
        # This ensures we can retry cleanup if needed and don't lose references prematurely
        self.thumbnail_widgets.clear()

        # Single processEvents call after all widgets are processed
        # This reduces UI blocking while ensuring proper cleanup
        QApplication.processEvents()

        if not cleanup_success:
            logger.warning("Some widget cleanup operations failed, but widgets were removed from UI")

    def stop_all_thumbnail_media(self) -> None:
        """Iterate through all visible thumbnails and stop their media."""
        logger.debug(f"Stopping media for {len(self.thumbnail_widgets)} thumbnail widgets.")
        for widget in self.thumbnail_widgets:
            if isinstance(widget, ThumbnailWidget): # Ensure it's the correct widget type
                try:
                    widget.stop_all_media()
                except Exception as e:
                    logger.error(f"Error stopping media in widget {getattr(widget, 'submission_id', 'N/A')}: {e}")
            # This function is kept in case it's needed elsewhere, but not called before pagination/load

    def show_next_page(self) -> None:
        """Show the next page of submissions."""
        logger.info("=== NEXT BUTTON CLICKED ===")

        # Cleanup is handled by clear_content() called within display_current_page/display_filtered_page
        # self.stop_all_thumbnail_media() # Removed redundant call

        current_list = self.current_filtered_snapshot if self.is_filtered else self.current_snapshot
        if not current_list:
            logger.debug("show_next_page: No current list available")
            return

        next_offset = self.snapshot_offset + self.snapshot_page_size
        logger.info(f"show_next_page: current_offset={self.snapshot_offset}, next_offset={next_offset}, list_length={len(current_list)}")

        if next_offset < len(current_list):
            # Normal pagination within current batch
            logger.info(f"Normal pagination to offset {next_offset}")
            self.snapshot_offset = next_offset
            self.prev_button.setEnabled(self.snapshot_offset > 0)
            # Enable next button if there are more pages OR we're at the last page (for batch fetch)
            self.next_button.setEnabled(self.snapshot_offset + self.snapshot_page_size <= len(current_list))
            if self.is_filtered:
                self.display_filtered_page()
            else:
                self.display_current_page()
        else:
            # Reached end of current batch - fetch next batch automatically
            logger.info(f"Reached end of current batch (offset {next_offset} >= length {len(current_list)}), fetching next batch of posts...")
            self.fetch_next_batch()

    def show_previous_page(self) -> None:
        """Show the previous page of submissions."""
        # Cleanup is handled by clear_content() called within display_current_page/display_filtered_page
        # self.stop_all_thumbnail_media() # Removed redundant call

        current_list = self.current_filtered_snapshot if self.is_filtered else self.current_snapshot
        if not current_list or self.snapshot_offset == 0:
            return

        self.snapshot_offset = max(0, self.snapshot_offset - self.snapshot_page_size)
        self.prev_button.setEnabled(self.snapshot_offset > 0)
        # Enable next button if there are more pages OR we're at the last page (for batch fetch)
        self.next_button.setEnabled(self.snapshot_offset + self.snapshot_page_size <= len(current_list))
        if self.is_filtered:
            self.display_filtered_page()
        else:
            self.display_current_page()

    def on_author_clicked(self, username: str) -> None:
        """Handle clicking on an author's name."""
        if self.is_loading_posts:
            logger.debug("Ignoring author click while content is loading")
            return

        # Stop currently playing media before showing options or navigating?
        # Let's rely on clear_content before the *next* action (view posts or ban) instead.
        # self.stop_all_thumbnail_media() # Removed redundant call

        self.is_author_navigation = True # Flag for load_content

        msg = QMessageBox()
        msg.setWindowTitle("Author Options")
        msg.setText(f"What would you like to do with u/{username}?")
        view_button = msg.addButton("View Posts", QMessageBox.ButtonRole.ActionRole)

        can_ban = False
        ban_subreddit_name = None
        ban_subreddit_obj = None

        # Determine ban context using helper method
        can_ban, ban_subreddit_name, ban_subreddit_obj = self._determine_ban_context()

        ban_button = None
        if can_ban and ban_subreddit_obj:
            ban_button = msg.addButton("Ban User", QMessageBox.ButtonRole.DestructiveRole)

        cancel_button = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        clicked_button = msg.clickedButton()

        if clicked_button == view_button:
            snapshot_fetcher = getattr(self, 'snapshot_fetcher', None)
            if snapshot_fetcher is not None and snapshot_fetcher.isRunning():
                logger.debug("Terminating previous snapshot fetcher...")
                try:
                    # Attempt graceful disconnect of signals first
                    snapshot_fetcher.snapshotFetched.disconnect()
                except (TypeError, RuntimeError):
                    pass  # Signal may not be connected or object may be deleted

                snapshot_fetcher.terminate()
                if not snapshot_fetcher.wait(THREAD_TERMINATION_TIMEOUT_MS):  # Wait for graceful termination
                    logger.warning("Snapshot fetcher did not terminate gracefully")

                # Clear reference to prevent issues
                self.snapshot_fetcher = None

            if self.current_model and not self.current_model.is_user_mode:
                self.previous_subreddit = self.current_model.source_name
                self.previous_offset = self.snapshot_offset
                logger.debug(f"Saved previous subreddit: {self.previous_subreddit}")
                self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
                self.filter_button.setStyleSheet("")
                self.filter_button.setVisible(True)
                self.is_filtered = False

            self.source_type_combo.setCurrentIndex(1)
            self.source_input.setText(username)

            if self.previous_subreddit:
                self.back_button.setText(f"Back to r/{self.previous_subreddit}")
                self.back_button.setVisible(True)

            QTimer.singleShot(UI_UPDATE_DELAY_MS, self.load_content)

        elif clicked_button == ban_button and ban_subreddit_obj:
            self.open_ban_dialog(username, ban_subreddit_obj) # Pass object directly
            self.is_author_navigation = False # Reset flag after action

        else: # Cancelled or other button
             self.is_author_navigation = False # Reset flag

    def _determine_ban_context(self) -> tuple:
        """
        Determine if the current user can ban and in which subreddit.

        Returns:
            tuple: (can_ban: bool, subreddit_name: str, subreddit_obj)
        """
        if not self.current_model:
            return False, None, None

        # Case 1: Subreddit mode with moderator privileges
        if self._is_subreddit_moderator_mode():
            return self._handle_subreddit_moderator_context()

        # Case 2: User mode with filtering active
        elif self._is_filtered_user_mode():
            return self._handle_filtered_user_context()

        # Case 3: No ban permissions
        return False, None, None

    def _is_subreddit_moderator_mode(self) -> bool:
        """Check if current mode is subreddit mode with moderator privileges."""
        return (hasattr(self.current_model, 'is_user_mode') and
                hasattr(self.current_model, 'is_moderator') and
                not self.current_model.is_user_mode and
                self.current_model.is_moderator)

    def _is_filtered_user_mode(self) -> bool:
        """Check if current mode is filtered user mode."""
        return (hasattr(self.current_model, 'is_user_mode') and
                self.current_model.is_user_mode and
                self.is_filtered and
                self.previous_subreddit)

    def _handle_subreddit_moderator_context(self) -> tuple:
        """Handle ban context for subreddit moderator mode."""
        if (hasattr(self.current_model, 'source_name') and
            hasattr(self.current_model, 'subreddit')):
            return True, self.current_model.source_name, self.current_model.subreddit
        else:
            logger.warning("Current model missing required attributes for banning")
            return False, None, None

    def _handle_filtered_user_context(self) -> tuple:
        """Handle ban context for filtered user mode."""
        moderated_subreddit_names = getattr(self.current_model, 'moderated_subreddit_names', set())
        if self.previous_subreddit.lower() in moderated_subreddit_names:
            try:
                ban_subreddit_obj = self.reddit.subreddit(self.previous_subreddit)
                return True, self.previous_subreddit, ban_subreddit_obj
            except Exception as e:
                logger.exception(f"Error getting subreddit object {self.previous_subreddit}: {e}")
                return False, None, None
        return False, None, None

    def go_back_to_subreddit(self) -> None:
        """Navigate back to the previously viewed subreddit."""
        if not self.previous_subreddit: return

        # Cleanup is handled by clear_content() called within display_current_page()
        # self.stop_all_thumbnail_media() # Removed redundant call

        target_offset = self.previous_offset
        subreddit_name = self.previous_subreddit

        self.loading_bar.show()
        self.is_loading_posts = True
        self.load_button.setEnabled(False); self.prev_button.setEnabled(False); self.next_button.setEnabled(False)
        self.clear_content()
        self.back_button.setVisible(False); self.filter_button.setVisible(False)
        self.is_filtered = False

        self.source_type_combo.setCurrentIndex(0)
        self.source_input.setText(subreddit_name)

        self.current_model = RedditGalleryModel(
            subreddit_name, is_user_mode=False, reddit_instance=self.reddit,
            prefetched_mod_logs=self.prefetched_mod_logs, mod_logs_ready=self.mod_logs_ready
        )
        is_mod = self.current_model.check_user_moderation_status()
        self.mod_status_label.setText("Moderator" if is_mod else "Not a moderator")
        self.source_label.setText(f"Loading Subreddit: {subreddit_name}")

        def on_snapshot_fetched_with_restore(snapshot):
            # Clean up the snapshot fetcher
            if hasattr(self, 'snapshot_fetcher'):
                self.cleanup_worker(self.snapshot_fetcher)

            # Check if snapshot fetch was successful
            if snapshot is None or (isinstance(snapshot, list) and len(snapshot) == 0):
                # Empty snapshot might indicate an error, call error handler
                logger.warning(f"Empty snapshot returned for subreddit {subreddit_name}, treating as error")
                on_snapshot_fetch_error()
                return

            self.current_snapshot = snapshot
            self.snapshot_offset = target_offset # Restore offset
            self.is_loading_posts = False
            self.load_button.setEnabled(True); self.loading_bar.hide()
            self.prev_button.setEnabled(self.snapshot_offset > 0)
            self.next_button.setEnabled(self.snapshot_offset + self.snapshot_page_size <= len(snapshot))
            self.source_label.setText(f"Subreddit: {subreddit_name} - {len(snapshot)} posts")
            self.display_current_page() # Will use the restored offset
            try:
                self.snapshot_fetcher.snapshotFetched.disconnect(on_snapshot_fetched_with_restore)
            except (TypeError, RuntimeError):
                # Signal may not be connected or object may be deleted
                pass

        def on_snapshot_fetch_error():
            """Handle errors during snapshot fetching."""
            logger.error(f"Failed to fetch snapshot for subreddit: {subreddit_name}")
            self.is_loading_posts = False
            self.loading_bar.hide()
            self.load_button.setEnabled(True)
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.source_label.setText(f"Failed to load subreddit: {subreddit_name}")
            self.statusBar.showMessage("Failed to load subreddit posts")

        try:
            self.snapshot_fetcher = SnapshotFetcher(self.current_model)
            self.snapshot_fetcher.snapshotFetched.connect(on_snapshot_fetched_with_restore, Qt.ConnectionType.QueuedConnection)
            self.add_worker(self.snapshot_fetcher)  # Track worker for proper cleanup
            self.snapshot_fetcher.start()
        except Exception as e:
            logger.exception(f"Error starting snapshot fetcher for {subreddit_name}: {e}")
            on_snapshot_fetch_error()

    def open_ban_dialog(self, username, subreddit_obj): # Accept subreddit object
        """Open the ban user dialog for moderators."""
        # No need for the checks here as they are done in on_author_clicked
        subreddit_name = subreddit_obj.display_name # Get name from object
        dialog = BanUserDialog(username, subreddit_name, self)
        if dialog.exec():
            # Stop media again just before initiating the ban action?
            # Let's rely on the ThumbnailWidget's stop_all_media called internally by the ban worker trigger if needed.
            # The main clear_content will handle cleanup if navigation happens later.
            # self.stop_all_thumbnail_media() # Removed redundant call

            reason = dialog.reason
            share = dialog.result_type == "share"
            ban_message = reason if share else None

            # Use BanWorker instead of direct call
            logger.info(f"Starting BanWorker for user {username} in r/{subreddit_name}")
            # Ensure reddit instance is passed if needed by worker (it wasn't in previous version, adding defensively)
            self.ban_worker = BanWorker(subreddit_obj, username, reason, ban_message, self.reddit)
            self.ban_worker.signals.success.connect(self.on_ban_success, Qt.ConnectionType.QueuedConnection)
            self.ban_worker.signals.error.connect(self.on_ban_error, Qt.ConnectionType.QueuedConnection)
            self.ban_worker.signals.finished.connect(self.on_worker_finished, Qt.ConnectionType.QueuedConnection) # Generic finished handler
            self.add_worker(self.ban_worker) # Track worker
            self.ban_worker.start()

    def closeEvent(self, event) -> None:
        """Handle application shutdown."""
        QThreadPool.globalInstance().clear()

        # Safely terminate all workers with timeouts
        workers_to_terminate = [
            ('snapshot_fetcher', getattr(self, 'snapshot_fetcher', None)),
            ('mod_log_fetcher_thread', getattr(self, 'mod_log_fetcher_thread', None)),
            ('filter_worker_thread', getattr(self, 'filter_worker_thread', None)),
            ('ban_worker', getattr(self, 'ban_worker', None)),
            ('next_500_fetcher', getattr(self, 'next_500_fetcher', None)),
            ('reports_fetcher', getattr(self, 'reports_fetcher', None)),
            ('removed_fetcher', getattr(self, 'removed_fetcher', None))
        ]

        # First terminate known workers
        for name, worker in workers_to_terminate:
            if worker is not None and worker.isRunning():
                logger.debug(f"Terminating {name}")
                worker.terminate()
                if not worker.wait(THREAD_TERMINATION_TIMEOUT_MS):
                    logger.warning(f"Worker {name} did not terminate gracefully within timeout")

        # Terminate any other active workers cleanly
        active_workers_copy = self.get_active_workers_copy()  # Thread-safe copy
        for worker in active_workers_copy:
            if worker is not None and worker.isRunning():
                worker_name = type(worker).__name__
                logger.debug(f"Terminating active worker: {worker_name}")
                worker.terminate()
                if not worker.wait(THREAD_TERMINATION_TIMEOUT_MS):
                    logger.warning(f"Active worker {worker_name} did not terminate gracefully within timeout")

        event.accept()

    def toggle_subreddit_filter(self) -> None:
        """Toggle filtering user posts by the previously viewed subreddit."""
        if not self.previous_subreddit or not self.current_model or not self.current_model.is_user_mode:
            self.filter_button.setVisible(False)
            return

        self.loading_bar.show(); self.is_loading_posts = True
        self.filter_button.setEnabled(False); self.load_button.setEnabled(False)
        self.prev_button.setEnabled(False); self.next_button.setEnabled(False)

        QTimer.singleShot(UI_UPDATE_DELAY_MS, self._perform_filtering)

    def _perform_filtering(self) -> None:
        """Perform the actual filtering operation after UI updates."""
        # Store original state before toggling for proper error recovery
        original_filtered_state = self.is_filtered

        try:
            self.is_filtered = not self.is_filtered
            username = self.source_input.text().strip()

            if self.is_filtered:
                self.filter_button.setText(f"Remove Filter")
                self.filter_button.setStyleSheet("background-color: #ffc107; color: black;")
                self.source_label.setText(f"Filtering posts by r/{self.previous_subreddit}...")
                QApplication.processEvents() # Allow label update

                # Start background filtering
                self.filter_worker_thread = FilterWorker(self.current_snapshot, self.previous_subreddit.lower())
                self.filter_worker_thread.filteringComplete.connect(self._on_filtering_complete, Qt.ConnectionType.QueuedConnection)
                self.add_worker(self.filter_worker_thread)  # Track worker for proper cleanup
                self.filter_worker_thread.start()

            else: # Removing filter
                self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
                self.filter_button.setStyleSheet("")
                self.source_label.setText(f"User: {username} - {len(self.current_snapshot)} posts")
                self.snapshot_offset = 0
                self.prev_button.setEnabled(False)
                self.next_button.setEnabled(len(self.current_snapshot) >= self.snapshot_page_size)
                self.display_current_page() # Display original unfiltered content
                # Reset UI state after displaying
                self.is_loading_posts = False
                self.loading_bar.hide()
                self.filter_button.setEnabled(True)
                self.load_button.setEnabled(True)

        except Exception as e:
            logger.exception(f"Error initiating filtering: {e}")
            self.statusBar.showMessage(f"Filtering error: {str(e)}")

            # Reset UI state on error
            self.is_loading_posts = False
            self.loading_bar.hide()
            self.filter_button.setEnabled(True)
            self.load_button.setEnabled(True)

            # Restore original state and corresponding button appearance
            self.is_filtered = original_filtered_state
            if original_filtered_state:
                # Was filtered, restore filtered appearance
                self.filter_button.setText(f"Remove Filter")
                self.filter_button.setStyleSheet("background-color: #ffc107; color: black;")
            else:
                # Was not filtered, restore unfiltered appearance
                self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
                self.filter_button.setStyleSheet("")

    def _on_filtering_complete(self, filtered_snapshot) -> None:
        """Handle completion of the background filtering."""
        try:
            # Clean up the filter worker
            if hasattr(self, 'filter_worker_thread'):
                self.cleanup_worker(self.filter_worker_thread)

            self.current_filtered_snapshot = filtered_snapshot
            username = self.source_input.text().strip()
            self.source_label.setText(f"User: {username} - Filtered by r/{self.previous_subreddit} - {len(filtered_snapshot)} posts")
            self.snapshot_offset = 0
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(len(filtered_snapshot) >= self.snapshot_page_size)
            self.display_filtered_page() # Display the filtered content
        except Exception as e:
             logger.exception(f"Error processing filtered results: {e}")
             self.statusBar.showMessage(f"Error displaying filtered results: {str(e)}")
             # Attempt to revert UI state
             self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
             self.filter_button.setStyleSheet("")
             self.is_filtered = False
             # Only display current page if we have valid snapshot data
             if hasattr(self, 'current_snapshot') and self.current_snapshot:
                 self.display_current_page() # Show original page
             else:
                 logger.warning("Cannot revert to current page - no valid snapshot available")
        finally:
            # Reset UI state regardless of success/failure in processing
            self.is_loading_posts = False
            self.loading_bar.hide()
            self.filter_button.setEnabled(True)
            self.load_button.setEnabled(True)


    def display_filtered_page(self) -> None:
        """Display the current page of filtered submissions."""
        self.clear_content()
        if not hasattr(self, 'current_filtered_snapshot') or not self.current_filtered_snapshot:
            label = QLabel(f"No posts found in r/{self.previous_subreddit}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.content_layout.addWidget(label, 0, 0, 1, 5)
            self.statusBar.showMessage(f"Filtered by r/{self.previous_subreddit}: 0 posts found")
            return

        start = self.snapshot_offset
        end = min(start + self.snapshot_page_size, len(self.current_filtered_snapshot))
        current_page = self.current_filtered_snapshot[start:end]

        if not current_page: # Should not happen if list is not empty, but check anyway
             label = QLabel(f"No posts found on this page in r/{self.previous_subreddit}")
             label.setAlignment(Qt.AlignmentFlag.AlignCenter)
             self.content_layout.addWidget(label, 0, 0, 1, 5)
             self.statusBar.showMessage(f"Filtered by r/{self.previous_subreddit}: Page {start//self.snapshot_page_size + 1} empty")
             return

        for i, submission in enumerate(current_page):
            try:
                row = i // 5
                col = i % 5
                self.add_submission_widget(submission, row, col)
            except Exception as e:
                 submission_id_str = getattr(submission, 'id', 'unknown ID')
                 logger.exception(f"Error displaying filtered submission {submission_id_str}: {e}")
                 error_widget = QLabel(f"Error:\n{submission_id_str}")
                 error_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
                 error_widget.setStyleSheet("background-color: #ffdddd; border: 1px solid #ffaaaa;")
                 self.content_layout.addWidget(error_widget, row, col)
                 self.thumbnail_widgets.append(error_widget)

        self.statusBar.showMessage(f"Showing posts {start+1} to {end} of {len(self.current_filtered_snapshot)} (Filtered by r/{self.previous_subreddit})")

        # Start prefetching media for upcoming filtered pages after a short delay
        # Only prefetch if we're not in the middle of loading posts
        if not self.is_loading_posts:
            QTimer.singleShot(2000, self.start_media_prefetch)

    # --- Generic Worker Finished Handler ---
    def on_worker_finished(self) -> None:
        """Remove finished worker from tracking list."""
        sender = self.sender()
        self.cleanup_worker(sender)  # Use thread-safe cleanup method
        # Specific worker references are cleared in their success/error handlers

    def cleanup_worker(self, worker) -> None:
        """Remove a specific worker from the active workers list."""
        self.workers_mutex.lock()
        try:
            if worker in self.active_workers:
                logger.debug(f"Cleaning up worker: {type(worker).__name__}")
                self.active_workers.remove(worker)
                logger.debug(f"Active workers remaining: {len(self.active_workers)}")
            else:
                logger.debug(f"Worker {type(worker).__name__} not found in active workers list")
        finally:
            self.workers_mutex.unlock()

    def add_worker(self, worker) -> None:
        """Thread-safely add a worker to the active workers list."""
        self.workers_mutex.lock()
        try:
            self.active_workers.append(worker)
            logger.debug(f"Added worker: {type(worker).__name__}, total active: {len(self.active_workers)}")
        finally:
            self.workers_mutex.unlock()

    def get_active_workers_copy(self) -> list:
        """Get a thread-safe copy of active workers list."""
        self.workers_mutex.lock()
        try:
            return self.active_workers[:]
        finally:
            self.workers_mutex.unlock()

    # --- Ban Worker Handlers ---
    def on_ban_success(self, success_message):
        """Handle successful ban signal from worker."""
        logger.info(f"Ban successful: {success_message}")
        self.statusBar.showMessage(f"Ban successful: {success_message}")

        # Clean up the ban worker (note: on_worker_finished will also be called)
        if hasattr(self, 'ban_worker') and self.ban_worker:
            self.cleanup_worker(self.ban_worker)
        self.ban_worker = None # Clear specific worker reference

    def on_ban_error(self, error_message):
        """Handle error signal from ban worker."""
        logger.error(f"Ban failed: {error_message}")
        self.statusBar.showMessage(f"Ban failed: {error_message}")

        # Clean up the ban worker (note: on_worker_finished will also be called)
        if hasattr(self, 'ban_worker') and self.ban_worker:
            self.cleanup_worker(self.ban_worker)
        self.ban_worker = None # Clear specific worker reference

    # --- Fetch Next Batch Logic ---
    def fetch_next_batch(self) -> None:
        """Automatically fetch the next batch of posts when reaching the end."""
        logger.debug("fetch_next_batch called")

        if not self.current_model or not self.current_snapshot:
            logger.debug("fetch_next_batch: No model or snapshot available")
            return

        # Temporarily disable next button to prevent double-clicking
        self.next_button.setEnabled(False)
        self.statusBar.showMessage("Fetching next batch of posts...")

        # Get the fullname of the last post in the current snapshot
        last_post = self.current_snapshot[-1]
        last_fullname = getattr(last_post, "fullname", None)
        if not last_fullname:
            # Try to construct fullname from id
            post_id = getattr(last_post, "id", None)
            if post_id:
                last_fullname = f"t3_{post_id}"

        if not last_fullname:
            logger.error("Cannot fetch next batch: unable to get fullname of last post")
            self.next_button.setEnabled(True)
            return

        logger.info(f"Starting fetch for next batch after post: {last_fullname}")

        # Show loading indicator
        self.loading_bar.show()
        self.is_loading_posts = True

        # Start a thread to fetch the next batch (100 posts)
        self.next_batch_fetcher = SnapshotFetcher(self.current_model, total=100, after=last_fullname)
        self.next_batch_fetcher.snapshotFetched.connect(self.on_next_batch_fetched, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.next_batch_fetcher)  # Track worker for proper cleanup
        self.next_batch_fetcher.start()
        logger.debug("SnapshotFetcher started for next batch")

    # --- Fetch Next 500 Logic ---
    def fetch_next_500(self) -> None:
        """Fetch the next 500 posts after the last currently loaded post."""
        if not self.current_model or not self.current_snapshot:
            return

        # Get the fullname of the last post in the current snapshot
        last_post = self.current_snapshot[-1]
        last_fullname = getattr(last_post, "fullname", None)
        if not last_fullname:
            # Try to construct fullname from id
            post_id = getattr(last_post, "id", None)
            if post_id:
                last_fullname = f"t3_{post_id}"
            else:
                self.statusBar.showMessage("Error: Could not determine the last post's fullname for fetching next 500.")
                return

        # Show loading indicator
        self.loading_bar.show()
        self.is_loading_posts = True
        self.fetch_next_500_button.setEnabled(False)

        # Start a thread to fetch the next 500 posts
        self.next_500_fetcher = SnapshotFetcher(self.current_model, total=POSTS_FETCH_LIMIT, after=last_fullname)
        self.next_500_fetcher.snapshotFetched.connect(self.on_next_500_fetched, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.next_500_fetcher)  # Track worker for proper cleanup
        self.next_500_fetcher.start()

    # --- View Reports Logic ---
    def view_reports(self) -> None:
        """Fetch and display up to 500 posts from the mod reports queue."""
        if not self.current_model or self.current_model.is_user_mode or not hasattr(self.current_model, "subreddit"):
            return

        self.loading_bar.show()
        self.is_loading_posts = True
        self.view_reports_button.setEnabled(False)

        # Clean up any existing reports fetcher
        if hasattr(self, 'reports_fetcher') and self.reports_fetcher is not None:
            if self.reports_fetcher.isRunning():
                self.reports_fetcher.terminate()
                self.reports_fetcher.wait(THREAD_TERMINATION_TIMEOUT_MS)
            self.cleanup_worker(self.reports_fetcher)

        # Create worker thread for fetching reports
        self.reports_fetcher = ReportsFetcher(self.current_model.subreddit)
        self.reports_fetcher.reportsFetched.connect(self.on_reports_fetched, Qt.ConnectionType.QueuedConnection)
        self.reports_fetcher.errorOccurred.connect(self.on_reports_error, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.reports_fetcher)
        self.reports_fetcher.start()

    def on_reports_fetched(self, reports) -> None:
        """Handle successfully fetched reports."""
        # Clean up the reports fetcher
        if hasattr(self, 'reports_fetcher'):
            self.cleanup_worker(self.reports_fetcher)

        self.current_snapshot = reports
        self.snapshot_offset = 0
        self.display_current_page()
        self.statusBar.showMessage(f"Showing up to 500 reported posts")
        self.source_label.setText(f"Subreddit: {self.current_model.source_name} - Reports ({len(reports)})")
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.view_reports_button.setEnabled(True)

    def on_reports_error(self, error_message: str) -> None:
        """Handle reports fetching error."""
        # Clean up the reports fetcher
        if hasattr(self, 'reports_fetcher'):
            self.cleanup_worker(self.reports_fetcher)

        logger.error(f"Failed to fetch reports: {error_message}")
        self.statusBar.showMessage(f"Failed to fetch reports: {error_message}")
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.view_reports_button.setEnabled(True)

    # --- View Removed Logic ---
    def view_removed(self) -> None:
        """Fetch and display up to 500 removed posts (using mod log)."""
        if not self.current_model or self.current_model.is_user_mode or not hasattr(self.current_model, "subreddit"):
            return

        self.loading_bar.show()
        self.is_loading_posts = True
        self.view_removed_button.setEnabled(False)

        # Clean up any existing removed fetcher
        if hasattr(self, 'removed_fetcher') and self.removed_fetcher is not None:
            if self.removed_fetcher.isRunning():
                self.removed_fetcher.terminate()
                self.removed_fetcher.wait(THREAD_TERMINATION_TIMEOUT_MS)
            self.cleanup_worker(self.removed_fetcher)

        # Create worker thread for fetching removed posts
        self.removed_fetcher = RemovedPostsFetcher(self.current_model.subreddit)
        self.removed_fetcher.removedPostsFetched.connect(self.on_removed_fetched, Qt.ConnectionType.QueuedConnection)
        self.removed_fetcher.errorOccurred.connect(self.on_removed_error, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.removed_fetcher)
        self.removed_fetcher.start()

    def on_removed_fetched(self, removed_posts) -> None:
        """Handle successfully fetched removed posts."""
        # Clean up the removed fetcher
        if hasattr(self, 'removed_fetcher'):
            self.cleanup_worker(self.removed_fetcher)

        self.current_snapshot = removed_posts
        self.snapshot_offset = 0
        self.display_current_page()
        self.statusBar.showMessage(f"Showing up to 500 removed posts")
        self.source_label.setText(f"Subreddit: {self.current_model.source_name} - Removed ({len(removed_posts)})")
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.view_removed_button.setEnabled(True)

    def on_removed_error(self, error_message: str) -> None:
        """Handle removed posts fetching error."""
        # Clean up the removed fetcher
        if hasattr(self, 'removed_fetcher'):
            self.cleanup_worker(self.removed_fetcher)

        logger.error(f"Failed to fetch removed posts: {error_message}")
        self.statusBar.showMessage(f"Failed to fetch removed posts: {error_message}")
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.view_removed_button.setEnabled(True)

    def on_next_batch_fetched(self, new_posts) -> None:
        """Handle completion of automatic next batch fetch and navigate to the new page."""
        # Clean up the next_batch_fetcher
        if hasattr(self, 'next_batch_fetcher'):
            self.cleanup_worker(self.next_batch_fetcher)

        # Deduplicate and add new posts (similar to next_500 logic)
        existing_ids = {getattr(post, "id", None) for post in self.current_snapshot
                       if getattr(post, "id", None) is not None}

        unique_new_posts = [post for post in new_posts
                           if getattr(post, "id", None) not in existing_ids]

        # Add new posts to current snapshot
        self.current_snapshot.extend(unique_new_posts)

        # Update after value for future fetches
        if new_posts:
            last_post = new_posts[-1]
            last_fullname = getattr(last_post, "fullname", None)
            if not last_fullname and hasattr(last_post, 'id'):
                last_fullname = f"t3_{last_post.id}"
            if last_fullname:
                self.current_after = last_fullname

        # Hide loading indicator
        self.is_loading_posts = False
        self.loading_bar.hide()

        # Navigate to the first page of the new batch
        if unique_new_posts:
            self.snapshot_offset = len(self.current_snapshot) - len(unique_new_posts)
            # Ensure offset is page-aligned
            self.snapshot_offset = (self.snapshot_offset // self.snapshot_page_size) * self.snapshot_page_size

            # Update buttons and display the new page
            self.prev_button.setEnabled(self.snapshot_offset > 0)
            self.next_button.setEnabled(self.snapshot_offset + self.snapshot_page_size <= len(self.current_snapshot))
            self.display_current_page()

            logger.info(f"Fetched {len(unique_new_posts)} new posts, navigated to page starting at {self.snapshot_offset + 1}")
        else:
            # No new posts available
            self.next_button.setEnabled(False)
            self.statusBar.showMessage("No more posts available")
            logger.info("No new posts fetched - reached end of content")

    def on_next_500_fetched(self, new_posts) -> None:
        """Append the next 500 posts to the current snapshot and update the UI."""
        # Clean up the next_500_fetcher
        if hasattr(self, 'next_500_fetcher'):
            self.cleanup_worker(self.next_500_fetcher)

        # Deduplicate using optimized set operations
        # Build set of existing IDs more efficiently
        existing_ids = {getattr(post, "id", None) for post in self.current_snapshot
                       if getattr(post, "id", None) is not None}

        # Filter new posts using set lookup (O(1) average case)
        unique_new_posts = []
        posts_without_id = 0

        for post in new_posts:
            post_id = getattr(post, "id", None)
            if post_id is not None:
                if post_id not in existing_ids:
                    unique_new_posts.append(post)
                    existing_ids.add(post_id)  # Prevent duplicates within new_posts as well
            else:
                posts_without_id += 1

        # Log summary instead of individual warnings to reduce log spam
        if posts_without_id > 0:
            logger.warning(f"Skipped {posts_without_id} posts without IDs during deduplication")

        if unique_new_posts:
            self.current_snapshot.extend(unique_new_posts)
            self.statusBar.showMessage(f"Fetched {len(unique_new_posts)} new posts. Total: {len(self.current_snapshot)}")
        else:
            self.statusBar.showMessage("No new posts found.")

        # Enable/disable the button depending on whether we got a full batch
        # If fewer posts returned than requested, we've likely reached the end
        self.fetch_next_500_button.setEnabled(len(new_posts) >= POSTS_FETCH_LIMIT)

        # Hide loading indicator
        self.is_loading_posts = False
        self.loading_bar.hide()

        # Update next/prev buttons
        self.next_button.setEnabled(self.snapshot_offset + self.snapshot_page_size <= len(self.current_snapshot))
        self.prev_button.setEnabled(self.snapshot_offset > 0)

        # Update the source label
        source_type = "User" if self.current_model.is_user_mode else "Subreddit"
        source = self.source_input.text().strip()
        self.source_label.setText(f"{source_type}: {source} - {len(self.current_snapshot)} posts")

        # If we're at the end and new posts were added, show the new page
        # Use safe bounds checking to prevent race conditions
        current_length = len(self.current_snapshot)
        if unique_new_posts and current_length > 0 and self.snapshot_offset + self.snapshot_page_size >= current_length - len(unique_new_posts):
            # Calculate new offset safely, ensuring it doesn't go out of bounds
            new_offset = max(0, current_length - self.snapshot_page_size)
            # Ensure offset is within valid range
            if new_offset < current_length:
                self.snapshot_offset = new_offset
                self.display_current_page()
            else:
                logger.warning(f"Calculated offset {new_offset} would exceed snapshot length {current_length}")
        else:
            # Otherwise, just update the status bar
            self.statusBar.showMessage(f"Fetched {len(unique_new_posts)} new posts. Total: {current_length}")

        # Start prefetching after new posts are added
        self.start_media_prefetch()

    def start_media_prefetch(self):
        """Start prefetching media for upcoming and previous pages."""
        if not self.prefetch_enabled or not self.current_snapshot:
            return

        try:
            current_list = self.current_filtered_snapshot if self.is_filtered else self.current_snapshot
            if not current_list:
                return

            submissions_to_prefetch = []

            # Prefetch ahead (next pages)
            for pages_ahead in range(1, self.prefetch_pages_ahead + 1):
                next_offset = self.snapshot_offset + (self.snapshot_page_size * pages_ahead)
                if next_offset < len(current_list):
                    end_offset = min(next_offset + self.snapshot_page_size, len(current_list))
                    submissions_to_prefetch.extend(current_list[next_offset:end_offset])

            # Prefetch behind (previous pages)
            for pages_behind in range(1, self.prefetch_pages_behind + 1):
                prev_offset = self.snapshot_offset - (self.snapshot_page_size * pages_behind)
                if prev_offset >= 0:
                    end_offset = min(prev_offset + self.snapshot_page_size, len(current_list))
                    submissions_to_prefetch.extend(current_list[prev_offset:end_offset])

            # Remove duplicates and filter out submissions already being prefetched
            unique_submissions = []
            seen_ids = set()

            for submission in submissions_to_prefetch:
                if hasattr(submission, 'id') and submission.id not in seen_ids:
                    seen_ids.add(submission.id)
                    unique_submissions.append(submission)

            if unique_submissions:
                logger.info(f"Starting media prefetch for {len(unique_submissions)} submissions from pages ahead/behind")
                worker = MediaPrefetchWorker(self, unique_submissions)
                QThreadPool.globalInstance().start(worker)

                with QMutexLocker(self.prefetch_mutex):
                    self.prefetch_workers.append(worker)

        except Exception as e:
            logger.exception(f"Error starting media prefetch: {e}")

    def cleanup_prefetch_data(self):
        """Clean up old prefetch data to prevent memory bloat."""
        try:
            current_time = time.time()
            cleanup_threshold = 300  # 5 minutes

            with QMutexLocker(self.prefetch_mutex):
                # Clean up old media prefetch entries
                urls_to_remove = []
                for url, data in self.prefetched_media.items():
                    if current_time - data.get('started_at', 0) > cleanup_threshold:
                        urls_to_remove.append(url)

                for url in urls_to_remove:
                    del self.prefetched_media[url]

                if urls_to_remove:
                    logger.debug(f"Cleaned up {len(urls_to_remove)} old prefetch entries")

        except Exception as e:
            logger.exception(f"Error cleaning up prefetch data: {e}")

# Main application entry point
if __name__ == "__main__":
    # Create and initialize cache directories
    cache_dir = get_cache_dir()
    logger.info(f"Using cache directory: {cache_dir}")

    # Preload file cache for fast existence checks
    from utils import preload_file_cache, repair_cache_index
    preload_file_cache()
    repair_cache_index()

    # Start the PyQt application
    app = QApplication(sys.argv)
    app.setApplicationName("Red Media Browser")
    main_window = RedMediaBrowser()
    main_window.show()
    sys.exit(app.exec())
