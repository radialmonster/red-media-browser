#!/usr/bin/env python3
"""
Reddit API Module for Red Media Browser

This module handles all Reddit API interactions, including fetching posts,
pagination, and moderation actions.
"""

import logging
from typing import List, Dict, Tuple, Optional, Set, Any
import os
import time
import threading
import praw
import prawcore.exceptions
from types import SimpleNamespace # Import SimpleNamespace
from PyQt6.QtCore import QThread, pyqtSignal, QObject # Import QObject for worker signals
# Import the Submission class for type checking
from praw.models import Submission, Subreddit

# Import caching utilities
from utils import (
    load_submission_index, get_metadata_file_path, read_metadata_file,
    write_metadata_file, get_cache_dir, update_metadata_cache, file_exists_in_cache
)

# Import constants
from constants import DEFAULT_POSTS_FETCH_LIMIT

# Set up logging
logger = logging.getLogger(__name__)

def get_moderated_subreddits(reddit_instance) -> List[Dict[str, str]]:
    """
    Get a list of subreddits moderated by the authenticated user.

    Args:
        reddit_instance: PRAW Reddit instance

    Returns:
        List of dictionaries with subreddit information (name, display_name, subscribers, etc.)
        Sorted alphabetically by display_name
    """
    try:
        user = reddit_instance.user.me()
        if not user:
            logger.error("Failed to get user information. User may not be authenticated.")
            return []

        # Get moderated subreddits
        logger.debug(f"Fetching moderated subreddits for user: {user.name}")
        mod_subreddits = []

        # Use a try-except block to handle any API errors
        try:
            for subreddit in reddit_instance.user.moderator_subreddits(limit=None):
                mod_subreddits.append({
                    "name": subreddit.display_name.lower(),
                    "display_name": subreddit.display_name,
                    "subscribers": getattr(subreddit, 'subscribers', 0),
                    "url": subreddit.url,
                    "description": getattr(subreddit, 'public_description', '')
                })
        except prawcore.exceptions.PrawcoreException as e:
            logger.error(f"PRAW error while fetching moderated subreddits: {e}")
        except Exception as e:
            logger.error(f"Unexpected error while fetching moderated subreddits: {e}")

        # Sort alphabetically by display_name
        mod_subreddits.sort(key=lambda x: x["display_name"].lower())
        logger.debug(f"Found {len(mod_subreddits)} moderated subreddits")
        return mod_subreddits
    except Exception as e:
        logger.exception(f"Error getting moderated subreddits: {e}")
        return []

class ModeratedSubredditsFetcher(QThread):
    """
    Worker thread for asynchronous fetching of moderated subreddits.
    """
    subredditsFetched = pyqtSignal(list)

    def __init__(self, reddit_instance):
        super().__init__()
        self.reddit_instance = reddit_instance

    def run(self):
        mod_subreddits = get_moderated_subreddits(self.reddit_instance)
        self.subredditsFetched.emit(mod_subreddits)

class RedditGalleryModel:
    """
    Model class for Reddit gallery data.
    Handles fetching and storing submissions from subreddits or user profiles.
    """
    def __init__(self, name: str, is_user_mode: bool = False, reddit_instance=None,
                 prefetched_mod_logs: Optional[Dict[str, List[Dict]]] = None,
                 mod_logs_ready: bool = False):
        """
        Initialize the gallery model.

        Args:
            name: Subreddit name or username
            is_user_mode: If True, name is treated as a username
            reddit_instance: PRAW Reddit instance to use
            prefetched_mod_logs: Dictionary of pre-fetched mod logs keyed by subreddit name.
            mod_logs_ready: Flag indicating if pre-fetched logs are ready.
        """
        self.is_user_mode = is_user_mode
        self.is_moderator = False # Specific to subreddit view, determined later
        self.snapshot = []  # Snapshot of submissions (up to 100)
        self.source_name = name
        self.reddit = reddit_instance # Store the PRAW instance
        self.prefetched_logs = prefetched_mod_logs if prefetched_mod_logs is not None else {}
        self.logs_ready = mod_logs_ready
        self.moderated_subreddit_names = set() # Store names of subs the app user mods

        if self.reddit:
            # Fetch moderated subreddit names immediately for use later
            try:
                mod_subs_info = get_moderated_subreddits(self.reddit)
                self.moderated_subreddit_names = {sub['name'] for sub in mod_subs_info}
                logger.debug(f"Model initialized with {len(self.moderated_subreddit_names)} moderated subreddit names.")
            except Exception as e:
                logger.error(f"Error fetching moderated subreddits during model init: {e}")

            # Set up user or subreddit object
            if self.is_user_mode:
                try:
                    self.user = self.reddit.redditor(name)
                except Exception as e:
                    logger.error(f"Error getting redditor object for {name}: {e}")
                    self.user = None # Handle potential errors
            else:
                try:
                    self.subreddit = self.reddit.subreddit(name)
                except Exception as e:
                    logger.error(f"Error getting subreddit object for {name}: {e}")
                    self.subreddit = None # Handle potential errors

    def check_user_moderation_status(self) -> bool:
        """
        Check if the current Reddit user is a moderator of the current subreddit.

        Returns:
            bool: True if user is a moderator, False otherwise
        """
        if self.is_user_mode or not self.subreddit or not self.reddit:
            return False

        try:
            logger.debug("Performing moderator status check")
            moderators = list(self.subreddit.moderator())
            user = self.reddit.user.me()
            if not user:
                logger.warning("Could not get current user for mod check.")
                return False
            logger.debug(f"Current user: {user.name}")
            logger.debug(f"Moderators in subreddit: {[mod.name for mod in moderators]}")
            self.is_moderator = any(mod.name.lower() == user.name.lower() for mod in moderators)
            logger.debug(f"Moderator status for current user: {self.is_moderator}")
            return self.is_moderator
        except prawcore.exceptions.PrawcoreException as e:
            logger.exception(f"PRAW error while checking moderation status: {e}")
            return False
        except Exception as e:
            logger.exception(f"Unexpected error while checking moderation status: {e}")
            return False


    def fetch_snapshot(self, total=DEFAULT_POSTS_FETCH_LIMIT, after=None) -> List[Any]:
        """
        Fetch a batch of submissions, utilizing the metadata cache.
        For "Fetch Next 100", set total=100 and after=fullname of last post.

        Args:
            total: Number of submissions to fetch (default from constants)
            after: Reddit fullname to fetch posts after

        Returns:
            List of submission objects (PRAW instances or SimpleNamespace objects from cache)
        """
        logger.info(f"Fetching snapshot (total={total}, after={after}) for {'user' if self.is_user_mode else 'subreddit'}: {self.source_name}")
        snapshot_results = []
        processed_ids = set()  # Keep track of IDs added to results

        # 1. Load the metadata index
        submission_index = load_submission_index()
        cache_dir = get_cache_dir()

        # 2. Get next page of Submission objects from Reddit API
        try:
            initial_listing = []
            params = {'limit': total}
            # PRAW's .new() does NOT accept 'after' as a direct argument, but the ListingGenerator supports .params
            if self.is_user_mode:
                if not self.user:
                    logger.error("Cannot fetch user submissions, user object is None.")
                    return []
                try:
                    gen = self.user.submissions.new(limit=total)
                    if after:
                        gen.params['after'] = after
                    initial_listing = list(gen)
                    logger.debug(f"Fetched {len(initial_listing)} items for user {self.source_name} (after={after})")
                except prawcore.exceptions.NotFound:
                    logger.warning(f"User '{self.source_name}' not found or inaccessible (404). Returning empty list.")
                    return []
                except Exception as user_fetch_err:
                    logger.error(f"Error fetching submissions for user {self.source_name}: {user_fetch_err}")
                    return []
                # Optionally: add removed posts from logs (not paginated, so skip for "next 100" fetches)
            else:
                if not self.subreddit:
                    logger.error("Cannot fetch subreddit submissions, subreddit object is None.")
                    return []
                is_mod = self.check_user_moderation_status()

                if is_mod:
                    logger.debug("Fetching moderator view sources...")
                    # For 'Fetch Next 500', only paginate the 'new' listing with 'after'
                    gen = self.subreddit.new(limit=total)
                    if after:
                        gen.params['after'] = after
                    initial_listing = list(gen)
                    logger.debug(f"Fetched {len(initial_listing)} items from mod 'new' listing (after={after})")
                else:
                    logger.debug("Fetching regular view...")
                    gen = self.subreddit.new(limit=total)
                    if after:
                        gen.params['after'] = after
                    initial_listing = list(gen)

            logger.debug(f"Processing {len(initial_listing)} submissions against cache...")

            for submission_obj in initial_listing:
                if not isinstance(submission_obj, Submission) or not hasattr(submission_obj, 'id'):
                    logger.warning(f"Skipping invalid object during cache processing: {type(submission_obj)}")
                    continue

                submission_id = submission_obj.id

                # Check cache using the ID
                cached_data = None
                metadata_path_rel = submission_index.get(submission_id)
                if metadata_path_rel:
                    abs_metadata_path = os.path.abspath(os.path.join(cache_dir, metadata_path_rel.replace('/', os.sep)))
                    if os.path.exists(abs_metadata_path):
                        cached_data = read_metadata_file(abs_metadata_path)
                        if cached_data:
                            media_cache_path = cached_data.get('cache_path')
                            if media_cache_path and os.path.exists(media_cache_path):
                                logger.debug(f"Cache HIT for {submission_id}.")
                                cached_obj = SimpleNamespace(**cached_data)
                                snapshot_results.append(cached_obj)
                                continue
                            else:
                                logger.debug(f"Cache MISS for {submission_id}: Media file missing.")
                        else:
                            logger.debug(f"Cache MISS for {submission_id}: Metadata invalid.")
                    else:
                        logger.debug(f"Cache MISS for {submission_id}: Metadata path not found.")
                else:
                    logger.debug(f"Cache MISS for {submission_id}: Not in index.")

                logger.debug(f"Using fetched PRAW object for {submission_id}.")
                snapshot_results.append(submission_obj)

            logger.info(f"Snapshot fetch complete. Returning {len(snapshot_results)} items.")
            return snapshot_results

        except Exception as e:
            logger.exception(f"Error during snapshot fetch: {e}")
            return []

class SnapshotFetcher(QThread):
    """
    Worker thread for asynchronous fetching of Reddit submission snapshots.
    """
    snapshotFetched = pyqtSignal(list)

    def __init__(self, model, total=DEFAULT_POSTS_FETCH_LIMIT, after=None):
        super().__init__()
        self.model = model
        self.total = total
        self.after = after

    def run(self):
        snapshot = self.model.fetch_snapshot(total=self.total, after=self.after)
        self.snapshotFetched.emit(snapshot)

# --- Worker Signals ---
class WorkerSignals(QObject):
    """
    Defines the signals available from a running worker thread.
    Supported signals are:
    finished: No data
    error: tuple (exctype, value, traceback.format_exc())
    result: object data returned from processing, anything
    progress: int indicating % progress
    """
    finished = pyqtSignal()
    error = pyqtSignal(str) # Simplified error signal with just a message
    success = pyqtSignal(str) # Signal for successful completion, with optional message

# --- Background Workers for Moderation ---

class ApproveWorker(QThread):
    """Worker thread to approve a submission."""
    signals = WorkerSignals()

    def __init__(self, submission_id: str, reddit_instance):
        super().__init__()
        self.submission_id = submission_id
        self.reddit_instance = reddit_instance

    def run(self):
        try:
            if not self.submission_id:
                raise ValueError("Missing submission ID.")
            if not self.reddit_instance:
                raise ValueError("Missing PRAW instance.")

            base_id = self.submission_id.split('_')[-1]
            praw_submission = self.reddit_instance.submission(id=base_id)
            praw_submission.mod.approve()
            logger.debug(f"Successfully approved submission via API: {self.submission_id}")

            # Update cache after successful API call
            metadata_path = get_metadata_file_path(self.submission_id)
            if metadata_path:
                metadata = read_metadata_file(metadata_path) or {'id': self.submission_id}
                metadata['approved'] = True
                metadata['removed'] = False
                metadata['moderation_status'] = "approved"
                metadata['last_checked_utc'] = time.time()
                try:
                    praw_submission.load() # Refresh data
                    metadata['score'] = praw_submission.score
                    metadata['num_comments'] = praw_submission.num_comments
                except Exception as refresh_e:
                    logger.warning(f"Could not refresh score/comments for {self.submission_id} after approve: {refresh_e}")

                if write_metadata_file(metadata_path, metadata):
                    logger.debug(f"Updated cached metadata for {self.submission_id} to approved.")
                else:
                    logger.error(f"Failed to write updated metadata cache for approved submission {self.submission_id}.")
            else:
                logger.warning(f"Could not determine metadata cache path for approved submission {self.submission_id}.")

            self.signals.success.emit(self.submission_id) # Emit success with ID

        except Exception as e:
            logger.exception(f"Error approving submission {self.submission_id} in worker: {e}")
            self.signals.error.emit(f"Error approving {self.submission_id}: {str(e)}")
        finally:
            self.signals.finished.emit()

class RemoveWorker(QThread):
    """Worker thread to remove a submission."""
    signals = WorkerSignals()

    def __init__(self, submission_id: str, reddit_instance):
        super().__init__()
        self.submission_id = submission_id
        self.reddit_instance = reddit_instance

    def run(self):
        moderation_status_update = "removed"
        update_cache = False
        error_message = None

        try:
            if not self.submission_id:
                raise ValueError("Missing submission ID.")
            if not self.reddit_instance:
                raise ValueError("Missing PRAW instance.")

            base_id = self.submission_id.split('_')[-1]
            praw_submission = self.reddit_instance.submission(id=base_id)
            praw_submission.mod.remove()
            logger.debug(f"Successfully removed submission via API: {self.submission_id}")
            update_cache = True

        except prawcore.exceptions.Forbidden as e:
            logger.error(f"Forbidden: You do not have permission to remove submission {self.submission_id}")
            error_message = f"Permission denied to remove {self.submission_id}."
        except prawcore.exceptions.RequestException as e:
            if "ConnectTimeout" in str(e) or "ConnectionError" in str(e):
                logger.error(f"Network connection error while removing submission {self.submission_id}: {e}")
                moderation_status_update = "removal_pending" # Mark as pending on network error
                update_cache = True
                # Don't treat network error as a failure for the signal, let UI handle pending state
            else:
                logger.error(f"API request error while removing submission {self.submission_id}: {e}")
                error_message = f"API error removing {self.submission_id}: {str(e)}"
        except Exception as e:
            logger.exception(f"Unexpected error while removing submission {self.submission_id} in worker: {e}")
            error_message = f"Error removing {self.submission_id}: {str(e)}"

        # Update cache if needed (successful removal or network error)
        if update_cache:
            metadata_path = get_metadata_file_path(self.submission_id)
            if metadata_path:
                metadata = read_metadata_file(metadata_path) or {'id': self.submission_id}
                metadata['approved'] = False
                metadata['removed'] = (moderation_status_update == "removed")
                metadata['moderation_status'] = moderation_status_update
                metadata['last_checked_utc'] = time.time()
                if write_metadata_file(metadata_path, metadata):
                    logger.debug(f"Updated cached metadata for {self.submission_id} to {moderation_status_update}.")
                else:
                    logger.error(f"Failed to write updated metadata cache for removed submission {self.submission_id}.")
            else:
                logger.warning(f"Could not determine metadata cache path for removed submission {self.submission_id}.")

        # Emit success if no critical error occurred (pending is considered success for signaling)
        if error_message:
            self.signals.error.emit(error_message)
        else:
            self.signals.success.emit(self.submission_id) # Emit success with ID
        self.signals.finished.emit()


class BanWorker(QThread):
    """Worker thread to ban a user."""
    signals = WorkerSignals()

    def __init__(self, subreddit: Subreddit, username: str, reason: str, message: Optional[str], reddit_instance):
        super().__init__()
        self.subreddit = subreddit
        self.username = username
        self.reason = reason
        self.message = message
        self.reddit_instance = reddit_instance # Needed? Subreddit object should be sufficient

    def run(self):
        try:
            if not self.subreddit:
                raise ValueError("Missing Subreddit object.")
            if not self.username:
                raise ValueError("Missing username.")
            if not self.reason:
                raise ValueError("Missing ban reason.")

            if self.message:
                self.subreddit.banned.add(self.username, ban_reason=self.reason, ban_message=self.message, note=self.reason)
            else:
                self.subreddit.banned.add(self.username, ban_reason=self.reason, note=self.reason)
            logger.debug(f"Banned user {self.username} from {self.subreddit.display_name}")
            self.signals.success.emit(f"User {self.username} banned from r/{self.subreddit.display_name}.")

        except Exception as e:
            logger.exception(f"Error banning user {self.username} from {self.subreddit.display_name} in worker: {e}")
            self.signals.error.emit(f"Error banning {self.username}: {str(e)}")
        finally:
            self.signals.finished.emit()


# --- Request Deduplication Cache ---
_active_report_requests = {}
_request_lock = threading.Lock()

# --- Standalone Report Functions ---

def get_submission_reports(submission_data, reddit_instance) -> tuple[int, list]:
    """
    Get reports for a submission, checking cache first.

    Args:
        submission_data: PRAW Submission object or SimpleNamespace from cache.
        reddit_instance: Active PRAW instance for API calls if needed.

    Returns:
        Tuple of (report_count, list of report reasons)
    """
    submission_id = getattr(submission_data, 'id', None)
    if not submission_id:
        logger.error("Cannot get reports: Missing submission ID.")
        return (0, [])

    # Check for active request to avoid duplicate API calls for the same submission
    with _request_lock:
        if submission_id in _active_report_requests:
            logger.debug(f"Request for reports of {submission_id} already in progress, waiting...")
            # Wait for the existing request to complete
            existing_request = _active_report_requests[submission_id]
        else:
            # Mark this request as active
            existing_request = threading.Event()
            _active_report_requests[submission_id] = existing_request

    # If we're waiting for an existing request, wait for it to complete then check cache
    if submission_id in _active_report_requests and _active_report_requests[submission_id] != existing_request:
        _active_report_requests[submission_id].wait(timeout=10)  # Wait up to 10 seconds
        # Try cache again after the other request completes
        metadata_path = get_metadata_file_path(submission_id)
        if metadata_path:
            metadata = read_metadata_file(metadata_path)
            if metadata and 'report_count' in metadata:
                last_checked = metadata.get('last_checked_utc', 0)
                current_time = time.time()
                if current_time - last_checked < 300:  # 5 minutes TTL
                    report_count = metadata.get('report_count', 0)
                    report_reasons = metadata.get('report_reasons', [])
                    logger.debug(f"Using cached reports after deduplication wait for {submission_id}: {report_count} reports.")
                    return (report_count, report_reasons)

    metadata_path = get_metadata_file_path(submission_id)
    if metadata_path:
        metadata = read_metadata_file(metadata_path)
        if metadata and 'report_count' in metadata:
            # Check if cached reports are still fresh (TTL: 5 minutes for reports)
            last_checked = metadata.get('last_checked_utc', 0)
            current_time = time.time()
            cache_ttl_seconds = 300  # 5 minutes

            if current_time - last_checked < cache_ttl_seconds:
                report_count = metadata.get('report_count', 0)
                report_reasons = metadata.get('report_reasons', [])
                logger.debug(f"Using cached reports for {submission_id}: {report_count} reports (cached {int(current_time - last_checked)}s ago).")
                return (report_count, report_reasons)
            else:
                logger.debug(f"Cached reports for {submission_id} expired ({int(current_time - last_checked)}s old), fetching fresh data.")

    logger.debug(f"No valid cache for reports of {submission_id}. Fetching from API.")
    if not reddit_instance:
        logger.error(f"Cannot fetch reports for {submission_id}: Missing PRAW instance.")
        return (0, [])

    try:
        base_id = submission_id.split('_')[-1]
        praw_submission = reddit_instance.submission(id=base_id)

        mod_reports = getattr(praw_submission, 'mod_reports', [])
        user_reports = getattr(praw_submission, 'user_reports', [])

        formatted_reports = []
        for reason, moderator in mod_reports:
            formatted_reports.append(f"Moderator {moderator}: {reason}")

        user_report_count = 0
        for report_item in user_reports:
            try:
                if isinstance(report_item, (list, tuple)) and len(report_item) >= 2:
                    reason, count = report_item[0], report_item[1]
                    if isinstance(count, int):
                        user_report_count += count
                        formatted_reports.append(f"Users ({count}): {reason}" if count > 1 else f"User: {reason}")
                    else:
                        user_report_count += 1
                        formatted_reports.append(f"User: {reason} ({count})")
                else:
                    user_report_count += 1
                    formatted_reports.append(f"Report: {report_item}")
            except Exception as item_e:
                logger.error(f"Error processing report item {report_item}: {item_e}")
                user_report_count += 1
                formatted_reports.append("Unprocessable report")

        total_reports = len(mod_reports) + user_report_count
        result = (total_reports, formatted_reports)

        if metadata_path:
            metadata = read_metadata_file(metadata_path) or {'id': submission_id}
            metadata['report_count'] = total_reports
            metadata['report_reasons'] = formatted_reports
            metadata['last_checked_utc'] = time.time()
            if write_metadata_file(metadata_path, metadata):
                logger.debug(f"Cached fetched reports for {submission_id}.")
            else:
                logger.error(f"Failed to cache fetched reports for {submission_id}.")
        else:
            logger.error(f"Could not determine metadata path to cache reports for {submission_id}.")

        if total_reports > 0:
            logger.debug(f"Submission {submission_id} has {total_reports} reports")

        return result
    except Exception as e:
        logger.exception(f"Error getting reports for submission {submission_id}: {e}")
        return (0, [])
    finally:
        # Clean up the active request tracking
        with _request_lock:
            if submission_id in _active_report_requests:
                _active_report_requests[submission_id].set()  # Signal completion
                del _active_report_requests[submission_id]

