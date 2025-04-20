#!/usr/bin/env python3
"""
UI Components for Red Media Browser

This module contains all the UI widgets and components used in the application,
including ThumbnailWidget, FullScreenViewer, and custom dialogs.
"""

import os
import sys
import logging
import weakref
import time
import webbrowser
import html
from typing import List, Optional, Dict, Any, Callable

import vlc

from PyQt6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton, QSizePolicy,
    QDialog, QMessageBox, QLineEdit, QProgressBar, QScrollArea, QTextBrowser
)
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal, QThreadPool
from PyQt6.QtGui import QPixmap, QPixmapCache, QMovie, QIcon

# Import caching utilities needed for moderation status
from utils import get_media_type, get_metadata_file_path, read_metadata_file
from media_handlers import process_media_url, MediaDownloadWorker
import reddit_api

# Set up logging
logger = logging.getLogger(__name__)

class ClickableLabel(QLabel):
    """
    QLabel subclass that emits a clicked signal when clicked.
    Useful for clickable labels acting as buttons.
    """
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

# (ClickableVLCWidget removed; use standard QWidget for VLC video area)


class FullScreenViewer(QDialog):
    """
    Dialog for displaying media full-screen.
    Supports static images, animated content (videos/GIFs).
    """
    closed = pyqtSignal()

    def __init__(self, pixmap=None, movie=None, video_path=None, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.movie = movie
        self.video_path = video_path
        self.vlc_instance = None
        self.vlc_player = None

        # Set window properties
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet("background-color: black;")
        self.setWindowTitle("Full Screen View")

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Content display area
        self.content_label = QLabel(self)
        self.content_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.content_label)

        # Set up the appropriate media
        if self.video_path:
            self.setup_video_player()
        elif self.movie:
            self.content_label.setMovie(self.movie)
            self.movie.start()
            # Set movie to loop continuously
            if not hasattr(self.movie, 'loopCount') or self.movie.loopCount() != -1:
                # Use a timer to manually restart the movie when it finishes
                self.movie_timer = QTimer(self)
                self.movie_timer.timeout.connect(self.check_movie_restart)
                self.movie_timer.start(100)
        elif self.pixmap:
            scaled_pixmap = self.pixmap.scaled(
                self.screen().size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.content_label.setPixmap(scaled_pixmap)

    def setup_video_player(self):
        """Set up the VLC video player for fullscreen playback."""
        if not self.video_path:
            return

        abs_video_path = os.path.abspath(self.video_path)
        logger.debug(f"Setting up fullscreen video player for: {abs_video_path}")

        instance_args = ['--no-video-title-show', '--quiet']
        self.vlc_instance = vlc.Instance(*instance_args)
        self.vlc_player = self.vlc_instance.media_player_new()

        # Set VLC output window
        if sys.platform.startswith('win'):
            self.vlc_player.set_hwnd(int(self.winId()))
        elif sys.platform.startswith('linux'):
            self.vlc_player.set_xwindow(int(self.winId()))
        elif sys.platform.startswith('darwin'):
            self.vlc_player.set_nsobject(int(self.winId()))

        media = self.vlc_instance.media_new_path(abs_video_path)
        media.add_option('input-repeat=-1')
        media.add_option(':repeat')
        media.add_option(':loop')
        media.add_option(':file-caching=3000')

        self.vlc_player.set_media(media)

        # Delay play to allow window to show and avoid blocking UI
        QTimer.singleShot(100, self.vlc_player.play)

        self.vlc_player.audio_set_mute(True)

        self.playback_check_timer = QTimer(self)
        self.playback_check_timer.timeout.connect(self.check_fullscreen_playback)
        self.playback_check_timer.start(1000)

    def check_fullscreen_playback(self):
        """Restart video if playback ended or errored."""
        if not self.vlc_player:
            return

        state = self.vlc_player.get_state()
        if state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
            logger.debug(f"Fullscreen video state: {state}, restarting playback")
            self.vlc_player.stop()
            self.vlc_player.play()

    def check_movie_restart(self):
        """Restart animated GIF if finished."""
        if self.movie and self.movie.state() == QMovie.MovieState.NotRunning:
            self.movie.start()

    def keyPressEvent(self, event):
        """Close on Escape key."""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        """Close on any mouse click."""
        self.close()
        super().mousePressEvent(event)

    def closeEvent(self, event):
        """Cleanup when the dialog is closed."""
        if hasattr(self, 'playback_check_timer') and self.playback_check_timer:
            self.playback_check_timer.stop()

        if self.vlc_player:
            self.vlc_player.stop()
            self.vlc_player.release()
        if self.vlc_instance:
            self.vlc_instance.release()
        self.closed.emit()
        super().closeEvent(event)

class BanUserDialog(QDialog):
    """
    Dialog for banning a user from a subreddit.
    Allows setting a reason and choosing whether to share it with the user.
    """
    def __init__(self, username, subreddit, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ban User")
        self.result_type = None  # "share" or "private"
        self.reason = ""

        layout = QVBoxLayout(self)
        label = QLabel(f"Enter the reason for banning {username} from r/{subreddit}:", self)
        layout.addWidget(label)

        self.reason_input = QLineEdit(self)
        layout.addWidget(self.reason_input)

        button_layout = QHBoxLayout()
        self.share_button = QPushButton("Ban and Share Reason with User", self)
        self.private_button = QPushButton("Ban and Set Private Reason", self)
        self.cancel_button = QPushButton("Cancel", self)

        button_layout.addWidget(self.share_button)
        button_layout.addWidget(self.private_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

        self.share_button.clicked.connect(self.share_clicked)
        self.private_button.clicked.connect(self.private_clicked)
        self.cancel_button.clicked.connect(self.reject)

    def share_clicked(self):
        text = self.reason_input.text().strip()
        if not text:
            QMessageBox.warning(self, "Warning", "You must enter a ban reason.")
            return
        self.reason = text
        self.result_type = "share"
        self.accept()

    def private_clicked(self):
        text = self.reason_input.text().strip()
        if not text:
            QMessageBox.warning(self, "Warning", "You must enter a ban reason.")
            return
        self.reason = text
        self.result_type = "private"
        self.accept()

class ReportsDialog(QDialog):
    """
    Dialog for displaying reports on a Reddit submission.
    """
    def __init__(self, report_reasons, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Post Reports")
        self.setMinimumSize(400, 300)

        layout = QVBoxLayout(self)
        self.reports_browser = QTextBrowser()
        self.reports_browser.setOpenExternalLinks(False)
        self.reports_browser.setReadOnly(True)

        try:
            if report_reasons and isinstance(report_reasons, list):
                html_content = "<h3>Report Reasons:</h3><ul>"
                for reason in report_reasons:
                    try:
                        if isinstance(reason, str):
                            html_content += f"<li>{html.escape(reason)}</li>"
                        else:
                            html_content += f"<li>Invalid report format: {type(reason)}</li>"
                    except Exception as e:
                        logger.error(f"Error formatting report reason: {e}")
                        html_content += "<li>Error formatting report</li>"
                html_content += "</ul>"
                self.reports_browser.setHtml(html_content)
            else:
                self.reports_browser.setHtml("<p>No detailed report information available.</p>")
        except Exception as e:
            logger.error(f"Error creating reports dialog content: {e}")
            self.reports_browser.setHtml(f"<p>Error displaying reports: {html.escape(str(e))}</p>")

        layout.addWidget(self.reports_browser)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        layout.addWidget(self.close_button)

from PyQt6.QtCore import QEvent

class ThumbnailWidget(QWidget):
    """
    Widget for displaying a single Reddit post with thumbnail image/video.
    Includes controls for navigation, moderation, and fullscreen viewing.
    """
    # Signal emitted when the author label is clicked
    authorClicked = pyqtSignal(str)
    
    # Signal emitted when media loading state changes
    loadingStateChanged = pyqtSignal(bool)
    
    # Signal emitted when media is ready for display
    mediaReady = pyqtSignal()

    def __init__(self, images, title, source_url, submission,
                 subreddit_name, has_multiple_images, post_url, is_moderator, reddit_instance):
        """
        Initialize ThumbnailWidget for a Reddit post.

        Args:
            images: List of image/video URLs.
            title: Post title.
            source_url: Source URL for the post.
            submission: PRAW submission or cached object.
            subreddit_name: Name of the subreddit.
            has_multiple_images: True if post is a gallery.
            post_url: Reddit post URL.
            is_moderator: True if user is a moderator.
            reddit_instance: Reddit API instance.
        """
        super().__init__()

        # Submission data
        self.reddit_instance = reddit_instance
        self.praw_submission = submission
        self.submission_id = submission.id
        self.images = images
        self.current_index = 0
        self.post_url = post_url
        self.source_url = source_url
        self.title = title
        self.subreddit_name = subreddit_name
        self.has_multiple_images = has_multiple_images
        self.is_moderator = is_moderator

        # Media display state
        self.pixmap = None
        self.movie = None
        self.vlc_instance = None
        self.vlc_player = None
        self.is_media_loaded = False
        self.is_fullscreen_open = False
        self.fullscreen_viewer = None
        self.original_title = title

        # Reports data
        self.reports_count = 0
        self.report_reasons = []

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.init_ui()

        # Check if post is removed/deleted (no images and specific flags)
        is_removed_or_deleted = False
        if not self.images:
            removed_by_cat = getattr(self.praw_submission, 'removed_by_category', None)
            banned_by = getattr(self.praw_submission, 'banned_by', None)
            author_name = getattr(self.praw_submission, 'author', None)
            is_author_deleted = author_name == "[deleted]" or author_name is None

            if removed_by_cat or banned_by or is_author_deleted:
                is_removed_or_deleted = True
                status_text = "Post Removed/Deleted"
                if removed_by_cat:
                    status_text = f"Post Removed ({removed_by_cat})"
                elif banned_by:
                    status_text = f"Post Removed (by {banned_by})"
                elif is_author_deleted:
                    status_text = "Post Deleted (Author)"

                logger.debug(f"Submission {self.submission_id} appears removed/deleted. Displaying status.")
                self.imageLabel.setText(status_text)
                self.imageLabel.setStyleSheet("background-color: #444; color: #ccc; font-style: italic;")
                self.imageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.loadingBar.hide()
                self.is_media_loaded = True

        # Load first image if available and not removed/deleted
        if self.images and not is_removed_or_deleted:
            self.load_image_async(self.images[self.current_index])
        elif not self.images and not is_removed_or_deleted:
            logger.warning(f"Submission {self.submission_id} has no images and is not marked as removed/deleted.")
            self.imageLabel.setText("No Image Available")
            self.imageLabel.setStyleSheet("background-color: #333; color: #aaa;")
            self.imageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.loadingBar.hide()
            self.is_media_loaded = True

        # Fetch reports if moderator and not removed/deleted
        if self.is_moderator and not is_removed_or_deleted:
            self.fetch_reports()

    def init_ui(self):
        """Initialize the UI layout and components."""
        self.layout = QVBoxLayout(self)
        self.layout.setSpacing(5)

        # Title section
        self.titleLabel = ClickableLabel()
        self.titleLabel.setText(self.title)
        self.titleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.titleLabel.setFixedHeight(40)
        self.titleLabel.setWordWrap(True)
        self.titleLabel.setStyleSheet("font-weight: bold;")
        self.titleLabel.clicked.connect(self.open_post_url)

        title_container = QWidget()
        title_container.setFixedHeight(40)
        title_layout = QVBoxLayout(title_container)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.addWidget(self.titleLabel)
        self.layout.addWidget(title_container)

        # Subreddit label
        self.subredditLabel = QLabel(f"r/{self.subreddit_name}")
        self.subredditLabel.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        self.subredditLabel.setStyleSheet("font-size: 9pt; color: grey;")
        self.subredditLabel.setFixedHeight(20)

        # Author, Subreddit, and URL info section
        self.infoLayout = QHBoxLayout()

        username = "unknown"
        try:
            author_data = self.praw_submission.author
            if author_data:
                if hasattr(author_data, 'name'):
                    username = author_data.name
                elif isinstance(author_data, str):
                    username = author_data
        except AttributeError:
            logger.warning(f"Submission {self.submission_id} missing 'author' attribute entirely.")
            username = "unknown"
        except Exception as e:
            logger.error(f"Error accessing author for {self.submission_id}: {e}")
            username = "unknown"

        if username is None:
            username = "unknown"

        self.authorLabel = ClickableLabel()
        self.authorLabel.setText(username)
        self.authorLabel.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.authorLabel.setFixedHeight(20)
        self.authorLabel.clicked.connect(lambda u=username: self.authorClicked.emit(u) if u != "unknown" else None)
        self.infoLayout.addWidget(self.authorLabel)
        self.infoLayout.addSpacing(10)
        self.infoLayout.addWidget(self.subredditLabel)
        self.infoLayout.addSpacing(10)

        self.postUrlLabel = QLabel(self.source_url)
        self.postUrlLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.postUrlLabel.setFixedHeight(20)
        self.infoLayout.addWidget(self.postUrlLabel)
        self.layout.addLayout(self.infoLayout)

        # Image/video display section
        self.imageLabel = ClickableLabel()
        self.imageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.imageLabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.imageLabel.setStyleSheet("background-color: #1a1a1a;")
        self.imageLabel.setScaledContents(False)
        self.imageLabel.clicked.connect(self.open_fullscreen_view)
        self.layout.addWidget(self.imageLabel, 1)

        # Loading indicator
        self.loadingBar = QProgressBar(self)
        self.loadingBar.setTextVisible(False)
        self.loadingBar.setMaximum(100)
        self.layout.addWidget(self.loadingBar)
        self.loadingBar.hide()

        # Navigation buttons for galleries
        if self.has_multiple_images:
            self.init_arrow_buttons()

        # Moderation buttons for moderators
        if self.is_moderator:
            self.create_moderation_buttons()
            self.update_moderation_status_ui()
    
    def init_arrow_buttons(self):
        """Initialize navigation buttons for gallery posts."""
        self.arrowLayout = QHBoxLayout()
        self.arrowLayout.setSpacing(5)
        self.arrowLayout.setContentsMargins(0, 0, 0, 0)
        
        # Image counter label
        self.imageCountLabel = QLabel(f"Image 1/{len(self.images)}")
        self.imageCountLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Left/right navigation buttons
        self.leftArrowButton = QPushButton("<")
        self.leftArrowButton.clicked.connect(self.show_previous_image)
        self.leftArrowButton.setToolTip("Previous image")
        
        self.rightArrowButton = QPushButton(">")
        self.rightArrowButton.clicked.connect(self.show_next_image)
        self.rightArrowButton.setToolTip("Next image")
        
        # Update enabled states
        self.leftArrowButton.setEnabled(len(self.images) > 1)
        self.rightArrowButton.setEnabled(len(self.images) > 1)
        
        # Add to layout
        self.arrowLayout.addWidget(self.leftArrowButton)
        self.arrowLayout.addWidget(self.imageCountLabel)
        self.arrowLayout.addWidget(self.rightArrowButton)
        self.layout.addLayout(self.arrowLayout)
    
    def create_moderation_buttons(self):
        """Initialize moderation buttons for moderators."""
        self.moderation_layout = QHBoxLayout()
        self.moderation_layout.setSpacing(5)
        
        # Approve button
        self.approve_button = QPushButton("Approve", self)
        self.approve_button.clicked.connect(self.approve_submission)
        self.approve_button.setToolTip("Approve this submission")
        # Remove fixed width to allow stretching
        self.approve_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        
        # Remove button
        self.remove_button = QPushButton("Remove", self)
        self.remove_button.clicked.connect(self.remove_submission)
        self.remove_button.setToolTip("Remove this submission")
        # Remove fixed width to allow stretching
        self.remove_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        
        # Reports button 
        self.reports_button = QPushButton("Reports", self)
        self.reports_button.clicked.connect(self.show_reports)
        self.reports_button.setToolTip("View reports for this submission")
        self.reports_button.setStyleSheet("background-color: yellow; color: black;")
        # Remove fixed width to allow stretching
        self.reports_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        
        # Check right away if there are reports directly available, with safer handling
        try:
            mod_reports = getattr(self.praw_submission, 'mod_reports', [])
            user_reports = getattr(self.praw_submission, 'user_reports', [])
            
            # Safely calculate report count
            mod_report_count = len(mod_reports)
            user_report_count = 0
            
            # Handle user reports safely regardless of format
            if user_reports:
                for report_item in user_reports:
                    # Check if it's a tuple/list with at least 2 items and second is an int
                    if isinstance(report_item, (list, tuple)) and len(report_item) >= 2:
                        if isinstance(report_item[1], int):
                            user_report_count += report_item[1]
                        else:
                            # If second item isn't an int, just count each item as 1
                            user_report_count += 1
                    else:
                        # If it's not in expected format, just count each item as 1
                        user_report_count += 1
            
            direct_report_count = mod_report_count + user_report_count
            
            if direct_report_count > 0:
                # Use a shorter format for report count to avoid width issues
                if direct_report_count > 999:
                    self.reports_button.setText(f"Rep(999+)")
                else:
                    self.reports_button.setText(f"Rep({direct_report_count})")
                logger.debug(f"Found reports during button creation: {direct_report_count} for post {self.praw_submission.id}")
            else:
                # Initially hide until we fetch reports and confirm they exist
                self.reports_button.hide()
        except Exception as e:
            logger.error(f"Error detecting reports in button creation: {e}")
            # If there's any error, hide the reports button
            self.reports_button.hide()
        
        # Remove the spacer widget - no longer needed as the buttons will expand naturally
        
        self.moderation_layout.addWidget(self.approve_button)
        self.moderation_layout.addWidget(self.remove_button)
        self.moderation_layout.addWidget(self.reports_button)
        # No spacer widget needed
        self.layout.addLayout(self.moderation_layout)
    
    def update_moderation_status_ui(self):
        """Update the moderation button appearance based on the current status from cache."""
        # Fetch status from cached metadata
        status = None
        metadata_path = get_metadata_file_path(self.submission_id)
        if metadata_path:
            metadata = read_metadata_file(metadata_path)
            if metadata:
                status = metadata.get('moderation_status')
                logger.debug(f"Moderation status for {self.submission_id} from cache: {status}")
            else:
                logger.debug(f"No metadata found for {self.submission_id} at {metadata_path}")
        else:
            logger.debug(f"Could not determine metadata path for {self.submission_id}")

        # Reset styles first
        self.approve_button.setStyleSheet("")
        self.remove_button.setStyleSheet("")
        self.approve_button.setText("Approve")
        self.remove_button.setText("Remove")
        self.remove_button.setToolTip("Remove this submission")
        # Ensure title is reset to original in case it was modified elsewhere (shouldn't happen now)
        self.titleLabel.setText(self.original_title)

        # Apply status-specific styling
        if status == "approved":
            self.approve_button.setStyleSheet("background-color: green; color: white;")
            self.approve_button.setText("Approved")
        elif status == "removed":
            self.remove_button.setStyleSheet("background-color: red; color: white;")
            self.remove_button.setText("Removed")
            # Do NOT modify the title here
        elif status == "removal_pending":
            self.remove_button.setStyleSheet("background-color: orange; color: white;")
            self.remove_button.setText("Retry Remove")
            self.remove_button.setToolTip("Network error occurred. Click to retry removing this submission")
            # Re-enable the button to allow retrying
            self.remove_button.setEnabled(True)
    
    def fetch_reports(self):
        """Fetch reports for the current submission using the dedicated API function."""
        try:
            # Directly call the API function which handles caching and fetching
            # Pass the stored reddit_instance
            report_count, report_reasons = reddit_api.get_submission_reports(self.praw_submission, self.reddit_instance)
            self.reports_count = report_count
            self.report_reasons = report_reasons

            # Update UI to show reports button if there are reports
            if report_count > 0:
                # Use a shorter format for report count to avoid width issues
                if report_count > 999:
                    self.reports_button.setText(f"Rep(999+)")
                else:
                    self.reports_button.setText(f"Reports ({report_count})")
                self.reports_button.show()
                logger.debug(f"Fetched reports via API function: {report_count} for post {self.praw_submission.id}")
            else:
                self.reports_button.hide()
                logger.debug(f"No reports found via API function for post {self.praw_submission.id}")

        except Exception as e:
            logger.exception(f"Error fetching reports via API function: {e}")
            # If there's any error, hide the reports button
            if hasattr(self, 'reports_button'):
                self.reports_button.hide()
    
    def show_reports(self):
        """Show dialog with report details."""
        try:
            # Make a clean copy of report reasons to avoid possible reference issues
            report_reasons_copy = []
            
            # Safely copy each report reason
            if self.report_reasons and isinstance(self.report_reasons, list):
                for reason in self.report_reasons:
                    if isinstance(reason, str):
                        report_reasons_copy.append(reason)
                    else:
                        # Convert non-string reasons to safe strings
                        try:
                            report_reasons_copy.append(f"Report: {str(reason)}")
                        except Exception:
                            report_reasons_copy.append("Unreadable report")
            
            # Create and show the dialog with the cleaned report data
            logger.debug(f"Showing reports dialog with {len(report_reasons_copy)} reports")
            dialog = ReportsDialog(report_reasons_copy, self)
            
            # Use non-blocking show instead of exec if there are many reports
            if len(report_reasons_copy) > 20:
                # For many reports, using non-modal dialog can prevent UI locking
                dialog.setModal(False)
                dialog.show()
            else:
                # For fewer reports, a modal dialog is fine
                dialog.exec()
                
        except Exception as e:
            # Display an error message if showing the reports fails
            logger.exception(f"Error showing reports dialog: {e}")
            QMessageBox.warning(
                self, 
                "Error Showing Reports",
                f"Could not display reports: {str(e)}"
            )
    
    def load_image_async(self, url):
        """
        Start asynchronous loading of media from URL.
        Shows loading indicator and handles caching.
        """
        # First check if we already have it in the QPixmapCache
        processed_url = process_media_url(url)
        cached_pixmap = QPixmapCache.find(processed_url)
        
        if cached_pixmap:
            self.pixmap = cached_pixmap
            self.update_pixmap()
            return
        
        # Show loading indicator
        self.loadingBar.setValue(0)
        self.loadingBar.show()
        self.loadingStateChanged.emit(True)
        
        # Create a weak reference to avoid memory leaks
        weak_self = weakref.ref(self)
        
        # Create a download worker, passing the submission data
        # self.praw_submission holds either the PRAW object or the SimpleNamespace from cache
        worker = MediaDownloadWorker(url, self.praw_submission)
        
        # Connect signals with safety checks
        # Use lambda functions with try/except to prevent crashes if widget is deleted
        worker.signals.progress.connect(lambda progress: 
            self._safe_update_progress(weak_self, progress))
        
        worker.signals.finished.connect(lambda file_path: 
            self._safe_call_finished(weak_self, file_path, processed_url))
        
        worker.signals.error.connect(lambda error_msg: 
            self._safe_call_error(weak_self, error_msg))
        
        # Start the worker
        QThreadPool.globalInstance().start(worker)
    
    @staticmethod
    def _safe_update_progress(weak_ref, progress):
        try:
            instance = weak_ref()
            if instance is not None and hasattr(instance, 'loadingBar'):
                instance.loadingBar.setValue(progress)
        except RuntimeError:
            # Widget was deleted between the check and the call
            pass
    
    @staticmethod
    def _safe_call_finished(weak_ref, file_path, url):
        try:
            instance = weak_ref()
            if instance is not None and hasattr(instance, 'on_media_downloaded'):
                instance.on_media_downloaded(file_path, url)
        except RuntimeError:
            # Widget was deleted between the check and the call
            pass
    
    @staticmethod
    def _safe_call_error(weak_ref, error_msg):
        try:
            instance = weak_ref()
            if instance is not None and hasattr(instance, 'on_media_error'):
                instance.on_media_error(error_msg)
        except RuntimeError:
            # Widget was deleted between the check and the call
            pass
    
    def on_media_downloaded(self, file_path, url):
        """Handle completed media download."""
        # First check if this widget has been deleted
        try:
            # Simple property access test to detect if this object is still valid
            test = self.is_media_loaded
        except RuntimeError:
            # Object has been deleted, silently abort
            logger.debug("Widget was deleted before media download completed")
            return
        
        if not file_path:
            self.on_media_error("Download failed")
            return
            
        # Hide loading indicator
        try:
            self.loadingBar.hide()
            self.loadingStateChanged.emit(False)
        except RuntimeError:
            # Object has been deleted during operation
            return
        
        try:
            # Determine media type and load appropriately
            media_type = get_media_type(file_path)
            logger.debug(f"Media type for {file_path}: {media_type}")
            
            # Update displayed URL with cached location
            domain = os.path.basename(os.path.dirname(file_path))
            filename = os.path.basename(file_path)
            display_path = f"{domain}/{filename}"
            self.postUrlLabel.setText(display_path)
            
            if media_type == "video":
                # For RedGifs videos, make sure file is fully downloaded before playing
                if "redgifs" in file_path.lower():
                    logger.debug(f"RedGifs video detected: {file_path}")
                    file_size = os.path.getsize(file_path)
                    
                    if file_size < 1000:  # If less than 1KB, likely not valid
                        logger.error(f"RedGifs video file too small: {file_size} bytes")
                        self.imageLabel.setText("Invalid video file")
                    else:
                        # Create a weak reference to self before scheduling the delayed play
                        self_ref = weakref.ref(self)
                        
                        # Use the weak reference in the lambda
                        def safe_play_video():
                            widget = self_ref()
                            if widget is not None:
                                try:
                                    widget.play_video(file_path)
                                except RuntimeError:
                                    # Widget was deleted
                                    logger.debug("Widget was deleted before delayed video play")
                        
                        # Add a short delay to ensure file is fully ready
                        QTimer.singleShot(200, safe_play_video)
                else:
                    # Regular video
                    self.play_video(file_path)
            elif media_type == "animated_image":
                # Clean up any existing media first
                self.cleanup_current_media()
                
                # Replace the standard QLabel with our specialized AnimatedGifDisplay
                if hasattr(self, 'imageLabel'):
                    # Remove the existing imageLabel from layout
                    self.layout.removeWidget(self.imageLabel)
                    self.imageLabel.hide()
                    
                    # Create the special GIF display widget if it doesn't exist
                    if not hasattr(self, 'gifDisplay') or self.gifDisplay is None:
                        self.gifDisplay = AnimatedGifDisplay(self)
                        self.gifDisplay.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
                        self.gifDisplay.setCursor(self.imageLabel.cursor())  # Copy cursor style
                        self.gifDisplay.mousePressEvent = lambda event: self.open_fullscreen_view()  # Make clickable
                        
                    # Add it to the layout in the same position
                    index = 2  # Index where imageLabel was (under title & info)
                    self.layout.insertWidget(index, self.gifDisplay, 1)  # Add with stretch factor 1
                    self.gifDisplay.show()
                
                # Load the GIF using our specialized method
                if hasattr(self, 'gifDisplay'):
                    logger.debug(f"Using AnimatedGifDisplay for: {file_path}")
                    self.gifDisplay.set_gif(file_path)
                    
                    # Store the file path for fullscreen viewing
                    self.gif_file_path = file_path
                else:
                    # Fallback to old method if something went wrong
                    logger.warning("AnimatedGifDisplay not available, using standard QMovie")
                    self.movie = QMovie(file_path)
                    self.movie.setCacheMode(QMovie.CacheMode.CacheAll)
                    self.imageLabel.setMovie(self.movie)
                    self.movie.start()
                    
            elif media_type == "image":
                pix = QPixmap(file_path)
                if not pix.isNull():
                    QPixmapCache.insert(url, pix)
                    self.pixmap = pix
                    
                    # Make sure the standard imageLabel is visible (not gif widget)
                    if hasattr(self, 'gifDisplay') and self.gifDisplay is not None and self.gifDisplay.isVisible():
                        self.layout.removeWidget(self.gifDisplay)
                        self.gifDisplay.hide()
                        self.layout.insertWidget(2, self.imageLabel, 1)  # Re-add image label
                        self.imageLabel.show()
                    
                    self.update_pixmap()
                else:
                    self.imageLabel.setText("Image not available")
            else:
                self.imageLabel.setText("Media not available")
            
            # Signal that media is ready
            self.is_media_loaded = True
            self.mediaReady.emit()
        
        except RuntimeError as e:
            # Widget was deleted during processing
            logger.debug(f"Widget was deleted during media processing: {e}")
        except Exception as e:
            logger.exception(f"Error handling downloaded media: {e}")
            try:
                self.imageLabel.setText(f"Error loading media: {str(e)}")
            except RuntimeError:
                # Widget already deleted
                pass

    def pre_scale_movie(self):
        """Pre-scale the movie based on the label size before displaying."""
        if not self.movie or not hasattr(self, 'imageLabel'):
            return
            
        # Get the first frame to determine dimensions
        self.movie.jumpToFrame(0)
        first_frame = self.movie.currentPixmap()
        
        if first_frame.isNull():
            logger.warning("pre_scale_movie: first frame is null")
            return
            
        # Calculate appropriate scaling
        label_size = self.imageLabel.size()
        if label_size.width() <= 0 or label_size.height() <= 0:
            return
            
        orig_width = first_frame.width()
        orig_height = first_frame.height()
        
        scale_factor = min(label_size.width() / orig_width, label_size.height() / orig_height)
        new_width = int(orig_width * scale_factor)
        new_height = int(orig_height * scale_factor)
        
        # Set the scaled size
        logger.debug(f"Pre-scaling movie to: {new_width}x{new_height}")
        self.movie.setScaledSize(QSize(new_width, new_height))

    def handle_frame_change(self, frame_number):
        """Handle frame changes in the animated GIF.
        This function is called only once per frame change rather than continuously.
        """
        # If this is the first frame, ensure proper sizing
        if frame_number == 0 and hasattr(self, 'first_frame_displayed') and not self.first_frame_displayed:
            self.first_frame_displayed = True
            self.pre_scale_movie()

    def restart_gif_smoothly(self):
        """Smoothly restart the GIF animation without flashing."""
        if hasattr(self, 'movie') and self.movie:
            # Only need to jump to the first frame, animation will continue
            QTimer.singleShot(0, lambda: self.movie.jumpToFrame(0))
            
        # Do not call start() as this causes the flashing

    def on_media_error(self, error_msg):
        """Handle media download/loading errors."""
        logger.error(f"Media error: {error_msg}")
        self.loadingBar.hide()
        self.loadingStateChanged.emit(False)
        
        # Set constrained width for error messages to prevent layout stretching
        self.imageLabel.setMaximumWidth(300)  # Limit width to prevent stretching
        self.imageLabel.setText("Media loading failed")
        self.imageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Set a fixed height to maintain consistent grid layout
        self.imageLabel.setMinimumHeight(200)
        self.imageLabel.setMaximumHeight(200)
        
        # Truncate long error messages to prevent layout issues
        if len(error_msg) > 50:
            short_error = error_msg[:47] + "..."
        else:
            short_error = error_msg
        self.postUrlLabel.setText(f"Error: {short_error}")
        
        # Ensure we emit mediaReady so the grid layout can properly arrange widgets
        self.is_media_loaded = True
        self.mediaReady.emit()
    
    def update_pixmap(self):
        """Update the displayed image with proper scaling."""
        if self.pixmap and not self.pixmap.isNull():
            scaled_pixmap = self.pixmap.scaled(
                self.imageLabel.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.imageLabel.setPixmap(scaled_pixmap)
        else:
            self.imageLabel.clear()
            self.imageLabel.setText("Image not available")
            self.imageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
    
    def resize_gif_first_frame(self, frame_number):
        """
        Resize animated GIF on the first frame.
        Disconnects after the first resize to avoid continuous resizing.
        """
        if frame_number == 0:
            self.update_movie_scale()
            try:
                self.movie.frameChanged.disconnect(self.resize_gif_first_frame)
            except Exception:
                # In case it's already disconnected
                pass
    
    def update_movie_scale(self):
        """Update the scaling of the QMovie to fit the label."""
        if not self.movie:
            return
            
        # Use the current pixmap for reliable size information
        current_pixmap = self.movie.currentPixmap()
        if current_pixmap.isNull():
            logger.warning("update_movie_scale: current pixmap is null.")
            return
            
        # Calculate scaling
        orig_width = current_pixmap.width()
        orig_height = current_pixmap.height()
        label_width = self.imageLabel.width()
        label_height = self.imageLabel.height()
        
        if orig_width <= 0 or orig_height <= 0 or label_width <= 0 or label_height <= 0:
            return
            
        scale_factor = min(label_width / orig_width, label_height / orig_height)
        new_width = int(orig_width * scale_factor)
        new_height = int(orig_height * scale_factor)
        new_size = QSize(new_width, new_height)
        
        logger.debug(f"Setting movie scaled size to: {new_width}x{new_height}")
        self.movie.setScaledSize(new_size)
    
    def play_video(self, video_path, no_hw=False):
        """
        Play a video using VLC.
        Sets up the VLC instance and media player.
        """
        abs_video_path = os.path.abspath(video_path)
        self.current_video_path = abs_video_path
        logger.debug(f"VLC: Playing video from file: {abs_video_path}")
        
        # Don't try to play non-existent or empty files
        if not os.path.exists(abs_video_path) or os.path.getsize(abs_video_path) == 0:
            logger.error(f"VLC: Video file does not exist or is empty: {abs_video_path}")
            self.imageLabel.setText("Video file not available")
            return
        
        try:
            # Clean up any existing VLC player
            self.cleanup_current_media()
            
            # Keep the image label in the same position but hide it
            # This helps maintain consistent layout and prevents flashing
            if hasattr(self, 'imageLabel'):
                self.imageLabel.hide()
            
            # Enhanced VLC arguments for better compatibility with looping - match test file
            instance_args = ['--quiet', '--loop', '--repeat']
            
            # Create VLC instance and player
            self.vlc_instance = vlc.Instance(*instance_args)
            self.vlc_player = self.vlc_instance.media_player_new()
            
            # Create widget for VLC output in the same position as the image label
            self.vlc_widget = QWidget(self)
            self.vlc_widget.setStyleSheet("background-color: black;")
            self.layout.insertWidget(2, self.vlc_widget)

            # Ensure the widget has the same size policy as the image label
            self.vlc_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

            # Set mousePressEvent directly to open_fullscreen_view (like GIFs)
            def vlc_mouse_press_event(event):
                self.open_fullscreen_view()
                event.accept()
            self.vlc_widget.mousePressEvent = vlc_mouse_press_event

            # Set the window for rendering
            if sys.platform.startswith('win'):
                self.vlc_player.set_hwnd(int(self.vlc_widget.winId()))
            elif sys.platform.startswith('linux'):
                self.vlc_player.set_xwindow(int(self.vlc_widget.winId()))
            elif sys.platform.startswith('darwin'):
                self.vlc_player.set_nsobject(int(self.vlc_widget.winId()))
            
            # Create media with robust path handling - match test file
            media = self.vlc_instance.media_new_path(abs_video_path)
            
            # Set multiple looping options for redundancy - match test file
            media.add_option('input-repeat=-1')  # Loop indefinitely
            media.add_option(':repeat')  # Additional looping option
            media.add_option(':loop')    # Another looping option
            media.add_option(':file-caching=3000')  # Increase caching
            
            # Set the media
            self.vlc_player.set_media(media)
            
            # Start playback
            play_result = self.vlc_player.play()
            logger.debug(f"Play result: {play_result}")
            
            # Mute audio
            self.vlc_player.audio_set_mute(True)
            
            # Use a timer to update the aspect ratio once the video starts playing
            QTimer.singleShot(500, self.update_video_aspect_ratio)
            
            # Set up regular check for playback status to handle looping - match test file
            self.playback_monitor = QTimer(self)
            self.playback_monitor.timeout.connect(self.check_and_restart_playback)
            self.playback_monitor.start(1000)  # Check every second
            
            # If we have moderation buttons, make sure they stay at the bottom
            if self.is_moderator and hasattr(self, 'moderation_layout'):
                self.layout.removeItem(self.moderation_layout)
                self.layout.addLayout(self.moderation_layout)
            
        except Exception as e:
            logger.exception(f"Error setting up video playback: {e}")
            # Show error in the image label
            if hasattr(self, 'imageLabel'):
                self.imageLabel.setText(f"Video error: {str(e)}")
                self.imageLabel.show()

    def check_and_restart_playback(self):
        """Check if playback has ended and restart if needed - same as test file"""
        if not hasattr(self, 'vlc_player') or not self.vlc_player:
            if hasattr(self, 'playback_monitor'):
                self.playback_monitor.stop()
            return
        
        state = self.vlc_player.get_state()
        
        # If the video ended, restart it - same as test file
        if state == vlc.State.Ended or state == vlc.State.Stopped or state == vlc.State.Error:
            logger.debug(f"Video playback state: {state}, restarting...")
            try:
                self.vlc_player.stop()
                self.vlc_player.play()
            except Exception as e:
                logger.error(f"Error restarting video: {e}")

    def update_video_aspect_ratio(self):
        """Update the aspect ratio of the video to maintain proper display."""
        if hasattr(self, 'vlc_player') and self.vlc_player:
            try:
                native_size = self.vlc_player.video_get_size(0)
                if native_size and native_size[0] > 0 and native_size[1] > 0:
                    aspect_ratio_str = f"{native_size[0]}:{native_size[1]}"
                    self.vlc_player.video_set_aspect_ratio(aspect_ratio_str)
                    logger.debug(f"Set video aspect ratio to {native_size[0]}:{native_size[1]}")
                    
                    # Special handling for portrait videos (taller than wide)
                    is_portrait = native_size[1] > native_size[0]
                    if is_portrait and hasattr(self, 'vlc_widget'):
                        # Set maximum width for portrait videos to prevent layout disruption
                        max_width = int(self.vlc_widget.height() * native_size[0] / native_size[1])
                        self.vlc_widget.setMaximumWidth(max_width)
                        logger.debug(f"Portrait video detected, setting max width to {max_width}")
            except Exception as e:
                logger.error(f"Error setting video aspect ratio: {e}")

    def cleanup_current_media(self):
        """Clean up current media before loading a new one."""
        try:
            # First check if this widget has been deleted
            test = self.is_media_loaded  # Simple property access to test if object is still valid
        except RuntimeError:
            # Widget has been deleted, nothing to clean up
            logger.debug("Widget was deleted before cleanup_current_media could complete")
            return
            
        try:
            # Stop the playback monitor if active
            if hasattr(self, 'playback_monitor') and self.playback_monitor:
                self.playback_monitor.stop()
            
            # Stop any playing video
            if hasattr(self, 'vlc_player') and self.vlc_player:
                try:
                    self.vlc_player.stop()
                    self.vlc_player.release()
                    self.vlc_instance.release()
                    
                    if hasattr(self, 'vlc_widget'):
                        self.vlc_widget.setParent(None)  # Properly detach from parent
                        self.vlc_widget.deleteLater()
                        
                    delattr(self, 'vlc_player')
                    delattr(self, 'vlc_instance')
                    if hasattr(self, 'vlc_widget'):
                        delattr(self, 'vlc_widget')
                except Exception as e:
                    logger.exception(f"Error during VLC cleanup: {e}")
            
            # Clean up the AnimatedGifDisplay
            if hasattr(self, 'gifDisplay') and self.gifDisplay is not None:
                try:
                    self.gifDisplay.cleanup()
                    self.gifDisplay.hide()
                except Exception as e:
                    logger.exception(f"Error cleaning up AnimatedGifDisplay: {e}")
            
            # Stop any playing animation using standard QMovie
            if hasattr(self, 'movie') and self.movie:
                self.movie.stop()
                self.movie = None
            
            # Show the image label again if it exists
            if hasattr(self, 'imageLabel'):
                self.imageLabel.clear()
                self.imageLabel.show()  # Make sure it's visible
        except RuntimeError as e:
            # Widget was deleted during cleanup
            logger.debug(f"Widget was deleted during cleanup_current_media: {e}")
        except Exception as e:
            logger.exception(f"Error during cleanup_current_media: {e}")
    

    def open_post_url(self):
        """Open the Reddit post URL in the default web browser."""
        full_url = "https://www.reddit.com" + self.post_url if self.post_url.startswith("/") else self.post_url
        logger.debug(f"Opening browser URL: {full_url}")
        webbrowser.open(full_url)
    
    def open_fullscreen_view(self):
        """Open full-screen view of the current media."""
        logger.debug("Opening fullscreen view.")
        
        # Don't open another if one is already open
        if self.is_fullscreen_open:
            return
            
        self.is_fullscreen_open = True
        
        # Determine what type of media we have
        if hasattr(self, 'vlc_player') and self.vlc_player:
            # For videos, pass the video path
            logger.debug("Opening fullscreen video player")
            viewer = FullScreenViewer(video_path=self.current_video_path)
            # Start playing video in fullscreen
            if viewer.vlc_player:
                viewer.vlc_player.play()
        elif hasattr(self, 'gifDisplay') and self.gifDisplay is not None and self.gifDisplay.movie is not None:
            # For GIFs in our custom display, create a new QMovie for fullscreen
            logger.debug("Opening fullscreen GIF viewer from AnimatedGifDisplay")
            if hasattr(self, 'gif_file_path') and os.path.exists(self.gif_file_path):
                fullscreen_movie = QMovie(self.gif_file_path)
                fullscreen_movie.setCacheMode(QMovie.CacheMode.CacheAll)
                viewer = FullScreenViewer(movie=fullscreen_movie)
                fullscreen_movie.start()
            else:
                logger.error("Cannot open fullscreen GIF - no file path")
                self.is_fullscreen_open = False
                return
        elif hasattr(self, 'movie') and self.movie:
            # For GIFs using standard player, pass the movie
            logger.debug("Opening fullscreen GIF viewer")
            viewer = FullScreenViewer(movie=self.movie)
            self.movie.start()
        elif self.pixmap and not self.pixmap.isNull():
            # For static images, pass the pixmap
            logger.debug("Opening fullscreen image viewer")
            viewer = FullScreenViewer(pixmap=self.pixmap)
        else:
            logger.debug("No valid media for fullscreen view")
            self.is_fullscreen_open = False
            return
        
        # Keep a reference and show
        self.fullscreen_viewer = viewer
        viewer.closed.connect(self.on_fullscreen_closed)
        viewer.showFullScreen()
    
    def on_fullscreen_closed(self):
        """Handle the fullscreen viewer being closed."""
        self.is_fullscreen_open = False
        self.fullscreen_viewer = None
    
    def stop_all_media(self):
        """
        Stop all playing media (videos, GIFs, fullscreen) and clean up resources.
        This should be called before any user action that could conflict with media playback.
        """
        logger.debug(f"Stopping all media for submission {self.submission_id}")
        # Stop embedded/inline media
        self.cleanup_current_media()
        # Stop fullscreen viewer if open
        if getattr(self, 'is_fullscreen_open', False) and getattr(self, 'fullscreen_viewer', None):
            try:
                logger.debug("Closing fullscreen viewer as part of media stop.")
                self.fullscreen_viewer.close()
            except Exception as e:
                logger.error(f"Error closing fullscreen viewer: {e}")

    def approve_submission(self): # Line 1195
        """Approve the current submission (moderator action)."""
        self.stop_all_media()
        # Pass the stored reddit_instance to the API function
        if reddit_api.approve_submission(self.praw_submission, self.reddit_instance):
            self.update_moderation_status_ui()

    def remove_submission(self): # Line 1201 - Corrected indentation
        """Remove the current submission (moderator action)."""
        self.stop_all_media()
        logger.debug(f"Remove clicked for {self.submission_id}. Media stopped and cleaned up.")
        # Now attempt the removal
        # Pass the stored reddit_instance to the API function
        if reddit_api.remove_submission(self.praw_submission, self.reddit_instance):
            self.update_moderation_status_ui()
    
    def close(self): # Line 1214 - Corrected indentation
        """Clean up resources when the widget is closed."""
        self.stop_all_media()
        super().close()
    
    def show_previous_image(self): # Line 1219 - Corrected indentation
        """Show the previous image in a gallery post."""
        self.stop_all_media()
        if not self.has_multiple_images or len(self.images) <= 1:
            return
            
        # Decrement index with wraparound
        self.current_index = (self.current_index - 1) % len(self.images)
        
        # Update image counter label
        if hasattr(self, 'imageCountLabel'):
            self.imageCountLabel.setText(f"Image {self.current_index + 1}/{len(self.images)}")
        
        # Load the new image
        self.load_image_async(self.images[self.current_index])
        
    def show_next_image(self): # Line 1234 - Corrected indentation
        """Show the next image in a gallery post."""
        self.stop_all_media()
        if not self.has_multiple_images or len(self.images) <= 1:
            return
            
        # Increment index with wraparound
        self.current_index = (self.current_index + 1) % len(self.images)
        
        # Update image counter label
        if hasattr(self, 'imageCountLabel'):
            self.imageCountLabel.setText(f"Image {self.current_index + 1}/{len(self.images)}")
        
        # Load the new image
        self.load_image_async(self.images[self.current_index])

class AnimatedGifDisplay(QLabel):
    """
    Custom widget specifically designed to handle animated GIFs without causing 
    window refreshes when the animation loops.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.movie = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background-color: transparent;")
        
        # Flag to track if we've already handled the first frame
        self.first_frame_handled = False
        
        # Create a timer for smooth looping
        self.loop_timer = QTimer(self)
        self.loop_timer.setSingleShot(True)
        self.loop_timer.timeout.connect(self.restart_movie_safely)
    
    def set_gif(self, file_path):
        """Set and start playing a GIF file"""
        # Clean up any existing movie
        if self.movie:
            self.movie.stop()
            self.movie = None
            self.clear()
            self.first_frame_handled = False
        
        # Create new movie
        self.movie = QMovie(file_path)
        
        # Configure movie
        self.movie.setCacheMode(QMovie.CacheMode.CacheAll)
        
        # Connect signals properly
        self.movie.frameChanged.connect(self.handle_frame_changed)
        
        # Start the movie without setting it directly on the label
        # This avoids the QMovie's default looping behavior which causes refreshes
        self.movie.start()
    
    def handle_frame_changed(self, frame_number):
        """Handle a frame change in the movie"""
        # Update the displayed pixmap
        pixmap = self.movie.currentPixmap()
        if not pixmap.isNull():
            # Scale the pixmap to fit the label
            scaled_pixmap = pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.setPixmap(scaled_pixmap)
            
            # Special handling for the first frame to set up proper scaling
            if not self.first_frame_handled:
                self.first_frame_handled = True
        
        # If we're near the end of the animation, prepare for smooth looping
        total_frames = self.movie.frameCount()
        if total_frames > 0 and frame_number >= total_frames - 1:
            # Schedule a smooth restart without stopping the movie
            self.loop_timer.start(10)  # Very small delay
    
    def restart_movie_safely(self):
        """Restart the movie without causing a visual refresh"""
        if self.movie:
            # Jump to the first frame without stopping the animation
            self.movie.jumpToFrame(0)
    
    def resizeEvent(self, event):
        """Handle resize events to rescale the current frame"""
        super().resizeEvent(event)
        if self.movie and not self.pixmap().isNull():
            # Re-scale the current frame to fit the new size
            pixmap = self.movie.currentPixmap()
            if not pixmap.isNull():
                scaled_pixmap = pixmap.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                self.setPixmap(scaled_pixmap)
    
    def cleanup(self):
        """Clean up resources when the widget is no longer needed"""
        if self.movie:
            self.movie.stop()
            self.movie = None
        
        if self.loop_timer.isActive():
            self.loop_timer.stop()
