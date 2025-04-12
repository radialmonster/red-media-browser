#!/usr/bin/env python3
"""
Reddit API Module for Red Media Browser

This module handles all Reddit API interactions, including fetching posts,
pagination, and moderation actions.
"""

import logging
from typing import List, Dict, Tuple, Optional, Set, Any
import praw
import prawcore.exceptions
from PyQt6.QtCore import QThread, pyqtSignal

# Import the main app's ModLogFetcher for type hinting if needed, or just use dict
# from red_media_browser import ModLogFetcher # Avoid circular import if possible

# Set up logging
logger = logging.getLogger(__name__)

# Global dictionary for storing moderation statuses (e.g., "approved" or "removed")
moderation_statuses = {}

# Global dictionary for caching submission reports
submission_reports = {}

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
        self.reddit = reddit_instance
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
        if self.is_user_mode:
            return False
            
        try:
            logger.debug("Performing moderator status check")
            moderators = list(self.subreddit.moderator())
            user = self.reddit.user.me()
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

    def fetch_submissions(self, after=None, count=10) -> Tuple[List[Any], Optional[str]]:
        """
        Fetch a page of submissions from Reddit.
        
        Args:
            after: Reddit fullname to fetch posts after
            count: Number of posts to fetch
            
        Returns:
            Tuple of (list of submissions, 'after' parameter for next page)
        """
        submissions = []
        new_after = None
        try:
            if self.is_user_mode:
                # Fetch user submissions
                already_fetched = sum(len(page) for page in self.snapshot) if self.snapshot else 0
                user_params = {
                    'limit': count,
                    'count': already_fetched
                }
                if after:
                    user_params['after'] = after
                submissions = list(self.user.submissions.new(limit=count, params=user_params))
            else:
                # Fetch subreddit submissions
                if self.is_moderator:
                    # For moderators, fetch a mix of submissions including modqueue
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
                    removed_subs = list(self.reddit.info(fullnames=list(removed_fullnames)))
                    
                    # Mark submissions as removed in our moderation_statuses dictionary
                    for sub in removed_subs:
                        moderation_statuses[sub.id] = "removed"
                    
                    # Merge submissions by unique id
                    merged = {s.id: s for s in new_subs + mod_subs + removed_subs}
                    submissions = list(merged.values())
                    submissions.sort(key=lambda s: s.created_utc, reverse=True)
                else:
                    # For regular users, just fetch new submissions
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
                    # Ensure removed posts are not shown in non-mod view
                    submissions = [s for s in submissions if moderation_statuses.get(s.id) != "removed"]
            
            # Update the 'after' parameter for the next page if we have results
            if submissions:
                new_after = submissions[-1].name
        except Exception as e:
            logger.exception(f"Error fetching submissions: {e}")
        return submissions, new_after

    def fetch_snapshot(self, total=100, after=None) -> List[Any]:
        """
        Fetch a larger batch of submissions to use for pagination.
        
        Args:
            total: Total number of submissions to fetch
            after: Reddit fullname to fetch posts after
            
        Returns:
            List of submissions
        """
        try:
            params = {'raw_json': 1, 'sort': 'new'}
            if after:
                params['after'] = after
            
            if self.is_user_mode:
                if not self.user:
                    logger.error("Cannot fetch user submissions, user object is None.")
                    return []
                # Fetch user submissions
                user_submissions = list(self.user.submissions.new(limit=total, params=params))
                logger.debug(f"Fetched {len(user_submissions)} initial posts for user {self.source_name}")

                # --- Logic to find and merge removed posts using pre-fetched logs ---
                removed_post_fullnames = set()
                target_username_lower = self.source_name.lower()

                if not self.logs_ready:
                    logger.warning("Mod logs not yet loaded. Removed posts might be missing from user view.")
                else:
                    logger.debug(f"Checking pre-fetched logs for removed posts by {target_username_lower} across {len(self.moderated_subreddit_names)} moderated subs.")
                    for sub_name in self.moderated_subreddit_names:
                        log_entries = self.prefetched_logs.get(sub_name, [])
                        found_in_sub = 0
                        for entry in log_entries:
                            # Check if the author matches the target user
                            if entry.get('author') == target_username_lower:
                                fullname = entry.get('fullname')
                                if fullname:
                                    removed_post_fullnames.add(fullname)
                                    found_in_sub += 1
                        if found_in_sub > 0:
                             logger.debug(f"Found {found_in_sub} potential removed posts by {target_username_lower} in r/{sub_name} log.")

                if removed_post_fullnames:
                    logger.info(f"Found {len(removed_post_fullnames)} potential removed posts across all moderated logs for user {target_username_lower}.")
                    try:
                        # Fetch the actual submission objects for removed posts
                        # Filter fullnames that might already be in user_submissions to avoid redundant fetch
                        existing_fullnames = {sub.fullname for sub in user_submissions}
                        fullnames_to_fetch = list(removed_post_fullnames - existing_fullnames)

                        removed_submissions = []
                        if fullnames_to_fetch:
                             removed_submissions = list(self.reddit.info(fullnames=fullnames_to_fetch))
                             logger.debug(f"Fetched {len(removed_submissions)} submission objects for removed posts.")
                        else:
                             logger.debug("All potential removed posts were already in the initial user fetch.")


                        # Mark these as removed in the global status dict
                        for sub in removed_submissions:
                            moderation_statuses[sub.id] = "removed"
                            logger.debug(f"Marked {sub.id} as removed based on mod log.")

                        # Merge removed_submissions with user_submissions
                        merged_dict = {sub.id: sub for sub in user_submissions}
                        # Add/overwrite with removed submissions (ensures they are included)
                        for sub in removed_submissions:
                             merged_dict[sub.id] = sub

                        snapshot = list(merged_dict.values())
                        # Sort the final list by creation time
                        snapshot.sort(key=lambda s: s.created_utc, reverse=True)
                        logger.debug(f"Merged snapshot size after adding removed posts: {len(snapshot)}")

                    except Exception as e:
                        logger.exception(f"Error fetching or merging removed posts for user {target_username_lower}: {e}")
                        # Fallback to just the initially fetched user submissions
                        snapshot = user_submissions
                else:
                    # No removed posts found in logs, use original list
                    snapshot = user_submissions
                # --- End of removed posts logic ---

            else:
                # Fetch subreddit submissions (existing logic)
                if not self.subreddit:
                    logger.error("Cannot fetch subreddit submissions, subreddit object is None.")
                    return []
                if self.check_user_moderation_status(): # Check mod status here
                    # For moderators, fetch a mix of submissions including modqueue and reported posts
                    new_subs = list(self.subreddit.new(limit=total, params=params))
                    logger.debug(f"Fetched {len(new_subs)} new posts from subreddit")
                    
                    # IMPORTANT: Get reported posts directly from the reports feed
                    # This is a more direct way to get reported posts than checking modqueue
                    try:
                        reported_subs = list(self.subreddit.mod.reports(limit=total//2))
                        logger.debug(f"Fetched {len(reported_subs)} reported posts directly from reports feed")
                    except Exception as e:
                        logger.error(f"Error fetching directly from reports feed: {e}")
                        reported_subs = []
                    
                    # Get posts from modqueue as backup
                    mod_limit = min(total // 2, 50)
                    mod_subs = list(self.subreddit.mod.modqueue(limit=mod_limit))
                    logger.debug(f"Fetched {len(mod_subs)} posts from modqueue")
                    
                    # Log all reported posts we found - with safer extraction of report counts
                    reported_count = 0
                    for sub in reported_subs:
                        # Check if the submission has reports and is a submission (not a comment)
                        if hasattr(sub, 'title'):
                            try:
                                mod_reports = getattr(sub, 'mod_reports', [])
                                user_reports = getattr(sub, 'user_reports', [])
                                
                                # Safely calculate report count - don't assume structure
                                mod_report_count = len(mod_reports)
                                
                                # User reports might be in different formats, so handle carefully
                                user_report_count = 0
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
                                
                                total_report_count = mod_report_count + user_report_count
                                
                                if total_report_count > 0:
                                    reported_count += 1
                                    logger.debug(f"Found reported post in reports feed: {sub.id} with {total_report_count} reports")
                            except Exception as e:
                                logger.error(f"Error processing reports for submission {sub.id}: {e}")
                    
                    logger.debug(f"Found {reported_count} posts with reports")
                    
                    # Fetch removed submissions from the mod log, but limit the number
                    mod_log_limit = min(total // 4, 25)  # Even fewer for removed posts
                    mod_log_entries = list(self.subreddit.mod.log(action="removelink", limit=mod_log_limit))
                    removed_fullnames = {entry.target_fullname for entry in mod_log_entries if entry.target_fullname.startswith("t3_")}
                    removed_subs = list(self.reddit.info(fullnames=list(removed_fullnames))) if removed_fullnames else []
                    logger.debug(f"Fetched {len(removed_subs)} posts from mod log (removed posts)")
                    
                    # Mark submissions as removed in our moderation_statuses dictionary
                    for sub in removed_subs:
                        moderation_statuses[sub.id] = "removed"
                    
                    # Pre-fetch reports for all submissions from the reports feed
                    for sub in reported_subs:
                        # Only fetch reports if this is a submission (not a comment)
                        if hasattr(sub, 'title'):
                            try:
                                reports_count, reports_reasons = get_submission_reports(sub)
                                if reports_count > 0:
                                    logger.debug(f"Submission {sub.id} has {reports_count} reports: {reports_reasons}")
                            except Exception as e:
                                logger.error(f"Error pre-fetching reports for submission {sub.id}: {e}")
                    
                    # Merge submissions while ensuring new posts have higher priority
                    # But reported posts should have the HIGHEST priority
                    new_dict = {s.id: s for s in new_subs}
                    reported_dict = {s.id: s for s in reported_subs if hasattr(s, 'title')}
                    mod_dict = {s.id: s for s in mod_subs}
                    removed_dict = {s.id: s for s in removed_subs}
                    
                    # Now merge, with reports having highest priority
                    merged = {}
                    merged.update(removed_dict)  # Lowest priority
                    merged.update(mod_dict)      # Medium priority
                    merged.update(new_dict)      # High priority
                    merged.update(reported_dict) # Highest priority - these will override any duplicates
                    
                    snapshot = list(merged.values())
                    
                    # Special sort: First sort reported posts by creation time, then non-removed posts, then removed posts
                    # Safely identify reported posts
                    reported = []
                    for s in snapshot:
                        try:
                            has_reports = False
                            if hasattr(s, 'mod_reports') and s.mod_reports:
                                has_reports = True
                            if hasattr(s, 'user_reports') and s.user_reports:
                                has_reports = True
                            if has_reports:
                                reported.append(s)
                        except Exception as e:
                            logger.error(f"Error checking reports for post {s.id}: {e}")
                    
                    non_reported_non_removed = [s for s in snapshot if s not in reported and 
                                             moderation_statuses.get(s.id) != "removed"]
                    removed = [s for s in snapshot if moderation_statuses.get(s.id) == "removed"]
                    
                    # Sort each group by creation time, newest first
                    reported.sort(key=lambda s: s.created_utc, reverse=True)
                    non_reported_non_removed.sort(key=lambda s: s.created_utc, reverse=True)
                    removed.sort(key=lambda s: s.created_utc, reverse=True)
                    
                    # Combine with reported posts first, then regular posts, then removed posts
                    snapshot = reported + non_reported_non_removed + removed
                    
                    # Filter out objects without a title (e.g. comments)
                    snapshot = [s for s in snapshot if hasattr(s, 'title')]
                    
                    logger.debug(f"Total submissions after merging: {len(snapshot)}")
                    logger.debug(f"Reported posts: {len(reported)}, Regular posts: {len(non_reported_non_removed)}, Removed posts: {len(removed)}")
                else:
                    # For regular users, just fetch new submissions
                    snapshot = list(self.subreddit.new(limit=total, params=params))
                    # Ensure removed posts are not shown in non-mod view
                    snapshot = [s for s in snapshot if moderation_statuses.get(s.id) != "removed"]
            
            logger.debug(f"Fetched snapshot of {len(snapshot)} submissions.")
            return snapshot
        except Exception as e:
            logger.exception(f"Error fetching snapshot: {e}")
            return []

class SnapshotFetcher(QThread):
    """
    Worker thread for asynchronous fetching of Reddit submission snapshots.
    """
    snapshotFetched = pyqtSignal(list)
    
    def __init__(self, model, total=100, after=None):
        super().__init__()
        self.model = model
        self.total = total
        self.after = after
        
    def run(self):
        snapshot = self.model.fetch_snapshot(total=self.total, after=self.after)
        self.snapshotFetched.emit(snapshot)

def approve_submission(submission):
    """
    Approve a Reddit submission.
    
    Args:
        submission: PRAW Submission object to approve
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        submission.mod.approve()
        moderation_statuses[submission.id] = "approved"
        logger.debug(f"Approved submission: {submission.id}")
        return True
    except Exception as e:
        logger.exception(f"Error approving submission {submission.id}: {e}")
        return False

def remove_submission(submission):
    """
    Remove a Reddit submission.
    
    Args:
        submission: PRAW Submission object to remove
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        submission.mod.remove()
        moderation_statuses[submission.id] = "removed"
        logger.debug(f"Removed submission: {submission.id}")
        return True
    except prawcore.exceptions.Forbidden:
        logger.error(f"Forbidden: You do not have permission to remove submission {submission.id}")
        return False
    except prawcore.exceptions.RequestException as e:
        if "ConnectTimeout" in str(e) or "ConnectionError" in str(e):
            logger.error(f"Network connection error while removing submission {submission.id}: {e}")
            # Still mark as "removal_pending" so the UI can show appropriate state
            moderation_statuses[submission.id] = "removal_pending"
        else:
            logger.error(f"API request error while removing submission {submission.id}: {e}")
        return False
    except Exception as e:
        logger.exception(f"Unexpected error while removing submission {submission.id}: {e}")
        return False

def get_submission_reports(submission):
    """
    Get reports for a submission.
    
    Args:
        submission: PRAW Submission object
    
    Returns:
        Tuple of (report_count, list of report reasons)
    """
    try:
        # Check if we already have cached reports for this submission
        if submission.id in submission_reports:
            return submission_reports[submission.id]
        
        # Get the mod reports and user reports directly from the submission attributes
        mod_reports = getattr(submission, 'mod_reports', [])
        user_reports = getattr(submission, 'user_reports', [])
        
        # Format the reports as strings
        formatted_reports = []
        
        # Add mod reports: [(report_reason, mod_name), ...]
        for reason, moderator in mod_reports:
            formatted_reports.append(f"Moderator {moderator}: {reason}")
        
        # Add user reports with safer handling for different formats
        user_report_count = 0
        
        for report_item in user_reports:
            try:
                if isinstance(report_item, (list, tuple)):
                    if len(report_item) >= 2:
                        reason = report_item[0]
                        if isinstance(report_item[1], int):
                            user_report_count += report_item[1]
                            if report_item[1] > 1:
                                formatted_reports.append(f"Users ({report_item[1]}): {reason}")
                            else:
                                formatted_reports.append(f"User: {reason}")
                        else:
                            user_report_count += 1
                            formatted_reports.append(f"User: {reason} ({report_item[1]})")
                    else:
                        # If it doesn't have at least 2 items, count it as 1
                        user_report_count += 1
                        formatted_reports.append(f"Report: {report_item}")
                else:
                    # If it's not a tuple/list, count it as 1
                    user_report_count += 1
                    formatted_reports.append(f"Report: {report_item}")
            except Exception as e:
                logger.error(f"Error processing report item: {e}")
                user_report_count += 1
                formatted_reports.append("Unprocessable report")
        
        # Calculate total report count
        total_reports = len(mod_reports) + user_report_count
        
        # Cache the results
        result = (total_reports, formatted_reports)
        submission_reports[submission.id] = result
        
        # Minimal logging
        if total_reports > 0:
            logger.debug(f"Submission {submission.id} has {total_reports} reports")
            
        return result
    except Exception as e:
        logger.exception(f"Error getting reports for submission {submission.id}: {e}")
        return (0, [])

def ban_user(subreddit, username, reason, message=None):
    """
    Ban a user from a subreddit.
    
    Args:
        subreddit: PRAW Subreddit object
        username: Username to ban
        reason: Ban reason (for mod notes)
        message: Optional message to send to the user
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        if message:
            subreddit.banned.add(username, ban_reason=reason, ban_message=message, note=reason)
        else:
            subreddit.banned.add(username, ban_reason=reason, note=reason)
        logger.debug(f"Banned user {username} from {subreddit.display_name}")
        return True
    except Exception as e:
        logger.exception(f"Error banning user {username} from {subreddit.display_name}: {e}")
        return False
