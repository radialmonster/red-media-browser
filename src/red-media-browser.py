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
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QScrollArea, QMessageBox,
    QComboBox, QProgressBar, QSplitter, QMenu, QStatusBar, QTabWidget,
    QGridLayout
)
from PyQt6.QtCore import Qt, QSize, QThreadPool, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QAction, QIcon, QPixmapCache

from red_config import load_config, get_new_refresh_token, update_config_with_new_token
from reddit_api import RedditGalleryModel, SnapshotFetcher, ban_user
from ui_components import ThumbnailWidget, BanUserDialog
from utils import get_cache_dir, ensure_directory
from media_handlers import process_media_url

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Set QPixmapCache size to 100MB to cache more thumbnails
QPixmapCache.setCacheLimit(100 * 1024)

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
            
            # Verify credentials work
            try:
                username = self.reddit.user.me().name
                logger.info(f"Authenticated as {username}")
                self.statusBar.showMessage(f"Authenticated as {username}")
                
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
        else:  # User mode
            self.source_input.setPlaceholderText("Enter username...")
    
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
        # This fixes the issue where the button remains visible when typing a new subreddit
        # But it preserves the button when coming from author navigation
        if not hasattr(self, 'is_author_navigation') or not self.is_author_navigation:
            self.back_button.setVisible(False)
            self.previous_subreddit = None
            self.previous_offset = 0
        
        # Reset the author navigation flag - we don't want to change it here
        # as we need it to persist through on_snapshot_fetched
        was_author_navigation = False
        if hasattr(self, 'is_author_navigation') and self.is_author_navigation:
            was_author_navigation = True
        self.is_author_navigation = False
        
        # Update the status label
        self.source_label.setText(f"Loading {source_type}: {source}")
        
        # Create a new gallery model
        self.current_model = RedditGalleryModel(source, is_user_mode=is_user_mode, reddit_instance=self.reddit)
        
        # Check if user is a moderator (only in subreddit mode)
        if not is_user_mode:
            is_mod = self.current_model.check_user_moderation_status()
            mod_status = "Moderator" if is_mod else "Not a moderator"
            self.mod_status_label.setText(mod_status)
        else:
            self.mod_status_label.setText("User mode")
            
            # If this is author navigation and we're in user mode, ensure the back button is visible
            if was_author_navigation and self.previous_subreddit:
                logger.debug(f"Ensuring back button remains visible for r/{self.previous_subreddit}")
                self.back_button.setText(f"Back to r/{self.previous_subreddit}")
                self.back_button.setVisible(True)
        
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
        # Grid layout will have 5 columns and up to 2 rows
        for i, submission in enumerate(current_page):
            try:
                # Calculate row and column positions
                row = i // 5  # Integer division to get row (0 or 1)
                col = i % 5   # Modulo to get column (0 to 4)
                
                self.add_submission_widget(submission, row, col)
            except Exception as e:
                logger.exception(f"Error displaying submission {submission.id}: {e}")
        
        # Update status
        self.statusBar.showMessage(f"Showing posts {start+1} to {end} of {len(self.current_snapshot)}")
    
    def add_submission_widget(self, submission, row=None, col=None):
        """Add a thumbnail widget for a submission."""
        try:
            # Extract the necessary information from the submission
            title = submission.title
            permalink = submission.permalink
            subreddit_name = submission.subreddit.display_name
            
            # Handle gallery posts vs. single image posts
            has_multiple_images = hasattr(submission, 'is_gallery') and submission.is_gallery
            
            # Get the source URL
            if has_multiple_images and hasattr(submission, 'gallery_data'):
                # For gallery posts, we'll just show the first image's URL in the info
                source_url = "Gallery post"
            else:
                source_url = submission.url
            
            # Get the image URLs
            from utils import extract_image_urls
            image_urls = extract_image_urls(submission)
            
            # Skip submissions without any images
            if not image_urls:
                logger.warning(f"No images found for submission ID {submission.id}")
                return
                
            # Create the thumbnail widget
            thumbnail = ThumbnailWidget(
                images=image_urls,
                title=title,
                source_url=source_url,
                submission=submission,
                subreddit_name=subreddit_name,
                has_multiple_images=has_multiple_images,
                post_url=permalink,
                is_moderator=self.current_model.is_moderator if self.current_model else False
            )
            
            # Connect signals
            thumbnail.authorClicked.connect(self.on_author_clicked)
            
            # Add to layout and store reference
            if row is not None and col is not None:
                self.content_layout.addWidget(thumbnail, row, col)
            else:
                # Fallback for backward compatibility
                self.content_layout.addWidget(thumbnail)
            self.thumbnail_widgets.append(thumbnail)
            
        except Exception as e:
            logger.exception(f"Error displaying submission {submission.id if hasattr(submission, 'id') else 'unknown'}: {e}")
            # Don't leave empty spaces in the grid
            if row is not None and col is not None:
                error_widget = QLabel(f"Error: Failed to load post")
                error_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
                error_widget.setStyleSheet("background-color: #ffdddd; border: 1px solid #ffaaaa;")
                self.content_layout.addWidget(error_widget, row, col)
                self.thumbnail_widgets.append(error_widget)
    
    def clear_content(self):
        """Clear all content from the display."""
        # First dispose of VLC video players separately to avoid flashing
        # This gives them time to clean up properly before removing from layout
        for widget in self.thumbnail_widgets:
            # Check if widget is a ThumbnailWidget with VLC player
            if hasattr(widget, 'cleanup_current_media'):
                try:
                    # Properly cleanup VLC resources first
                    widget.cleanup_current_media()
                except Exception as e:
                    logger.debug(f"Error during video cleanup: {e}")
        
        # Small delay to allow VLC resources to be fully released
        QApplication.processEvents()
        
        # Now remove widgets from layout and close them
        for widget in self.thumbnail_widgets:
            self.content_layout.removeWidget(widget)
            widget.setParent(None)  # Detach from parent before closing
            widget.deleteLater()  # Schedule for deletion instead of immediate close
        
        self.thumbnail_widgets = []
    
    def show_next_page(self):
        """Show the next page of submissions."""
        if not self.current_snapshot:
            return
            
        next_offset = self.snapshot_offset + self.snapshot_page_size
        if next_offset < len(self.current_snapshot):
            self.snapshot_offset = next_offset
            self.prev_button.setEnabled(True)
            self.next_button.setEnabled(next_offset + self.snapshot_page_size < len(self.current_snapshot))
            self.display_current_page()
    
    def show_previous_page(self):
        """Show the previous page of submissions."""
        if not self.current_snapshot or self.snapshot_offset == 0:
            return
            
        prev_offset = max(0, self.snapshot_offset - self.snapshot_page_size)
        self.snapshot_offset = prev_offset
        self.prev_button.setEnabled(prev_offset > 0)
        self.next_button.setEnabled(True)
        self.display_current_page()
    
    def on_author_clicked(self, username):
        """Handle clicking on an author's name."""
        # Prevent clicks while loading
        if self.is_loading_posts:
            logger.debug("Ignoring author click while content is loading")
            return
        
        # Set a flag to track that this is an author navigation (to be used in load_content)
        self.is_author_navigation = True
        
        # Ask if user wants to view the author's posts or take moderation action
        msg = QMessageBox()
        msg.setWindowTitle("Author Options")
        msg.setText(f"What would you like to do with u/{username}?")
        
        view_button = msg.addButton("View Posts", QMessageBox.ButtonRole.ActionRole)
        
        # Only show moderation options if we're in a subreddit and user is a moderator
        ban_button = None
        if (self.current_model and not self.current_model.is_user_mode and 
            self.current_model.is_moderator):
            ban_button = msg.addButton("Ban User", QMessageBox.ButtonRole.DestructiveRole)
        
        cancel_button = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        
        msg.exec()
        
        clicked_button = msg.clickedButton()
        
        if clicked_button == view_button:
            # Make sure we're not already loading something
            if hasattr(self, 'snapshot_fetcher') and self.snapshot_fetcher.isRunning():
                logger.debug("Canceling previous snapshot fetch before loading author content")
                try:
                    self.snapshot_fetcher.terminate()
                    self.snapshot_fetcher.wait(500)  # Wait up to 500ms for clean termination
                except Exception as e:
                    logger.error(f"Error terminating previous snapshot fetcher: {e}")
            
            # Save current subreddit state for back navigation
            if self.current_model and not self.current_model.is_user_mode:
                self.previous_subreddit = self.current_model.source_name
                self.previous_offset = self.snapshot_offset
                logger.debug(f"Saved previous subreddit: {self.previous_subreddit}")
                
                # Setup filter button
                self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
                self.filter_button.setStyleSheet("")
                self.filter_button.setVisible(True)
                self.is_filtered = False
            
            # Switch to user mode and load the user's posts
            self.source_type_combo.setCurrentIndex(1)  # Switch to User mode
            self.source_input.setText(username)
            
            # Show the back button BEFORE loading content (important order change)
            if self.previous_subreddit:
                self.back_button.setText(f"Back to r/{self.previous_subreddit}")
                self.back_button.setVisible(True)
                logger.debug(f"Set back button visible for r/{self.previous_subreddit}")
            
            # Use a brief delay to allow the UI to update before loading content
            QTimer.singleShot(100, lambda: self.load_content())
                    
        elif clicked_button == ban_button:
            # Open ban user dialog
            self.open_ban_dialog(username)
        
        # Reset the flag when we're done (in case load_content wasn't called)
        if clicked_button != view_button:
            self.is_author_navigation = False
    
    def go_back_to_subreddit(self):
        """Navigate back to the previously viewed subreddit."""
        if not self.previous_subreddit:
            return
            
        # Store the target offset we want to restore to
        target_offset = self.previous_offset
        subreddit_name = self.previous_subreddit
        
        # Show loading indicator
        self.loading_bar.show()
        self.is_loading_posts = True
        self.load_button.setEnabled(False)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        
        # Clear any previous content and hide navigation buttons
        self.clear_content()
        self.back_button.setVisible(False)
        self.filter_button.setVisible(False)  # Hide the filter button when going back to subreddit
        
        # Reset filtering state
        self.is_filtered = False
        
        # Switch to subreddit mode
        self.source_type_combo.setCurrentIndex(0)  # Switch to Subreddit mode
        self.source_input.setText(subreddit_name)
        
        # Create the model manually instead of using load_content
        self.current_model = RedditGalleryModel(subreddit_name, is_user_mode=False, reddit_instance=self.reddit)
        
        # Check moderation status
        is_mod = self.current_model.check_user_moderation_status()
        mod_status = "Moderator" if is_mod else "Not a moderator"
        self.mod_status_label.setText(mod_status)
        
        # Update status
        self.source_label.setText(f"Loading Subreddit: {subreddit_name}")
        
        # Create a special version of on_snapshot_fetched that will restore the page
        def on_snapshot_fetched_with_restore(snapshot):
            # Call the normal snapshot handler first
            self.current_snapshot = snapshot
            
            # Set the offset to the saved offset
            self.snapshot_offset = target_offset
            
            # Update status and buttons
            self.is_loading_posts = False
            self.load_button.setEnabled(True)
            self.loading_bar.hide()
            
            # Enable appropriate navigation buttons
            self.prev_button.setEnabled(self.snapshot_offset > 0)
            self.next_button.setEnabled(self.snapshot_offset + self.snapshot_page_size < len(snapshot))
            
            self.source_label.setText(f"Subreddit: {subreddit_name} - {len(snapshot)} posts")
            
            # Display the current page (which will use the offset we just set)
            self.display_current_page()
            
            # Disconnect this handler after use to avoid it being called again
            try:
                self.snapshot_fetcher.snapshotFetched.disconnect(on_snapshot_fetched_with_restore)
            except:
                pass
        
        # Connect our specialized handler and start fetching
        self.snapshot_fetcher = SnapshotFetcher(self.current_model)
        self.snapshot_fetcher.snapshotFetched.connect(on_snapshot_fetched_with_restore)
        self.snapshot_fetcher.start()
    
    def restore_previous_page(self):
        """Restore to the previous page offset after going back to subreddit."""
        if hasattr(self, 'previous_offset') and self.previous_offset > 0:
            target_offset = self.previous_offset
            self.snapshot_offset = 0  # Reset first
            
            # Move to the target page
            while self.snapshot_offset < target_offset and self.snapshot_offset + self.snapshot_page_size < len(self.current_snapshot):
                next_offset = self.snapshot_offset + self.snapshot_page_size
                self.snapshot_offset = next_offset
                self.prev_button.setEnabled(True)
            
            # Update next button state
            self.next_button.setEnabled(self.snapshot_offset + self.snapshot_page_size < len(self.current_snapshot))
            
            # Display the current page
            self.display_current_page()
    
    def open_ban_dialog(self, username):
        """Open the ban user dialog for moderators."""
        if not self.current_model or self.current_model.is_user_mode or not self.current_model.is_moderator:
            return
            
        subreddit_name = self.current_model.source_name
        dialog = BanUserDialog(username, subreddit_name, self)
        if dialog.exec():
            # User clicked either "Ban and Share Reason" or "Ban and Set Private Reason"
            reason = dialog.reason
            share = dialog.result_type == "share"
            
            # Execute the ban
            subreddit = self.current_model.subreddit
            if share:
                ban_message = reason
                success = ban_user(subreddit, username, reason, ban_message)
            else:
                success = ban_user(subreddit, username, reason)
                
            if success:
                QMessageBox.information(
                    self, 
                    "Success", 
                    f"User {username} has been banned from r/{subreddit_name}."
                )
            else:
                QMessageBox.warning(
                    self, 
                    "Error", 
                    f"Failed to ban {username} from r/{subreddit_name}."
                )
    
    def closeEvent(self, event):
        """Handle application shutdown."""
        # Clean up resources
        QThreadPool.globalInstance().clear()
        if hasattr(self, 'snapshot_fetcher') and self.snapshot_fetcher.isRunning():
            self.snapshot_fetcher.terminate()
            self.snapshot_fetcher.wait()
        
        event.accept()
    
    def toggle_subreddit_filter(self):
        """Toggle filtering user posts by the previously viewed subreddit."""
        if not self.previous_subreddit or not self.current_model or not self.current_model.is_user_mode:
            # Can't filter if not in user mode or no previous subreddit
            self.filter_button.setVisible(False)
            return
        
        # Show loading indicator and disable buttons to prevent multiple clicks
        self.loading_bar.show()
        self.is_loading_posts = True
        self.filter_button.setEnabled(False)
        self.load_button.setEnabled(False)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        
        # Use QTimer to allow UI to update before processing
        QTimer.singleShot(100, self._perform_filtering)

    def _perform_filtering(self):
        """Perform the actual filtering operation after UI updates."""
        try:
            # Toggle the filter state
            self.is_filtered = not self.is_filtered
            
            if self.is_filtered:
                # Update the button appearance first
                self.filter_button.setText(f"Remove Filter")
                self.filter_button.setStyleSheet("background-color: #ffc107; color: black;")
                
                # Show a status message
                username = self.source_input.text().strip()
                self.source_label.setText(f"Filtering posts by r/{self.previous_subreddit}...")
                
                # Apply the filter - find posts in the specific subreddit
                logger.debug(f"Filtering {len(self.current_snapshot)} posts for subreddit: {self.previous_subreddit}")
                
                # Process in smaller batches if there are many posts
                filtered_snapshot = []
                total_posts = len(self.current_snapshot)
                
                # Process in batches of 100
                batch_size = 100
                for i in range(0, total_posts, batch_size):
                    batch = self.current_snapshot[i:i+batch_size]
                    
                    # Allow UI to update between batches
                    QApplication.processEvents()
                    
                    # Filter this batch
                    batch_filtered = [
                        post for post in batch 
                        if hasattr(post, 'subreddit') and 
                        post.subreddit.display_name.lower() == self.previous_subreddit.lower()
                    ]
                    filtered_snapshot.extend(batch_filtered)
                
                # Store the filtered results
                self.current_filtered_snapshot = filtered_snapshot
                
                # Update the source label to show we're filtered
                username = self.source_input.text().strip()
                self.source_label.setText(f"User: {username} - Filtered by r/{self.previous_subreddit} - {len(filtered_snapshot)} posts")
                
                # Reset to first page
                self.snapshot_offset = 0
                
                # Update navigation buttons
                self.prev_button.setEnabled(False)
                self.next_button.setEnabled(len(filtered_snapshot) > self.snapshot_page_size)
                
                # Display the filtered content
                self.display_filtered_page()
            else:
                # Remove the filter
                self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
                self.filter_button.setStyleSheet("")
                
                # Restore original display
                username = self.source_input.text().strip()
                self.source_label.setText(f"User: {username} - {len(self.current_snapshot)} posts")
                
                # Reset to first page
                self.snapshot_offset = 0
                
                # Update navigation buttons
                self.prev_button.setEnabled(False)
                self.next_button.setEnabled(len(self.current_snapshot) > self.snapshot_page_size)
                
                # Display the original content
                self.display_current_page()
        
        except Exception as e:
            # Handle any errors
            logger.exception(f"Error during filtering: {e}")
            QMessageBox.warning(self, "Error", f"An error occurred while filtering: {str(e)}")
            
            # Restore button state
            if self.is_filtered:
                self.filter_button.setText(f"Remove Filter")
                self.filter_button.setStyleSheet("background-color: #ffc107; color: black;")
            else:
                self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
                self.filter_button.setStyleSheet("")
        
        finally:
            # Reset UI state
            self.is_loading_posts = False
            self.loading_bar.hide()
            self.filter_button.setEnabled(True)
            self.load_button.setEnabled(True)
    
    def display_filtered_page(self):
        """Display the current page of filtered submissions."""
        # Clear any existing content
        self.clear_content()
        
        if not hasattr(self, 'current_filtered_snapshot') or not self.current_filtered_snapshot:
            label = QLabel(f"No posts found in r/{self.previous_subreddit}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.content_layout.addWidget(label, 0, 0, 1, 5)  # span across all columns
            return
        
        # Get the current page of submissions
        start = self.snapshot_offset
        end = min(start + self.snapshot_page_size, len(self.current_filtered_snapshot))
        current_page = self.current_filtered_snapshot[start:end]
        
        if not current_page:
            label = QLabel(f"No posts found in r/{self.previous_subreddit}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.content_layout.addWidget(label, 0, 0, 1, 5)  # span across all columns
            return
        
        # Create thumbnail widgets for each submission and place them in the grid
        # Grid layout will have 5 columns and up to 2 rows
        for i, submission in enumerate(current_page):
            try:
                # Calculate row and column positions
                row = i // 5  # Integer division to get row (0 or 1)
                col = i % 5   # Modulo to get column (0 to 4)
                
                self.add_submission_widget(submission, row, col)
            except Exception as e:
                logger.exception(f"Error displaying submission {submission.id}: {e}")
        
        # Update status
        self.statusBar.showMessage(f"Showing posts {start+1} to {end} of {len(self.current_filtered_snapshot)} in r/{self.previous_subreddit}")

# Main application entry point
if __name__ == "__main__":
    # Create and initialize cache directories
    cache_dir = get_cache_dir()
    logger.info(f"Using cache directory: {cache_dir}")
    
    # Start the PyQt application
    app = QApplication(sys.argv)
    app.setApplicationName("Red Media Browser")
    main_window = RedMediaBrowser()
    main_window.show()
    sys.exit(app.exec())