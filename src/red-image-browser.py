import sys
import requests
from PyQt5.QtCore import QAbstractListModel, Qt, QModelIndex, QVariant, QSize, QThread, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QPushButton, QLineEdit, QWidget, QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QHBoxLayout, QMessageBox, QSizePolicy
)
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtCore import QUrl
import praw
import prawcore.exceptions
import tempfile
import shutil
import json
import os
from urllib.parse import urlparse, unquote
import logging
import html
import webbrowser
import time
import datetime

# Set up basic logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Configure PRAW logging
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger = logging.getLogger('prawcore')
logger.addHandler(handler)


# Load Reddit credentials from config.json
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path, 'r') as config_file:
    config = json.load(config_file)

# Initialize Reddit instance
reddit = praw.Reddit(
    client_id=config['client_id'],
    client_secret=config['client_secret'],
    refresh_token=config['refresh_token'],
    user_agent=config['user_agent'],
    log_request=2
)



class RedditGalleryModel(QAbstractListModel):
    def __init__(self, subreddit='pics', parent=None):
        super().__init__(parent)
        self.subreddit = reddit.subreddit(subreddit)
        self.current_items = []
        self.current_page = 0
        self.initial_fetch = True
        self.after = None
        self.moderators = None
        self.is_moderator = False
        

    def check_user_moderation_status(self):
        logging.debug("Checking user moderation status for redditgallerymodel for subreddit: %s", self.subreddit.display_name)
        try:
            self.moderators = list(self.subreddit.moderator())
            user = reddit.user.me()
            is_moderator = any(mod.name == user.name for mod in self.moderators)
            logging.debug("User %s is a moderator: %s", user.name, is_moderator)
            self.is_moderator = is_moderator  # Store the moderation status in the model
            return is_moderator
        except prawcore.exceptions.PrawcoreException as e:
            logging.error(f"PRAW error while checking moderation status: {e}")
            return False
        except Exception as e:
            logging.error(f"Unexpected error while checking moderation status: {e}")
            return False
    
    
    def cache_submissions(self, submissions, after):
        cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
        os.makedirs(cache_dir, exist_ok=True)

        subreddit_dir = os.path.join(cache_dir, self.subreddit.display_name_prefixed)
        os.makedirs(subreddit_dir, exist_ok=True)

        cache_file = os.path.join(subreddit_dir, "submissions.json")

        cached_data = {}
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached_data = json.load(f)
            except json.JSONDecodeError as e:
                logging.error(f"Error decoding JSON data from {cache_file}: {e}")
                cached_data = {}  # Initialize an empty dictionary if decoding fails

        cached_submissions = cached_data.get('submissions', [])
        existing_ids = {sub['id']: sub for sub in cached_submissions}

        for submission in submissions:
            submission_id = submission.id if isinstance(submission, praw.models.Submission) else submission['id']
            if submission_id not in existing_ids:
                cached_submission = {
                    'id': submission_id,
                    'title': submission.title if isinstance(submission, praw.models.Submission) else submission['title'],
                    'url': submission.url if isinstance(submission, praw.models.Submission) else submission['url'],
                    'image_urls': [],
                    'gallery_urls': [],
                    'video_urls': [],
                }

                retries = 0
                max_retries = 5
                wait_time = 60  # Initial wait time in seconds

                while retries < max_retries:
                    try:
                        if isinstance(submission, praw.models.Submission):
                            if hasattr(submission, 'is_gallery') and submission.is_gallery:
                                if hasattr(submission, 'media_metadata') and submission.media_metadata is not None:
                                    cached_submission['gallery_urls'] = [html.unescape(media['s']['u'])
                                                                        for media in submission.media_metadata.values()
                                                                        if 's' in media and 'u' in media['s']]
                            else:
                                cached_submission['image_urls'].append(submission.url)

                            if submission.url.endswith('.mp4'):
                                cached_submission['video_urls'].append(submission.url)
                        else:
                            cached_submission['image_urls'] = submission.get('image_urls', [])
                            cached_submission['gallery_urls'] = submission.get('gallery_urls', [])
                            cached_submission['video_urls'] = submission.get('video_urls', [])

                        existing_ids[submission_id] = cached_submission
                        break  # Exit the retry loop if successful

                    except prawcore.exceptions.TooManyRequests as e:
                        if retries < max_retries:
                            wait_time = int(e.response.headers.get('Retry-After', wait_time))  # Use the wait time from the response if available
                            logging.warning(f"Rate limit exceeded. Waiting for {wait_time} seconds before retrying...")
                            time.sleep(wait_time)
                            retries += 1
                            wait_time *= 2  # Exponential backoff
                        else:
                            logging.error("Max retries reached while caching submissions. Skipping this submission.")
                            break

        # Update cached_data with the new submissions
        cached_data['submissions'] = list(existing_ids.values())
        cached_data['after'] = after

        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cached_data, f, ensure_ascii=False, indent=4)
            logging.debug(f"Cached data written to {cache_file}")


    def fetch_submissions(self, initial_fetch=False):
        if initial_fetch:
            submissions = []  # Initialize submissions as an empty list for initial fetch
        else:
            self.load_cached_data()
            submissions = self.cached_submissions  # Load cached submissions for subsequent fetches

        after = self.after
        while True:
            try:
                logging.debug(f"Fetching: GET https://oauth.reddit.com/r/{self.subreddit.display_name}/new with after={after}")
                new_submissions = list(self.subreddit.new(limit=100, params={'after': after}))
                if not new_submissions:
                    break  # Exit the loop if there are no new submissions

                submissions.extend(new_submissions)
                after = new_submissions[-1].name
            except prawcore.exceptions.TooManyRequests as e:
                wait_time = int(e.response.headers.get('Retry-After', 60))  # Default to 60 seconds if not specified
                logging.warning(f"Rate limit exceeded. Waiting for {wait_time} seconds.")
                time.sleep(wait_time)
                continue

        if not submissions:
            logging.warning("No submissions available to display.")
            return [], None

        self.current_items = submissions
        self.after = after
        self.cache_submissions(self.current_items, self.after)
        return self.current_items, self.after



    def load_cached_data(self):
        cache_dir = os.path.join(os.path.dirname(__file__), 'cache', self.subreddit.display_name_prefixed)
        cache_file = os.path.join(cache_dir, "submissions.json")

        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached_data = json.load(f)
                    self.cached_submissions = [reddit.submission(id=sub['id']) for sub in cached_data.get('submissions', [])]
                    self.after = cached_data.get('after', None)
            except json.JSONDecodeError as e:
                logging.error(f"Error decoding JSON data from {cache_file}: {e}")
                self.cached_submissions = []
                self.after = None

                # Print out the exact error and the problematic part of the JSON
                with open(cache_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    error_line = lines[e.lineno - 1] if e.lineno <= len(lines) else "Line not found"
                    logging.error(f"Error at line {e.lineno}, column {e.colno}: {error_line.strip()}")
                    logging.error(f"Context: {e.msg}")

                # Handle the case where the corrupted file already exists
                corrupted_file = cache_file + ".corrupted"
                if os.path.exists(corrupted_file):
                    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                    corrupted_file = f"{corrupted_file}_{timestamp}"

                try:
                    os.rename(cache_file, corrupted_file)
                    logging.error(f"Renamed corrupted cache file to {corrupted_file}")
                except Exception as rename_error:
                    logging.error(f"Failed to rename corrupted file: {rename_error}")
        else:
            self.cached_submissions = []
            self.after = None


    def submission_to_dict(self, submission):
        submission_dict = {
            'id': submission.id,
            'title': submission.title,
            'url': submission.url,
            'image_urls': [],
            'gallery_urls': [],
            'video_urls': [],
        }

        if hasattr(submission, 'is_gallery') and submission.is_gallery:
            if hasattr(submission, 'media_metadata'):
                submission_dict['gallery_urls'] = [html.unescape(media['s']['u'])
                                                for media in submission.media_metadata.values()
                                                if 's' in media and 'u' in media['s']]
        else:
            submission_dict['image_urls'].append(submission.url)

        if submission.url.endswith('.mp4'):
            submission_dict['video_urls'].append(submission.url)

        return submission_dict


class SubmissionFetcher(QThread):
    submissionsFetched = pyqtSignal(list, str)

    def __init__(self, model, after=None, initial_fetch=False, parent=None):
        super().__init__(parent)
        self.model = model
        self.after = after
        self.initial_fetch = initial_fetch

    def run(self):
        # Adjusted call to match the method definition
        submissions, after = self.model.fetch_submissions(initial_fetch=self.model.initial_fetch)
        self.submissionsFetched.emit(submissions, after)


class MainWindow(QMainWindow):
    updateTableSignal = pyqtSignal(list)

    def __init__(self, subreddit='pics'):
        super().__init__()
        self.setWindowTitle("Reddit Image and Video Gallery")
        
        self.current_page = 0
        self.initial_fetch = True

        self.central_widget = QWidget()
        self.layout = QVBoxLayout(self.central_widget)

        self.subreddit_input = QLineEdit(subreddit)
        
        self.load_subreddit_button = QPushButton('Load Subreddit')

        # Create the buttons for Previous and Next
        self.load_previous_button = QPushButton('Previous')
        self.load_next_button = QPushButton('Next')

        # Connect the buttons to their respective methods
        self.load_subreddit_button.clicked.connect(self.load_subreddit)
        self.load_previous_button.clicked.connect(self.load_previous)
        self.load_next_button.clicked.connect(self.load_next)

        # Initially disable the Previous button
        self.load_previous_button.setEnabled(False)

        # Create a horizontal layout for Previous and Next buttons
        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self.load_previous_button)
        buttons_layout.addWidget(self.load_next_button)

        self.table_widget = QTableWidget(2, 5, self)  # 2 rows and 5 columns
        self.table_widget.setHorizontalHeaderLabels(['A', 'B', 'C', 'D', 'E'])
        self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_widget.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_widget.setEditTriggers(QTableWidget.NoEditTriggers)

        # Add widgets to the main layout
        self.layout.addWidget(self.subreddit_input)
        self.layout.addWidget(self.load_subreddit_button)
        self.layout.addWidget(self.table_widget)
        self.layout.addLayout(buttons_layout)  # Add the horizontal layout for buttons

        self.setCentralWidget(self.central_widget)
        
        self.update_previous_button_state()
        
        self.page_history = []  # This will store the 'after' parameter for each page
        self.submissions_history = []

        self.updateTableSignal.connect(self.fill_table)

        # Initial fetch and display of data
        self.model = RedditGalleryModel(subreddit)
        self.initial_load()


    def initial_load(self):
        # Start the fetcher to load initial data
        self.fetcher = SubmissionFetcher(self.model, self.model.after, initial_fetch=True)
        self.fetcher.submissionsFetched.connect(self.on_submissions_fetched)
        self.fetcher.start()
    
    
    
    def load_subreddit(self):
        subreddit_name = self.subreddit_input.text()
        try:
            self.model.subreddit = reddit.subreddit(subreddit_name)
            logging.debug(f"Loading subreddit: {self.model.subreddit.url}")
            is_moderator = self.model.check_user_moderation_status()
            self.model.is_moderator = is_moderator  # Store the moderation status in the model
            logging.debug(f"User is moderator: {is_moderator}")
        except prawcore.exceptions.Redirect:
            error_msg = f"Subreddit '{subreddit_name}' does not exist."
            logging.error(error_msg)
            QMessageBox.critical(self, "Subreddit Error", error_msg)
            self.subreddit_input.clear()  # Optionally clear the input
            return
        except prawcore.exceptions.ResponseException as e:
            error_msg = f"Error loading subreddit: {str(e)}"
            logging.error(error_msg)
            QMessageBox.critical(self, "Subreddit Error", error_msg)
            return
        except Exception as e:
            error_msg = f"An unexpected error occurred: {str(e)}"
            logging.error(error_msg)
            QMessageBox.critical(self, "Error", error_msg)
            return

        # Reset the current page and the 'after' parameter when a new subreddit is loaded.
        self.current_page = 0
        self.model.after = None
        self.model.initial_fetch = True  # Set initial_fetch to True

        # Clear current_items in the model, as we're switching to a new subreddit and need to start fresh.
        self.model.current_items = []

        # Reset the 'after' parameter to None
        self.model.after = None

        # Update the Previous button state; it should be disabled when a new subreddit is loaded as we start from the first page.
        self.update_previous_button_state()

        # Make sure the table is cleared as we're switching to a new subreddit.
        self.table_widget.clearContents()

        # Begin loading submissions from the new subreddit.
        self.fetcher = SubmissionFetcher(self.model, self.model.after, initial_fetch=self.model.initial_fetch)
        self.fetcher.submissionsFetched.connect(self.on_submissions_fetched)
        self.fetcher.start()  # Start the fetcher thread to asynchronously load the data.
        

    def load_next(self):
        logging.debug(f"Next button clicked. Current page before increment: {self.current_page}")
        self.current_page += 1
        logging.debug(f"Current page after increment: {self.current_page}")
        next_page_start_index = self.current_page * 10
        logging.debug(f"Next page start index: {next_page_start_index}")

        if next_page_start_index >= len(self.model.current_items):
            logging.debug("Need to fetch new items from Reddit.")
            self.fetcher = SubmissionFetcher(self.model, self.model.after, initial_fetch=False)
            self.fetcher.submissionsFetched.connect(self.on_submissions_fetched)
            self.fetcher.start()
        else:
            logging.debug("Displaying already fetched submissions.")
            self.display_current_page_submissions()

        self.update_previous_button_state()



    def load_previous(self):
        logging.debug(f"Previous button clicked. Current page before decrement: {self.current_page}")

        if self.current_page > 0:
            self.current_page -= 1
            logging.debug(f"Current page after decrement: {self.current_page}")

            logging.debug("Displaying previous set of submissions.")
            self.display_previous_set_of_submissions()
        else:
            logging.debug("Already at the first page. No action taken.")

        self.update_previous_button_state()


    def display_previous_set_of_submissions(self):
        logging.debug(f"Displaying previous set of submissions for page {self.current_page}")
        items_per_page = 10
        start_index = self.current_page * items_per_page
        end_index = min(start_index + items_per_page, len(self.model.current_items))

        if start_index >= len(self.model.current_items):
            logging.debug(f"No submissions to display for index range {start_index}-{end_index}")
            return  # No submissions to display

        logging.debug(f"Displaying submissions from index {start_index} to {end_index}")

        self.table_widget.clearContents()
        submissions_to_display = self.model.current_items[start_index:end_index]
        self.fill_table(submissions_to_display)

        self.table_widget.viewport().update()
        self.table_widget.update()
        QApplication.processEvents()

    def update_previous_button_state(self):
        # Enable the Previous button if the current page is greater than 0
        self.load_previous_button.setEnabled(self.current_page > 0)


    def on_submissions_fetched(self, submissions, after):
        logging.debug(f"Submissions fetched: {len(submissions)}")

        if submissions:
            # New submissions fetched
            if self.model.initial_fetch or not self.model.current_items:
                logging.debug("Initial fetch or current_items is empty. Resetting current_items.")
                self.model.current_items = submissions
                self.current_page = 0
                logging.debug(f"Reset current page to 0. Current items count: {len(self.model.current_items)}")
            else:
                logging.debug("Processing fetched submissions for existing items.")
                existing_ids = set(sub.id for sub in self.model.current_items)
                logging.debug(f"Existing IDs: {existing_ids}")

                fetched_ids = set(sub.id for sub in submissions)
                logging.debug(f"Fetched IDs: {fetched_ids}")

                new_submissions = [sub for sub in submissions if sub.id not in existing_ids]
                logging.debug(f"New submissions: {len(new_submissions)} (out of {len(submissions)} fetched)")

                if new_submissions:
                    logging.debug("Adding new submissions to current_items.")
                    self.model.current_items.extend(new_submissions)
                    logging.debug(f"Updated current items count: {len(self.model.current_items)}")
                else:
                    logging.debug("No new submissions to add.")
        else:
            # No new submissions fetched, load cached submissions
            logging.debug("No new submissions fetched. Loading cached submissions.")
            self.model.current_items = self.model.cached_submissions
            self.current_page = 0
            logging.debug(f"Loaded cached submissions. Current items count: {len(self.model.current_items)}")

        self.model.after = after
        logging.debug(f"Model's 'after' parameter updated to: {self.model.after}")

        self.model.initial_fetch = False
        logging.debug("Set initial_fetch to False.")

        self.update_previous_button_state()
        self.display_current_page_submissions()
        logging.debug(f"Displayed submissions for the current page: {self.current_page}")
        
    def display_current_page_submissions(self):
        items_per_page = 10
        start_index = self.current_page * items_per_page
        end_index = start_index + items_per_page

        submissions_to_display = self.model.current_items[start_index:end_index]
        self.fill_table(submissions_to_display)


    def fill_table(self, submissions):
        logging.debug(f"---------------------------Processing a new batch of submissions...")
        logging.debug(f"Starting to fill table with new submissions. Total submissions: {len(submissions)}")

        if not submissions:
            logging.debug("No new submissions to display.")
            return
        
        self.table_widget.clearContents()

        column_labels = ['A', 'B', 'C', 'D', 'E']
        row, col = 0, 0

        for submission in submissions:
            if row >= 2:  # Only display up to 2 rows
                break

            title = submission.title if isinstance(submission, praw.models.Submission) else submission['title']
            post_id = submission.id if isinstance(submission, praw.models.Submission) else submission['id']

            image_urls = []
            if isinstance(submission, praw.models.Submission):
                if hasattr(submission, 'is_gallery') and submission.is_gallery:
                    if hasattr(submission, 'media_metadata'):
                        image_urls = [html.unescape(media['s']['u'])
                                      for media in submission.media_metadata.values()
                                      if 's' in media and 'u' in media['s']]
                else:
                    image_urls = [submission.url]
            else:
                image_urls = submission.get('image_urls', [])
                if not image_urls:
                    image_urls = submission.get('gallery_urls', [])

            if not image_urls:
                logging.error(f"No image URLs found for submission: {post_id}")
                continue

            logging.debug(f"Adding submission to table: Post ID - {post_id}, Row - {row}, Col - {column_labels[col]}")

            try:
                local_image_paths = [self.download_file(image_url) for image_url in image_urls]
                local_image_paths = [path for path in local_image_paths if path]

                has_multiple_images = len(image_urls) > 1
                post_url = f"https://www.reddit.com{submission.permalink}" if isinstance(submission, praw.models.Submission) else submission.get('url', '')

                praw_submission = submission if isinstance(submission, praw.models.Submission) else None

                widget = ThumbnailWidget(local_image_paths, title, image_urls[0], post_id, self.model.subreddit.display_name, has_multiple_images, post_url, praw_submission, self.model)
                

                self.table_widget.setCellWidget(row, col, widget)

                if has_multiple_images:
                    widget.init_arrow_buttons()

                if praw_submission and widget.check_user_moderation_status():
                    widget.create_moderation_buttons()

                col += 1
                if col >= 5:
                    col = 0
                    row += 1

            except Exception as e:
                logging.exception("Error processing submission: %s", e)

        logging.debug("Table updated with new submissions.")
        self.table_widget.viewport().update()
        self.table_widget.update()
        self.table_widget.repaint()
        QApplication.processEvents()





    def download_file(self, url):
        if url.endswith('.gifv'):
            # Convert .gifv to .mp4
            url = url.replace('.gifv', '.mp4')

        # Check if the URL is a Reddit gallery URL
        if 'reddit.com/gallery/' in url:
            return self.download_gallery_images(url)

        # Create 'cache' directory inside the 'src' directory
        cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
        os.makedirs(cache_dir, exist_ok=True)

        # Extract domain name from URL
        domain = urlparse(url).netloc

        # Create a subdirectory for the domain if it doesn't exist
        domain_dir = os.path.join(cache_dir, domain)
        os.makedirs(domain_dir, exist_ok=True)

        # Extract filename and create a path in the domain directory
        parsed_url = urlparse(url)
        path = unquote(parsed_url.path)  # Decode URL encoding if present
        filename = os.path.basename(path)

        # Remove or replace characters that are not allowed in Windows file names
        filename = filename.replace('?', '_').replace('&', '_').replace('=', '_')

        # Construct the file path using the sanitized file name
        file_path = os.path.join(domain_dir, filename)

        # Check if the file already exists to avoid re-downloading
        if os.path.exists(file_path):
            logging.debug(f"File already exists: {file_path}")
            return file_path

        # Retry mechanism for handling rate limiting and redirects
        max_retries = 5
        retry_delay = 5  # initial delay in seconds

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }

        for attempt in range(max_retries):
            try:
                logging.debug(f"Attempting to download file from URL: {url}")
                response = requests.get(url, stream=True, allow_redirects=True, headers=headers)
                if response.status_code == 200 and 'image' in response.headers.get('Content-Type', ''):
                    with open(file_path, 'wb') as local_file:
                        shutil.copyfileobj(response.raw, local_file)
                        logging.debug(f"File downloaded to: {file_path}")
                        return file_path
                elif response.status_code == 429:
                    logging.warning(f"Rate limit exceeded. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                    continue
                elif response.history:
                    # Handle redirects
                    final_url = response.url
                    logging.debug(f"Redirected to {final_url}")
                    url = final_url
                    continue
                else:
                    logging.error('Invalid image URL or content type: %s', url)
                    return None
            except requests.RequestException as e:
                logging.exception("Request failed: %s", e)
                return None

        logging.error(f"Failed to download file after {max_retries} attempts: {url}")
        return None

    def download_gallery_images(self, gallery_url):
        try:
            # Extract the submission ID from the gallery URL
            submission_id = gallery_url.split('/')[-1]
            
            # Fetch the submission using PRAW
            submission = reddit.submission(id=submission_id)
            
            if submission.is_gallery and hasattr(submission, 'media_metadata'):
                image_urls = [html.unescape(media['s']['u'])
                            for media in submission.media_metadata.values()
                            if 's' in media and 'u' in media['s']]
                local_image_paths = [self.download_file(image_url) for image_url in image_urls]
                return [path for path in local_image_paths if path]
            else:
                logging.error(f"No gallery metadata found for submission: {gallery_url}")
                return []
        except Exception as e:
            logging.exception("Failed to fetch gallery metadata: %s", e)
            return []

class ThumbnailWidget(QWidget):
    def __init__(self, images, title, source_url, submission_id, subreddit_name, has_multiple_images, post_url, praw_submission, model):
        super().__init__(parent=None)
        self.praw_submission = praw_submission
        self.model = model
        
        self.images = images
        self.current_index = 0
        self.post_url = post_url


        self.layout = QVBoxLayout(self)

        self.titleLabel = QLabel(title)
        self.titleLabel.setAlignment(Qt.AlignCenter)
        self.titleLabel.setMaximumHeight(30)
        self.layout.addWidget(self.titleLabel)

        self.urlLabel = QLabel(source_url)
        self.urlLabel.setAlignment(Qt.AlignCenter)
        self.urlLabel.setMaximumHeight(30)
        self.layout.addWidget(self.urlLabel)

        self.imageLabel = QLabel()  # Define the imageLabel attribute here
        self.imageLabel.setAlignment(Qt.AlignCenter)
        self.imageLabel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.imageLabel.mouseReleaseEvent = lambda event: self.open_post_url()
        self.layout.addWidget(self.imageLabel)

        self.has_multiple_images = has_multiple_images

        # Initialize arrow buttons if there are multiple images
        if self.has_multiple_images:
            self.init_arrow_buttons()

        self.pixmap = None

        # Set the first image if available
        if images:
            self.set_pixmap(images[0])

        self.subreddit_name = subreddit_name

        # Check if the user is a moderator and create moderation buttons if true
        # if self.praw_submission and self.model.is_moderator:  # Use the stored moderation status from the model
        #    logging.debug("User is a moderator. Creating moderation buttons.")
        #    self.create_moderation_buttons()
        # else:
        #    logging.debug("User is not a moderator.")
    
    
    def set_model(self, model):
        self.model = model
    
    def open_post_url(self):
        webbrowser.open(self.post_url)
        
    def create_moderation_buttons(self):
        logging.debug("Creating moderation buttons.")
        self.approve_button = QPushButton("Approve", self)
        self.remove_button = QPushButton("Remove", self)
        
        self.approve_button.clicked.connect(lambda: self.approve_submission(self.praw_submission))
        self.remove_button.clicked.connect(lambda: self.remove_submission(self.praw_submission))
        
        moderation_layout = QHBoxLayout()
        moderation_layout.addWidget(self.approve_button)
        moderation_layout.addWidget(self.remove_button)
        self.layout.addLayout(moderation_layout)

    def init_arrow_buttons(self):
        if hasattr(self, 'leftArrowButton') and hasattr(self, 'rightArrowButton'):
            return 
        self.arrowLayout = QHBoxLayout()
        self.arrowLayout.setSpacing(5)
        self.arrowLayout.setContentsMargins(0, 0, 0, 0)

        self.leftArrowButton = QPushButton("<")
        self.leftArrowButton.clicked.connect(self.show_previous_image)
        self.arrowLayout.addWidget(self.leftArrowButton)

        self.rightArrowButton = QPushButton(">")
        self.rightArrowButton.clicked.connect(self.show_next_image)
        self.arrowLayout.addWidget(self.rightArrowButton)

        # Enable or disable arrow buttons based on the number of images
        self.leftArrowButton.setEnabled(len(self.images) > 1)
        self.rightArrowButton.setEnabled(len(self.images) > 1)

        # Add the arrow layout to the main layout
        self.layout.addLayout(self.arrowLayout)

    def set_pixmap(self, pixmap_path):
        if isinstance(pixmap_path, list):
            pixmap_path = pixmap_path[0]  # Ensure pixmap_path is a string

        # Extract domain name and relative URL from the file path for display
        domain = os.path.basename(os.path.dirname(pixmap_path))
        filename = os.path.basename(pixmap_path)
        display_path = f"{domain}/{filename}"
        self.urlLabel.setText(display_path)

        # Handling for displaying the image or video
        if pixmap_path.endswith('.mp4'):
            self.play_video(pixmap_path)
        else:
            self.pixmap = QPixmap(pixmap_path)
            self.update_pixmap()

    def update_pixmap(self):
        if self.pixmap and not self.pixmap.isNull():
            scaled_pixmap = self.pixmap.scaled(self.imageLabel.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.imageLabel.setPixmap(scaled_pixmap)
        else:
            self.imageLabel.clear()
            self.imageLabel.setText("Image not available")
            self.imageLabel.setAlignment(Qt.AlignCenter)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_pixmap() 

    def show_next_image(self):
        if self.images:
            self.current_index = (self.current_index + 1) % len(self.images)
            self.set_pixmap(self.images[self.current_index])

    def show_previous_image(self):
        if self.images:
            self.current_index = (self.current_index - 1) % len(self.images)
            self.set_pixmap(self.images[self.current_index])

    def play_video(self, video_url):
        # Initialize the media player and video widget if not already done
        if not hasattr(self, 'mediaPlayer'):
            self.mediaPlayer = QMediaPlayer(None, QMediaPlayer.VideoSurface)
            self.videoWidget = QVideoWidget()
            self.layout.addWidget(self.videoWidget)
            self.mediaPlayer.setVideoOutput(self.videoWidget)

        # Play the video
        self.mediaPlayer.setMedia(QMediaContent(QUrl.fromLocalFile(video_url)))
        self.mediaPlayer.play()

    def approve_submission(self, submission):
        submission.mod.approve()
        logging.debug(f"Approved: {submission.id}")  # Log to console when approved
        
        # Update button appearance after approval
        self.approve_button.setStyleSheet("background-color: green;")
        self.approve_button.setText("Approved")

    def remove_submission(self, submission):
        submission.mod.remove()
        logging.debug(f"Removed: {submission.id}")  # Log to console when removed

        # Update button appearance after removal
        self.remove_button.setStyleSheet("background-color: red;")
        self.remove_button.setText("Removed")
 
    def check_user_moderation_status(self):
        if self.model:
            return self.model.is_moderator
        return False
    


if __name__ == '__main__':
    app = QApplication(sys.argv)
    # Set the application-wide stylesheet
    app.setStyleSheet("""
        QMainWindow {
            background-color: #121212;  /* Dark background for the main window */
        }
        QWidget {
            background-color: #121212;  /* Dark background for widgets */
        }
        QPushButton { 
            color: white; 
            background-color: gray; 
            border: 1px solid white; 
        }
        QLineEdit {
            color: white;
            background-color: #1e1e1e;  /* Slightly lighter dark background for line edit */
            border: 1px solid #333333;  /* Subtle border for line edit */
        }
        QLabel {
            color: white;  /* White text for labels */
        }
        QMessageBox {
            color: white; 
            background-color: black;
        }
    """)

    # Load the default subreddit from the config.json file
    default_subreddit = config.get('default_subreddit', 'pics')

    # Initialize the main window with the default subreddit
    main_win = MainWindow(subreddit=default_subreddit)
    main_win.show()

    # Load cached data and fetch new submissions
    main_win.model.fetch_submissions(initial_fetch=True)

    sys.exit(app.exec_())