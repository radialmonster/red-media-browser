# Changelog

## [Unreleased] - 2025-11-22

### Fixed
- **Freezing/Not Responding Issues:**
    - Offloaded VLC video player initialization and playback start to a background thread (`VlcWorker`).
    - Offloaded VLC resource cleanup (stopping/releasing player) to a background thread (`VlcCleanupWorker`).
    - Moved blocking network calls for URL resolution (e.g., RedGifs API) from the main thread to the background `MediaDownloadWorker`.
- **Crashes:**
    - Fixed `TypeError` in `MediaDownloadWorker` and `MediaPrefetchWorker` by correcting the arguments emitted by the `finished` signal.
    - Fixed `RuntimeError` ("dictionary changed size during iteration") in `save_submission_index` by creating a thread-safe copy of the index before saving.
    - Fixed `ValueError` in `extract_reports_from_submission` by adding robust handling for varying Reddit API report data structures.
    - Fixed `ImportError` in `red-media-browser.py` regarding `get_cache_path`.
    - Fixed `NameError` in `ui_components.py` regarding `QObject`.
- **Resource Management:**
    - Implemented `cancel_active_workers` in `ThumbnailWidget` to properly stop background tasks (like moderation actions) when a widget is closed or reused.
    - Updated `clear_content` to explicitly close widgets before deletion, ensuring graceful shutdown of associated resources.

### Changed
- Refactored `ThumbnailWidget.play_video` to use the asynchronous `VlcWorker`.
- Updated `ThumbnailWidget.load_image_async` to use non-blocking cache checks (`get_cached_processed_url`).
