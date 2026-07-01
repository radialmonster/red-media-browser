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
DEFAULT_PREFETCH_MEDIA_LIMIT = 25
DEFAULT_MAX_CONCURRENT_PREFETCH_DOWNLOADS = 3
DEFAULT_POST_PREFETCH_BUFFER_SIZE = 30
MAX_AUTO_PREFETCHED_POSTS = 120
PREFETCH_RETRY_BASE_DELAY_MS = 30000
PREFETCH_RETRY_MAX_ATTEMPTS = 2

# UI Update Timing Constants (in milliseconds)
UI_UPDATE_DELAY_MS = 50
MOD_STATUS_DELAY_MS = 5000
THREAD_TERMINATION_TIMEOUT_MS = 2000
# Delay between a page render and the next-page media prefetch. Coalesced via a
# restartable single-shot timer so rapid paging re-arms it instead of stacking
# multiple prefetch triggers; short enough that the next page gets a head start.
MEDIA_PREFETCH_RENDER_DELAY_MS = 500

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
REPORT_CACHE_TTL_SECONDS = 300

# Snapshot Grid Layout Constants
SNAPSHOT_GRID_COLUMNS = 5
SNAPSHOT_GRID_CELL_MIN_WIDTH = 150
SNAPSHOT_GRID_CELL_MIN_HEIGHT = 250
