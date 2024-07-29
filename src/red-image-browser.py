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
logger = logging.getLogger()
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

# Configure PRAW logging
praw_logger = logging.getLogger('prawcore')
praw_logger.setLevel(logging.DEBUG)  # Ensure the logger level is set to DEBUG
praw_logger.propagate = True


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
        self.before = None
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

     
    def fetch_submissions(self, after=None, before=None, count=10):
        submissions = []
        try:
            logging.debug(f"Fetching: GET https://oauth.reddit.com/r/{self.subreddit.display_name}/new with after={after}, before={before}, count={count}")
            params = {'limit': count}
            if after:
                params['after'] = after
            if before:
                params['before'] = before
            
            submissions = list(self.subreddit.new(limit=count, params=params))
            
            if submissions:
                self.after = submissions[-1].name
            else:
                self.after = None
        except prawcore.exceptions.TooManyRequests as e:
            wait_time = int(e.response.headers.get('Retry-After', 60))
            logging.warning(f"Rate limit exceeded. Waiting for {wait_time} seconds.")
            time.sleep(wait_time)
        return submissions, self.after


class SubmissionFetcher(QThread):
    submissionsFetched = pyqtSignal(list, str)

    def __init__(self, model, after=None, before=None, count=10, parent=None):
        super().__init__(parent)
        self.model = model
        self.after = after
        self.before = before
        self.count = count

    def run(self):
        submissions, after = self.model.fetch_submissions(after=self.after, before=self.before, count=self.count)
        self.submissionsFetched.emit(submissions, after)


class MainWindow(QMainWindow):
    def __init__(self, subreddit='pics'):
        super().__init__()
        self.setWindowTitle("Reddit Image and Video Gallery")
        self.current_page = 0
        self.central_widget = QWidget()
        self.layout = QVBoxLayout(self.central_widget)
        self.subreddit_input = QLineEdit(subreddit)
        self.load_subreddit_button = QPushButton('Load Subreddit')
        self.load_previous_button = QPushButton('Previous')
        self.load_next_button = QPushButton('Next')
        self.download_100_button = QPushButton('Download Next 100')
        self.load_subreddit_button.clicked.connect(self.load_subreddit)
        self.load_previous_button.clicked.connect(self.load_previous)
        self.load_next_button.clicked.connect(lambda: self.load_next(10))
        self.download_100_button.clicked.connect(lambda: self.load_next(100, download_only=True))
        self.load_previous_button.setEnabled(False)
        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self.load_previous_button)
        buttons_layout.addWidget(self.load_next_button)
        buttons_layout.addWidget(self.download_100_button)
        self.table_widget = QTableWidget(2, 5, self)
        self.table_widget.setHorizontalHeaderLabels(['A', 'B', 'C', 'D', 'E'])
        self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_widget.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_widget.setEditTriggers(QTableWidget.NoEditTriggers)
        self.layout.addWidget(self.subreddit_input)
        self.layout.addWidget(self.load_subreddit_button)
        self.layout.addWidget(self.table_widget)
        self.layout.addLayout(buttons_layout)
        self.setCentralWidget(self.central_widget)
        self.update_previous_button_state()
        self.model = RedditGalleryModel(subreddit)
        self.load_subreddit()


    def load_subreddit(self):
        subreddit_name = self.subreddit_input.text()
        try:
            self.model.subreddit = reddit.subreddit(subreddit_name)
            logging.debug(f"Loading subreddit: {self.model.subreddit.url}")
            is_moderator = self.model.check_user_moderation_status()
            self.model.is_moderator = is_moderator
            logging.debug(f"User is moderator: {is_moderator}")
        except prawcore.exceptions.Redirect:
            error_msg = f"Subreddit '{subreddit_name}' does not exist."
            logging.error(error_msg)
            QMessageBox.critical(self, "Subreddit Error", error_msg)
            self.subreddit_input.clear()
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
        
        is_moderator = self.model.check_user_moderation_status()
        self.model.is_moderator = is_moderator

        self.current_page = 0
        self.model.current_items = []
        self.model.after = None
        self.update_previous_button_state()
        self.table_widget.clearContents()
        self.fetcher = SubmissionFetcher(self.model, after=None)
        self.fetcher.submissionsFetched.connect(self.on_submissions_fetched)
        self.fetcher.start()
        
    def load_next(self, count=10, download_only=False):
        if not download_only:
            self.current_page += 1
            self.update_previous_button_state()
            self.table_widget.clearContents()
        
        # Use the 'after' parameter for pagination
        after = self.model.after if hasattr(self.model, 'after') else None
        
        self.fetcher = SubmissionFetcher(self.model, after=after, count=count)
        self.fetcher.submissionsFetched.connect(lambda submissions, after: self.on_submissions_fetched(submissions, after, download_only))
        self.fetcher.start()

    def load_previous(self):
        logging.debug(f"Previous button clicked. Current page before decrement: {self.current_page}")
        if self.current_page > 0:
            self.current_page -= 1
            logging.debug(f"Current page after decrement: {self.current_page}")
            self.display_current_page_submissions()
        else:
            logging.debug("Already at the first page. No action taken.")
        self.update_previous_button_state()



    def update_previous_button_state(self):
        self.load_previous_button.setEnabled(self.current_page > 0)


    def on_submissions_fetched(self, submissions, after, download_only=False):
        logging.debug(f"Submissions fetched: {len(submissions)}")
        if submissions:
            self.model.current_items.extend(submissions)
            self.model.after = after  # Store the 'after' value for next pagination
            self.update_previous_button_state()
            if not download_only:
                self.display_current_page_submissions()
            else:
                self.download_submissions(submissions)
        else:
            logging.debug("No new submissions found.")
            if not download_only:
                # Only decrement current page if not in download only mode
                self.current_page -= 1 
                self.update_previous_button_state()
        
    def display_current_page_submissions(self):
        items_per_page = 10
        start_index = self.current_page * items_per_page
        end_index = start_index + items_per_page
        submissions_to_display = self.model.current_items[start_index:end_index]
        
        # Removed redundant fetching logic
        self.fill_table(submissions_to_display)
    
    def update_previous_button_state(self):
        self.load_previous_button.setEnabled(self.current_page > 0)

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

            post_id = submission.id
            title = submission.title
            url = submission.url

            logging.debug(f"Adding submission to table: Post ID - {post_id}, Row - {row}, Col - {column_labels[col]}")

            try:
                image_urls = []
                if hasattr(submission, 'is_gallery') and submission.is_gallery:
                    if hasattr(submission, 'media_metadata') and submission.media_metadata is not None:
                        image_urls = [html.unescape(media['s']['u'])
                                    for media in submission.media_metadata.values()
                                    if 's' in media and 'u' in media['s']]
                else:
                    image_urls = [url]

                local_image_paths = [self.download_file(image_url) for image_url in image_urls]
                local_image_paths = [path for path in local_image_paths if path]

                has_multiple_images = len(image_urls) > 1
                post_url = f"https://www.reddit.com{submission.permalink}"

                widget = ThumbnailWidget(local_image_paths, title, url, submission, self.model.subreddit.display_name, has_multiple_images, post_url, self.model.is_moderator)
                
                self.table_widget.setCellWidget(row, col, widget)

                if has_multiple_images:
                    widget.init_arrow_buttons()

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


    def download_submissions(self, submissions):
        for submission in submissions:
            image_urls = self.get_image_urls(submission)
            for url in image_urls:
                self.download_file(url, log_skip=True)
        # Update the 'after' attribute after downloading submissions
        if submissions:
            self.model.after = submissions[-1].name


    def download_file(self, url, log_skip=False):
        if url.endswith('.gifv'):
            url = url.replace('.gifv', '.mp4')

        cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
        os.makedirs(cache_dir, exist_ok=True)

        domain = urlparse(url).netloc
        domain_dir = os.path.join(cache_dir, domain)
        os.makedirs(domain_dir, exist_ok=True)

        parsed_url = urlparse(url)
        path = unquote(parsed_url.path)
        filename = os.path.basename(path)
        filename = filename.replace('?', '_').replace('&', '_').replace('=', '_')
        file_path = os.path.join(domain_dir, filename)

        if os.path.exists(file_path):
            if log_skip:
                logging.debug(f"File already exists, skipping: {file_path}")
            return file_path

        max_retries = 5
        retry_delay = 5

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }

        for attempt in range(max_retries):
            try:
                response = requests.get(url, stream=True, allow_redirects=True, headers=headers)
                if response.status_code == 200 and 'image' in response.headers.get('Content-Type', ''):
                    with open(file_path, 'wb') as local_file:
                        shutil.copyfileobj(response.raw, local_file)
                    logging.debug(f"File downloaded to: {file_path}")
                    return file_path
                elif response.status_code == 429:
                    logging.warning(f"Rate limit exceeded. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                elif response.history:
                    url = response.url
                    continue
                else:
                    logging.error('Invalid image URL or content type: %s', url)
                    return None
            except requests.RequestException as e:
                logging.exception("Request failed: %s", e)
                return None

        logging.error(f"Failed to download file after {max_retries} attempts: {url}")
        return None

    def get_image_urls(self, submission):
        if isinstance(submission, praw.models.Submission):
            if hasattr(submission, 'is_gallery') and submission.is_gallery:
                if hasattr(submission, 'media_metadata') and submission.media_metadata is not None:
                    return [html.unescape(media['s']['u'])
                            for media in submission.media_metadata.values()
                            if 's' in media and 'u' in media['s']]
            return [submission.url]
        else:
            return submission.get('image_urls', []) or submission.get('gallery_urls', [])
    
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
    def __init__(self, images, title, source_url, submission, subreddit_name, has_multiple_images, post_url, is_moderator):
        super().__init__(parent=None)
        self.praw_submission = submission
        self.submission_id = submission.id
        
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

        self.is_moderator = is_moderator
        
        # Check if the user is a moderator
        if self.is_moderator:
            logging.debug("User is a moderator. Creating moderation buttons.")
            self.create_moderation_buttons()
        else:
            logging.debug("User is not a moderator.")
    
    
    def set_model(self, model):
        self.model = model
    
    def open_post_url(self):
        webbrowser.open(self.post_url)
        
    def create_moderation_buttons(self):
        logging.debug("Creating moderation buttons.")
        self.approve_button = QPushButton("Approve", self)
        self.remove_button = QPushButton("Remove", self)
        
        self.approve_button.clicked.connect(self.approve_submission)
        self.remove_button.clicked.connect(self.remove_submission)
        
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

    def approve_submission(self):
        self.praw_submission.mod.approve()
        logging.debug(f"Approved: {self.submission_id}")
        
        # Update button appearance after approval
        self.approve_button.setStyleSheet("background-color: green;")
        self.approve_button.setText("Approved")

    def remove_submission(self):
        self.praw_submission.mod.remove()
        logging.debug(f"Removed: {self.submission_id}")

        # Update button appearance after removal
        self.remove_button.setStyleSheet("background-color: red;")
        self.remove_button.setText("Removed")
    


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

    # The initial fetch is now handled by load_subreddit()

    sys.exit(app.exec_())