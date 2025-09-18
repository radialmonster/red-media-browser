#!/usr/bin/env python3
"""
Constants for Red Media Browser Application

This module contains all application-wide constants to avoid hardcoded magic numbers
and improve maintainability.
"""

# Cache and Performance Constants
PIXMAP_CACHE_SIZE_MB = 100
POSTS_FETCH_LIMIT = 500
DEFAULT_POSTS_FETCH_LIMIT = 100
MOD_LOG_FETCH_LIMIT = 1000

# UI Update Timing Constants (in milliseconds)
UI_UPDATE_DELAY_MS = 50
MOD_STATUS_DELAY_MS = 5000
THREAD_TERMINATION_TIMEOUT_MS = 2000

# Video/Media Playback Constants (in milliseconds)
VIDEO_PLAYBACK_CHECK_INTERVAL_MS = 500
VIDEO_ASPECT_RATIO_DELAY_MS = 500
PLAYBACK_MONITOR_INTERVAL_MS = 1000
FULLSCREEN_CLOSE_DELAY_MS = 500
GIF_FRAME_TIMER_MS = 100

# File Size Constants (in bytes)
MIN_VALID_FILE_SIZE_BYTES = 1000

# Report Count Display Constants
MAX_DISPLAYED_REPORT_COUNT = 999