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

# Set up logging
logger = logging.getLogger(__name__)

# Global dictionary for storing moderation statuses (e.g., "approved" or "removed")
moderation_statuses = {}

class RedditGalleryModel:
    """
    Model class for Reddit gallery data.
    Handles fetching and storing submissions from subreddits or user profiles.
    """
    def __init__(self, name: str, is_user_mode: bool = False, reddit_instance=None):
        """
        Initialize the gallery model.
        
        Args:
            name: Subreddit name or username
            is_user_mode: If True, name is treated as a username
            reddit_instance: PRAW Reddit instance to use
        """
        self.is_user_mode = is_user_mode
        self.is_moderator = False
        self.snapshot = []  # Snapshot of submissions (up to 100)
        self.source_name = name
        self.reddit = reddit_instance
        
        if self.reddit:
            if self.is_user_mode:
                self.user = self.reddit.redditor(name)
            else:
                self.subreddit = self.reddit.subreddit(name)

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
                # Fetch user submissions
                snapshot = list(self.user.submissions.new(limit=total, params=params))
            else:
                # Fetch subreddit submissions
                if self.is_moderator:
                    # For moderators, fetch a mix of submissions including modqueue
                    new_subs = list(self.subreddit.new(limit=total, params=params))
                    mod_subs = list(self.subreddit.mod.modqueue(limit=total))
                    mod_log_entries = list(self.subreddit.mod.log(action="removelink", limit=total))
                    removed_fullnames = {entry.target_fullname for entry in mod_log_entries if entry.target_fullname.startswith("t3_")}
                    removed_subs = list(self.reddit.info(fullnames=list(removed_fullnames)))
                    
                    # Mark submissions as removed in our moderation_statuses dictionary
                    for sub in removed_subs:
                        moderation_statuses[sub.id] = "removed"
                    
                    # Merge all submissions and sort by creation date
                    merged = {s.id: s for s in new_subs + mod_subs + removed_subs}
                    snapshot = list(merged.values())
                    snapshot.sort(key=lambda s: s.created_utc, reverse=True)
                    # Filter out objects without a title (e.g. comments)
                    snapshot = [s for s in snapshot if hasattr(s, 'title')]
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