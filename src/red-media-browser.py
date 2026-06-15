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
from collections import OrderedDict
from typing import List, Optional, Dict, Any

import praw
# Import praw.models.Subreddit for type checking
from praw.models import Subreddit as PrawSubreddit

# PRAW 8 raises ReadOnlyException from Reddit.user.me() in read-only mode instead of
# returning None. Import it defensively so older PRAW builds and the stubbed test
# environment (which has no praw.exceptions) still load this module.
try:
    from praw.exceptions import ReadOnlyException
except Exception:  # pragma: no cover - older PRAW or stubbed test environment
    class ReadOnlyException(Exception):
        """Fallback when praw.exceptions.ReadOnlyException is unavailable."""
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QScrollArea, QMessageBox,
    QComboBox, QProgressBar, QSplitter, QMenu, QStatusBar, QTabWidget,
    QGridLayout, QDialog, QTextBrowser, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView
)
from PyQt6.QtCore import Qt, QSize, QThreadPool, QThread, pyqtSignal, QTimer, QMutex, QRunnable, QMutexLocker
from PyQt6.QtGui import QAction, QPixmapCache

from red_config import (
    load_config, get_new_refresh_token, update_config_with_new_token,
    configure_logging, get_log_level_from_config_file
)
# Import specific workers and functions
from reddit_api import RedditGalleryModel, SnapshotFetcher, ModeratedSubredditsFetcher, BanWorker
from ui_components import ThumbnailWidget, BanUserDialog
from utils import (
    get_cache_dir, ensure_directory, extract_image_urls,
    record_removed_submission, remove_approved_submission_from_removal_log,
    get_removed_user_summaries, backfill_removal_log_from_mod_actions,
    rebuild_media_usage_index, get_duplicate_media_usage_groups,
    get_media_usage_index_path
)
from media_handlers import process_media_url, MediaDownloadWorker, WorkerSignals

# Import constants
from constants import (
    PIXMAP_CACHE_SIZE_MB, POSTS_FETCH_LIMIT, DEFAULT_POSTS_FETCH_LIMIT, MOD_LOG_FETCH_LIMIT,
    UI_UPDATE_DELAY_MS, MOD_STATUS_DELAY_MS, THREAD_TERMINATION_TIMEOUT_MS,
    SNAPSHOT_GRID_COLUMNS, SNAPSHOT_GRID_CELL_MIN_WIDTH, SNAPSHOT_GRID_CELL_MIN_HEIGHT,
    DEFAULT_PREFETCH_MEDIA_LIMIT, DEFAULT_MAX_CONCURRENT_PREFETCH_DOWNLOADS,
    DEFAULT_POST_PREFETCH_BUFFER_SIZE, MAX_AUTO_PREFETCHED_POSTS,
    PREFETCH_RETRY_BASE_DELAY_MS, PREFETCH_RETRY_MAX_ATTEMPTS
)

# Configure logging using config.json when available, defaulting to INFO.
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
configure_logging(get_log_level_from_config_file(CONFIG_PATH))
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
            if self.isInterruptionRequested():
                logger.debug("ModLogFetcher interrupted before completion")
                return
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
                    if self.isInterruptionRequested():
                        logger.debug(f"ModLogFetcher interrupted while processing r/{display_name}")
                        return
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
                if self.isInterruptionRequested():
                    logger.debug("ReportsFetcher interrupted before completion")
                    return
                reports.append(report)
                # Optionally limit memory usage by processing in batches
                if len(reports) >= POSTS_FETCH_LIMIT:
                    break
            if self.isInterruptionRequested():
                logger.debug("ReportsFetcher interrupted before emit")
                return
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
                if self.isInterruptionRequested():
                    logger.debug("RemovedPostsFetcher interrupted before completion")
                    return
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
                    if self.isInterruptionRequested():
                        logger.debug("RemovedPostsFetcher interrupted during info() batching")
                        return
                    batch = fullnames[i:i + batch_size]
                    batch_submissions = list(self.subreddit._reddit.info(fullnames=batch))
                    removed.extend(batch_submissions)
            else:
                removed = []
            if self.isInterruptionRequested():
                logger.debug("RemovedPostsFetcher interrupted before emit")
                return
            self.removedPostsFetched.emit(removed)
        except Exception as e:
            logger.exception(f"Error fetching removed posts in worker: {e}")
            self.errorOccurred.emit(str(e))

class BannedUsersFetcher(QThread):
    """Worker thread for checking which users are already banned in a subreddit."""
    bannedUsersFetched = pyqtSignal(set)
    errorOccurred = pyqtSignal(str)

    def __init__(self, subreddit, usernames: List[str]) -> None:
        super().__init__()
        self.subreddit = subreddit
        self.usernames = usernames

    def run(self) -> None:
        banned_users = set()
        try:
            requested_users = {
                str(username or "").strip().lower()
                for username in self.usernames
                if str(username or "").strip()
            }
            requested_users.difference_update({"[deleted]", "unknown"})

            try:
                for banned_user in self.subreddit.banned(limit=None):
                    if self.isInterruptionRequested():
                        return
                    username = str(
                        getattr(banned_user, "name", None) or
                        getattr(banned_user, "__dict__", {}).get("name") or
                        banned_user
                    ).strip().lower()
                    if username in requested_users:
                        banned_users.add(username)
            except Exception as bulk_error:
                logger.debug("Bulk banned-user fetch failed, falling back to per-user checks: %s", bulk_error)
                for username in requested_users:
                    if self.isInterruptionRequested():
                        return
                    matches = list(self.subreddit.banned(redditor=username, limit=1))
                    if matches:
                        banned_users.add(username)

            if not self.isInterruptionRequested():
                self.bannedUsersFetched.emit(banned_users)
        except Exception as e:
            logger.exception(f"Error fetching banned-user status: {e}")
            if not self.isInterruptionRequested():
                self.errorOccurred.emit(str(e))

class BannedUserVerificationFetcher(QThread):
    """Worker thread for verifying one user's subreddit ban status."""
    banStatusVerified = pyqtSignal(str, bool)
    errorOccurred = pyqtSignal(str, str)

    def __init__(self, subreddit, username: str, attempts: int = 3, retry_delay_seconds: float = 1.5) -> None:
        super().__init__()
        self.subreddit = subreddit
        self.username = username
        self.attempts = attempts
        self.retry_delay_seconds = retry_delay_seconds

    def run(self) -> None:
        try:
            username = str(self.username or "").strip()
            if not username:
                raise ValueError("Missing username.")

            is_banned = False
            for attempt in range(max(1, self.attempts)):
                if self.isInterruptionRequested():
                    return

                matches = list(self.subreddit.banned(redditor=username, limit=1))
                is_banned = any(
                    str(
                        getattr(banned_user, "name", None) or
                        getattr(banned_user, "__dict__", {}).get("name") or
                        banned_user
                    ).strip().lower()
                    == username.lower()
                    for banned_user in matches
                )
                if is_banned or attempt >= self.attempts - 1:
                    break

                time.sleep(max(0.1, self.retry_delay_seconds))

            if not self.isInterruptionRequested():
                self.banStatusVerified.emit(username, is_banned)
        except Exception as e:
            logger.exception(f"Error verifying banned-user status for {self.username}: {e}")
            if not self.isInterruptionRequested():
                self.errorOccurred.emit(str(self.username or ""), str(e))

class RemovalLogBackfillWorker(QThread):
    """Fetch mod-log removals/approvals and replay them into the local removal log."""
    backfillFinished = pyqtSignal(dict)
    errorOccurred = pyqtSignal(str)

    def __init__(self, subreddit, removal_moderators: List[str], limit: int) -> None:
        super().__init__()
        self.subreddit = subreddit
        self.removal_moderators = removal_moderators
        self.limit = limit

    def run(self) -> None:
        try:
            actions = []
            seen_action_ids = set()

            for moderator in self.removal_moderators:
                if self.isInterruptionRequested():
                    return
                for entry in self.subreddit.mod.log(
                    action="removelink",
                    mod=moderator,
                    limit=self.limit,
                ):
                    action_id = str(getattr(entry, "id", "") or "")
                    if action_id and action_id in seen_action_ids:
                        continue
                    if action_id:
                        seen_action_ids.add(action_id)
                    actions.append(entry)

            if self.isInterruptionRequested():
                return

            for entry in self.subreddit.mod.log(action="approvelink", limit=self.limit):
                if self.isInterruptionRequested():
                    return
                action_id = str(getattr(entry, "id", "") or "")
                if action_id and action_id in seen_action_ids:
                    continue
                if action_id:
                    seen_action_ids.add(action_id)
                actions.append(entry)

            stats = backfill_removal_log_from_mod_actions(
                actions,
                allowed_removal_moderators=self.removal_moderators,
            )
            stats["subreddit"] = getattr(self.subreddit, "display_name", "")
            stats["moderators"] = list(self.removal_moderators)
            if not self.isInterruptionRequested():
                self.backfillFinished.emit(stats)
        except Exception as e:
            logger.exception(f"Error backfilling removal log: {e}")
            if not self.isInterruptionRequested():
                self.errorOccurred.emit(str(e))

class DuplicateMediaIndexWorker(QThread):
    """Load or rebuild the media usage index and return duplicate media groups."""
    duplicateMediaReady = pyqtSignal(list, dict)
    errorOccurred = pyqtSignal(str)

    def __init__(
        self,
        subreddit_name: Optional[str] = None,
        current_subreddit_only: bool = False,
        rebuild_index: bool = False,
    ) -> None:
        super().__init__()
        self.subreddit_name = subreddit_name
        self.current_subreddit_only = current_subreddit_only
        self.rebuild_index = rebuild_index

    def run(self) -> None:
        try:
            index_exists = os.path.exists(get_media_usage_index_path())
            if self.rebuild_index or not index_exists:
                stats = rebuild_media_usage_index(compute_missing_hashes=True)
                stats["rebuilt_index"] = True
            else:
                stats = {
                    "rebuilt_index": False,
                    "hashed_files": 0,
                }
            groups = get_duplicate_media_usage_groups(
                require_multiple_authors=True,
                subreddit_name=self.subreddit_name if self.current_subreddit_only else None,
            )
            if not self.isInterruptionRequested():
                self.duplicateMediaReady.emit(groups, stats)
        except Exception as e:
            logger.exception(f"Error rebuilding duplicate media index: {e}")
            if not self.isInterruptionRequested():
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
                 if self.isInterruptionRequested():
                     logger.debug("FilterWorker interrupted before completion")
                     return
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
            if not self.isInterruptionRequested():
                self.filteringComplete.emit(filtered_snapshot)


class MediaPrefetchWorker(QRunnable):
    """Worker for prefetching media files in the background."""

    def __init__(self, main_window, submissions_to_prefetch, batch_id):
        super().__init__()
        self.main_window = main_window
        self.submissions_to_prefetch = submissions_to_prefetch
        self.batch_id = batch_id
        self.signals = WorkerSignals()

    def run(self):
        """Prefetch media files for given submissions."""
        batch_stats = {
            'submissions_considered': len(self.submissions_to_prefetch),
            'media_urls_considered': 0,
            'queued': 0,
            'cache_hits': 0,
            'duplicate_skips': 0,
            'retry_backoff_skips': 0,
            'retry_exhausted_skips': 0,
            'errors': 0,
        }
        try:
            logger.info(
                "prefetch_batch_start batch_id=%s submissions=%s",
                self.batch_id,
                len(self.submissions_to_prefetch),
            )
            for submission in self.submissions_to_prefetch:
                if not hasattr(submission, 'id'):
                    continue

                image_urls = extract_image_urls(submission)

                # Prefetch each media file
                for url in image_urls:
                    batch_stats['media_urls_considered'] += 1
                    submission_id = getattr(submission, 'id', 'UnknownID')
                    try:
                        # Resolve provider-specific URLs before dedupe so equivalent
                        # source links do not start duplicate downloads.
                        processed_url = process_media_url(url)
                        if not processed_url:
                            raise ValueError("Media URL processing returned an empty URL")

                        from utils import get_cache_path_for_url, get_existing_cache_path_for_url
                        cache_path = get_cache_path_for_url(processed_url)
                        if not cache_path:
                            raise ValueError(f"Could not determine cache path for prefetched URL: {processed_url}")

                        with QMutexLocker(self.main_window.prefetch_mutex):
                            existing_prefetch = self.main_window._find_prefetched_media_match_locked(
                                url,
                                processed_url,
                            )
                            should_queue_download = existing_prefetch is None
                            counted_non_duplicate_skip = False
                            if existing_prefetch is not None:
                                alias_data = dict(existing_prefetch)
                                alias_data.setdefault('started_at', time.time())
                                alias_data['processed_url'] = processed_url
                                status = alias_data.get('status')
                                duplicate_reason = f"existing_{status}" if status else "existing"

                                if status == 'error':
                                    attempts = int(alias_data.get('attempts', 0))
                                    next_retry_at = float(alias_data.get('next_retry_at', 0) or 0)
                                    should_queue_download = (
                                        attempts < PREFETCH_RETRY_MAX_ATTEMPTS and
                                        next_retry_at <= time.time()
                                    )

                                    if should_queue_download:
                                        alias_data['status'] = 'queued'
                                        alias_data.pop('completed_at', None)
                                        alias_data.pop('error', None)
                                        alias_data.pop('next_retry_at', None)
                                    elif attempts >= PREFETCH_RETRY_MAX_ATTEMPTS:
                                        self.main_window._increment_prefetch_stat_locked(
                                            'retry_exhausted_skips'
                                        )
                                        batch_stats['retry_exhausted_skips'] += 1
                                        counted_non_duplicate_skip = True
                                    else:
                                        self.main_window._increment_prefetch_stat_locked(
                                            'retry_backoff_skips'
                                        )
                                        batch_stats['retry_backoff_skips'] += 1
                                        counted_non_duplicate_skip = True
                                else:
                                    should_queue_download = False

                                self.main_window._sync_prefetched_media_entries_locked(
                                    url,
                                    alias_data,
                                    processed_url=processed_url,
                                )
                            else:
                                self.main_window._sync_prefetched_media_entries_locked(
                                    url,
                                    {
                                    'status': 'queued',
                                    'started_at': time.time(),
                                        'attempts': 0,
                                    'processed_url': processed_url,
                                    },
                                    processed_url=processed_url,
                                )
                                self.main_window._increment_prefetch_stat_locked('scheduled')

                        existing_cache_path = get_existing_cache_path_for_url(processed_url)
                        if not existing_cache_path:
                            if should_queue_download:
                                if self.main_window.queue_prefetch_download(
                                    url,
                                    processed_url,
                                    submission,
                                    batch_id=self.batch_id,
                                ):
                                    batch_stats['queued'] += 1
                                    logger.info(
                                        "Queued media prefetch: batch_id=%s submission_id=%s processed_url=%s",
                                        self.batch_id,
                                        submission_id,
                                        processed_url,
                                    )
                            elif not counted_non_duplicate_skip:
                                with QMutexLocker(self.main_window.prefetch_mutex):
                                    self.main_window._increment_prefetch_stat_locked(
                                        'duplicate_skips'
                                    )
                                batch_stats['duplicate_skips'] += 1
                                self.main_window._log_prefetch_skip(
                                    duplicate_reason,
                                    url,
                                    processed_url=processed_url,
                                    submission_id=submission_id,
                                )
                            elif existing_prefetch is not None:
                                attempts = int(existing_prefetch.get('attempts', 0))
                                self.main_window._log_prefetch_skip(
                                    'retry_exhausted' if attempts >= PREFETCH_RETRY_MAX_ATTEMPTS else 'retry_backoff',
                                    url,
                                    processed_url=processed_url,
                                    submission_id=submission_id,
                                    attempts=attempts,
                                    next_retry_at=existing_prefetch.get('next_retry_at'),
                                )
                        else:
                            # Already cached
                            self.main_window._update_prefetched_media_status(
                                url,
                                "cached",
                                processed_url=processed_url,
                                cache_path=existing_cache_path,
                            )
                            with QMutexLocker(self.main_window.prefetch_mutex):
                                self.main_window._increment_prefetch_stat_locked('cache_hits')
                            batch_stats['cache_hits'] += 1
                            self.main_window._log_prefetch_skip(
                                'already_cached',
                                url,
                                processed_url=processed_url,
                                submission_id=submission_id,
                            )

                    except Exception as e:
                        logger.debug(f"Error prefetching media {url}: {e}")
                        batch_stats['errors'] += 1
                        self.main_window._update_prefetched_media_status(
                            url,
                            "error",
                            error_message=str(e),
                        )
                        continue

        except Exception as e:
            logger.exception(f"Error in media prefetch worker: {e}")
        finally:
            self.main_window._record_prefetch_batch_scan_result(self.batch_id, batch_stats)
            try:
                self.signals.finished.emit("", "", None)
            except RuntimeError as e:
                logger.debug(
                    "Skipping MediaPrefetchWorker finished signal during shutdown for batch %s: %s",
                    self.batch_id,
                    e,
                )


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
        self.all_current_snapshot = []
        self.current_snapshot = []
        self.all_current_filtered_snapshot = []
        self.current_filtered_snapshot = []
        self.can_fetch_more_posts = False
        self.view_mode = "empty"
        self.active_queue_name = None
        self.snapshot_page_size = 10
        self.snapshot_offset = 0
        self.thumbnail_widgets = []
        self.is_loading_posts = False
        self.selected_author = None
        self.authenticated_username = None

        # For back navigation
        self.previous_subreddit = None
        self.previous_offset = 0
        self.back_button = None

        # For moderated subreddits
        self.moderated_subreddits = []
        self.moderated_subreddit_names = set()
        self.mod_subreddits_fetched = False
        self.prefetched_mod_logs = {}
        self.mod_logs_ready = False
        self.mod_log_fetcher_thread = None # To keep a reference
        self.filter_worker_thread = None # To keep reference to filter worker
        self.ban_worker = None # To keep reference to ban worker
        self.snapshot_fetcher = None
        self.next_batch_fetcher = None
        self.next_prefetch_fetcher = None
        self.prefetched_next_batch = None
        self.prefetched_next_after = None
        self.post_prefetch_buffer_size = DEFAULT_POST_PREFETCH_BUFFER_SIZE
        self.max_auto_prefetched_posts = MAX_AUTO_PREFETCHED_POSTS
        self.next_500_fetcher = None
        self.reports_fetcher = None
        self.removed_fetcher = None
        self.banned_users_fetcher = None
        self.removed_user_ban_verification_fetcher = None
        self.removal_backfill_fetcher = None
        self.duplicate_media_fetcher = None
        self.removed_user_summaries = []
        self.removed_user_summary_by_author = {}
        self.removed_users_banned_set = set()
        self.removed_users_pending_ban_verification = set()
        self.removed_users_banned_status_ready = False
        self.removed_users_table = None
        self.removed_users_view_button = None
        self.removed_users_ignore_button = None
        self.removed_users_ban_button = None
        self.removed_users_min_count = 1
        self.removed_users_ignored_users = set()
        self.removal_log_moderator_allowlist = []
        self.removal_log_backfill_limit = MOD_LOG_FETCH_LIMIT
        self.duplicate_media_groups = []
        self.duplicate_media_rows = []
        self.duplicate_media_table = None
        self.duplicate_media_view_author_button = None
        self.duplicate_media_ban_author_button = None
        self.duplicate_media_refresh_button = None
        self.active_workers = [] # Keep track of active workers
        self.workers_mutex = QMutex() # Thread safety for active_workers list

        # Prefetch system for media only (post data already fetched at startup)
        self.prefetch_enabled = True
        self.prefetch_media_limit = DEFAULT_PREFETCH_MEDIA_LIMIT
        self.max_concurrent_prefetch_downloads = DEFAULT_MAX_CONCURRENT_PREFETCH_DOWNLOADS
        self.prefetched_media = {}  # url -> prefetch_status
        self.prefetch_workers = []  # Track active prefetch workers
        self.prefetch_download_queue = OrderedDict()
        self.active_prefetch_downloads = set()
        self.prefetch_mutex = QMutex()  # Thread safety for prefetch data
        self.is_shutting_down = False
        self.prefetch_stats = {
            'scheduled': 0,
            'started': 0,
            'completed': 0,
            'failed': 0,
            'retried': 0,
            'cache_hits': 0,
            'duplicate_skips': 0,
            'retry_backoff_skips': 0,
            'retry_exhausted_skips': 0,
        }
        self.prefetch_batch_counter = 0
        self.prefetch_batches = {}
        self.filtered_auto_fetch_empty_batches = 0
        self.page_render_token = 0
        self.page_render_started_at = 0.0
        self.page_render_expected = 0
        self.page_render_ready = 0
        self.page_render_build_logged = False
        self.page_render_media_logged = False
        self.page_render_build_completed_at = 0.0
        self.page_render_summary_logged = False
        self.page_render_summary_timeout_ms = 5000
        self.page_render_summary_timer = QTimer(self)
        self.page_render_summary_timer.setSingleShot(True)
        self.page_render_summary_timer.timeout.connect(self._on_page_render_summary_timeout)

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

        self.view_removed_users_button = QPushButton("Removed Users")
        self.view_removed_users_button.setToolTip("View users sorted by removed posts in this subreddit")
        self.view_removed_users_button.clicked.connect(self.view_removed_users)
        self.view_removed_users_button.setVisible(False)
        nav_layout.addWidget(self.view_removed_users_button)

        self.view_duplicate_media_button = QPushButton("Duplicate Media")
        self.view_duplicate_media_button.setToolTip("Find cached media posted by multiple users")
        self.view_duplicate_media_button.clicked.connect(self.view_duplicate_media)
        self.view_duplicate_media_button.setVisible(True)
        nav_layout.addWidget(self.view_duplicate_media_button)

        self.backfill_removals_button = QPushButton("Backfill Removals")
        self.backfill_removals_button.setToolTip("Backfill local removal counts from the Reddit moderator log")
        self.backfill_removals_button.clicked.connect(self.backfill_removal_log)
        self.backfill_removals_button.setVisible(False)
        nav_layout.addWidget(self.backfill_removals_button)

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
        self._configure_content_grid()

        self.scroll_area.setWidget(self.content_widget)

        main_layout.addWidget(self.scroll_area, stretch=1)

        # Status bar at bottom
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.mod_log_status_label = QLabel("") # Label for mod log status
        self.status_bar.addPermanentWidget(self.mod_log_status_label)

        # Set initial values
        self.on_source_type_changed(0)  # Default to subreddit mode

    def _get_grid_column_count(self) -> int:
        """Return the responsive column count used for snapshot page rendering."""
        max_columns = max(1, SNAPSHOT_GRID_COLUMNS)

        viewport = getattr(self, 'scroll_area', None)
        if viewport is None:
            return max_columns

        viewport_width = self.scroll_area.viewport().width()
        if viewport_width <= 0:
            return max_columns

        margins = self.content_layout.contentsMargins()
        horizontal_padding = margins.left() + margins.right()
        spacing = self.content_layout.horizontalSpacing()
        if spacing < 0:
            spacing = self.content_layout.spacing()

        usable_width = max(0, viewport_width - horizontal_padding)
        cell_width = max(1, SNAPSHOT_GRID_CELL_MIN_WIDTH)

        for columns in range(max_columns, 0, -1):
            required_width = (columns * cell_width) + (max(0, columns - 1) * max(0, spacing))
            if required_width <= usable_width:
                return columns

        return 1

    def _get_grid_row_count(self) -> int:
        """Return the number of rows needed for the current snapshot page size."""
        columns = self._get_grid_column_count()
        return max(1, (self.snapshot_page_size + columns - 1) // columns)

    def _configure_content_grid(self) -> None:
        """Keep the content grid dimensions aligned with the active page size."""
        columns = self._get_grid_column_count()
        rows = self._get_grid_row_count()
        max_columns = max(1, SNAPSHOT_GRID_COLUMNS)
        max_rows = max(1, self.snapshot_page_size)

        for col in range(max_columns):
            is_active = col < columns
            self.content_layout.setColumnStretch(col, 1 if is_active else 0)
            self.content_layout.setColumnMinimumWidth(
                col,
                SNAPSHOT_GRID_CELL_MIN_WIDTH if is_active else 0,
            )

        for row in range(max_rows):
            is_active = row < rows
            self.content_layout.setRowStretch(row, 1 if is_active else 0)
            self.content_layout.setRowMinimumHeight(
                row,
                SNAPSHOT_GRID_CELL_MIN_HEIGHT if is_active else 0,
            )

    def resizeEvent(self, event) -> None:
        """Reflow the thumbnail grid when the window size changes."""
        super().resizeEvent(event)
        if not hasattr(self, 'content_layout'):
            return

        self._configure_content_grid()

    def _add_full_width_content_label(self, text: str) -> None:
        """Add a centered placeholder label spanning the configured snapshot grid."""
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content_layout.addWidget(label, 0, 0, 1, self._get_grid_column_count())
        self.thumbnail_widgets.append(label)

    def init_reddit(self) -> None:
        """Initialize Reddit API connection."""
        try:
            # Load config with error handling
            config_path = CONFIG_PATH
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

            self.removal_log_moderator_allowlist = [
                str(name).strip()
                for name in config.get("removal_log_moderator_allowlist", [])
                if str(name).strip()
            ]
            self.removed_users_min_count = max(1, int(config.get("removed_users_min_count", 1) or 1))
            self.removed_users_ignored_users = {
                str(name).strip().lower()
                for name in config.get("removed_users_ignored_users", [])
                if str(name).strip()
            }
            self.removal_log_backfill_limit = max(
                1,
                int(config.get("removal_log_backfill_limit", MOD_LOG_FETCH_LIMIT) or MOD_LOG_FETCH_LIMIT),
            )

            # Verify credentials work with better error handling
            try:
                user = self.reddit.user.me()
                if user is None:
                    raise Exception("Reddit user authentication returned None")

                username = getattr(user, 'name', None)
                if not username:
                    raise Exception("Unable to retrieve username from Reddit API")

                self.authenticated_username = username
                if not self.removal_log_moderator_allowlist:
                    self.removal_log_moderator_allowlist = [username]
                RedditGalleryModel.clear_moderated_subreddit_cache(username)
                logger.info(f"Authenticated as {username}")
                self.status_bar.showMessage(f"Authenticated as {username}")

                # Fetch moderated subreddits in background
                self.fetch_moderated_subreddits()

                # Load default subreddit if specified
                if config.get("default_subreddit"):
                    try:
                        self.source_input.setText(config["default_subreddit"])
                        QTimer.singleShot(100, self.load_content)  # Defer to avoid blocking startup
                    except Exception as load_error:
                        logger.warning(f"Failed to load default subreddit: {load_error}")

            except ReadOnlyException:
                # Under PRAW 8 a missing/invalid refresh token surfaces here instead of a
                # None return. The app is always built with a refresh_token, so this means
                # credentials are bad rather than a deliberate read-only client.
                logger.exception("Reddit instance is read-only; a valid refresh token is required.")
                self.status_bar.showMessage("Authentication token expired. Please refresh token.")
                self.handle_invalid_token(config, config_path)
                return

            except Exception as e:
                error_str = str(e).lower()
                logger.exception(f"Authentication failed: {e}")

                # Handle different types of authentication errors
                if any(phrase in error_str for phrase in ["invalid_grant", "invalid refresh token", "401", "unauthorized"]):
                    self.status_bar.showMessage("Authentication token expired. Please refresh token.")
                    self.handle_invalid_token(config, config_path)
                elif any(phrase in error_str for phrase in ["403", "forbidden", "insufficient scope"]):
                    self.status_bar.showMessage("Insufficient permissions. Please check Reddit app scopes.")
                elif any(phrase in error_str for phrase in ["timeout", "connection", "network"]):
                    self.status_bar.showMessage("Network error. Please check internet connection and try again.")
                else:
                    self.status_bar.showMessage(f"Authentication failed: {str(e)}")

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
        self.moderated_subreddit_names = {
            str(subreddit.get("name") or subreddit.get("display_name") or "").strip().lower()
            for subreddit in mod_subreddits
            if str(subreddit.get("name") or subreddit.get("display_name") or "").strip()
        }
        RedditGalleryModel.set_cached_moderated_subreddit_names(
            self.authenticated_username,
            self.moderated_subreddit_names,
        )
        if self.current_model is not None:
            self.current_model.moderated_subreddit_names = set(self.moderated_subreddit_names)
            if not self.current_model.is_user_mode:
                self.current_model.check_user_moderation_status()
                self._sync_source_specific_controls()
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

        # If user moderates subreddits, start fetching mod logs in the background.
        if count > 0:
            logger.info("Moderated subreddits found. Starting background mod log fetch.")
            self._start_mod_log_fetch()
        elif count == 0:
            self.mod_log_status_label.setText("No mod logs to fetch.")
            self.mod_logs_ready = True # Mark as ready even if empty

    def _start_mod_log_fetch(self) -> None:
        """Start background mod-log fetching when moderated subreddits are available."""
        if self.mod_logs_ready or not self.moderated_subreddits:
            return

        if self.mod_log_fetcher_thread is not None and self.mod_log_fetcher_thread.isRunning():
            return

        self.mod_log_fetcher_thread = ModLogFetcher(self.reddit, self.moderated_subreddits)
        self.mod_log_fetcher_thread.modLogsReady.connect(self.on_mod_logs_ready, Qt.ConnectionType.QueuedConnection)
        self.mod_log_fetcher_thread.progressUpdate.connect(self.update_mod_log_status, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.mod_log_fetcher_thread)  # Track worker for proper cleanup
        self.mod_log_fetcher_thread.start()

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
            self.status_bar.showMessage("You don't moderate any subreddits.")
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

    def _reset_source_specific_controls(self, preserve_filter_button: bool = False) -> None:
        """Clear source-specific controls so a new load never shows stale actions."""
        self.view_reports_button.setVisible(False)
        self.view_reports_button.setEnabled(True)
        self.view_removed_button.setVisible(False)
        self.view_removed_button.setEnabled(True)
        self.view_removed_users_button.setVisible(False)
        self.view_removed_users_button.setEnabled(True)
        self.view_duplicate_media_button.setVisible(True)
        self.view_duplicate_media_button.setEnabled(True)
        self.backfill_removals_button.setVisible(False)
        self.backfill_removals_button.setEnabled(True)
        self._update_queue_button_styles()
        self.fetch_next_500_button.setEnabled(False)

        self.filter_button.setEnabled(False)
        self.filter_button.setStyleSheet("")
        if self.previous_subreddit:
            self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
        else:
            self.filter_button.setText("Filter by Subreddit")
        self.is_filtered = False

        if not preserve_filter_button:
            self.filter_button.setVisible(False)

    def _sync_source_specific_controls(self) -> None:
        """Show only the controls that apply to the currently loaded source."""
        is_mod = False
        if self.current_model and not self.current_model.is_user_mode:
            is_mod = self.current_model.check_user_moderation_status()

        self.view_reports_button.setVisible(is_mod)
        self.view_reports_button.setEnabled(True)
        self.view_removed_button.setVisible(is_mod)
        self.view_removed_button.setEnabled(True)
        self.view_removed_users_button.setVisible(is_mod)
        self.view_removed_users_button.setEnabled(True)
        self.view_duplicate_media_button.setVisible(True)
        self.view_duplicate_media_button.setEnabled(True)
        self.backfill_removals_button.setVisible(is_mod)
        self.backfill_removals_button.setEnabled(True)
        self._update_queue_button_styles()
        can_fetch_next_500 = (
            self.view_mode not in {"reports", "removed", "removed_users", "duplicate_media"} and
            len(self.current_snapshot) > 0 and
            self.can_fetch_more_posts
        )
        self.fetch_next_500_button.setEnabled(can_fetch_next_500)

        show_filter_button = bool(
            self.current_model and self.current_model.is_user_mode and self.previous_subreddit
        )
        self.filter_button.setVisible(show_filter_button)
        self.filter_button.setEnabled(show_filter_button)
        if show_filter_button:
            self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
            self.filter_button.setStyleSheet("")

    def _set_model_status_label(self) -> None:
        """Refresh the status label that describes the active source mode."""
        if not self.current_model:
            self.mod_status_label.setText("No content loaded")
            return

        if self.current_model.is_user_mode:
            self.mod_status_label.setText("User mode")
            return

        is_mod = self.current_model.check_user_moderation_status()
        self.mod_status_label.setText("Moderator" if is_mod else "Not a moderator")

    def _set_view_mode(self, mode: str, queue_name: Optional[str] = None) -> None:
        """Track the active content view so UI text derives from one authoritative state."""
        self.view_mode = mode
        self.active_queue_name = queue_name
        self._update_queue_button_styles()

    def _update_queue_button_styles(self) -> None:
        """Highlight queue buttons when their moderator-only feed is active."""
        reports_style = ""
        removed_style = ""
        removed_users_style = ""
        duplicate_media_style = ""

        if getattr(self, 'view_mode', None) == "reports":
            reports_style = "background-color: #ffe082; color: black; font-weight: bold;"
        elif getattr(self, 'view_mode', None) == "removed":
            removed_style = "background-color: #ef9a9a; color: black; font-weight: bold;"
        elif getattr(self, 'view_mode', None) == "removed_users":
            removed_users_style = "background-color: #ef9a9a; color: black; font-weight: bold;"
        elif getattr(self, 'view_mode', None) == "duplicate_media":
            duplicate_media_style = "background-color: #ef9a9a; color: black; font-weight: bold;"

        if hasattr(self, 'view_reports_button') and self.view_reports_button is not None:
            self.view_reports_button.setStyleSheet(reports_style)
        if hasattr(self, 'view_removed_button') and self.view_removed_button is not None:
            self.view_removed_button.setStyleSheet(removed_style)
        if hasattr(self, 'view_removed_users_button') and self.view_removed_users_button is not None:
            self.view_removed_users_button.setStyleSheet(removed_users_style)
        if hasattr(self, 'view_duplicate_media_button') and self.view_duplicate_media_button is not None:
            self.view_duplicate_media_button.setStyleSheet(duplicate_media_style)

    def _sync_view_mode_with_model(self) -> None:
        """Set the base view mode that matches the active model."""
        if not self.current_model:
            self._set_view_mode("empty")
        elif self.current_model.is_user_mode:
            self._set_view_mode("user")
        else:
            self._set_view_mode("subreddit")

    def _build_source_label(self) -> str:
        """Derive the source label from the active content view state."""
        if self.view_mode == "filtered_user":
            username = self.source_input.text().strip()
            return (
                f"User: {username} - Filtered by r/{self.previous_subreddit} - "
                f"{len(self.current_filtered_snapshot)} posts"
            )

        if self.view_mode in {"reports", "removed"} and self.current_model:
            queue_name = self.active_queue_name or self.view_mode.title()
            return f"Subreddit: {self.current_model.source_name} - {queue_name} ({len(self.current_snapshot)})"

        if self.view_mode == "removed_users" and self.current_model:
            return f"Subreddit: {self.current_model.source_name} - Removed Users"

        if self.view_mode == "duplicate_media":
            if self.current_model and not self.current_model.is_user_mode:
                return f"Subreddit: {self.current_model.source_name} - Duplicate Media"
            return "Duplicate Media"

        if self.current_model and self.current_model.is_user_mode:
            return f"User: {self.source_input.text().strip()} - {len(self.current_snapshot)} posts"

        if self.current_model:
            return f"Subreddit: {self.source_input.text().strip()} - {len(self.current_snapshot)} posts"

        return "No content loaded"

    def _build_empty_content_label(self) -> str:
        """Return the empty-state label for the active content view."""
        if self.view_mode == "filtered_user" and self.previous_subreddit:
            return f"No posts found in r/{self.previous_subreddit}"
        if self.view_mode == "reports":
            return "No reported posts found"
        if self.view_mode == "removed":
            return "No removed posts found"
        if self.view_mode == "removed_users":
            return "No removal log entries found"
        if self.view_mode == "duplicate_media":
            return "No duplicate media groups found"
        return "No posts found"

    def _build_page_status_message(self, start: int, end: int, total: int) -> str:
        """Return the pagination status message for the active content view."""
        if self.view_mode == "filtered_user" and self.previous_subreddit:
            return (
                f"Showing posts {start+1} to {end} of {total} "
                f"(Filtered by r/{self.previous_subreddit})"
            )
        if self.view_mode in {"reports", "removed"}:
            queue_name = (self.active_queue_name or self.view_mode.title()).lower()
            return f"Showing {queue_name} posts {start+1} to {end} of {total}"
        if self.view_mode == "removed_users":
            return f"Showing removed-user summary for {total} users"
        if self.view_mode == "duplicate_media":
            return f"Showing duplicate media summary for {total} groups"
        return f"Showing posts {start+1} to {end} of {total}"

    def _begin_source_load(
        self,
        source: str,
        is_user_mode: bool,
        *,
        reset_navigation: bool = True,
        preserve_filter_button: bool = False,
    ) -> None:
        """Prepare the UI and model for loading a new source."""
        self._cancel_content_fetch_workers()
        self.prefetched_next_batch = None
        self.prefetched_next_after = None

        self.loading_bar.show()
        self.is_loading_posts = True
        self.load_button.setEnabled(False)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)

        self.clear_content()

        if reset_navigation:
            self.back_button.setVisible(False)
            self.previous_subreddit = None
            self.previous_offset = 0

        self._reset_source_specific_controls(preserve_filter_button=preserve_filter_button)

        source_type = "User" if is_user_mode else "Subreddit"
        self.source_label.setText(f"Loading {source_type}: {source}")

        self.current_model = RedditGalleryModel(
            source,
            is_user_mode=is_user_mode,
            reddit_instance=self.reddit,
            prefetched_mod_logs=self.prefetched_mod_logs,
            mod_logs_ready=self.mod_logs_ready,
            moderated_subreddit_names=self.moderated_subreddit_names,
        )
        self._sync_view_mode_with_model()
        self._set_model_status_label()

    def _finalize_snapshot_load(
        self,
        snapshot: List[Any],
        *,
        snapshot_offset: int = 0,
    ) -> None:
        """Apply a fetched snapshot to the current view and refresh related UI state."""
        finalize_started_at = time.perf_counter()
        self.all_current_snapshot = list(snapshot)
        self.current_snapshot = self._get_visible_snapshot(self.all_current_snapshot)
        self.all_current_filtered_snapshot = []
        self.current_filtered_snapshot = []
        self._sync_view_mode_with_model()

        max_offset = max(0, len(self.current_snapshot) - 1)
        aligned_offset = min(snapshot_offset, max_offset)
        self.snapshot_offset = (aligned_offset // self.snapshot_page_size) * self.snapshot_page_size
        self.can_fetch_more_posts = len(snapshot) >= DEFAULT_POSTS_FETCH_LIMIT

        self.is_loading_posts = False
        self.load_button.setEnabled(True)
        self.loading_bar.hide()

        self._update_pagination_buttons(self.current_snapshot)
        self._sync_source_specific_controls()
        logger.info(
            "Snapshot finalize before render: source=%s raw=%s visible=%s elapsed_ms=%.1f",
            self.current_model.source_name if self.current_model else "",
            len(self.all_current_snapshot),
            len(self.current_snapshot),
            (time.perf_counter() - finalize_started_at) * 1000,
        )
        self.display_current_page()

    def _fail_snapshot_load(self, source_label: str, status_message: str) -> None:
        """Reset shared UI state after a snapshot load fails."""
        self.is_loading_posts = False
        self.loading_bar.hide()
        self.load_button.setEnabled(True)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.source_label.setText(source_label)
        self.status_bar.showMessage(status_message)

    def _show_moderator_queue_view(self, posts: List[Any], queue_name: str) -> None:
        """Display a fixed moderator queue snapshot without exposing normal feed pagination."""
        normalized_posts = list(posts)
        if queue_name.lower() == "removed":
            for post in normalized_posts:
                try:
                    setattr(post, 'moderation_status', "removed")
                    setattr(post, 'removed', True)
                    setattr(post, 'approved', False)
                except Exception:
                    pass

        self.is_filtered = False
        self.all_current_snapshot = normalized_posts
        self.current_snapshot = list(normalized_posts)
        self.all_current_filtered_snapshot = []
        self.current_filtered_snapshot = []
        self.snapshot_offset = 0
        self.current_after = None
        self.can_fetch_more_posts = False
        self._set_view_mode(queue_name.lower(), queue_name)
        self._sync_source_specific_controls()
        self.display_current_page()

    def load_content(self) -> None:
        """Load content from the specified subreddit or user."""
        source = self.source_input.text().strip()
        if not source:
            self.status_bar.showMessage("Please enter a subreddit name or username.")
            return

        # Determine if we're in subreddit or user mode
        is_user_mode = self.source_type_combo.currentIndex() == 1

        # Hide the back button ONLY when manually loading new content that's not author navigation
        was_author_navigation = False
        if hasattr(self, 'is_author_navigation') and self.is_author_navigation:
            was_author_navigation = True
        self.is_author_navigation = False # Reset flag after checking
        self._begin_source_load(
            source,
            is_user_mode,
            reset_navigation=not was_author_navigation,
            preserve_filter_button=was_author_navigation,
        )

        if is_user_mode and was_author_navigation and self.previous_subreddit:
            logger.debug(f"Ensuring back button remains visible for r/{self.previous_subreddit}")
            self.back_button.setText(f"Back to r/{self.previous_subreddit}")
            self.back_button.setVisible(True)

        # Cleanup is handled by clear_content() called within display_current_page()
        # self.stop_all_thumbnail_media() # Removed redundant call

        # Start a thread to fetch the snapshot
        self.fetch_snapshot()

    def fetch_snapshot(self) -> None:
        """Fetch a snapshot of submissions asynchronously."""
        self._start_snapshot_fetch(self.on_snapshot_fetched, self.on_snapshot_fetch_error)

    def _start_snapshot_fetch(self, success_handler, error_handler) -> None:
        """Start a snapshot fetch and route results to the provided handler."""
        if not self.current_model:
            return

        # Clean up any existing snapshot fetcher before starting new one
        if hasattr(self, 'snapshot_fetcher') and self.snapshot_fetcher is not None:
            if self.snapshot_fetcher.isRunning():
                logger.debug("Retiring previous snapshot fetcher before starting new one")
                self._retire_worker(self.snapshot_fetcher, clear_reference=False)
            self.cleanup_worker(self.snapshot_fetcher)

        # Create a thread to fetch the snapshot
        self.snapshot_fetcher = SnapshotFetcher(self.current_model)
        self.snapshot_fetcher.snapshotFetched.connect(success_handler, Qt.ConnectionType.QueuedConnection)
        self.snapshot_fetcher.snapshotFailed.connect(error_handler, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.snapshot_fetcher)  # Track worker for proper cleanup
        self.snapshot_fetcher.start()

    def on_snapshot_fetched(self, snapshot: List[Any]) -> None:
        """Handle the fetched snapshot of submissions."""
        sender = self.sender()
        if sender is not self.snapshot_fetcher:
            logger.debug("Ignoring stale snapshot fetch result")
            self.cleanup_worker(sender)
            return

        # Clean up the snapshot fetcher
        if hasattr(self, 'snapshot_fetcher'):
            self.cleanup_worker(self.snapshot_fetcher)
            self.snapshot_fetcher = None

        self._finalize_snapshot_load(snapshot)

    def on_snapshot_fetch_error(self, error_message: str) -> None:
        """Handle a failed initial snapshot load."""
        sender = self.sender()
        if sender is not None and sender is not self.snapshot_fetcher:
            logger.debug(f"Ignoring stale snapshot fetch error: {error_message}")
            self.cleanup_worker(sender)
            return

        source_type = "user" if self.current_model and self.current_model.is_user_mode else "subreddit"
        source_name = self.current_model.source_name if self.current_model else self.source_input.text().strip()

        if hasattr(self, 'snapshot_fetcher'):
            self.cleanup_worker(self.snapshot_fetcher)
            self.snapshot_fetcher = None

        logger.error(f"Failed to fetch initial snapshot for {source_type} '{source_name}': {error_message}")
        self._fail_snapshot_load(
            f"Failed to load {source_type}: {source_name}",
            error_message,
        )

    def display_current_page(self) -> None:
        """Display the current page of submissions using the configured snapshot grid."""
        self._display_snapshot_page(self.current_snapshot)

    def _is_removed_submission(self, submission: Any) -> bool:
        """Return True when cached/live moderation metadata indicates a removed post."""
        if submission is None:
            return False

        submission_fields = getattr(submission, "__dict__", {})

        if submission_fields.get('moderation_status') == "removed":
            return True

        if bool(submission_fields.get('removed', False)):
            return True

        return bool(submission_fields.get('removed_by_category'))

    def _get_visible_snapshot(
        self,
        posts: List[Any],
        *,
        view_mode: Optional[str] = None,
    ) -> List[Any]:
        """Hide removed posts in normal browsing while preserving queue-only feeds."""
        active_view_mode = view_mode or self.view_mode
        if active_view_mode in {"reports", "removed"}:
            return list(posts)

        return [post for post in posts if not self._is_removed_submission(post)]

    def _display_snapshot_page(
        self,
        snapshot: List[Any],
        *,
        empty_page_label: Optional[str] = None,
        empty_page_status: Optional[str] = None,
    ) -> None:
        """Render one page from the provided snapshot list using the active view mode."""
        self.content_widget.setUpdatesEnabled(False)
        self.scroll_area.viewport().setUpdatesEnabled(False)

        try:
            self.clear_content()
            self._configure_content_grid()
            self.source_label.setText(self._build_source_label())

            start = self.snapshot_offset
            end = min(start + self.snapshot_page_size, len(snapshot))
            current_page = snapshot[start:end]
            self._begin_page_render_tracking()

            if not snapshot:
                empty_message = self._build_empty_content_label()
                self._add_full_width_content_label(empty_message)
                self.status_bar.showMessage(empty_message)
                self._update_pagination_buttons(snapshot)
                return

            if not current_page:
                label_text = empty_page_label or self._build_empty_content_label()
                status_message = empty_page_status or label_text
                self._add_full_width_content_label(label_text)
                self.status_bar.showMessage(status_message)
                self._update_pagination_buttons(snapshot)
                return

            columns = self._get_grid_column_count()
            for i, submission in enumerate(current_page):
                row = i // columns
                col = i % columns
                try:
                    widget = self.add_submission_widget(submission, row, col)
                    self._track_page_render_widget(widget)
                except Exception as e:
                    submission_id_str = getattr(submission, 'id', 'unknown ID')
                    logger.exception(f"Error creating widget for submission {submission_id_str}: {e}")
                    try:
                        error_widget = QLabel(f"Error loading post:\n{submission_id_str}")
                        error_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
                        error_widget.setStyleSheet("background-color: #ffdddd; border: 1px solid #ffaaaa;")
                        self.content_layout.addWidget(error_widget, row, col)
                        self.thumbnail_widgets.append(error_widget)
                    except Exception as placeholder_e:
                        logger.error(f"Failed to add error placeholder: {placeholder_e}")

            self._log_page_render_build_if_ready()

            self.status_bar.showMessage(self._build_page_status_message(start, end, len(snapshot)))
            self._update_pagination_buttons(snapshot)

            if not self.is_loading_posts:
                QTimer.singleShot(2000, self.start_media_prefetch)
                QTimer.singleShot(0, self._maybe_prefetch_next_batch)

            self.cleanup_prefetch_data()
        finally:
            self.content_widget.setUpdatesEnabled(True)
            self.scroll_area.viewport().setUpdatesEnabled(True)
            self.content_widget.update()
            self.scroll_area.viewport().update()

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
                vlc_path=self.vlc_path,  # Pass the VLC path from config
                prefetch_state_getter=self.get_prefetch_state_for_url,
            )
            thumbnail.authorClicked.connect(self.on_author_clicked)
            thumbnail.moderationStateChanged.connect(
                self.on_submission_moderation_state_changed,
                Qt.ConnectionType.QueuedConnection,
            )
            if row is not None and col is not None:
                self.content_layout.addWidget(thumbnail, row, col)
            else:
                self.content_layout.addWidget(thumbnail)
            self.thumbnail_widgets.append(thumbnail)
            return thumbnail

        except Exception as e:
            logger.exception(f"Error adding widget for submission {submission_id_str}: {e}")
            if row is not None and col is not None:
                error_widget = QLabel(f"Error:\n{submission_id_str}")
                error_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
                error_widget.setStyleSheet("background-color: #ffdddd; border: 1px solid #ffaaaa;")
                self.content_layout.addWidget(error_widget, row, col)
                self.thumbnail_widgets.append(error_widget)
                return error_widget
        return None

    def _begin_page_render_tracking(self) -> None:
        """Reset per-page render timing so navigation bottlenecks are measurable."""
        self.page_render_summary_timer.stop()
        self.page_render_token += 1
        self.page_render_started_at = time.perf_counter()
        self.page_render_expected = 0
        self.page_render_ready = 0
        self.page_render_build_logged = False
        self.page_render_media_logged = False
        self.page_render_build_completed_at = 0.0
        self.page_render_summary_logged = False

    def _track_page_render_widget(self, widget: Optional[QWidget]) -> None:
        """Track visible thumbnail readiness for the current page render."""
        if not isinstance(widget, ThumbnailWidget):
            return

        render_token = self.page_render_token
        self.page_render_expected += 1
        widget.render_media_waiting_at_build = not widget.is_media_loaded
        widget.render_media_was_waiting_at_build = widget.render_media_waiting_at_build
        if widget.is_media_loaded:
            self._mark_page_render_widget_ready(widget, render_token)
            return

        widget.mediaReady.connect(
            lambda tracked_widget=widget, token=render_token:
            self._mark_page_render_widget_ready(tracked_widget, token),
            Qt.ConnectionType.QueuedConnection,
        )

    def _mark_page_render_widget_ready(self, widget: ThumbnailWidget, render_token: int) -> None:
        """Count a thumbnail as ready once for the active page render only."""
        if render_token != self.page_render_token:
            return

        if self.page_render_summary_logged:
            widget.render_media_ready_after_summary = True
            self._log_page_render_media_summary(reason="late_ready")
            return

        if getattr(widget, "_page_render_ready_token", None) == render_token:
            return

        widget._page_render_ready_token = render_token
        widget.render_media_waiting_at_build = False
        self.page_render_ready += 1
        self._log_page_render_media_if_ready()

    def on_submission_moderation_state_changed(self, submission_id: str, moderation_status: str) -> None:
        """Apply a moderation-state update to cached snapshots and refresh visible pages."""
        if not submission_id:
            return

        def _find_submission() -> Optional[Any]:
            for posts in (self.all_current_snapshot, self.all_current_filtered_snapshot, self.current_snapshot):
                for post in posts:
                    if getattr(post, 'id', None) == submission_id:
                        return post
            return None

        def _apply_status(posts: List[Any]) -> None:
            for post in posts:
                if getattr(post, 'id', None) != submission_id:
                    continue

                try:
                    setattr(post, 'moderation_status', moderation_status)
                    setattr(post, 'removed', moderation_status == "removed")
                    if moderation_status == "removed":
                        setattr(post, 'approved', False)
                    elif moderation_status == "approved":
                        setattr(post, 'approved', True)
                        setattr(post, 'removed', False)
                        setattr(post, 'removed_by_category', None)
                except Exception:
                    pass

        _apply_status(self.all_current_snapshot)
        _apply_status(self.all_current_filtered_snapshot)
        target_submission = _find_submission()

        if moderation_status == "removed" and target_submission is not None:
            subreddit_name = (
                getattr(self.current_model, 'source_name', None)
                if self.current_model and not self.current_model.is_user_mode
                else None
            )
            removal_count = record_removed_submission(target_submission, subreddit_name=subreddit_name)
            author_name = getattr(getattr(target_submission, 'author', None), 'name', None)
            if not author_name:
                author_name = str(getattr(target_submission, 'author', '[deleted]') or '[deleted]')
            logger.info(
                "User %s now has %s removed posts in r/%s",
                author_name,
                removal_count,
                subreddit_name or getattr(target_submission, 'subreddit', 'unknown'),
            )
        elif moderation_status == "approved":
            remove_approved_submission_from_removal_log(submission_id)

        self._refresh_visible_snapshots()

        if self.view_mode == "removed":
            self.current_snapshot = [
                post for post in self.all_current_snapshot
                if self._is_removed_submission(post)
            ]
            active_snapshot = self.current_snapshot
        else:
            active_snapshot = self.current_filtered_snapshot if self.is_filtered else self.current_snapshot

        if self.snapshot_offset >= len(active_snapshot):
            if active_snapshot:
                max_offset = len(active_snapshot) - 1
                self.snapshot_offset = (max_offset // self.snapshot_page_size) * self.snapshot_page_size
            else:
                self.snapshot_offset = 0

        if self.view_mode == "removed":
            self._update_pagination_buttons(active_snapshot)
            self.display_current_page()
            return

        if self.view_mode == "reports":
            return

        self._update_pagination_buttons(active_snapshot)
        if self.is_filtered:
            self.display_filtered_page()
        else:
            self.display_current_page()

    def _log_page_render_build_if_ready(self) -> None:
        """Log widget-construction cost for the current page."""
        if self.page_render_build_logged:
            return

        build_ms = (time.perf_counter() - self.page_render_started_at) * 1000
        self.page_render_build_logged = True
        self.page_render_build_completed_at = time.perf_counter()
        logger.info(
            "Page render build: token=%s widgets=%s ready=%s build_ms=%.1f",
            self.page_render_token,
            self.page_render_expected,
            self.page_render_ready,
            build_ms,
        )
        if self.page_render_ready < self.page_render_expected:
            self.page_render_summary_timer.start(self.page_render_summary_timeout_ms)
        self._log_page_render_media_if_ready()

    def _log_page_render_media_if_ready(self) -> None:
        """Log total visible-page readiness when all tracked thumbnails finish."""
        if self.page_render_media_logged:
            return

        if not self.page_render_build_logged:
            return

        if self.page_render_expected == 0:
            self.page_render_media_logged = True
            return

        if self.page_render_ready < self.page_render_expected:
            return

        total_ms = (time.perf_counter() - self.page_render_started_at) * 1000
        self.page_render_media_logged = True
        logger.info(
            "Page render ready: token=%s widgets=%s total_ms=%.1f",
            self.page_render_token,
            self.page_render_expected,
            total_ms,
        )
        self._log_page_render_media_summary(reason="all_ready")

    def _on_page_render_summary_timeout(self) -> None:
        """Log visible-media attribution even when some widgets are still pending."""
        self._log_page_render_media_summary(reason="timeout")

    def _log_page_render_media_summary(self, reason: str) -> None:
        """Emit a cache-vs-download summary for the currently visible page."""
        if not self.page_render_build_logged:
            return
        if self.page_render_summary_logged and reason != "late_ready":
            return

        if reason != "late_ready":
            self.page_render_summary_timer.stop()

        widgets = [widget for widget in self.thumbnail_widgets if isinstance(widget, ThumbnailWidget)]
        if not widgets:
            self.page_render_summary_logged = True
            return

        timed_out = 0
        late_ready = 0
        cached_before_render = 0
        prefetch_hits = 0
        foreground_downloads = 0
        waiting_at_build = 0

        for widget in widgets:
            if widget.render_media_cached_before_render:
                cached_before_render += 1
            if widget.render_media_prefetch_hit:
                prefetch_hits += 1
            if widget.render_media_started_foreground_download:
                foreground_downloads += 1
            if getattr(widget, "_page_render_ready_token", None) != self.page_render_token:
                timed_out += 1
                widget.render_media_timed_out = True
            if widget.render_media_was_waiting_at_build:
                waiting_at_build += 1
            if widget.render_media_ready_after_summary:
                late_ready += 1

        elapsed_since_build_ms = max(0.0, (time.perf_counter() - self.page_render_build_completed_at) * 1000)
        self.page_render_summary_logged = True
        logger.info(
            "render_media_summary token=%s reason=%s widgets=%s cached_before_render=%s "
            "prefetch_hits=%s foreground_downloads=%s waiting_at_build=%s timed_out=%s "
            "late_ready=%s build_to_summary_ms=%.1f",
            self.page_render_token,
            reason,
            len(widgets),
            cached_before_render,
            prefetch_hits,
            foreground_downloads,
            waiting_at_build,
            timed_out,
            late_ready,
            elapsed_since_build_ms,
        )

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

                # Explicitly close the widget to trigger its internal cleanup (workers, etc.)
                try:
                    widget.close()
                except Exception as e:
                    logger.debug(f"Error closing widget in clear_content: {e}")

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

        self.content_layout.invalidate()

        if not cleanup_success:
            logger.warning("Some widget cleanup operations failed, but widgets were removed from UI")

    def get_prefetch_state_for_url(
        self,
        original_url: str,
        processed_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return prefetched-media state for the given raw/resolved media URL pair."""
        with QMutexLocker(self.prefetch_mutex):
            existing = self._find_prefetched_media_match_locked(original_url, processed_url)
            return dict(existing) if existing else None

    def _ensure_prefetch_batch_record_locked(self, batch_id: int) -> Dict[str, int]:
        """Create or return the mutable metrics record for one prefetch batch."""
        batch_record = self.prefetch_batches.get(batch_id)
        if batch_record is None:
            batch_record = {
                'queued': 0,
                'cache_hits': 0,
                'duplicate_skips': 0,
                'retry_backoff_skips': 0,
                'retry_exhausted_skips': 0,
                'errors': 0,
                'downloads_started': 0,
                'downloads_completed': 0,
                'downloads_failed': 0,
                'downloads_retried': 0,
                'downloads_inflight': 0,
                'queue_phase_done': 0,
            }
            self.prefetch_batches[batch_id] = batch_record
        return batch_record

    def _record_prefetch_batch_scan_result(self, batch_id: int, batch_stats: Dict[str, int]) -> None:
        """Store queue-time decisions for a prefetch batch and log the initial summary."""
        with QMutexLocker(self.prefetch_mutex):
            batch_record = self._ensure_prefetch_batch_record_locked(batch_id)
            batch_record.update({
                'queued': batch_stats['queued'],
                'cache_hits': batch_stats['cache_hits'],
                'duplicate_skips': batch_stats['duplicate_skips'],
                'retry_backoff_skips': batch_stats['retry_backoff_skips'],
                'retry_exhausted_skips': batch_stats['retry_exhausted_skips'],
                'errors': batch_stats['errors'],
                'queue_phase_done': 1,
            })
            inflight = batch_record['downloads_inflight']
            completed = batch_record['downloads_completed']
            failed = batch_record['downloads_failed']
            started = batch_record['downloads_started']
            retried = batch_record['downloads_retried']
            queued_now = len(self.prefetch_download_queue)
            active_now = len(self.active_prefetch_downloads)

        logger.info(
            "prefetch_batch_done batch_id=%s submissions=%s media_urls=%s queued=%s "
            "cache_hits=%s duplicate_skips=%s retry_backoff_skips=%s "
            "retry_exhausted_skips=%s errors=%s downloads_started=%s downloads_completed=%s "
            "downloads_failed=%s downloads_retried=%s inflight=%s queued_now=%s active_now=%s",
            batch_id,
            batch_stats['submissions_considered'],
            batch_stats['media_urls_considered'],
            batch_stats['queued'],
            batch_stats['cache_hits'],
            batch_stats['duplicate_skips'],
            batch_stats['retry_backoff_skips'],
            batch_stats['retry_exhausted_skips'],
            batch_stats['errors'],
            started,
            completed,
            failed,
            retried,
            inflight,
            queued_now,
            active_now,
        )
        self._log_prefetch_batch_async_outcome_if_complete(batch_id)

    def _record_prefetch_batch_download_started_locked(self, batch_id: Optional[int]) -> None:
        """Attribute a started prefetch download to its originating batch."""
        if batch_id is None:
            return

        batch_record = self._ensure_prefetch_batch_record_locked(batch_id)
        batch_record['downloads_started'] += 1
        batch_record['downloads_inflight'] += 1

    def _record_prefetch_batch_download_finished(self, batch_id: Optional[int], *, failed: bool, retried: bool = False) -> None:
        """Attribute one completed/failed async prefetch outcome to its batch."""
        if batch_id is None:
            return

        with QMutexLocker(self.prefetch_mutex):
            batch_record = self._ensure_prefetch_batch_record_locked(batch_id)
            if batch_record['downloads_inflight'] > 0:
                batch_record['downloads_inflight'] -= 1
            if failed:
                batch_record['downloads_failed'] += 1
            else:
                batch_record['downloads_completed'] += 1
            if retried:
                batch_record['downloads_retried'] += 1

        self._log_prefetch_batch_async_outcome_if_complete(batch_id)

    def _log_prefetch_batch_async_outcome_if_complete(self, batch_id: Optional[int]) -> None:
        """Emit a final async-outcome summary once a batch has no inflight downloads left."""
        if batch_id is None:
            return

        with QMutexLocker(self.prefetch_mutex):
            batch_record = self.prefetch_batches.get(batch_id)
            if not batch_record:
                return
            if not batch_record.get('queue_phase_done'):
                return
            if batch_record['downloads_inflight'] > 0:
                return

            outcome = dict(batch_record)
            del self.prefetch_batches[batch_id]

        logger.info(
            "prefetch_batch_async_done batch_id=%s queued=%s cache_hits=%s duplicate_skips=%s "
            "retry_backoff_skips=%s retry_exhausted_skips=%s errors=%s downloads_started=%s "
            "downloads_completed=%s downloads_failed=%s downloads_retried=%s",
            batch_id,
            outcome['queued'],
            outcome['cache_hits'],
            outcome['duplicate_skips'],
            outcome['retry_backoff_skips'],
            outcome['retry_exhausted_skips'],
            outcome['errors'],
            outcome['downloads_started'],
            outcome['downloads_completed'],
            outcome['downloads_failed'],
            outcome['downloads_retried'],
        )

    def _abort_prefetch_activity(self, reason: str) -> None:
        """Retire queued/inflight prefetch bookkeeping and log each unfinished batch once."""
        self.is_shutting_down = True

        with QMutexLocker(self.prefetch_mutex):
            queued_downloads = list(self.prefetch_download_queue.values())
            active_downloads = len(self.active_prefetch_downloads)
            tracked_workers = len(self.prefetch_workers)
            batch_queue_counts = {}
            batch_records = {
                batch_id: dict(batch_record)
                for batch_id, batch_record in self.prefetch_batches.items()
            }

            for queued_download in queued_downloads:
                batch_id = queued_download.get('batch_id')
                if batch_id is None:
                    continue
                batch_queue_counts[batch_id] = batch_queue_counts.get(batch_id, 0) + 1

            self.prefetch_download_queue.clear()
            self.active_prefetch_downloads.clear()
            self.prefetch_batches.clear()
            self.prefetch_workers.clear()

        for batch_id, batch_record in batch_records.items():
            logger.warning(
                "prefetch_batch_aborted batch_id=%s reason=%s queued=%s cache_hits=%s "
                "duplicate_skips=%s retry_backoff_skips=%s retry_exhausted_skips=%s "
                "errors=%s downloads_started=%s downloads_completed=%s downloads_failed=%s "
                "downloads_retried=%s downloads_inflight=%s abandoned_queue=%s",
                batch_id,
                reason,
                batch_record.get('queued', 0),
                batch_record.get('cache_hits', 0),
                batch_record.get('duplicate_skips', 0),
                batch_record.get('retry_backoff_skips', 0),
                batch_record.get('retry_exhausted_skips', 0),
                batch_record.get('errors', 0),
                batch_record.get('downloads_started', 0),
                batch_record.get('downloads_completed', 0),
                batch_record.get('downloads_failed', 0),
                batch_record.get('downloads_retried', 0),
                batch_record.get('downloads_inflight', 0),
                batch_queue_counts.get(batch_id, 0),
            )

        if batch_records or queued_downloads or active_downloads or tracked_workers:
            logger.info(
                "prefetch_shutdown_cleanup reason=%s batches=%s queued_downloads=%s "
                "active_downloads=%s tracked_workers=%s",
                reason,
                len(batch_records),
                len(queued_downloads),
                active_downloads,
                tracked_workers,
            )

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

    def _get_post_subreddit_name(self, post) -> str:
        """Return a lowercase subreddit name for PRAW submissions and cached objects."""
        subreddit_attr = getattr(post, 'subreddit', None)
        if subreddit_attr is None:
            return ""

        display_name = getattr(subreddit_attr, 'display_name', None)
        if display_name:
            return str(display_name).lower()

        return str(subreddit_attr).lower()

    def _filter_posts_for_previous_subreddit(self, posts: List[Any]) -> List[Any]:
        """Keep only posts that belong to the previously viewed subreddit."""
        if not self.previous_subreddit:
            return []

        target_subreddit = self.previous_subreddit.lower()
        return [
            post for post in posts
            if self._get_post_subreddit_name(post) == target_subreddit
        ]

    def _refresh_visible_snapshots(self) -> None:
        """Rebuild visible snapshot lists from the full fetched sets."""
        self.current_snapshot = self._get_visible_snapshot(self.all_current_snapshot)
        self.current_filtered_snapshot = self._get_visible_snapshot(
            self.all_current_filtered_snapshot,
            view_mode="filtered_user",
        )

    def _reset_filtered_auto_fetch_state(self) -> None:
        """Clear the sparse-batch auto-fetch guard for filtered user browsing."""
        self.filtered_auto_fetch_empty_batches = 0

    def _maybe_continue_filtered_fetch(
        self,
        *,
        fetch_method,
        unique_new_posts: List[Any],
        filtered_new_posts: List[Any],
        empty_batch_message: str,
        exhausted_message: str,
    ) -> bool:
        """
        Auto-fetch additional batches in filtered user mode when a batch yields
        no visible subreddit matches.
        """
        if filtered_new_posts:
            self._reset_filtered_auto_fetch_state()
            return False

        self._update_pagination_buttons(self.current_filtered_snapshot)
        if not self.can_fetch_more_posts:
            self._reset_filtered_auto_fetch_state()
            self.status_bar.showMessage(exhausted_message)
            return False

        if not unique_new_posts:
            self._reset_filtered_auto_fetch_state()
            self.status_bar.showMessage(empty_batch_message)
            return False

        self.filtered_auto_fetch_empty_batches += 1
        if self.filtered_auto_fetch_empty_batches > 3:
            logger.info(
                "Filtered auto-fetch paused after %s empty matching batches",
                self.filtered_auto_fetch_empty_batches - 1,
            )
            self._reset_filtered_auto_fetch_state()
            self.status_bar.showMessage(empty_batch_message)
            return False

        self.status_bar.showMessage(
            f"No new posts from r/{self.previous_subreddit} yet. Searching the next batch automatically..."
        )
        logger.info(
            "Auto-fetching another filtered batch for r/%s after %s empty matching batches",
            self.previous_subreddit,
            self.filtered_auto_fetch_empty_batches,
        )
        QTimer.singleShot(0, fetch_method)
        return True

    def _update_pagination_buttons(self, current_list: List[Any]) -> None:
        """Refresh previous/next button state for the currently displayed list."""
        self.prev_button.setEnabled(self.snapshot_offset > 0)
        has_visible_next_page = self.snapshot_offset + self.snapshot_page_size < len(current_list)
        self.next_button.setEnabled(has_visible_next_page or self.can_fetch_more_posts)

    def show_next_page(self) -> None:
        """Show the next page of submissions."""
        logger.info("=== NEXT BUTTON CLICKED ===")

        # Cleanup is handled by clear_content() called within display_current_page/display_filtered_page
        # self.stop_all_thumbnail_media() # Removed redundant call

        current_list = self.current_filtered_snapshot if self.is_filtered else self.current_snapshot
        if not current_list:
            if self.can_fetch_more_posts:
                logger.debug("show_next_page: No visible posts yet, fetching additional posts")
                self.fetch_next_batch()
            logger.debug("show_next_page: No current list available")
            return

        next_offset = self.snapshot_offset + self.snapshot_page_size
        logger.info(f"show_next_page: current_offset={self.snapshot_offset}, next_offset={next_offset}, list_length={len(current_list)}")

        if next_offset < len(current_list):
            # Normal pagination within current batch
            logger.info(f"Normal pagination to offset {next_offset}")
            self.snapshot_offset = next_offset
            self._update_pagination_buttons(current_list)
            if self.is_filtered:
                self.display_filtered_page()
            else:
                self.display_current_page()
            self._reset_filtered_auto_fetch_state()
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
        self._update_pagination_buttons(current_list)
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
            self._cancel_content_fetch_workers()

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

        target_offset = self.previous_offset
        subreddit_name = self.previous_subreddit

        self.source_type_combo.setCurrentIndex(0)
        self.source_input.setText(subreddit_name)
        self._begin_source_load(subreddit_name, is_user_mode=False, reset_navigation=False)
        self.back_button.setVisible(False)

        def on_snapshot_fetched_with_restore(snapshot):
            sender = self.sender()
            if sender is not self.snapshot_fetcher:
                logger.debug("Ignoring stale restored snapshot fetch result")
                self.cleanup_worker(sender)
                return

            # Clean up the snapshot fetcher
            if hasattr(self, 'snapshot_fetcher'):
                self.cleanup_worker(self.snapshot_fetcher)
                self.snapshot_fetcher = None

            self._finalize_snapshot_load(
                snapshot,
                snapshot_offset=target_offset,
            )
            try:
                sender = self.sender()
                if sender is not None:
                    sender.snapshotFetched.disconnect(on_snapshot_fetched_with_restore)
            except (TypeError, RuntimeError):
                # Signal may not be connected or object may be deleted
                pass

        def on_snapshot_fetch_error(error_message: str):
            """Handle errors during snapshot fetching."""
            sender = self.sender()
            if sender is not None and sender is not self.snapshot_fetcher:
                logger.debug(f"Ignoring stale restored snapshot fetch error: {error_message}")
                self.cleanup_worker(sender)
                return

            if hasattr(self, 'snapshot_fetcher'):
                self.cleanup_worker(self.snapshot_fetcher)
                self.snapshot_fetcher = None

            logger.error(f"Failed to fetch snapshot for subreddit {subreddit_name}: {error_message}")
            self._fail_snapshot_load(
                f"Failed to load subreddit: {subreddit_name}",
                error_message,
            )

        try:
            self._start_snapshot_fetch(on_snapshot_fetched_with_restore, on_snapshot_fetch_error)
        except Exception as e:
            logger.exception(f"Error starting snapshot fetcher for {subreddit_name}: {e}")
            on_snapshot_fetch_error("Failed to start subreddit snapshot fetch.")

    def open_ban_dialog(self, username, subreddit_obj, default_reason: str = ""): # Accept subreddit object
        """Open the ban user dialog for moderators."""
        # No need for the checks here as they are done in on_author_clicked
        subreddit_name = subreddit_obj.display_name # Get name from object
        dialog = BanUserDialog(username, subreddit_name, self)
        if default_reason:
            dialog.reason_input.setText(default_reason)
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
        self._abort_prefetch_activity("close_event")
        self._log_prefetch_stats("shutdown")
        QThreadPool.globalInstance().clear()

        # Safely terminate all workers with timeouts
        workers_to_terminate = [
            ('snapshot_fetcher', getattr(self, 'snapshot_fetcher', None)),
            ('mod_log_fetcher_thread', getattr(self, 'mod_log_fetcher_thread', None)),
            ('filter_worker_thread', getattr(self, 'filter_worker_thread', None)),
            ('ban_worker', getattr(self, 'ban_worker', None)),
            ('next_batch_fetcher', getattr(self, 'next_batch_fetcher', None)),
            ('next_500_fetcher', getattr(self, 'next_500_fetcher', None)),
            ('reports_fetcher', getattr(self, 'reports_fetcher', None)),
            ('removed_fetcher', getattr(self, 'removed_fetcher', None)),
            ('banned_users_fetcher', getattr(self, 'banned_users_fetcher', None)),
            ('removed_user_ban_verification_fetcher', getattr(self, 'removed_user_ban_verification_fetcher', None)),
            ('removal_backfill_fetcher', getattr(self, 'removal_backfill_fetcher', None)),
            ('duplicate_media_fetcher', getattr(self, 'duplicate_media_fetcher', None))
        ]

        # First terminate known workers
        for name, worker in workers_to_terminate:
            if worker is not None and worker.isRunning():
                logger.debug(f"Retiring {name}")
                self._retire_worker(worker, clear_reference=False)

        # Terminate any other active workers cleanly
        active_workers_copy = self.get_active_workers_copy()  # Thread-safe copy
        for worker in active_workers_copy:
            if worker is not None and worker.isRunning():
                worker_name = type(worker).__name__
                logger.debug(f"Retiring active worker: {worker_name}")
                self._retire_worker(worker, clear_reference=False)

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
                self._reset_filtered_auto_fetch_state()
                self.filter_button.setText(f"Remove Filter")
                self.filter_button.setStyleSheet("background-color: #ffc107; color: black;")
                self._set_view_mode("filtered_user")
                self.source_label.setText(f"Filtering posts by r/{self.previous_subreddit}...")
                QApplication.processEvents() # Allow label update

                # Start background filtering
                self.filter_worker_thread = FilterWorker(self.all_current_snapshot, self.previous_subreddit.lower())
                self.filter_worker_thread.filteringComplete.connect(self._on_filtering_complete, Qt.ConnectionType.QueuedConnection)
                self.add_worker(self.filter_worker_thread)  # Track worker for proper cleanup
                self.filter_worker_thread.start()

            else: # Removing filter
                self._reset_filtered_auto_fetch_state()
                self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
                self.filter_button.setStyleSheet("")
                self._set_view_mode("user")
                self.snapshot_offset = 0
                self.all_current_filtered_snapshot = []
                self.current_filtered_snapshot = []
                self._update_pagination_buttons(self.current_snapshot)
                self.display_current_page() # Display original unfiltered content
                # Reset UI state after displaying
                self.is_loading_posts = False
                self.loading_bar.hide()
                self.filter_button.setEnabled(True)
                self.load_button.setEnabled(True)

        except Exception as e:
            logger.exception(f"Error initiating filtering: {e}")
            self.status_bar.showMessage(f"Filtering error: {str(e)}")

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
        sender = self.sender()
        if sender is not self.filter_worker_thread:
            logger.debug("Ignoring stale filtering result")
            self.cleanup_worker(sender)
            return

        try:
            # Clean up the filter worker
            if hasattr(self, 'filter_worker_thread'):
                self.cleanup_worker(self.filter_worker_thread)
                self.filter_worker_thread = None

            self.all_current_filtered_snapshot = list(filtered_snapshot)
            self.current_filtered_snapshot = self._get_visible_snapshot(
                self.all_current_filtered_snapshot,
                view_mode="filtered_user",
            )
            self._set_view_mode("filtered_user")
            self.snapshot_offset = 0
            self._reset_filtered_auto_fetch_state()
            self._update_pagination_buttons(self.current_filtered_snapshot)
            self.display_filtered_page() # Display the filtered content
        except Exception as e:
             logger.exception(f"Error processing filtered results: {e}")
             self.status_bar.showMessage(f"Error displaying filtered results: {str(e)}")
             # Attempt to revert UI state
             self.filter_button.setText(f"Filter by r/{self.previous_subreddit}")
             self.filter_button.setStyleSheet("")
             self.is_filtered = False
             self._set_view_mode("user")
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
        self._set_view_mode("filtered_user")
        page_number = (self.snapshot_offset // self.snapshot_page_size) + 1
        self._display_snapshot_page(
            self.current_filtered_snapshot,
            empty_page_label="No posts found on this page",
            empty_page_status=f"Filtered by r/{self.previous_subreddit}: Page {page_number} empty",
        )

    # --- Generic Worker Finished Handler ---
    def on_worker_finished(self) -> None:
        """Remove finished worker from tracking list."""
        sender = self.sender()
        self.cleanup_worker(sender)  # Use thread-safe cleanup method
        # Specific worker references are cleared in their success/error handlers

    def _retire_worker(self, worker, clear_reference: bool = True) -> None:
        """Disconnect a worker from the UI and request cooperative shutdown."""
        if worker is None:
            return

        try:
            worker.requestInterruption()
        except Exception:
            pass

        signal_names = (
            'snapshotFetched', 'snapshotFailed', 'subredditsFetched', 'modLogsReady', 'progressUpdate',
            'reportsFetched', 'removedPostsFetched', 'bannedUsersFetched', 'backfillFinished',
            'banStatusVerified', 'duplicateMediaReady', 'errorOccurred', 'filteringComplete'
        )
        for signal_name in signal_names:
            signal = getattr(worker, signal_name, None)
            if signal is None:
                continue
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass

        worker_signals = getattr(worker, 'signals', None)
        if worker_signals is not None:
            for signal_name in ('success', 'error', 'finished', 'progress'):
                signal = getattr(worker_signals, signal_name, None)
                if signal is None:
                    continue
                try:
                    signal.disconnect()
                except (TypeError, RuntimeError):
                    pass

        if clear_reference:
            for attr_name in (
                'snapshot_fetcher', 'mod_log_fetcher_thread', 'filter_worker_thread',
                'ban_worker', 'next_batch_fetcher', 'next_prefetch_fetcher', 'next_500_fetcher',
                'reports_fetcher', 'removed_fetcher', 'banned_users_fetcher',
                'removed_user_ban_verification_fetcher', 'removal_backfill_fetcher',
                'duplicate_media_fetcher'
            ):
                if getattr(self, attr_name, None) is worker:
                    setattr(self, attr_name, None)
                    break

    def _cancel_content_fetch_workers(self, exclude: tuple[str, ...] = ()) -> None:
        """Retire content-loading workers so stale results cannot update the active view."""
        content_worker_names = (
            'snapshot_fetcher',
            'next_batch_fetcher',
            'next_prefetch_fetcher',
            'next_500_fetcher',
            'reports_fetcher',
            'removed_fetcher',
            'banned_users_fetcher',
            'removed_user_ban_verification_fetcher',
            'removal_backfill_fetcher',
            'duplicate_media_fetcher',
            'filter_worker_thread',
        )

        for attr_name in content_worker_names:
            if attr_name in exclude:
                continue

            worker = getattr(self, attr_name, None)
            if worker is None:
                continue

            try:
                is_running = worker.isRunning()
            except RuntimeError:
                is_running = False

            if is_running:
                logger.debug(f"Retiring active content worker: {attr_name}")
                self._retire_worker(worker, clear_reference=False)

            self.cleanup_worker(worker)
            setattr(self, attr_name, None)

        if 'next_prefetch_fetcher' not in exclude:
            self.prefetched_next_batch = None
            self.prefetched_next_after = None

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
        self.status_bar.showMessage(f"Ban successful: {success_message}")

        worker = getattr(self, 'ban_worker', None)
        if worker is not None:
            username = getattr(worker, 'username', None)
            subreddit_obj = getattr(worker, 'subreddit', None)
            if self.view_mode == "removed_users" and username and subreddit_obj is not None:
                self._start_removed_user_ban_verification(username, subreddit_obj)

        # Clean up the ban worker (note: on_worker_finished will also be called)
        if hasattr(self, 'ban_worker') and self.ban_worker:
            self.cleanup_worker(self.ban_worker)
        self.ban_worker = None # Clear specific worker reference

    def on_ban_error(self, error_message):
        """Handle error signal from ban worker."""
        logger.error(f"Ban failed: {error_message}")
        self.status_bar.showMessage(f"Ban failed: {error_message}")

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

        self._cancel_content_fetch_workers(exclude=('next_prefetch_fetcher',))

        # Temporarily disable next button to prevent double-clicking
        self.next_button.setEnabled(False)
        self.status_bar.showMessage("Fetching next batch of posts...")

        # Get the fullname of the last post in the current snapshot
        last_fullname = self._get_last_fullname(self.all_current_snapshot or self.current_snapshot)

        if not last_fullname:
            logger.error("Cannot fetch next batch: unable to get fullname of last post")
            self.next_button.setEnabled(True)
            return

        if self.prefetched_next_after == last_fullname and self.prefetched_next_batch is not None:
            logger.info("Using prefetched next batch after post: %s", last_fullname)
            prefetched_posts = self.prefetched_next_batch
            self.prefetched_next_batch = None
            self.prefetched_next_after = None
            if self.next_prefetch_fetcher is not None:
                self.cleanup_worker(self.next_prefetch_fetcher)
                self.next_prefetch_fetcher = None
            QTimer.singleShot(0, lambda posts=prefetched_posts: self._integrate_next_batch(posts))
            return

        if self.next_prefetch_fetcher is not None:
            self._retire_worker(self.next_prefetch_fetcher)
            self.next_prefetch_fetcher = None
            self.prefetched_next_batch = None
            self.prefetched_next_after = None

        logger.info(f"Starting fetch for next batch after post: {last_fullname}")

        # Show loading indicator
        self.loading_bar.show()
        self.is_loading_posts = True

        # Start a thread to fetch the next batch (100 posts)
        self.next_batch_fetcher = SnapshotFetcher(self.current_model, total=100, after=last_fullname)
        self.next_batch_fetcher.snapshotFetched.connect(self.on_next_batch_fetched, Qt.ConnectionType.QueuedConnection)
        self.next_batch_fetcher.snapshotFailed.connect(self.on_next_batch_error, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.next_batch_fetcher)  # Track worker for proper cleanup
        self.next_batch_fetcher.start()
        logger.debug("SnapshotFetcher started for next batch")

    def _get_last_fullname(self, posts: List[Any]) -> Optional[str]:
        """Return the fullname for the last post in a list, constructing it from id if needed."""
        if not posts:
            return None

        last_post = posts[-1]
        last_fullname = getattr(last_post, "fullname", None)
        if last_fullname:
            return last_fullname

        post_id = getattr(last_post, "id", None)
        if post_id:
            return f"t3_{post_id}"

        return None

    def _maybe_prefetch_next_batch(self) -> None:
        """Fetch the next 100 posts before the user reaches the end of the current batch."""
        if (
            not self.current_model
            or not self.current_snapshot
            or not self.can_fetch_more_posts
            or self.is_loading_posts
            or self.view_mode in {"reports", "removed"}
        ):
            return

        remaining_loaded_posts = len(self.current_snapshot) - (self.snapshot_offset + self.snapshot_page_size)
        if remaining_loaded_posts > self.post_prefetch_buffer_size:
            return
        if remaining_loaded_posts >= self.max_auto_prefetched_posts:
            return

        last_fullname = self._get_last_fullname(self.all_current_snapshot or self.current_snapshot)
        if not last_fullname:
            return

        if self.prefetched_next_after == last_fullname and self.prefetched_next_batch is not None:
            return

        if self.next_prefetch_fetcher is not None and self.next_prefetch_fetcher.isRunning():
            return

        logger.info("Starting background prefetch for next batch after post: %s", last_fullname)
        self.prefetched_next_after = last_fullname
        self.prefetched_next_batch = None
        self.next_prefetch_fetcher = SnapshotFetcher(self.current_model, total=100, after=last_fullname)
        self.next_prefetch_fetcher.snapshotFetched.connect(
            self.on_next_prefetch_fetched,
            Qt.ConnectionType.QueuedConnection,
        )
        self.next_prefetch_fetcher.snapshotFailed.connect(
            self.on_next_prefetch_error,
            Qt.ConnectionType.QueuedConnection,
        )
        self.add_worker(self.next_prefetch_fetcher)
        self.next_prefetch_fetcher.start()

    def on_next_prefetch_fetched(self, posts: List[Any]) -> None:
        """Append a background-fetched batch so later Next clicks stay local."""
        sender = self.sender()
        if sender is not self.next_prefetch_fetcher:
            logger.debug("Ignoring stale next-prefetch result")
            self.cleanup_worker(sender)
            return

        existing_ids = {
            getattr(post, "id", None)
            for post in self.all_current_snapshot
            if getattr(post, "id", None) is not None
        }
        unique_new_posts = [
            post for post in posts
            if getattr(post, "id", None) not in existing_ids
        ]

        if unique_new_posts:
            self.all_current_snapshot.extend(unique_new_posts)
            if self.is_filtered and self.current_model and self.current_model.is_user_mode:
                self.all_current_filtered_snapshot.extend(
                    self._filter_posts_for_previous_subreddit(unique_new_posts)
                )
            self._refresh_visible_snapshots()

        self.can_fetch_more_posts = len(posts) >= DEFAULT_POSTS_FETCH_LIMIT
        if posts:
            last_fullname = self._get_last_fullname(list(posts))
            if last_fullname:
                self.current_after = last_fullname

        self.prefetched_next_batch = None
        self.prefetched_next_after = None
        if self.next_prefetch_fetcher is not None:
            self.cleanup_worker(self.next_prefetch_fetcher)
            self.next_prefetch_fetcher = None
        self._update_pagination_buttons(self.current_filtered_snapshot if self.is_filtered else self.current_snapshot)
        logger.info(
            "Next batch prefetch complete. Appended %s unique posts; loaded visible posts=%s.",
            len(unique_new_posts),
            len(self.current_snapshot),
        )
        QTimer.singleShot(0, self._maybe_prefetch_next_batch)

    def on_next_prefetch_error(self, error_message: str) -> None:
        """Drop failed next-batch prefetches; foreground fetch still handles errors."""
        sender = self.sender()
        if sender is not None and sender is not self.next_prefetch_fetcher:
            logger.debug("Ignoring stale next-prefetch error: %s", error_message)
            self.cleanup_worker(sender)
            return

        logger.debug("Next batch prefetch failed: %s", error_message)
        self.prefetched_next_batch = None
        self.prefetched_next_after = None
        if self.next_prefetch_fetcher is not None:
            self.cleanup_worker(self.next_prefetch_fetcher)
            self.next_prefetch_fetcher = None

    # --- Fetch Next 500 Logic ---
    def fetch_next_500(self) -> None:
        """Fetch the next 500 posts after the last currently loaded post."""
        if not self.current_model or not self.current_snapshot:
            return

        self._cancel_content_fetch_workers()

        # Get the fullname of the last post in the current snapshot
        last_post = self.current_snapshot[-1]
        last_fullname = getattr(last_post, "fullname", None)
        if not last_fullname:
            # Try to construct fullname from id
            post_id = getattr(last_post, "id", None)
            if post_id:
                last_fullname = f"t3_{post_id}"
            else:
                self.status_bar.showMessage("Error: Could not determine the last post's fullname for fetching next 500.")
                return

        # Show loading indicator
        self.loading_bar.show()
        self.is_loading_posts = True
        self.fetch_next_500_button.setEnabled(False)

        # Start a thread to fetch the next 500 posts
        self.next_500_fetcher = SnapshotFetcher(self.current_model, total=POSTS_FETCH_LIMIT, after=last_fullname)
        self.next_500_fetcher.snapshotFetched.connect(self.on_next_500_fetched, Qt.ConnectionType.QueuedConnection)
        self.next_500_fetcher.snapshotFailed.connect(self.on_next_500_error, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.next_500_fetcher)  # Track worker for proper cleanup
        self.next_500_fetcher.start()

    # --- View Reports Logic ---
    def view_reports(self) -> None:
        """Fetch and display up to 500 posts from the mod reports queue."""
        if not self.current_model or self.current_model.is_user_mode or not hasattr(self.current_model, "subreddit"):
            return

        self._cancel_content_fetch_workers()

        self.loading_bar.show()
        self.is_loading_posts = True
        self.view_reports_button.setEnabled(False)

        # Create worker thread for fetching reports
        self.reports_fetcher = ReportsFetcher(self.current_model.subreddit)
        self.reports_fetcher.reportsFetched.connect(self.on_reports_fetched, Qt.ConnectionType.QueuedConnection)
        self.reports_fetcher.errorOccurred.connect(self.on_reports_error, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.reports_fetcher)
        self.reports_fetcher.start()

    def on_reports_fetched(self, reports) -> None:
        """Handle successfully fetched reports."""
        sender = self.sender()
        if sender is not self.reports_fetcher:
            logger.debug("Ignoring stale reports fetch result")
            self.cleanup_worker(sender)
            return

        # Clean up the reports fetcher
        if hasattr(self, 'reports_fetcher'):
            self.cleanup_worker(self.reports_fetcher)
            self.reports_fetcher = None

        self._show_moderator_queue_view(reports, "Reports")
        self.status_bar.showMessage(f"Showing up to 500 reported posts")
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.view_reports_button.setEnabled(True)

    def on_reports_error(self, error_message: str) -> None:
        """Handle reports fetching error."""
        sender = self.sender()
        if sender is not self.reports_fetcher:
            logger.debug(f"Ignoring stale reports fetch error: {error_message}")
            self.cleanup_worker(sender)
            return

        # Clean up the reports fetcher
        if hasattr(self, 'reports_fetcher'):
            self.cleanup_worker(self.reports_fetcher)
            self.reports_fetcher = None

        logger.error(f"Failed to fetch reports: {error_message}")
        self.status_bar.showMessage(f"Failed to fetch reports: {error_message}")
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.view_reports_button.setEnabled(True)

    # --- View Removed Logic ---
    def view_removed(self) -> None:
        """Fetch and display up to 500 removed posts (using mod log)."""
        if not self.current_model or self.current_model.is_user_mode or not hasattr(self.current_model, "subreddit"):
            return

        self._cancel_content_fetch_workers()

        self.loading_bar.show()
        self.is_loading_posts = True
        self.view_removed_button.setEnabled(False)

        # Create worker thread for fetching removed posts
        self.removed_fetcher = RemovedPostsFetcher(self.current_model.subreddit)
        self.removed_fetcher.removedPostsFetched.connect(self.on_removed_fetched, Qt.ConnectionType.QueuedConnection)
        self.removed_fetcher.errorOccurred.connect(self.on_removed_error, Qt.ConnectionType.QueuedConnection)
        self.add_worker(self.removed_fetcher)
        self.removed_fetcher.start()

    def on_removed_fetched(self, removed_posts) -> None:
        """Handle successfully fetched removed posts."""
        sender = self.sender()
        if sender is not self.removed_fetcher:
            logger.debug("Ignoring stale removed-posts fetch result")
            self.cleanup_worker(sender)
            return

        # Clean up the removed fetcher
        if hasattr(self, 'removed_fetcher'):
            self.cleanup_worker(self.removed_fetcher)
            self.removed_fetcher = None

        self._show_moderator_queue_view(removed_posts, "Removed")
        self.status_bar.showMessage(f"Showing up to 500 removed posts")
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.view_removed_button.setEnabled(True)

    def on_removed_error(self, error_message: str) -> None:
        """Handle removed posts fetching error."""
        sender = self.sender()
        if sender is not self.removed_fetcher:
            logger.debug(f"Ignoring stale removed-posts fetch error: {error_message}")
            self.cleanup_worker(sender)
            return

        # Clean up the removed fetcher
        if hasattr(self, 'removed_fetcher'):
            self.cleanup_worker(self.removed_fetcher)
            self.removed_fetcher = None

        logger.error(f"Failed to fetch removed posts: {error_message}")
        self.status_bar.showMessage(f"Failed to fetch removed posts: {error_message}")
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.view_removed_button.setEnabled(True)

    # --- Removed Users Review ---
    def backfill_removal_log(self) -> None:
        """Backfill local removal counts from Reddit's moderator log."""
        if not self.current_model or self.current_model.is_user_mode or not hasattr(self.current_model, "subreddit"):
            return

        moderators = [
            str(name).strip()
            for name in self.removal_log_moderator_allowlist
            if str(name).strip()
        ]
        if not moderators and self.authenticated_username:
            moderators = [self.authenticated_username]

        if not moderators:
            self.status_bar.showMessage("No removal-log moderators configured for backfill.")
            return

        self._cancel_content_fetch_workers(exclude=('removal_backfill_fetcher',))
        self.backfill_removals_button.setEnabled(False)
        self.loading_bar.show()
        self.is_loading_posts = True
        self.status_bar.showMessage(
            f"Backfilling removals from mod log for r/{self.current_model.source_name}..."
        )

        self.removal_backfill_fetcher = RemovalLogBackfillWorker(
            self.current_model.subreddit,
            moderators,
            self.removal_log_backfill_limit,
        )
        self.removal_backfill_fetcher.backfillFinished.connect(
            self.on_removal_backfill_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        self.removal_backfill_fetcher.errorOccurred.connect(
            self.on_removal_backfill_error,
            Qt.ConnectionType.QueuedConnection,
        )
        self.add_worker(self.removal_backfill_fetcher)
        self.removal_backfill_fetcher.start()

    def on_removal_backfill_finished(self, stats: Dict[str, Any]) -> None:
        """Refresh removed-users view after mod-log backfill completes."""
        sender = self.sender()
        if sender is not self.removal_backfill_fetcher:
            logger.debug("Ignoring stale removal-backfill result")
            self.cleanup_worker(sender)
            return

        self.cleanup_worker(self.removal_backfill_fetcher)
        self.removal_backfill_fetcher = None
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.backfill_removals_button.setEnabled(True)

        message = (
            f"Backfill complete: {stats.get('removals_recorded', 0)} removals, "
            f"{stats.get('approvals_applied', 0)} approvals applied."
        )
        self.status_bar.showMessage(message)
        logger.info("Removal backfill complete: %s", stats)
        self.view_removed_users()

    def on_removal_backfill_error(self, error_message: str) -> None:
        """Handle mod-log backfill errors."""
        sender = self.sender()
        if sender is not self.removal_backfill_fetcher:
            logger.debug("Ignoring stale removal-backfill error: %s", error_message)
            self.cleanup_worker(sender)
            return

        self.cleanup_worker(self.removal_backfill_fetcher)
        self.removal_backfill_fetcher = None
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.backfill_removals_button.setEnabled(True)
        self.status_bar.showMessage(f"Removal backfill failed: {error_message}")

    # --- Duplicate Media Review ---
    def view_duplicate_media(self) -> None:
        """Build and display cached media used by multiple distinct authors."""
        self._start_duplicate_media_scan(rebuild_index=False)

    def _refresh_duplicate_media_index(self) -> None:
        """Force a rebuild of the duplicate media index."""
        self._start_duplicate_media_scan(rebuild_index=True)

    def _start_duplicate_media_scan(self, rebuild_index: bool = False) -> None:
        """Load or rebuild cached media duplicates in a worker."""
        self._cancel_content_fetch_workers(exclude=('duplicate_media_fetcher',))
        self.loading_bar.show()
        self.is_loading_posts = True
        self.view_duplicate_media_button.setEnabled(False)
        self._set_view_mode("duplicate_media", "Duplicate Media")
        self._sync_source_specific_controls()
        self.source_label.setText(self._build_source_label())
        self.clear_content()
        self._configure_content_grid()
        if rebuild_index or not os.path.exists(get_media_usage_index_path()):
            loading_text = "Building duplicate media index..."
            status_text = "Hashing cached media and finding multi-user duplicates..."
        else:
            loading_text = "Loading duplicate media index..."
            status_text = "Loading cached duplicate media results..."
        self._add_full_width_content_label(loading_text)
        self.status_bar.showMessage(status_text)

        subreddit_name = (
            self.current_model.source_name
            if self.current_model and not self.current_model.is_user_mode
            else None
        )
        self.duplicate_media_fetcher = DuplicateMediaIndexWorker(
            subreddit_name,
            current_subreddit_only=False,
            rebuild_index=rebuild_index,
        )
        self.duplicate_media_fetcher.duplicateMediaReady.connect(
            self.on_duplicate_media_ready,
            Qt.ConnectionType.QueuedConnection,
        )
        self.duplicate_media_fetcher.errorOccurred.connect(
            self.on_duplicate_media_error,
            Qt.ConnectionType.QueuedConnection,
        )
        self.add_worker(self.duplicate_media_fetcher)
        self.duplicate_media_fetcher.start()

    def on_duplicate_media_ready(self, groups: List[Dict[str, Any]], stats: Dict[str, Any]) -> None:
        """Display duplicate media groups after the index worker completes."""
        sender = self.sender()
        if sender is not self.duplicate_media_fetcher:
            logger.debug("Ignoring stale duplicate-media result")
            self.cleanup_worker(sender)
            return

        self.cleanup_worker(self.duplicate_media_fetcher)
        self.duplicate_media_fetcher = None
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.view_duplicate_media_button.setEnabled(True)

        self.duplicate_media_groups = groups or []
        self._display_duplicate_media_page(self.duplicate_media_groups)
        if stats.get("rebuilt_index"):
            self.status_bar.showMessage(
                f"Duplicate media: {len(self.duplicate_media_groups)} groups, "
                f"{stats.get('hashed_files', 0)} files hashed, "
                f"{stats.get('visual_hashed_images', 0)} image visual hashes."
            )
        else:
            self.status_bar.showMessage(
                f"Duplicate media: {len(self.duplicate_media_groups)} groups loaded from index."
            )

    def on_duplicate_media_error(self, error_message: str) -> None:
        """Handle duplicate-media index errors."""
        sender = self.sender()
        if sender is not self.duplicate_media_fetcher:
            logger.debug("Ignoring stale duplicate-media error: %s", error_message)
            self.cleanup_worker(sender)
            return

        self.cleanup_worker(self.duplicate_media_fetcher)
        self.duplicate_media_fetcher = None
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.view_duplicate_media_button.setEnabled(True)
        self.status_bar.showMessage(f"Duplicate media scan failed: {error_message}")

    def _display_duplicate_media_page(self, groups: List[Dict[str, Any]]) -> None:
        """Render the duplicate media review table."""
        self.content_widget.setUpdatesEnabled(False)
        self.scroll_area.viewport().setUpdatesEnabled(False)

        try:
            self.duplicate_media_table = None
            self.duplicate_media_view_author_button = None
            self.duplicate_media_ban_author_button = None
            self.duplicate_media_refresh_button = None
            self.duplicate_media_rows = self._build_duplicate_media_rows(groups)

            self.clear_content()
            self._configure_content_grid()
            self.source_label.setText(self._build_source_label())
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.fetch_next_500_button.setEnabled(False)

            page_widget = QWidget(self.content_widget)
            page_layout = QVBoxLayout(page_widget)
            page_layout.setContentsMargins(0, 0, 0, 0)
            page_layout.setSpacing(6)

            actions_widget = QWidget(page_widget)
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            actions_layout.setSpacing(6)

            refresh_button = QPushButton("Refresh Index", actions_widget)
            refresh_button.clicked.connect(self._refresh_duplicate_media_index)
            actions_layout.addWidget(refresh_button)

            view_button = QPushButton("View Author Posts", actions_widget)
            view_button.clicked.connect(self._view_selected_duplicate_media_author)
            actions_layout.addWidget(view_button)

            ban_button = QPushButton("Ban Author", actions_widget)
            ban_button.clicked.connect(self._ban_selected_duplicate_media_author)
            actions_layout.addWidget(ban_button)
            actions_layout.addStretch(1)
            page_layout.addWidget(actions_widget)

            self.duplicate_media_refresh_button = refresh_button
            self.duplicate_media_view_author_button = view_button
            self.duplicate_media_ban_author_button = ban_button

            if not self.duplicate_media_rows:
                empty_label = QLabel("No cached media posted by multiple users")
                empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                page_layout.addWidget(empty_label)
                self._update_duplicate_media_action_buttons()
                self.content_layout.addWidget(page_widget, 0, 0, 1, self._get_grid_column_count())
                self.thumbnail_widgets.append(page_widget)
                return

            table = QTableWidget(len(self.duplicate_media_rows), 7, page_widget)
            table.setHorizontalHeaderLabels((
                "Match", "Author", "Author Posts", "Other Authors",
                "Subreddits", "Recent Posts", "Media"
            ))
            table.setAlternatingRowColors(True)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setWordWrap(False)
            table.setTextElideMode(Qt.TextElideMode.ElideRight)
            table.setMinimumHeight(max(360, self.scroll_area.viewport().height() - 80))
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(34)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
            table.cellDoubleClicked.connect(self._on_duplicate_media_cell_double_clicked)
            table.itemSelectionChanged.connect(self._update_duplicate_media_action_buttons)

            for row, row_data in enumerate(self.duplicate_media_rows):
                self._populate_duplicate_media_table_row(table, row, row_data)

            self.duplicate_media_table = table
            table.selectRow(0)
            page_layout.addWidget(table)
            self._update_duplicate_media_action_buttons()

            self.content_layout.addWidget(page_widget, 0, 0, 1, self._get_grid_column_count())
            self.thumbnail_widgets.append(page_widget)
        finally:
            self.content_widget.setUpdatesEnabled(True)
            self.scroll_area.viewport().setUpdatesEnabled(True)
            self.content_widget.update()
            self.scroll_area.viewport().update()

    def _build_duplicate_media_rows(self, groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Flatten duplicate media groups into one row per author per media item."""
        rows = []
        for group in groups:
            posts = [post for post in group.get("posts", []) if isinstance(post, dict)]
            author_posts = OrderedDict()
            for post in posts:
                author = str(post.get("author") or "[deleted]").strip()
                if not author or author.lower() in {"[deleted]", "unknown", "none"}:
                    continue
                author_posts.setdefault(author, []).append(post)

            authors = list(author_posts.keys())
            if len(authors) < 2:
                continue

            for author, posts_for_author in author_posts.items():
                other_authors = [name for name in authors if name.lower() != author.lower()]
                rows.append({
                    "group": group,
                    "author": author,
                    "author_posts": posts_for_author,
                    "other_authors": other_authors,
                    "all_posts": posts,
                })

        rows.sort(
            key=lambda item: (
                int(item["group"].get("author_count") or 0),
                int(item["group"].get("post_count") or 0),
                len(item.get("author_posts") or []),
            ),
            reverse=True,
        )
        return rows

    def _populate_duplicate_media_table_row(self, table: QTableWidget, row: int, row_data: Dict[str, Any]) -> None:
        """Populate one duplicate-media review row."""
        group = row_data.get("group") or {}
        author = row_data.get("author") or ""
        author_posts = row_data.get("author_posts") or []
        all_posts = row_data.get("all_posts") or []
        other_authors = row_data.get("other_authors") or []
        media_sha256 = group.get("media_sha256") or ""
        image_visual_hash = group.get("image_visual_hash") or ""
        media_url = group.get("media_url") or ""
        media_label = media_sha256[:16] if media_sha256 else (image_visual_hash or media_url)
        match_type = group.get('match_type', '')
        if match_type == "visual_image":
            match_label = f"VISUAL <= {group.get('visual_distance_threshold', 0)} {group.get('author_count', 0)} users"
        else:
            match_label = f"{match_type.upper()} {group.get('author_count', 0)} users"

        values = (
            match_label,
            author,
            str(len(author_posts)),
            ", ".join(other_authors[:4]) + ("..." if len(other_authors) > 4 else ""),
            ", ".join(sorted({str(post.get('subreddit') or '') for post in all_posts if post.get('subreddit')})[:5]),
            self._build_duplicate_media_posts_summary(author_posts),
            media_label,
        )

        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            if col == 1:
                item.setData(Qt.ItemDataRole.UserRole, row)
            if col in {0, 2}:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if col in {5, 6}:
                item.setToolTip(value)
            table.setItem(row, col, item)

    def _build_duplicate_media_posts_summary(self, posts: List[Dict[str, Any]]) -> str:
        """Build compact post details for duplicate-media table rows."""
        lines = []
        for post in posts[:4]:
            subreddit = post.get("subreddit") or ""
            title = (post.get("title") or "").replace("\n", " ").strip()
            if len(title) > 90:
                title = title[:87] + "..."
            lines.append(f"r/{subreddit}: {title}" if subreddit else title)
        if len(posts) > 4:
            lines.append(f"... {len(posts) - 4} more")
        return " | ".join(line for line in lines if line)

    def _get_selected_duplicate_media_row(self) -> Optional[Dict[str, Any]]:
        """Return selected duplicate-media row data."""
        table = self.duplicate_media_table
        if table is None:
            return None
        row = table.currentRow()
        if row < 0 or row >= len(self.duplicate_media_rows):
            return None
        return self.duplicate_media_rows[row]

    def _update_duplicate_media_action_buttons(self) -> None:
        """Enable duplicate-media actions for the selected row."""
        row_data = self._get_selected_duplicate_media_row()
        author = str((row_data or {}).get("author") or "").strip()
        actionable = bool(author and author.lower() not in {"[deleted]", "unknown", "none"})
        if self.duplicate_media_view_author_button is not None:
            self.duplicate_media_view_author_button.setEnabled(actionable)
        if self.duplicate_media_ban_author_button is not None:
            can_ban = bool(
                actionable and
                self.current_model and
                not self.current_model.is_user_mode and
                self.current_model.check_user_moderation_status()
            )
            self.duplicate_media_ban_author_button.setEnabled(can_ban)
        if self.duplicate_media_refresh_button is not None:
            self.duplicate_media_refresh_button.setEnabled(self.duplicate_media_fetcher is None)

    def _on_duplicate_media_cell_double_clicked(self, row: int, column: int) -> None:
        """Open selected duplicate-media author posts on double-click."""
        table = self.duplicate_media_table
        if table is not None:
            table.selectRow(row)
        self._view_selected_duplicate_media_author()

    def _view_selected_duplicate_media_author(self) -> None:
        """Open posts for the selected duplicate-media author."""
        row_data = self._get_selected_duplicate_media_row()
        author = str((row_data or {}).get("author") or "").strip()
        if author and author.lower() not in {"[deleted]", "unknown", "none"}:
            self.on_author_clicked(author)

    def _ban_selected_duplicate_media_author(self) -> None:
        """Open ban dialog for the selected duplicate-media author."""
        row_data = self._get_selected_duplicate_media_row()
        author = str((row_data or {}).get("author") or "").strip()
        if not author or author.lower() in {"[deleted]", "unknown", "none"}:
            return
        if not self.current_model:
            return

        group = row_data.get("group") or {}
        reason = (
            f"Same cached media posted by multiple users: "
            f"{group.get('author_count', 0)} users, {group.get('post_count', 0)} posts"
        )
        self.open_ban_dialog(author, self.current_model.subreddit, default_reason=reason)

    def view_removed_users(self) -> None:
        """Display users sorted by the number of locally logged removals."""
        if not self.current_model or self.current_model.is_user_mode or not hasattr(self.current_model, "subreddit"):
            return

        self._cancel_content_fetch_workers()
        self.loading_bar.hide()
        self.is_loading_posts = False
        self.is_filtered = False
        self.current_snapshot = []
        self.current_filtered_snapshot = []
        self.all_current_filtered_snapshot = []
        self.snapshot_offset = 0
        self.current_after = None
        self.can_fetch_more_posts = False
        self._set_view_mode("removed_users", "Removed Users")
        self._sync_source_specific_controls()

        subreddit_name = self.current_model.source_name
        summaries = self._filter_removed_user_summaries(
            get_removed_user_summaries(subreddit_name)
        )
        self.removed_user_summaries = summaries
        self.removed_users_banned_set = set()
        self.removed_users_pending_ban_verification.clear()
        self.removed_users_banned_status_ready = False
        self._display_removed_users_page(subreddit_name, summaries)
        self._start_removed_users_banned_status_fetch(summaries)

    def _filter_removed_user_summaries(self, summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Apply threshold and ignore-list filtering to removed-user summaries."""
        filtered = []
        for summary in summaries:
            author = str(summary.get("author") or "").strip()
            if author.lower() in self.removed_users_ignored_users:
                continue
            if int(summary.get("count") or 0) < self.removed_users_min_count:
                continue
            filtered.append(summary)
        return filtered

    def _start_removed_users_banned_status_fetch(self, summaries: List[Dict[str, Any]]) -> None:
        """Fetch already-banned status for the users shown in the review page."""
        if not self.current_model or not summaries:
            return

        usernames = [
            str(summary.get("author") or "").strip()
            for summary in summaries
            if str(summary.get("author") or "").strip()
        ]
        if not usernames:
            return

        if self.banned_users_fetcher is not None and self.banned_users_fetcher.isRunning():
            self._retire_worker(self.banned_users_fetcher)

        self.banned_users_fetcher = BannedUsersFetcher(self.current_model.subreddit, usernames)
        self.banned_users_fetcher.bannedUsersFetched.connect(
            self.on_removed_users_banned_status_fetched,
            Qt.ConnectionType.QueuedConnection,
        )
        self.banned_users_fetcher.errorOccurred.connect(
            self.on_removed_users_banned_status_error,
            Qt.ConnectionType.QueuedConnection,
        )
        self.add_worker(self.banned_users_fetcher)
        self.banned_users_fetcher.start()

    def _start_removed_user_ban_verification(self, username: str, subreddit_obj) -> None:
        """Verify a just-banned user against Reddit before marking the row banned."""
        if not username or not subreddit_obj:
            return

        normalized = username.strip().lower()
        if not self._is_removed_user_actionable(normalized):
            return

        if (
            self.removed_user_ban_verification_fetcher is not None and
            self.removed_user_ban_verification_fetcher.isRunning()
        ):
            self._retire_worker(self.removed_user_ban_verification_fetcher)

        self.removed_users_pending_ban_verification.add(normalized)
        self._update_removed_users_table_statuses()
        self.status_bar.showMessage(f"Verifying ban status for {username}...")

        self.removed_user_ban_verification_fetcher = BannedUserVerificationFetcher(subreddit_obj, username)
        self.removed_user_ban_verification_fetcher.banStatusVerified.connect(
            self.on_removed_user_ban_status_verified,
            Qt.ConnectionType.QueuedConnection,
        )
        self.removed_user_ban_verification_fetcher.errorOccurred.connect(
            self.on_removed_user_ban_status_verify_error,
            Qt.ConnectionType.QueuedConnection,
        )
        self.add_worker(self.removed_user_ban_verification_fetcher)
        self.removed_user_ban_verification_fetcher.start()

    def on_removed_user_ban_status_verified(self, username: str, is_banned: bool) -> None:
        """Apply a one-user Reddit ban-status verification result."""
        sender = self.sender()
        if sender is not self.removed_user_ban_verification_fetcher:
            logger.debug("Ignoring stale removed-user ban verification result")
            self.cleanup_worker(sender)
            return

        self.cleanup_worker(self.removed_user_ban_verification_fetcher)
        self.removed_user_ban_verification_fetcher = None

        normalized = str(username or "").strip().lower()
        self.removed_users_pending_ban_verification.discard(normalized)
        if is_banned:
            self.removed_users_banned_set.add(normalized)
            self.status_bar.showMessage(f"Verified {username} is banned.")
        else:
            self.removed_users_banned_set.discard(normalized)
            self.status_bar.showMessage(f"Reddit does not show {username} as banned yet.")

        if self.view_mode == "removed_users":
            self._update_removed_users_table_statuses()

    def on_removed_user_ban_status_verify_error(self, username: str, error_message: str) -> None:
        """Leave the row unconfirmed if Reddit ban-status verification fails."""
        sender = self.sender()
        if sender is not self.removed_user_ban_verification_fetcher:
            logger.debug("Ignoring stale removed-user ban verification error: %s", error_message)
            self.cleanup_worker(sender)
            return

        self.cleanup_worker(self.removed_user_ban_verification_fetcher)
        self.removed_user_ban_verification_fetcher = None

        normalized = str(username or "").strip().lower()
        self.removed_users_pending_ban_verification.discard(normalized)
        logger.error("Failed to verify ban status for %s: %s", username, error_message)
        self.status_bar.showMessage(f"Could not verify ban status for {username}: {error_message}")
        if self.view_mode == "removed_users":
            self._update_removed_users_table_statuses()

    def on_removed_users_banned_status_fetched(self, banned_users: set) -> None:
        """Update removed-users page with already-banned markers."""
        sender = self.sender()
        if sender is not self.banned_users_fetcher:
            logger.debug("Ignoring stale banned-user status result")
            self.cleanup_worker(sender)
            return

        self.cleanup_worker(self.banned_users_fetcher)
        self.banned_users_fetcher = None
        self.removed_users_banned_set.update(banned_users or set())
        self.removed_users_banned_status_ready = True
        if self.view_mode == "removed_users":
            self._update_removed_users_table_statuses()

    def on_removed_users_banned_status_error(self, error_message: str) -> None:
        """Keep the page usable if banned-status lookup fails."""
        sender = self.sender()
        if sender is not self.banned_users_fetcher:
            logger.debug("Ignoring stale banned-user status error: %s", error_message)
            self.cleanup_worker(sender)
            return

        self.cleanup_worker(self.banned_users_fetcher)
        self.banned_users_fetcher = None
        self.removed_users_banned_status_ready = True
        self._update_removed_users_table_statuses()
        logger.error("Failed to fetch banned-user status: %s", error_message)
        self.status_bar.showMessage(f"Could not refresh banned-user status: {error_message}")

    def _display_removed_users_page(self, subreddit_name: str, summaries: List[Dict[str, Any]]) -> None:
        """Render the local removal summary page."""
        self.content_widget.setUpdatesEnabled(False)
        self.scroll_area.viewport().setUpdatesEnabled(False)

        try:
            self.removed_users_table = None
            self.removed_users_view_button = None
            self.removed_users_ignore_button = None
            self.removed_users_ban_button = None
            self.removed_user_summary_by_author = {
                str(summary.get("author") or "").strip().lower(): summary
                for summary in summaries
                if str(summary.get("author") or "").strip()
            }

            self.clear_content()
            self._configure_content_grid()
            self.source_label.setText(self._build_source_label())
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.fetch_next_500_button.setEnabled(False)

            if not summaries:
                self._add_full_width_content_label(f"No logged removals for r/{subreddit_name}")
                self.status_bar.showMessage(f"No logged removals for r/{subreddit_name}")
                return

            page_widget = QWidget(self.content_widget)
            page_layout = QVBoxLayout(page_widget)
            page_layout.setContentsMargins(0, 0, 0, 0)
            page_layout.setSpacing(6)

            actions_widget = QWidget(page_widget)
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            actions_layout.setSpacing(6)

            view_button = QPushButton("View User Posts", actions_widget)
            view_button.clicked.connect(self._view_selected_removed_user)
            actions_layout.addWidget(view_button)

            ignore_button = QPushButton("Ignore Selected", actions_widget)
            ignore_button.clicked.connect(self._ignore_selected_removed_user)
            actions_layout.addWidget(ignore_button)

            ban_button = QPushButton("Ban Selected", actions_widget)
            ban_button.clicked.connect(lambda: self._ban_selected_removed_user(subreddit_name))
            actions_layout.addWidget(ban_button)
            actions_layout.addStretch(1)
            page_layout.addWidget(actions_widget)

            table = QTableWidget(len(summaries), 5, page_widget)
            table.setHorizontalHeaderLabels(("User", "Removed", "Last Removal", "Recent Removed Posts", "Status"))
            table.setAlternatingRowColors(True)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setWordWrap(False)
            table.setTextElideMode(Qt.TextElideMode.ElideRight)
            table.setMinimumHeight(max(360, self.scroll_area.viewport().height() - 80))
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(34)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
            table.cellDoubleClicked.connect(self._on_removed_users_cell_double_clicked)
            table.itemSelectionChanged.connect(self._update_removed_users_action_buttons)

            for row, summary in enumerate(summaries):
                self._populate_removed_users_table_row(table, row, summary)

            self.removed_users_table = table
            self.removed_users_view_button = view_button
            self.removed_users_ignore_button = ignore_button
            self.removed_users_ban_button = ban_button
            if summaries:
                table.selectRow(0)

            page_layout.addWidget(table)
            self._update_removed_users_action_buttons()

            self.content_layout.addWidget(page_widget, 0, 0, 1, self._get_grid_column_count())
            self.thumbnail_widgets.append(page_widget)

            self.status_bar.showMessage(
                f"Showing {len(summaries)} users with logged removals in r/{subreddit_name}"
            )
        finally:
            self.content_widget.setUpdatesEnabled(True)
            self.scroll_area.viewport().setUpdatesEnabled(True)
            self.content_widget.update()
            self.scroll_area.viewport().update()

    def _populate_removed_users_table_row(
        self,
        table: QTableWidget,
        row: int,
        summary: Dict[str, Any],
    ) -> None:
        """Populate one removed-user summary row in the review table."""
        author = summary.get("author") or "[deleted]"
        count = int(summary.get("count") or 0)
        posts = summary.get("posts") or []
        last_removed_at = summary.get("last_removed_at_utc")
        details = self._build_removed_posts_summary(posts)
        compact_details = details.replace("\n", " | ")

        author_item = QTableWidgetItem(author)
        author_item.setData(Qt.ItemDataRole.UserRole, author)
        table.setItem(row, 0, author_item)

        count_item = QTableWidgetItem(str(count))
        count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        count_item.setData(Qt.ItemDataRole.UserRole, count)
        table.setItem(row, 1, count_item)

        last_item = QTableWidgetItem(self._format_removal_time(last_removed_at))
        table.setItem(row, 2, last_item)

        details_item = QTableWidgetItem(compact_details)
        if details:
            details_item.setToolTip(details)
        table.setItem(row, 3, details_item)

        status_item = QTableWidgetItem(self._get_removed_user_ban_status_text(author))
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(row, 4, status_item)

    def _get_selected_removed_user_summary(self) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Return the selected removed-user author and summary."""
        table = self.removed_users_table
        if table is None:
            return None, None

        row = table.currentRow()
        if row < 0:
            return None, None

        author_item = table.item(row, 0)
        if author_item is None:
            return None, None

        author = str(author_item.data(Qt.ItemDataRole.UserRole) or author_item.text() or "").strip()
        summary = self.removed_user_summary_by_author.get(author.lower())
        return author, summary

    def _is_removed_user_actionable(self, username: Optional[str]) -> bool:
        """Return whether the removed-user row can be opened or banned."""
        normalized = str(username or "").strip().lower()
        return bool(normalized and normalized not in {"[deleted]", "unknown"})

    def _get_removed_user_ban_status_text(self, username: str) -> str:
        """Return display text for a removed user's ban status."""
        if not self._is_removed_user_actionable(username):
            return ""
        normalized = username.lower()
        if normalized in self.removed_users_pending_ban_verification:
            return "Verifying"
        if normalized in self.removed_users_banned_set:
            return "Already banned"
        if not self.removed_users_banned_status_ready:
            return "Checking"
        return "Not banned"

    def _update_removed_users_action_buttons(self) -> None:
        """Enable or disable fixed removed-user action buttons for the selected row."""
        author, _ = self._get_selected_removed_user_summary()
        actionable = self._is_removed_user_actionable(author)
        already_banned = bool(author and author.lower() in self.removed_users_banned_set)
        verifying = bool(author and author.lower() in self.removed_users_pending_ban_verification)

        if self.removed_users_view_button is not None:
            self.removed_users_view_button.setEnabled(actionable)
        if self.removed_users_ignore_button is not None:
            self.removed_users_ignore_button.setEnabled(bool(author))
        if self.removed_users_ban_button is not None:
            self.removed_users_ban_button.setEnabled(actionable and not already_banned and not verifying)

    def _update_removed_users_table_statuses(self) -> None:
        """Refresh ban-status cells without rebuilding the whole removed-users table."""
        table = self.removed_users_table
        if table is None:
            return

        for row in range(table.rowCount()):
            author_item = table.item(row, 0)
            status_item = table.item(row, 4)
            if author_item is None or status_item is None:
                continue
            author = str(author_item.data(Qt.ItemDataRole.UserRole) or author_item.text() or "").strip()
            status_item.setText(self._get_removed_user_ban_status_text(author))

        self._update_removed_users_action_buttons()

    def _on_removed_users_cell_double_clicked(self, row: int, column: int) -> None:
        """Open the selected user's posts when a removed-users row is double-clicked."""
        table = self.removed_users_table
        if table is not None:
            table.selectRow(row)
        self._view_selected_removed_user()

    def _view_selected_removed_user(self) -> None:
        """Open the selected user's posts using the existing author view."""
        author, _ = self._get_selected_removed_user_summary()
        if self._is_removed_user_actionable(author):
            self.on_author_clicked(author)

    def _ignore_selected_removed_user(self) -> None:
        """Hide the selected removed user from the current review page."""
        author, _ = self._get_selected_removed_user_summary()
        if author:
            self._ignore_removed_user(author)

    def _ban_selected_removed_user(self, subreddit_name: str) -> None:
        """Open the ban dialog for the selected removed user."""
        author, summary = self._get_selected_removed_user_summary()
        if self._is_removed_user_actionable(author):
            self._ban_removed_user(author, subreddit_name, summary)

    def _format_removal_time(self, timestamp) -> str:
        """Format a removal timestamp for display."""
        try:
            if not timestamp:
                return "Unknown"
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(timestamp)))
        except Exception:
            return "Unknown"

    def _build_removed_posts_summary(self, posts: List[Dict[str, Any]]) -> str:
        """Build compact post details for the removed-users review table."""
        if not posts:
            return ""

        lines = []
        for post in posts[:5]:
            removed_time = self._format_removal_time(post.get("removed_at_utc"))
            title = (post.get("title") or "").replace("\n", " ").strip()
            if len(title) > 120:
                title = title[:117] + "..."
            submission_id = post.get("submission_id") or "unknown"
            lines.append(f"{removed_time} - {submission_id} - {title}")

        if len(posts) > 5:
            lines.append(f"... {len(posts) - 5} more")

        return "\n".join(lines)

    def _ignore_removed_user(self, username: str) -> None:
        """Hide a user from the current removed-users review session."""
        if not username:
            return
        self.removed_users_ignored_users.add(username.lower())
        if self.current_model:
            self.view_removed_users()

    def _ban_removed_user(self, username: str, subreddit_name: str, summary: Optional[Dict[str, Any]] = None) -> None:
        """Open the ban dialog from the removed-users review page."""
        if not username or username in {"[deleted]", "unknown"}:
            return
        try:
            subreddit_obj = self.current_model.subreddit if self.current_model else self.reddit.subreddit(subreddit_name)
            reason = self._build_removed_user_ban_reason(subreddit_name, summary or {})
            self.open_ban_dialog(username, subreddit_obj, default_reason=reason)
        except Exception as e:
            logger.exception(f"Could not open ban dialog for {username} in r/{subreddit_name}: {e}")
            self.status_bar.showMessage(f"Could not open ban dialog for {username}")

    def _build_removed_user_ban_reason(self, subreddit_name: str, summary: Dict[str, Any]) -> str:
        """Build a concise default ban note for repeated removals."""
        count = int(summary.get("count") or 0)
        if count <= 0:
            return f"Repeated removed posts in r/{subreddit_name}"
        return f"Repeated removed posts in r/{subreddit_name}: {count} removals"

    def on_next_batch_fetched(self, new_posts) -> None:
        """Handle completion of automatic next batch fetch and navigate to the new page."""
        sender = self.sender()
        if sender is not self.next_batch_fetcher:
            logger.debug("Ignoring stale next-batch fetch result")
            self.cleanup_worker(sender)
            return

        # Clean up the next_batch_fetcher
        if hasattr(self, 'next_batch_fetcher'):
            self.cleanup_worker(self.next_batch_fetcher)
            self.next_batch_fetcher = None

        self._integrate_next_batch(new_posts)

    def _integrate_next_batch(self, new_posts) -> None:
        """Append a fetched next batch and navigate to the first newly visible page."""
        # Deduplicate and add new posts (similar to next_500 logic)
        existing_ids = {getattr(post, "id", None) for post in self.all_current_snapshot
                       if getattr(post, "id", None) is not None}

        unique_new_posts = [post for post in new_posts
                           if getattr(post, "id", None) not in existing_ids]

        self.can_fetch_more_posts = len(new_posts) >= DEFAULT_POSTS_FETCH_LIMIT

        # Add new posts to current snapshot
        self.all_current_snapshot.extend(unique_new_posts)
        self._refresh_visible_snapshots()

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

        if self.is_filtered and self.current_model and self.current_model.is_user_mode:
            filtered_new_posts = self._filter_posts_for_previous_subreddit(unique_new_posts)
            visible_filtered_new_posts = self._get_visible_snapshot(
                filtered_new_posts,
                view_mode="filtered_user",
            )

            if visible_filtered_new_posts:
                visible_before_count = len(self.current_filtered_snapshot)
                self.all_current_filtered_snapshot.extend(filtered_new_posts)
                self._refresh_visible_snapshots()
                visible_added_count = max(0, len(self.current_filtered_snapshot) - visible_before_count)
                self.snapshot_offset = len(self.current_filtered_snapshot) - visible_added_count
                self.snapshot_offset = (self.snapshot_offset // self.snapshot_page_size) * self.snapshot_page_size
                self._reset_filtered_auto_fetch_state()
                self._set_view_mode("filtered_user")
                self.display_filtered_page()
                self.status_bar.showMessage(
                    f"Fetched {len(filtered_new_posts)} filtered posts. Total filtered: {len(self.current_filtered_snapshot)}"
                )
            else:
                if self._maybe_continue_filtered_fetch(
                    fetch_method=self.fetch_next_batch,
                    unique_new_posts=unique_new_posts,
                    filtered_new_posts=filtered_new_posts,
                    empty_batch_message=(
                        f"No new posts from r/{self.previous_subreddit} in the latest batch. Press Next to keep searching."
                    ),
                    exhausted_message=f"No more posts found in r/{self.previous_subreddit}.",
                ):
                    return
            return

        # Navigate to the first page of the new batch
        visible_unique_new_posts = self._get_visible_snapshot(unique_new_posts)
        if visible_unique_new_posts:
            self._reset_filtered_auto_fetch_state()
            self.snapshot_offset = len(self.current_snapshot) - len(visible_unique_new_posts)
            # Ensure offset is page-aligned
            self.snapshot_offset = (self.snapshot_offset // self.snapshot_page_size) * self.snapshot_page_size

            # Update buttons and display the new page
            self._update_pagination_buttons(self.current_snapshot)
            self.display_current_page()

            logger.info(f"Fetched {len(unique_new_posts)} new posts, navigated to page starting at {self.snapshot_offset + 1}")
        else:
            self._reset_filtered_auto_fetch_state()
            self._update_pagination_buttons(self.current_snapshot)
            if unique_new_posts:
                self.status_bar.showMessage("Fetched posts were already removed, so they remain hidden from the default feed")
                logger.info("Fetched %s new posts, but all were removed and hidden from the default feed", len(unique_new_posts))
            else:
                self.status_bar.showMessage("No more posts available")
                logger.info("No new posts fetched - reached end of content")

    def on_next_batch_error(self, error_message: str) -> None:
        """Handle a failed automatic next-batch fetch."""
        sender = self.sender()
        if sender is not self.next_batch_fetcher:
            logger.debug(f"Ignoring stale next-batch fetch error: {error_message}")
            self.cleanup_worker(sender)
            return

        if hasattr(self, 'next_batch_fetcher'):
            self.cleanup_worker(self.next_batch_fetcher)
            self.next_batch_fetcher = None

        self.is_loading_posts = False
        self.loading_bar.hide()
        self._reset_filtered_auto_fetch_state()
        self._update_pagination_buttons(self.current_filtered_snapshot if self.is_filtered else self.current_snapshot)
        self.fetch_next_500_button.setEnabled(self.can_fetch_more_posts)
        logger.error(f"Failed to fetch next batch: {error_message}")
        self.status_bar.showMessage(error_message)

    def on_next_500_fetched(self, new_posts) -> None:
        """Append the next 500 posts to the current snapshot and update the UI."""
        sender = self.sender()
        if sender is not self.next_500_fetcher:
            logger.debug("Ignoring stale next-500 fetch result")
            self.cleanup_worker(sender)
            return

        # Clean up the next_500_fetcher
        if hasattr(self, 'next_500_fetcher'):
            self.cleanup_worker(self.next_500_fetcher)
            self.next_500_fetcher = None

        # Deduplicate using optimized set operations
        # Build set of existing IDs more efficiently
        existing_ids = {getattr(post, "id", None) for post in self.all_current_snapshot
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

        self.can_fetch_more_posts = len(new_posts) >= POSTS_FETCH_LIMIT

        if unique_new_posts:
            self.all_current_snapshot.extend(unique_new_posts)
            self._refresh_visible_snapshots()
            self.status_bar.showMessage(f"Fetched {len(unique_new_posts)} new posts. Total: {len(self.current_snapshot)}")
        else:
            self.status_bar.showMessage("No new posts found.")

        if self.is_filtered and self.current_model and self.current_model.is_user_mode:
            filtered_new_posts = self._filter_posts_for_previous_subreddit(unique_new_posts)
            if filtered_new_posts:
                self.all_current_filtered_snapshot.extend(filtered_new_posts)
                self._refresh_visible_snapshots()

        # Hide loading indicator
        self.is_loading_posts = False
        self.loading_bar.hide()

        if self.is_filtered and self.current_model and self.current_model.is_user_mode:
            self._set_view_mode("filtered_user")
            visible_filtered_new_posts = self._get_visible_snapshot(
                filtered_new_posts,
                view_mode="filtered_user",
            )
            if visible_filtered_new_posts:
                self._reset_filtered_auto_fetch_state()
                current_length = len(self.current_filtered_snapshot)
                if self.snapshot_offset + self.snapshot_page_size >= current_length - len(visible_filtered_new_posts):
                    new_offset = max(0, current_length - self.snapshot_page_size)
                    if new_offset < current_length:
                        self.snapshot_offset = new_offset
                        self.display_filtered_page()
            else:
                if self._maybe_continue_filtered_fetch(
                    fetch_method=self.fetch_next_500,
                    unique_new_posts=unique_new_posts,
                    filtered_new_posts=filtered_new_posts,
                    empty_batch_message=(
                        f"No new posts from r/{self.previous_subreddit} in the latest batch. Use Next to continue searching."
                    ),
                    exhausted_message=f"No more posts found in r/{self.previous_subreddit}.",
                ):
                    return

            self.fetch_next_500_button.setEnabled(self.can_fetch_more_posts)
            return

        self._sync_view_mode_with_model()

        # Enable/disable the button depending on whether we got a full batch
        # If fewer posts returned than requested, we've likely reached the end
        self.fetch_next_500_button.setEnabled(self.can_fetch_more_posts)
        self._update_pagination_buttons(self.current_snapshot)

        # If we're at the end and new posts were added, show the new page
        # Use safe bounds checking to prevent race conditions
        current_length = len(self.current_snapshot)
        visible_unique_new_posts = self._get_visible_snapshot(unique_new_posts)
        if visible_unique_new_posts and current_length > 0 and self.snapshot_offset + self.snapshot_page_size >= current_length - len(visible_unique_new_posts):
            self._reset_filtered_auto_fetch_state()
            # Calculate new offset safely, ensuring it doesn't go out of bounds
            new_offset = max(0, current_length - self.snapshot_page_size)
            # Ensure offset is within valid range
            if new_offset < current_length:
                self.snapshot_offset = new_offset
                self.display_current_page()
            else:
                logger.warning(f"Calculated offset {new_offset} would exceed snapshot length {current_length}")
        else:
            self._reset_filtered_auto_fetch_state()
            # Otherwise, just update the status bar
            self.status_bar.showMessage(f"Fetched {len(unique_new_posts)} new posts. Total: {current_length}")

        # Start prefetching after new posts are added
        self.start_media_prefetch()

    def on_next_500_error(self, error_message: str) -> None:
        """Handle a failed next-500 fetch."""
        sender = self.sender()
        if sender is not self.next_500_fetcher:
            logger.debug(f"Ignoring stale next-500 fetch error: {error_message}")
            self.cleanup_worker(sender)
            return

        if hasattr(self, 'next_500_fetcher'):
            self.cleanup_worker(self.next_500_fetcher)
            self.next_500_fetcher = None

        self.is_loading_posts = False
        self.loading_bar.hide()
        self._reset_filtered_auto_fetch_state()
        self.fetch_next_500_button.setEnabled(self.can_fetch_more_posts)
        self._update_pagination_buttons(self.current_filtered_snapshot if self.is_filtered else self.current_snapshot)
        logger.error(f"Failed to fetch next 500 posts: {error_message}")
        self.status_bar.showMessage(error_message)

    def _find_prefetched_media_match_locked(
        self,
        original_url: str,
        processed_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return existing prefetch state for either the raw or resolved media URL."""
        if original_url in self.prefetched_media:
            return self.prefetched_media[original_url]

        if processed_url and processed_url in self.prefetched_media:
            return self.prefetched_media[processed_url]

        if processed_url:
            for data in self.prefetched_media.values():
                if data.get('processed_url') == processed_url:
                    return data

        return None

    def _sync_prefetched_media_entries_locked(
        self,
        original_url: str,
        entry_data: Dict[str, Any],
        *,
        processed_url: Optional[str] = None,
    ) -> None:
        """Keep raw and resolved URL aliases aligned to one prefetch state."""
        effective_processed_url = processed_url or entry_data.get('processed_url')
        self.prefetched_media[original_url] = dict(entry_data)

        if effective_processed_url:
            alias_data = dict(entry_data)
            alias_data['processed_url'] = effective_processed_url
            self.prefetched_media[effective_processed_url] = alias_data

            for key, existing_data in list(self.prefetched_media.items()):
                if key in {original_url, effective_processed_url}:
                    continue
                if existing_data.get('processed_url') == effective_processed_url:
                    self.prefetched_media[key] = dict(alias_data)

    def _update_prefetched_media_status(
        self,
        original_url: str,
        status: str,
        *,
        processed_url: Optional[str] = None,
        cache_path: Optional[str] = None,
        error_message: Optional[str] = None,
        attempts: Optional[int] = None,
        next_retry_at: Optional[float] = None,
    ) -> None:
        """Record the terminal state for a prefetched media URL."""
        with QMutexLocker(self.prefetch_mutex):
            existing_data = self._find_prefetched_media_match_locked(original_url, processed_url) or {}
            effective_processed_url = processed_url or existing_data.get('processed_url')
            updated_data = {
                'status': status,
                'started_at': existing_data.get('started_at', time.time()),
                'completed_at': time.time(),
            }
            if effective_processed_url:
                updated_data['processed_url'] = effective_processed_url
            updated_data['attempts'] = (
                attempts if attempts is not None else int(existing_data.get('attempts', 0))
            )

            if cache_path:
                updated_data['cache_path'] = cache_path
            elif existing_data.get('cache_path'):
                updated_data['cache_path'] = existing_data['cache_path']

            if error_message:
                updated_data['error'] = error_message

            if next_retry_at is not None:
                updated_data['next_retry_at'] = next_retry_at
            elif status == 'error' and existing_data.get('next_retry_at'):
                updated_data['next_retry_at'] = existing_data['next_retry_at']

            self._sync_prefetched_media_entries_locked(
                original_url,
                updated_data,
                processed_url=effective_processed_url,
            )

    def _increment_prefetch_stat_locked(self, key: str, amount: int = 1) -> None:
        """Increment a prefetch metric while the prefetch mutex is held."""
        self.prefetch_stats[key] = self.prefetch_stats.get(key, 0) + amount

    def _log_prefetch_stats(self, reason: str) -> None:
        """Emit a compact snapshot of prefetch effectiveness metrics."""
        with QMutexLocker(self.prefetch_mutex):
            stats = dict(self.prefetch_stats)
            queued = len(self.prefetch_download_queue)
            active = len(self.active_prefetch_downloads)
            tracked = len(self.prefetched_media)

        logger.info(
            "Prefetch stats [%s]: scheduled=%s started=%s completed=%s failed=%s retried=%s "
            "cache_hits=%s duplicate_skips=%s retry_backoff_skips=%s retry_exhausted_skips=%s "
            "queued=%s active=%s tracked=%s",
            reason,
            stats.get('scheduled', 0),
            stats.get('started', 0),
            stats.get('completed', 0),
            stats.get('failed', 0),
            stats.get('retried', 0),
            stats.get('cache_hits', 0),
            stats.get('duplicate_skips', 0),
            stats.get('retry_backoff_skips', 0),
            stats.get('retry_exhausted_skips', 0),
            queued,
            active,
            tracked,
        )

    def _log_prefetch_skip(
        self,
        reason: str,
        original_url: str,
        *,
        processed_url: Optional[str] = None,
        submission_id: Optional[str] = None,
        attempts: Optional[int] = None,
        next_retry_at: Optional[float] = None,
    ) -> None:
        """Emit one explicit log per item that prefetch declined to queue."""
        log_method = logger.debug if reason in {"already_cached", "existing_cached"} else logger.info
        log_method(
            "prefetch_skip reason=%s submission_id=%s original_url=%s processed_url=%s "
            "attempts=%s next_retry_at=%s",
            reason,
            submission_id or "UnknownID",
            original_url,
            processed_url or "",
            attempts if attempts is not None else "",
            next_retry_at if next_retry_at is not None else "",
        )

    def queue_prefetch_download(
        self,
        original_url: str,
        processed_url: str,
        submission_data: object,
        *,
        batch_id: Optional[int] = None,
    ) -> bool:
        """Queue a prefetched download and start it when capacity is available."""
        queued = False
        submission_id = getattr(submission_data, 'id', 'UnknownID')

        with QMutexLocker(self.prefetch_mutex):
            if self.is_shutting_down:
                duplicate_reason = 'shutdown'
            elif processed_url in self.active_prefetch_downloads:
                self._increment_prefetch_stat_locked('duplicate_skips')
                duplicate_reason = 'already_active'
            elif processed_url in self.prefetch_download_queue:
                self._increment_prefetch_stat_locked('duplicate_skips')
                duplicate_reason = 'already_queued'
            else:
                self.prefetch_download_queue[processed_url] = {
                    'original_url': original_url,
                    'processed_url': processed_url,
                    'submission_data': submission_data,
                    'batch_id': batch_id,
                }
                queued = True
                duplicate_reason = None

        if duplicate_reason:
            self._log_prefetch_skip(
                duplicate_reason,
                original_url,
                processed_url=processed_url,
                submission_id=submission_id,
            )
            return False

        if queued:
            self._start_queued_prefetch_downloads()

        return queued

    def _start_queued_prefetch_downloads(self) -> None:
        """Start queued prefetch downloads up to the configured concurrency limit."""
        if self.is_shutting_down:
            return

        downloads_to_start = []

        with QMutexLocker(self.prefetch_mutex):
            if self.is_shutting_down:
                return
            active_before = len(self.active_prefetch_downloads)
            queued_before = len(self.prefetch_download_queue)
            while (
                len(self.active_prefetch_downloads) < self.max_concurrent_prefetch_downloads and
                self.prefetch_download_queue
            ):
                processed_url = next(iter(self.prefetch_download_queue))
                queued_download = self.prefetch_download_queue.pop(processed_url)
                self.active_prefetch_downloads.add(processed_url)
                self._increment_prefetch_stat_locked('started')
                self._record_prefetch_batch_download_started_locked(
                    queued_download.get('batch_id')
                )

                existing_data = self._find_prefetched_media_match_locked(
                    queued_download['original_url'],
                    processed_url,
                ) or {}
                updated_data = dict(existing_data)
                updated_data['status'] = 'prefetching'
                updated_data['processed_url'] = processed_url
                updated_data.pop('completed_at', None)
                updated_data.pop('error', None)
                updated_data.pop('next_retry_at', None)
                updated_data.setdefault('started_at', time.time())
                updated_data.setdefault('attempts', 0)
                self._sync_prefetched_media_entries_locked(
                    queued_download['original_url'],
                    updated_data,
                    processed_url=processed_url,
                )

                downloads_to_start.append(queued_download)

            active_after = len(self.active_prefetch_downloads)
            queued_after = len(self.prefetch_download_queue)

        logger.info(
            "prefetch_dispatch started=%s active_before=%s active_after=%s "
            "queued_before=%s queued_after=%s concurrency_limit=%s",
            len(downloads_to_start),
            active_before,
            active_after,
            queued_before,
            queued_after,
            self.max_concurrent_prefetch_downloads,
        )

        for queued_download in downloads_to_start:
            worker = MediaDownloadWorker(
                queued_download['original_url'],
                queued_download['submission_data'],
                source="prefetch",
            )
            worker.signals.finished.connect(
                lambda file_path, finished_processed_url, submission_data,
                original_url=queued_download['original_url'],
                batch_id=queued_download.get('batch_id'):
                self.on_prefetch_download_finished(
                    original_url,
                    file_path,
                    finished_processed_url,
                    submission_data,
                    batch_id=batch_id,
                ),
                Qt.ConnectionType.QueuedConnection
            )
            worker.signals.error.connect(
                lambda error_message, submission_data,
                original_url=queued_download['original_url'],
                processed_url=queued_download['processed_url'],
                batch_id=queued_download.get('batch_id'):
                self.on_prefetch_download_error(
                    original_url,
                    processed_url,
                    error_message,
                    submission_data,
                    batch_id=batch_id,
                ),
                Qt.ConnectionType.QueuedConnection
            )

            QThreadPool.globalInstance().start(worker)

    def _finalize_prefetch_download(self, processed_url: Optional[str]) -> None:
        """Release a completed prefetch download slot and start the next queued item."""
        should_drain_queue = False

        with QMutexLocker(self.prefetch_mutex):
            active_before = len(self.active_prefetch_downloads)
            queued_before = len(self.prefetch_download_queue)
            if processed_url in self.active_prefetch_downloads:
                self.active_prefetch_downloads.remove(processed_url)
                should_drain_queue = True
            active_after = len(self.active_prefetch_downloads)
            queued_after = len(self.prefetch_download_queue)

        logger.info(
            "prefetch_finalize processed_url=%s active_before=%s active_after=%s "
            "queued_before=%s queued_after=%s will_continue=%s",
            processed_url or "",
            active_before,
            active_after,
            queued_before,
            queued_after,
            should_drain_queue and queued_after > 0 and not self.is_shutting_down,
        )

        if should_drain_queue and not self.is_shutting_down:
            self._start_queued_prefetch_downloads()

    def on_prefetch_download_finished(
        self,
        original_url: str,
        file_path: str,
        processed_url: str,
        _submission_data: object,
        *,
        batch_id: Optional[int] = None,
    ) -> None:
        """Mark a prefetched media URL as cached after background download completes."""
        if not file_path:
            self._update_prefetched_media_status(
                original_url,
                "error",
                processed_url=processed_url,
                error_message="Prefetch finished without a cache path.",
            )
            self._record_prefetch_batch_download_finished(batch_id, failed=True)
            self._finalize_prefetch_download(processed_url)
            return

        self._update_prefetched_media_status(
            original_url,
            "cached",
            processed_url=processed_url,
            cache_path=file_path,
        )
        with QMutexLocker(self.prefetch_mutex):
            self._increment_prefetch_stat_locked('completed')
            completed = self.prefetch_stats.get('completed', 0)
            active = len(self.active_prefetch_downloads)
            queued = len(self.prefetch_download_queue)
        logger.info(
            "prefetch_completed batch_id=%s original_url=%s processed_url=%s file_path=%s "
            "completed=%s active=%s queued=%s",
            batch_id,
            original_url,
            processed_url,
            file_path,
            completed,
            active,
            queued,
        )
        self._record_prefetch_batch_download_finished(batch_id, failed=False)
        self._finalize_prefetch_download(processed_url)

    def on_prefetch_download_error(
        self,
        original_url: str,
        processed_url: Optional[str],
        error_message: str,
        _submission_data: object,
        *,
        batch_id: Optional[int] = None,
    ) -> None:
        """Mark a prefetched media URL as failed and retry after backoff when appropriate."""
        with QMutexLocker(self.prefetch_mutex):
            existing_data = self._find_prefetched_media_match_locked(original_url, processed_url) or {}
            attempts = int(existing_data.get('attempts', 0)) + 1
            should_retry = attempts < PREFETCH_RETRY_MAX_ATTEMPTS
            next_retry_at = None

            if should_retry:
                retry_delay_ms = PREFETCH_RETRY_BASE_DELAY_MS * attempts
                next_retry_at = time.time() + (retry_delay_ms / 1000)
                self._increment_prefetch_stat_locked('retried')
            self._increment_prefetch_stat_locked('failed')

        self._update_prefetched_media_status(
            original_url,
            "error",
            processed_url=processed_url,
            attempts=attempts,
            next_retry_at=next_retry_at,
            error_message=error_message,
        )
        self._record_prefetch_batch_download_finished(
            batch_id,
            failed=True,
            retried=should_retry,
        )
        self._finalize_prefetch_download(processed_url)

        if should_retry and not self.is_shutting_down:
            retry_delay_ms = PREFETCH_RETRY_BASE_DELAY_MS * attempts
            logger.info(
                "Retrying prefetched media after %sms (attempt %s/%s): %s",
                retry_delay_ms,
                attempts + 1,
                PREFETCH_RETRY_MAX_ATTEMPTS,
                processed_url or original_url,
            )
            QTimer.singleShot(retry_delay_ms, self.start_media_prefetch)

    def _cleanup_prefetch_worker(self, worker) -> None:
        """Drop completed prefetch workers so the tracking list does not grow indefinitely."""
        if worker is None:
            return

        with QMutexLocker(self.prefetch_mutex):
            try:
                self.prefetch_workers.remove(worker)
            except ValueError:
                return

    def start_media_prefetch(self):
        """Keep a rolling window of upcoming media prefetched ahead of the current page."""
        if self.is_shutting_down or not self.prefetch_enabled or not self.current_snapshot:
            return

        try:
            current_list = self.current_filtered_snapshot if self.is_filtered else self.current_snapshot
            if not current_list:
                return

            submissions_to_prefetch = []
            prefetch_limit = max(0, self.prefetch_media_limit)

            if prefetch_limit == 0:
                return

            next_offset = self.snapshot_offset + self.snapshot_page_size
            if next_offset >= len(current_list):
                return

            end_offset = min(next_offset + prefetch_limit, len(current_list))
            submissions_to_prefetch.extend(current_list[next_offset:end_offset])

            # Remove duplicates while capping background work to the configured limit.
            unique_submissions = []
            seen_ids = set()

            for submission in submissions_to_prefetch:
                if hasattr(submission, 'id') and submission.id not in seen_ids:
                    seen_ids.add(submission.id)
                    unique_submissions.append(submission)
                    if len(unique_submissions) >= prefetch_limit:
                        break

            if unique_submissions:
                self.prefetch_batch_counter += 1
                batch_id = self.prefetch_batch_counter
                logger.info(
                    "Starting media prefetch for %s submissions (limit=%s) from the "
                    "forward rolling window [batch_id=%s next_offset=%s end_offset=%s]",
                    len(unique_submissions),
                    prefetch_limit,
                    batch_id,
                    next_offset,
                    end_offset,
                )
                worker = MediaPrefetchWorker(self, unique_submissions, batch_id)
                worker.signals.finished.connect(
                    lambda _file_path, _processed_url, _submission_data, completed_worker=worker:
                    self._cleanup_prefetch_worker(completed_worker),
                    Qt.ConnectionType.QueuedConnection
                )
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

            self._log_prefetch_stats("cleanup")

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
