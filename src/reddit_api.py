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
# PRAW 8's Reddit.user.me() raises ReadOnlyException in read-only mode instead of
# returning None. Import defensively so older PRAW and the stubbed test environment
# (which provides no praw.exceptions) still load this module.
try:
    from praw.exceptions import ReadOnlyException
except Exception:  # pragma: no cover - older PRAW or stubbed test environment
    class ReadOnlyException(Exception):
        """Fallback when praw.exceptions.ReadOnlyException is unavailable."""
from types import SimpleNamespace # Import SimpleNamespace
from PyQt6.QtCore import QThread, pyqtSignal, QObject # Import QObject for worker signals
# Import the Submission class for type checking
from praw.models import Submission, Subreddit

# Import caching utilities
from utils import (
    load_submission_index, get_metadata_file_path, read_metadata_file,
    write_metadata_file, get_cache_dir, update_metadata_cache, file_exists_in_cache,
    get_cached_submission_media_match
)

# Import constants
from constants import DEFAULT_POSTS_FETCH_LIMIT, REPORT_CACHE_TTL_SECONDS

# Set up logging
logger = logging.getLogger(__name__)

SNAPSHOT_METADATA_HYDRATION_FIELDS = (
    'approved',
    'removed',
    'moderation_status',
    'report_count',
    'report_reasons',
    'report_last_checked_utc',
    'media_assets',
    'cache_path',
    'media_url',
    'last_checked_utc',
)


class SnapshotFetchError(Exception):
    """Raised when a snapshot load fails and the UI should show an error."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def hydrate_submission_with_cached_metadata(submission: Submission, metadata: Dict[str, Any]) -> None:
    """Attach cache-derived fields to a live submission without overwriting Reddit-owned fields."""
    if not submission or not isinstance(metadata, dict):
        return

    for field in SNAPSHOT_METADATA_HYDRATION_FIELDS:
        if field not in metadata:
            continue

        value = metadata[field]
        if isinstance(value, list):
            value = list(value)
        elif isinstance(value, dict):
            value = dict(value)

        try:
            setattr(submission, field, value)
        except Exception as error:
            logger.debug(
                "Unable to hydrate cached field %s onto submission %s: %s",
                field,
                getattr(submission, 'id', 'unknown'),
                error,
            )

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
        try:
            user = reddit_instance.user.me()
        except ReadOnlyException:
            logger.error("Reddit instance is read-only; cannot fetch moderated subreddits without authentication.")
            return []
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
        if not self.isInterruptionRequested():
            self.subredditsFetched.emit(mod_subreddits)

class RedditGalleryModel:
    """
    Model class for Reddit gallery data.
    Handles fetching and storing submissions from subreddits or user profiles.
    """
    _cached_moderated_subreddit_names_by_user: Dict[str, Set[str]] = {}

    def __init__(self, name: str, is_user_mode: bool = False, reddit_instance=None,
                 prefetched_mod_logs: Optional[Dict[str, List[Dict]]] = None,
                 mod_logs_ready: bool = False,
                 moderated_subreddit_names: Optional[Set[str]] = None):
        """
        Initialize the gallery model.

        Args:
            name: Subreddit name or username
            is_user_mode: If True, name is treated as a username
            reddit_instance: PRAW Reddit instance to use
            prefetched_mod_logs: Dictionary of pre-fetched mod logs keyed by subreddit name.
            mod_logs_ready: Flag indicating if pre-fetched logs are ready.
            moderated_subreddit_names: Already fetched moderated subreddit names.
        """
        self.is_user_mode = is_user_mode
        self.is_moderator = False # Specific to subreddit view, determined later
        self.snapshot = []  # Snapshot of submissions (up to 100)
        self.source_name = name
        self.reddit = reddit_instance # Store the PRAW instance
        self.prefetched_logs = prefetched_mod_logs if prefetched_mod_logs is not None else {}
        self.logs_ready = mod_logs_ready

        if moderated_subreddit_names is None and self.reddit is not None:
            # No names supplied by the caller: hydrate once per authenticated user
            # from the shared cross-model cache. Gated on an explicit ``None`` (not an
            # empty set) so the app's normal construction path -- which always passes
            # its own set -- never triggers an extra me()/moderator_subreddits() call.
            names_source = self._get_cached_moderated_subreddit_names()
        else:
            names_source = moderated_subreddit_names or set()

        self.moderated_subreddit_names = {
            str(name).strip().lower()
            for name in names_source
            if str(name).strip()
        } # Store names of subs the app user mods

        if self.reddit:
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

        logger.debug(
            f"Model initialized with {len(self.moderated_subreddit_names)} moderated subreddit names."
        )

    @classmethod
    def clear_moderated_subreddit_cache(cls, username: Optional[str] = None) -> None:
        """Clear cached moderated-subreddit names for one user or all users."""
        if username is None:
            cls._cached_moderated_subreddit_names_by_user.clear()
            return

        normalized_username = str(username).strip().lower()
        if normalized_username:
            cls._cached_moderated_subreddit_names_by_user.pop(normalized_username, None)

    @classmethod
    def set_cached_moderated_subreddit_names(cls, username: Optional[str], names: Set[str]) -> None:
        """Store moderated-subreddit names fetched by the background worker."""
        normalized_username = str(username or "").strip().lower()
        if not normalized_username:
            return

        cls._cached_moderated_subreddit_names_by_user[normalized_username] = {
            str(name).strip().lower()
            for name in names
            if str(name).strip()
        }

    def _get_authenticated_username(self) -> Optional[str]:
        """Return the active authenticated username for cache namespacing."""
        if not self.reddit:
            return None

        try:
            user = self.reddit.user.me()
        except ReadOnlyException:
            logger.debug("Reddit instance is read-only; no authenticated username for mod cache.")
            return None
        except Exception as error:
            logger.debug(f"Unable to determine authenticated username for mod cache: {error}")
            return None

        username = getattr(user, 'name', None)
        if not username:
            return None

        return str(username).strip().lower() or None

    def _get_cached_moderated_subreddit_names(self) -> Set[str]:
        """Load moderated subreddit names once per authenticated user and reuse them across models."""
        username = self._get_authenticated_username()
        if not username:
            mod_subs_info = get_moderated_subreddits(self.reddit)
            return {sub['name'] for sub in mod_subs_info}

        if username not in RedditGalleryModel._cached_moderated_subreddit_names_by_user:
            mod_subs_info = get_moderated_subreddits(self.reddit)
            RedditGalleryModel._cached_moderated_subreddit_names_by_user[username] = {
                sub['name'] for sub in mod_subs_info
            }

        return set(RedditGalleryModel._cached_moderated_subreddit_names_by_user[username])

    def check_user_moderation_status(self) -> bool:
        """
        Check if the current Reddit user is a moderator of the current subreddit.

        Returns:
            bool: True if user is a moderator, False otherwise
        """
        if self.is_user_mode or not self.subreddit or not self.reddit:
            return False

        try:
            subreddit_name = getattr(self.subreddit, 'display_name', self.source_name).lower()
            self.is_moderator = subreddit_name in self.moderated_subreddit_names
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

        # 1. Load the metadata index
        submission_index = load_submission_index()
        cache_dir = get_cache_dir()

        # 2. Get next page of Submission objects from Reddit API
        try:
            initial_listing = []
            # PRAW's .new() does NOT accept 'after' as a direct argument, but the ListingGenerator supports .params
            if self.is_user_mode:
                if not self.user:
                    raise SnapshotFetchError(
                        f"Unable to load user '{self.source_name}': user object is unavailable."
                    )
                try:
                    gen = self.user.submissions.new(limit=total)
                    if after:
                        gen.params['after'] = after
                    initial_listing = list(gen)
                    logger.debug(f"Fetched {len(initial_listing)} items for user {self.source_name} (after={after})")
                except prawcore.exceptions.NotFound:
                    logger.warning(f"User '{self.source_name}' not found or inaccessible (404).")
                    raise SnapshotFetchError(f"User '{self.source_name}' was not found or is inaccessible.")
                except prawcore.exceptions.Forbidden:
                    logger.warning(f"User '{self.source_name}' is forbidden or inaccessible.")
                    raise SnapshotFetchError(f"User '{self.source_name}' is inaccessible with the current account.")
                except prawcore.exceptions.PrawcoreException as user_fetch_err:
                    logger.error(f"PRAW error fetching submissions for user {self.source_name}: {user_fetch_err}")
                    raise SnapshotFetchError(f"Reddit request failed while loading user '{self.source_name}'.")
                except Exception as user_fetch_err:
                    logger.exception(f"Error fetching submissions for user {self.source_name}: {user_fetch_err}")
                    raise SnapshotFetchError(f"Unexpected error while loading user '{self.source_name}'.")
                # Optionally: add removed posts from logs (not paginated, so skip for "next 100" fetches)
            else:
                if not self.subreddit:
                    raise SnapshotFetchError(
                        f"Unable to load subreddit '{self.source_name}': subreddit object is unavailable."
                    )
                is_mod = self.check_user_moderation_status()

                try:
                    if is_mod:
                        logger.debug("Fetching moderator view sources...")
                    else:
                        logger.debug("Fetching regular view...")
                    gen = self.subreddit.new(limit=total)
                    if after:
                        gen.params['after'] = after
                    initial_listing = list(gen)
                    if is_mod:
                        logger.debug(f"Fetched {len(initial_listing)} items from mod 'new' listing (after={after})")
                except prawcore.exceptions.NotFound:
                    logger.warning(f"Subreddit '{self.source_name}' not found or inaccessible (404).")
                    raise SnapshotFetchError(f"Subreddit '{self.source_name}' was not found or is inaccessible.")
                except prawcore.exceptions.Forbidden:
                    logger.warning(f"Subreddit '{self.source_name}' is forbidden or inaccessible.")
                    raise SnapshotFetchError(
                        f"Subreddit '{self.source_name}' is private or inaccessible with the current account."
                    )
                except prawcore.exceptions.PrawcoreException as subreddit_fetch_err:
                    logger.error(
                        f"PRAW error fetching submissions for subreddit {self.source_name}: {subreddit_fetch_err}"
                    )
                    raise SnapshotFetchError(
                        f"Reddit request failed while loading subreddit '{self.source_name}'."
                    )
                except Exception as subreddit_fetch_err:
                    logger.exception(
                        f"Unexpected error fetching submissions for subreddit {self.source_name}: {subreddit_fetch_err}"
                    )
                    raise SnapshotFetchError(f"Unexpected error while loading subreddit '{self.source_name}'.")

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
                            if get_cached_submission_media_match(cached_data):
                                logger.debug(f"Cache HIT for {submission_id}.")
                                cached_obj = SimpleNamespace(**cached_data)
                                snapshot_results.append(cached_obj)
                                continue
                            else:
                                hydrate_submission_with_cached_metadata(submission_obj, cached_data)
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

        except SnapshotFetchError:
            raise
        except Exception as e:
            logger.exception(f"Error during snapshot fetch: {e}")
            raise SnapshotFetchError(f"Unexpected error while loading {self.source_name}.")

class SnapshotFetcher(QThread):
    """
    Worker thread for asynchronous fetching of Reddit submission snapshots.
    """
    snapshotFetched = pyqtSignal(list)
    snapshotFailed = pyqtSignal(str)

    def __init__(self, model, total=DEFAULT_POSTS_FETCH_LIMIT, after=None):
        super().__init__()
        self.model = model
        self.total = total
        self.after = after

    def run(self):
        try:
            snapshot = self.model.fetch_snapshot(total=self.total, after=self.after)
        except SnapshotFetchError as error:
            if not self.isInterruptionRequested():
                self.snapshotFailed.emit(error.message)
            return
        except Exception as error:
            logger.exception(f"Unexpected SnapshotFetcher failure for {self.model.source_name}: {error}")
            if not self.isInterruptionRequested():
                self.snapshotFailed.emit(f"Unexpected error while loading {self.model.source_name}.")
            return

        if not self.isInterruptionRequested():
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
    def __init__(self, submission_id: str, reddit_instance):
        super().__init__()
        self.submission_id = submission_id
        self.reddit_instance = reddit_instance
        self.signals = WorkerSignals()

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
    def __init__(self, submission_id: str, reddit_instance):
        super().__init__()
        self.submission_id = submission_id
        self.reddit_instance = reddit_instance
        self.signals = WorkerSignals()

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
    def __init__(self, subreddit: Subreddit, username: str, reason: str, message: Optional[str], reddit_instance):
        super().__init__()
        self.subreddit = subreddit
        self.username = username
        self.reason = reason
        self.message = message
        self.reddit_instance = reddit_instance # Needed? Subreddit object should be sufficient
        self.signals = WorkerSignals()

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

def _read_fresh_report_cache(submission_id: str) -> Optional[tuple[int, list]]:
    """Return cached report data when it exists and is still fresh."""
    metadata_path = get_metadata_file_path(submission_id)
    if not metadata_path:
        return None

    metadata = read_metadata_file(metadata_path)
    if not metadata or 'report_count' not in metadata:
        return None

    last_checked = (
        metadata.get('report_last_checked_utc')
        or metadata.get('last_checked_utc', 0)
    )
    current_time = time.time()
    if current_time - last_checked >= REPORT_CACHE_TTL_SECONDS:
        logger.debug(
            f"Cached reports for {submission_id} expired ({int(current_time - last_checked)}s old), fetching fresh data."
        )
        return None

    return (
        metadata.get('report_count', 0),
        metadata.get('report_reasons', []),
    )

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

    cached_reports = _read_fresh_report_cache(submission_id)
    if cached_reports is not None:
        logger.debug(
            f"Using cached reports for {submission_id}: {cached_reports[0]} reports."
        )
        return cached_reports

    while True:
        with _request_lock:
            existing_request = _active_report_requests.get(submission_id)
            if existing_request is None:
                _active_report_requests[submission_id] = threading.Event()
                break

        logger.debug(f"Request for reports of {submission_id} already in progress, waiting...")
        existing_request.wait(timeout=10)
        cached_reports = _read_fresh_report_cache(submission_id)
        if cached_reports is not None:
            logger.debug(
                f"Using cached reports after deduplication wait for {submission_id}: {cached_reports[0]} reports."
            )
            return cached_reports

    try:
        logger.debug(f"No valid cache for reports of {submission_id}. Fetching from API.")
        if not reddit_instance:
            logger.error(f"Cannot fetch reports for {submission_id}: Missing PRAW instance.")
            return (0, [])

        base_id = submission_id.split('_')[-1]
        praw_submission = reddit_instance.submission(id=base_id)
        metadata_path = get_metadata_file_path(submission_id)

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
            metadata['report_last_checked_utc'] = time.time()
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

