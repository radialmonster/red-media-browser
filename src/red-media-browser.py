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
import webbrowser
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
from PyQt6.QtCore import Qt, QSize, QThreadPool, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QAction, QIcon, QPixmapCache

from red_config import load_config, get_new_refresh_token, update_config_with_new_token
# Import specific workers and functions
from reddit_api import RedditGalleryModel, SnapshotFetcher, ModeratedSubredditsFetcher, BanWorker, ban_user as sync_ban_user
from ui_components import ThumbnailWidget, BanUserDialog
from utils import get_cache_dir, ensure_directory, extract_image_urls
from media_handlers import process_media_url
import reddit_api # Import the module itself for accessing moderation_statuses

# Configure logging
logging.basicConfig(
    level=logging.DEBUG, # Changed to DEBUG for more detailed startup info
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Set QPixmapCache size to 100MB to cache more thumbnails
QPixmapCache.setCacheLimit(100 * 1024)

# --- Background Thread for Mod Log Fetching ---
class ModLogFetcher(QThread):
    """
    Worker thread for asynchronous fetching of moderator logs.
    """
    modLogsReady = pyqtSignal(dict)
    progressUpdate = pyqtSignal(str)

    def __init__(self, reddit_instance, moderated_subreddits):
        super().__init__()
        self.reddit_instance = reddit_instance
        self.moderated_subreddits = moderated_subreddits
        self.prefetched_mod_logs = {}

    def run(self):
        total_subs = len(self.moderated_subreddits)
        logger.info(f"Starting background fetch for mod logs of {total_subs} subreddits...")
        self.progressUpdate.emit(f"Fetching mod logs (0/{total_subs})...")

        for i, subreddit_info in enumerate(self.moderated_subreddits):
            sub_name = subreddit_info['name']
            display_name = subreddit_info['display_name']
            logger.debug(f"Fetching mod log for r/{display_name} ({i+1}/{total_subs})")
            try:
                log_entries = list(self.reddit_instance.subreddit(sub_name).mod.log(action="removelink", limit=1000))
                # Store only necessary info efficiently
                self.prefetched_mod_logs[sub_name] = [
                    {'author': entry.target_author.lower() if entry.target_author else None,
                     'fullname': entry.target_fullname}
                    for entry in log_entries if entry.target_fullname and entry.target_fullname.startswith('t3_')
                ]
                logger.debug(f"Fetched {len(log_entries)} log entries for r/{display_name}, stored {len(self.prefetched_mod_logs[sub_name])} relevant entries.")
            except Exception as e:
                logger.error(f"Error fetching mod log for r/{display_name}: {e}")
                self.prefetched_mod_logs[sub_name] = [] # Store empty list on error

            # Update progress
            self.progressUpdate.emit(f"Fetching mod logs ({i+1}/{total_subs})...")

        logger.info(f"Finished fetching mod logs for {total_subs} subreddits.")
        self.modLogsReady.emit(self.prefetched_mod_logs)

# --- Background Thread for Filtering ---
class FilterWorker(QThread):
    """
    Worker thread for filtering posts by subreddit.
    """
    filteringComplete = pyqtSignal(list)

    def __init__(self, snapshot, subreddit_name_lower):
        super().__init__()
        self.snapshot = snapshot
        self.subreddit_name_lower = subreddit_name_lower

    def run(self):
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
                      subreddit_name = subreddit_attr.display_name

                 if subreddit_name.lower() == self.subreddit_name_lower:
                     filtered_snapshot.append(post)

            logger.debug(f"FilterWorker finished. Found {len(filtered_snapshot)} posts.")
        except Exception as e:
            logger.exception(f"Error during background filtering: {e}")
            filtered_snapshot = []
        finally:
            self.filteringComplete.emit(filtered_snapshot)


class RedMediaBrowser(QMainWindow):
    """
    The main application window for Red Media Browser.
    Handles layout, navigation, and Reddit API integration.
    """

    def __init__(self):
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

        # Set up the UI
        self.init_ui()

        # Initialize Reddit API connection
        self.init_reddit()

        # Set up the global thread pool
        QThreadPool.globalInstance().setMaxThreadCount(10)

    def init_ui(self):
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

    def init_reddit(self):
        """Initialize Reddit API connection."""
        try:
            # Load config
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
            config = load_config(config_path)

            # Set up Reddit instance
            self.reddit = praw.Reddit(
                client_id=config["client_id"],
                client_secret=config["client_secret"],
                refresh_token=config["refresh_token"],
                redirect_uri=config["redirect_uri"],
                user_agent=config["user_agent"]
            )
            
            # Store VLC path from config (if available)
            self.vlc_path = config.get("vlc_path", "")
            if self.vlc_path and os.path.exists(self.vlc_path):
                logger.info(f"Using custom VLC path from config: {self.vlc_path}")
            else:
                self.vlc_path = ""
                logger.info("Using default VLC paths")

            # Verify credentials work
            try:
                username = self.reddit.user.me().name
                logger.info(f"Authenticated as {username}")
                self.statusBar.showMessage(f"Authenticated as {username}")

                # Fetch moderated subreddits
                self.fetch_moderated_subreddits()

                # Load default subreddit if specified
                if "default_subreddit" in config and config["default_subreddit"]:
                    self.source_input.setText(config["default_subreddit"])
                    self.load_content()
            except Exception as e:
                logger.error(f"Authentication failed: {e}")
                self.statusBar.showMessage("Authentication failed. Please check your credentials.")

                # Try to get a new token if refresh token is invalid
                if "invalid_grant" in str(e).lower():
                    self.handle_invalid_token(config, config_path)

        except Exception as e:
            logger.error(f"Error initializing Reddit API: {e}")
            QMessageBox.critical(self, "Error", f"Failed to initialize Reddit API: {str(e)}")

    def fetch_moderated_subreddits(self):
        """Fetch the list of subreddits moderated by the current user."""
        self.mod_subreddits_button.setEnabled(False)
        self.mod_subreddits_button.setText("Loading Mod Subreddits...")

        # Create a thread to fetch moderated subreddits
        self.mod_subreddits_fetcher = ModeratedSubredditsFetcher(self.reddit)
        self.mod_subreddits_fetcher.subredditsFetched.connect(self.on_mod_subreddits_fetched)
        self.mod_subreddits_fetcher.start()

    def on_mod_subreddits_fetched(self, mod_subreddits):
        """Handle the fetched list of moderated subreddits and start mod log fetching."""
        self.moderated_subreddits = mod_subreddits
        self.mod_subreddits_fetched = True

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
            self.mod_log_fetcher_thread.modLogsReady.connect(self.on_mod_logs_ready)
            self.mod_log_fetcher_thread.progressUpdate.connect(self.update_mod_log_status)
            self.mod_log_fetcher_thread.start()
        elif count == 0:
            self.mod_log_status_label.setText("No mod logs to fetch.")
            self.mod_logs_ready = True # Mark as ready even if empty

    def on_mod_logs_ready(self, prefetched_logs):
        """Handle the fetched moderator logs."""
        logger.info("Background mod log fetching complete.")
        self.prefetched_mod_logs = prefetched_logs
        self.mod_logs_ready = True
        self.mod_log_status_label.setText("Mod logs loaded.")
        # Optionally hide the progress label after a delay
        QTimer.singleShot(5000, lambda: self.mod_log_status_label.setText("Mod logs loaded.") if self.mod_logs_ready else None)


    def update_mod_log_status(self, status_message):
        """Update the status bar with mod log fetching progress."""
        self.mod_log_status_label.setText(status_message)

    def show_mod_subreddits_menu(self):
        """Show a dropdown menu of moderated subreddits."""
        if not self.mod_subreddits_fetched:
            self.fetch_moderated_subreddits()
            return

        if not self.moderated_subreddits:
            QMessageBox.information(self, "No Moderated Subreddits",
                                   "You don't moderate any subreddits.")
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

    def handle_invalid_token(self, config, config_path):
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
            if input("Request moderation scopes as well? (y/n): ").lower() == 'y':
                requested_scopes.extend(['modcontributors', 'modconfig', 'modflair', 'modlog', 'modposts', 'modwiki'])

            # Get a new refresh token
            new_token = get_new_refresh_token(temp_reddit, requested_scopes)
            if new_token:
                update_config_with_new_token(config, config_path, new_token)
                QMessageBox.information(self, "Success", "Authentication successful. Please restart the application.")
                self.close()
            else:
                QMessageBox.critical(self, "Error", "Failed to obtain a new authentication token.")

    def on_source_type_changed(self, index):
        """Handle change between subreddit and user mode."""
        if index == 0:  # Subreddit mode
            self.source_input.setPlaceholderText("Enter subreddit name...")
            # Only show mod subreddits button in subreddit mode
            self.mod_subreddits_button.setVisible(True)
        else:  # User mode
            self.source_input.setPlaceholderText("Enter username...")
            # Hide mod subreddits button in user mode
            self.mod_subreddits_button.setVisible(False)

    def load_content(self):
        """Load content from the specified subreddit or user."""
        source = self.source_input.text().strip()
        if not source:
            QMessageBox.warning(self, "Warning", "Please enter a subreddit name or username.")
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

    def fetch_snapshot(self):
        """Fetch a snapshot of submissions asynchronously."""
        if not self.current_model:
            return

        # Create a thread to fetch the snapshot
        self.snapshot_fetcher = SnapshotFetcher(self.current_model)
        self.snapshot_fetcher.snapshotFetched.connect(self.on_snapshot_fetched)
        self.snapshot_fetcher.start()

    def on_snapshot_fetched(self, snapshot):
        """Handle the fetched snapshot of submissions."""
        self.current_snapshot = snapshot
        self.snapshot_offset = 0

        # Reset pagination controls
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(len(self.current_snapshot) > self.snapshot_page_size)

        # Update status
        self.is_loading_posts = False
        self.load_button.setEnabled(True)
        self.loading_bar.hide()

        source_type = "User" if self.current_model.is_user_mode else "Subreddit"
        source = self.source_input.text().strip()
        self.source_label.setText(f"{source_type}: {source} - {len(self.current_snapshot)} posts")

        # Show the first page
        self.display_current_page()

    def display_current_page(self):
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

    def clear_content(self):
        """Remove all thumbnail widgets and clean up their resources."""
        for widget in self.thumbnail_widgets:
            # Clean up media if method exists
            cleanup = getattr(widget, "cleanup_current_media", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception as e:
                    logger.debug(f"Error during media cleanup in clear_content: {e}")
            # Remove widget from layout and schedule for deletion
            self.content_layout.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
            # Process events after each widget deletion to force cleanup
            QApplication.processEvents()
        self.thumbnail_widgets.clear()
        # Final processEvents after loop might still be beneficial
        QApplication.processEvents()

    def stop_all_thumbnail_media(self):
        """Iterate through all visible thumbnails and stop their media."""
        logger.debug(f"Stopping media for {len(self.thumbnail_widgets)} thumbnail widgets.")
        for widget in self.thumbnail_widgets:
            if isinstance(widget, ThumbnailWidget): # Ensure it's the correct widget type
                try:
                    widget.stop_all_media()
                except Exception as e:
                    logger.error(f"Error stopping media in widget {getattr(widget, 'submission_id', 'N/A')}: {e}")
            # This function is kept in case it's needed elsewhere, but not called before pagination/load

    def show_next_page(self):
        """Show the next page of submissions."""
        # Cleanup is handled by clear_content() called within display_current_page/display_filtered_page
        # self.stop_all_thumbnail_media() # Removed redundant call

        current_list = self.current_filtered_snapshot if self.is_filtered else self.current_snapshot
        if not current_list:
            return

        next_offset = self.snapshot_offset + self.snapshot_page_size
        if next_offset < len(current_list):
            self.snapshot_offset = next_offset
            self.prev_button.setEnabled(self.snapshot_offset > 0)
            self.next_button.setEnabled(self.snapshot_offset + self.snapshot_page_size < len(current_list))
            if self.is_filtered:
                self.display_filtered_page()
            else:
                self.display_current_page()

    def show_previous_page(self):
        """Show the previous page of submissions."""
        # Cleanup is handled by clear_content() called within display_current_page/display_filtered_page
        # self.stop_all_thumbnail_media() # Removed redundant call

        current_list = self.current_filtered_snapshot if self.is_filtered else self.current_snapshot
        if not current_list or self.snapshot_offset == 0:
            return

        self.snapshot_offset = max(0, self.snapshot_offset - self.snapshot_page_size)
        self.prev_button.setEnabled(self.snapshot_offset > 0)
        self.next_button.setEnabled(self.snapshot_offset + self.snapshot_page_size < len(current_list))
        if self.is_filtered:
            self.display_filtered_page()
        else:
            self.display_current_page()

    def on_author_clicked(self, username):
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

        # Determine ban context
        if self.current_model and not self.current_model.is_user_mode and self.current_model.is_moderator:
            can_ban = True
            ban_subreddit_name = self.current_model.source_name
            ban_subreddit_obj = self.current_model.subreddit
        elif self.current_model and self.current_model.is_user_mode and self.is_filtered and self.previous_subreddit:
             if self.previous_subreddit.lower() in self.current_model.moderated_subreddit_names:
                 can_ban = True
                 ban_subreddit_name = self.previous_subreddit
                 try:
                     ban_subreddit_obj = self.reddit.subreddit(ban_subreddit_name)
                 except Exception as e:
                     logger.error(f"Error getting subreddit object {ban_subreddit_name}: {e}")
                     can_ban = False

        ban_button = None
        if can_ban and ban_subreddit_obj:
            ban_button = msg.addButton("Ban User", QMessageBox.ButtonRole.DestructiveRole)

        cancel_button = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        clicked_button = msg.clickedButton()

        if clicked_button == view_button:
            if hasattr(self, 'snapshot_fetcher') and self.snapshot_fetcher.isRunning():
                logger.debug("Terminating previous snapshot fetcher...")
                self.snapshot_fetcher.terminate()
                self.snapshot_fetcher.wait(500)

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

            QTimer.singleShot(50, self.load_content) # Shorter delay

        elif clicked_button == ban_button and ban_subreddit_obj:
            self.open_ban_dialog(username, ban_subreddit_obj) # Pass object directly
            self.is_author_navigation = False # Reset flag after action

        else: # Cancelled or other button
             self.is_author_navigation = False # Reset flag

    def go_back_to_subreddit(self):
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
            self.current_snapshot = snapshot
            self.snapshot_offset = target_offset # Restore offset
            self.is_loading_posts = False
            self.load_button.setEnabled(True); self.loading_bar.hide()
            self.prev_button.setEnabled(self.snapshot_offset > 0)
            self.next_button.setEnabled(self.snapshot_offset + self.snapshot_page_size < len(snapshot))
            self.source_label.setText(f"Subreddit: {subreddit_name} - {len(snapshot)} posts")
            self.display_current_page() # Will use the restored offset
            try: self.snapshot_fetcher.snapshotFetched.disconnect(on_snapshot_fetched_with_restore)
            except: pass

        self.snapshot_fetcher = SnapshotFetcher(self.current_model)
        self.snapshot_fetcher.snapshotFetched.connect(on_snapshot_fetched_with_restore)
        self.snapshot_fetcher.start()

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
            self.ban_worker.signals.success.connect(self.on_ban_success)
            self.ban_worker.signals.error.connect(self.on_ban_error)
            self.ban_worker.signals.finished.connect(self.on_worker_finished) # Generic finished handler
            self.active_workers.append(self.ban_worker) # Track worker
            self.ban_worker.start()

    def closeEvent(self, event):
        """Handle application shutdown."""
        QThreadPool.globalInstance().clear()
        if hasattr(self, 'snapshot_fetcher') and self.snapshot_fetcher.isRunning():
            self.snapshot_fetcher.terminate()
            self.snapshot_fetcher.wait()
        if hasattr(self, 'mod_log_fetcher_thread') and self.mod_log_fetcher_thread.isRunning():
             self.mod_log_fetcher_thread.terminate()
             self.mod_log_fetcher_thread.wait()
        # Check if filter worker exists, is not None, and is running before terminating
        if hasattr(self, 'filter_worker_thread') and self.filter_worker_thread is not None and self.filter_worker_thread.isRunning():
             self.filter_worker_thread.terminate()
             self.filter_worker_thread.wait()
        if hasattr(self, 'ban_worker') and self.ban_worker is not None and self.ban_worker.isRunning():
             self.ban_worker.terminate()
             self.ban_worker.wait()
        # Terminate any other active workers cleanly
        for worker in self.active_workers:
             if worker is not None and worker.isRunning():
                  logger.debug(f"Terminating active worker: {type(worker).__name__}")
                  worker.terminate()
                  worker.wait()

        event.accept()

    def toggle_subreddit_filter(self):
        """Toggle filtering user posts by the previously viewed subreddit."""
        if not self.previous_subreddit or not self.current_model or not self.current_model.is_user_mode:
            self.filter_button.setVisible(False)
            return

        self.loading_bar.show(); self.is_loading_posts = True
        self.filter_button.setEnabled(False); self.load_button.setEnabled(False)
        self.prev_button.setEnabled(False); self.next_button.setEnabled(False)

        QTimer.singleShot(50, self._perform_filtering) # Shorter delay

    def _perform_filtering(self):
        """Perform the actual filtering operation after UI updates."""
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
                self.filter_worker_thread.filteringComplete.connect(self._on_filtering_complete)
                self.filter_worker_thread.start()

            else: # Removing filter
                self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
                self.filter_button.setStyleSheet("")
                self.source_label.setText(f"User: {username} - {len(self.current_snapshot)} posts")
                self.snapshot_offset = 0
                self.prev_button.setEnabled(False)
                self.next_button.setEnabled(len(self.current_snapshot) > self.snapshot_page_size)
                self.display_current_page() # Display original unfiltered content
                # Reset UI state after displaying
                self.is_loading_posts = False
                self.loading_bar.hide()
                self.filter_button.setEnabled(True)
                self.load_button.setEnabled(True)

        except Exception as e:
            logger.exception(f"Error initiating filtering: {e}")
            QMessageBox.warning(self, "Error", f"An error occurred during filtering: {str(e)}")
            # Reset UI state on error
            self.is_loading_posts = False
            self.loading_bar.hide()
            self.filter_button.setEnabled(True)
            self.load_button.setEnabled(True)
            # Restore button text based on intended state before error
            if self.is_filtered: # If error happened while trying to filter
                 self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
                 self.filter_button.setStyleSheet("")
                 self.is_filtered = False # Reset state
            else: # If error happened while trying to remove filter
                 self.filter_button.setText(f"Remove Filter")
                 self.filter_button.setStyleSheet("background-color: #ffc107; color: black;")
                 self.is_filtered = True # Reset state

    def _on_filtering_complete(self, filtered_snapshot):
        """Handle completion of the background filtering."""
        try:
            self.current_filtered_snapshot = filtered_snapshot
            username = self.source_input.text().strip()
            self.source_label.setText(f"User: {username} - Filtered by r/{self.previous_subreddit} - {len(filtered_snapshot)} posts")
            self.snapshot_offset = 0
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(len(filtered_snapshot) > self.snapshot_page_size)
            self.display_filtered_page() # Display the filtered content
        except Exception as e:
             logger.exception(f"Error processing filtered results: {e}")
             QMessageBox.warning(self, "Error", f"An error occurred displaying filtered results: {str(e)}")
             # Attempt to revert UI state
             self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
             self.filter_button.setStyleSheet("")
             self.is_filtered = False
             self.display_current_page() # Show original page
        finally:
            # Reset UI state regardless of success/failure in processing
            self.is_loading_posts = False
            self.loading_bar.hide()
            self.filter_button.setEnabled(True)
            self.load_button.setEnabled(True)


    def display_filtered_page(self):
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

    # --- Generic Worker Finished Handler ---
    def on_worker_finished(self):
        """Remove finished worker from tracking list."""
        sender = self.sender()
        if sender in self.active_workers:
            logger.debug(f"Worker finished and removed from tracking: {type(sender).__name__}")
            self.active_workers.remove(sender)
        # Specific worker references are cleared in their success/error handlers

    # --- Ban Worker Handlers ---
    def on_ban_success(self, success_message):
        """Handle successful ban signal from worker."""
        logger.info(f"Ban successful: {success_message}")
        QMessageBox.information(self, "Success", success_message)
        self.ban_worker = None # Clear specific worker reference

    def on_ban_error(self, error_message):
        """Handle error signal from ban worker."""
        logger.error(f"Ban failed: {error_message}")
        QMessageBox.warning(self, "Ban Error", f"Failed to ban user:\n{error_message}")
        self.ban_worker = None # Clear specific worker reference

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
