import sys
from PyQt5.QtCore import QAbstractListModel, Qt, QModelIndex, QVariant, QSize, QThread, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QPushButton, QLineEdit, QWidget, QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QHBoxLayout, QMessageBox, QSizePolicy, QDesktopWidget
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtCore import QUrl
import praw
import prawcore.exceptions
import tempfile
import shutil
import json
import os
import logging
from urllib.parse import urlparse, unquote
import requests
import time
import html
import webbrowser


# Set up basic logging
logger = logging.getLogger()
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

config_path = os.path.join(os.path.dirname(__file__), 'config.json')
try:
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
    logger.info("Successfully initialized Reddit API client.")

    # Load the default subreddit from the config.json file
    default_subreddit = config.get('default_subreddit', 'pics')
    logger.info(f"Default subreddit set to: {default_subreddit}")

except FileNotFoundError:
    logger.error(f"config.json not found at {config_path}. Please create a config file with your Reddit API credentials.")
    logger.debug(f"Script directory: {os.path.dirname(__file__)}")
    logger.debug(f"Contents of script directory: {os.listdir(os.path.dirname(__file__))}")
    sys.exit(1)
except KeyError as e:
    logger.error(f"Missing key in config.json: {e}")
    sys.exit(1)
except Exception as e:
    logger.error(f"Error initializing Reddit API client: {e}")
    sys.exit(1)

class RedditGalleryModel(QAbstractListModel):
    def __init__(self, subreddit='pics', parent=None):
        super().__init__(parent)
        self.subreddit = reddit.subreddit(subreddit)
        self.current_items = []
        self.after = None
        self.moderators = None
        self.is_moderator = False
        #self.check_user_moderation_status()
        self.fetch_initial_submissions()

    def check_user_moderation_status(self):
        try:
            logger.debug("Starting moderation status check")
            self.moderators = list(self.subreddit.moderator())
            user = reddit.user.me()
            logger.debug(f"Current user: {user.name}")
            logger.debug(f"Moderators: {[mod.name for mod in self.moderators]}")
            is_moderator = any(mod.name == user.name for mod in self.moderators)
            self.is_moderator = is_moderator
            logger.debug(f"Is moderator: {is_moderator}")
            return is_moderator
        except prawcore.exceptions.PrawcoreException as e:
            logger.error(f"PRAW error while checking moderation status: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error while checking moderation status: {e}")
            return False

    def fetch_initial_submissions(self):
        try:
            logger.debug("Fetching initial 100 submissions")
            self.current_items, self.after = self.fetch_submissions(count=100)
            logger.debug(f"Fetched {len(self.current_items)} initial submissions")
        except Exception as e:
            logger.error(f"Error fetching initial submissions: {e}")
    
    def fetch_submissions(self, after=None, before=None, count=10):
        submissions = []
        try:
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
            logger.warning(f"Rate limit exceeded. Waiting for {wait_time} seconds.")
            time.sleep(wait_time)
        return submissions, self.after

class SubmissionFetcher(QThread):
    submissionsFetched = pyqtSignal(list, str)

    def __init__(self, model, after=None, before=None, count=10):
        super().__init__()
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
        self.current_page = 1
        self.central_widget = QWidget()
        self.layout = QVBoxLayout(self.central_widget)
        
        # Add a status label
        self.status_label = QLabel("Loading...")
        self.layout.addWidget(self.status_label)
        
        # Subreddit input and load button
        subreddit_layout = QHBoxLayout()
        self.subreddit_input = QLineEdit(subreddit)
        self.load_subreddit_button = QPushButton('Load Subreddit')
        subreddit_layout.addWidget(self.subreddit_input)
        subreddit_layout.addWidget(self.load_subreddit_button)
        self.layout.addLayout(subreddit_layout)
        
        # Table widget
        self.table_widget = QTableWidget(2, 5, self)
        self.table_widget.setHorizontalHeaderLabels(['A', 'B', 'C', 'D', 'E'])
        self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_widget.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_widget.setEditTriggers(QTableWidget.NoEditTriggers)
        self.layout.addWidget(self.table_widget)
        
        # Navigation buttons at the bottom
        nav_layout = QHBoxLayout()
        self.prev_page_button = QPushButton('Previous Page')
        self.next_page_button = QPushButton('Next Page')
        self.download_100_button = QPushButton('Download Next 100')
        nav_layout.addWidget(self.prev_page_button)
        nav_layout.addWidget(self.next_page_button)
        nav_layout.addWidget(self.download_100_button)
        self.layout.addLayout(nav_layout)
        
        # Set up the central widget
        self.setCentralWidget(self.central_widget)
        
        # Connect button signals
        self.load_subreddit_button.clicked.connect(self.load_subreddit)
        self.subreddit_input.returnPressed.connect(self.load_subreddit)
        self.prev_page_button.clicked.connect(self.load_previous_page)
        self.next_page_button.clicked.connect(lambda: self.load_next_page(10))
        self.download_100_button.clicked.connect(lambda: self.load_next_page(100, download_only=True))
        
        # Set up the model and other initializations
        self.model = RedditGalleryModel(subreddit)
        logger.debug(f"MainWindow initialized with is_moderator: {self.model.is_moderator}")
        self.update_previous_button_state()
        
        # Set a minimum size for the window
        self.setMinimumSize(800, 600)
        
        # Center the window on the screen
        self.center()
        # Start fetching initial submissions
        QApplication.processEvents()
        self.on_initial_submissions_fetched(self.model.current_items, self.model.after)

    def load_subreddit(self):
        subreddit_name = self.subreddit_input.text()
        try:
            self.model.subreddit = reddit.subreddit(subreddit_name)
            logger.debug(f"Loading subreddit: {self.model.subreddit.url}")
            
            # Explicitly call and log the moderation status check
            is_moderator = self.model.check_user_moderation_status()
            logger.debug(f"Moderation status check result: {is_moderator}")
            
            self.model.is_moderator = is_moderator
            logger.debug(f"Is moderator after check: {self.model.is_moderator}")

            # Clear current items and reset pagination
            self.model.current_items = []
            self.current_page = 1
            self.update_previous_button_state()
            self.table_widget.clearContents()

            # Fetch new submissions after loading the subreddit
            self.fetcher = SubmissionFetcher(self.model)
            self.fetcher.submissionsFetched.connect(self.on_initial_submissions_fetched)
            self.fetcher.start()

        except prawcore.exceptions.Redirect as e:
            error_msg = f"Subreddit '{subreddit_name}' does not exist."
            logger.error(error_msg)
            QMessageBox.critical(self, "Subreddit Error", error_msg)
            return
        except Exception as e:
            logger.error(f"Error loading subreddit: {str(e)}")
            return

    def center(self):
        qr = self.frameGeometry()
        cp = QDesktopWidget().availableGeometry().center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())
    
    def on_initial_submissions_fetched(self, submissions, after):
        self.status_label.setText("Submissions fetched, updating UI...")
        self.on_submissions_fetched(submissions, after)
        self.status_label.setText("UI updated.")

    def update_navigation_buttons(self):
        self.prev_page_button.setEnabled(self.current_page > 1)

    def load_next_page(self, count=10, download_only=False):
        logger.debug(f"Next Page button clicked")
        if not download_only:
            self.current_page += 1
            self.update_navigation_buttons()
            self.table_widget.clearContents()

        # Calculate 'after' based on current_page and download_only
        if download_only:
            # Get the index of the last displayed submission on the current page
            last_displayed_index = (self.current_page * 10) - 1 
            after = self.model.current_items[last_displayed_index].name if len(self.model.current_items) > last_displayed_index else None
        else:
            after = self.model.after if hasattr(self.model, 'after') else None

        self.fetcher = SubmissionFetcher(self.model, after=after, count=count)
        self.fetcher.submissionsFetched.connect(lambda submissions, after: self.on_next_page_fetched(submissions, after, download_only))
        self.fetcher.start()


    
    def load_previous_page(self):
        logger.debug(f"Previous Page button clicked")
        if self.current_page > 0:
            self.current_page -= 1
            self.update_navigation_buttons()
            self.table_widget.clearContents()
            self.display_current_page_submissions()
    

    def on_submissions_fetched(self, submissions, after):
        if submissions:
            self.model.current_items.extend(submissions)
            self.model.after = after
            self.update_previous_button_state()
            logger.debug(f"Fetched {len(submissions)} new submissions. Total: {len(self.model.current_items)}")
        else:
            logger.debug("No new submissions fetched.")

        # Only download submissions for the current page
        self.display_current_page_submissions()
        
        self.update_navigation_buttons()

    
    
    def on_next_page_fetched(self, submissions, after, download_only=False):
        if download_only:
            self.download_submissions(submissions)
            # Filter submissions based on successful downloads
            self.model.current_items.extend([
                s for s in submissions 
                if (
                    hasattr(s, 'is_gallery') and s.is_gallery and hasattr(s, 'media_metadata') and s.media_metadata is not None and
                    any(os.path.exists(self.download_file(html.unescape(media['s']['u']), log_skip=True)) for media in s.media_metadata.values() if 's' in media and 'u' in media['s'])
                ) or (
                    not (hasattr(s, 'is_gallery') and s.is_gallery) and
                    self.download_file(s.url, log_skip=True) is not None and
                    os.path.exists(self.download_file(s.url, log_skip=True))
                )
            ])
            self.model.after = after
        else:
            self.model.current_items.extend(submissions)
            self.model.after = after
            self.display_current_page_submissions()

    def display_current_page_submissions(self):
        items_per_page = 10
        start_index = (self.current_page - 1) * items_per_page
        end_index = start_index + items_per_page
        submissions_to_display = self.model.current_items[start_index:end_index]

        logger.debug(f"Displaying page {self.current_page}, items {start_index + 1}-{end_index}")
        logger.debug(f"Total items: {len(self.model.current_items)}")
        logger.debug(f"Submissions to display: {len(submissions_to_display)}")

        if not submissions_to_display:
            logger.debug("No submissions to display on this page.")
            return

        self.update_navigation_buttons()
        self.fill_table(submissions_to_display)
    
    def update_previous_button_state(self):
        self.prev_page_button.setEnabled(self.current_page > 0)

    def fill_table(self, submissions):
        if not submissions:
            logger.debug("No submissions to display on this page.")
            return

        self.table_widget.clearContents()

        column_labels = ['A', 'B', 'C', 'D', 'E']
        row, col = 0, 0

        for submission in submissions:
            if row >= 2:  
                break

            post_id = submission.id
            title = submission.title
            url = submission.url

            logger.debug(f"Adding submission to table: Post ID - {post_id}, Row - {row}, Col - {column_labels[col]}")

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
                logger.exception("Error processing submission: %s", e)


    def download_submissions(self, submissions):
        for submission in submissions:
            image_urls = []
            try:
                if hasattr(submission, 'is_gallery') and submission.is_gallery:
                    if hasattr(submission, 'media_metadata') and submission.media_metadata is not None:
                        image_urls = [html.unescape(media['s']['u'])
                                      for media in submission.media_metadata.values()
                                      if 's' in media and 'u' in media['s']]
                else:
                    image_urls = [submission.url]

                for url in image_urls:
                    self.download_file(url, log_skip=True)
            except AttributeError as e:
                logger.error(f"AttributeError: {e} for submission {submission.id}")
            except Exception as e:
                logger.exception(f"Unexpected error while downloading images for submission {submission.id}: {e}")

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

        # Log the file path before checking if it exists
        logger.debug(f"File path to check: {file_path}")
        if os.path.exists(file_path):
            logger.debug(f"File found in cache: {file_path}")
            if log_skip:
                logger.debug(f"File already exists in cache, skipping: {file_path} - {url}")
            return file_path
        else:
            logger.debug(f"File not found in cache: {file_path}, downloading...")

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
                    logger.debug(f"File downloaded to: {file_path}")

                    # Verify that the file was saved correctly
                    if os.path.exists(file_path):
                        logger.debug(f"File successfully saved: {file_path}")
                    else:
                        logger.error(f"File was not saved correctly: {file_path}")

                    return file_path
                elif response.status_code == 429:
                    logger.warning(f"Rate limit exceeded. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                elif response.history:
                    url = response.url
                    continue
                else:
                    logger.error('Invalid image URL or content type: %s', url)
                    return None
            except requests.RequestException as e:
                logger.exception("Request failed: %s", e)
                return None

        logger.error(f"Failed to download file after {max_retries} attempts: {url}")
        return None


class ThumbnailWidget(QWidget):    
    def __init__(self, images, title, source_url, submission, subreddit_name, has_multiple_images, post_url, is_moderator):
        super().__init__()
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
        logger.debug(f"ThumbnailWidget initialized with is_moderator: {self.is_moderator}")
        
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
        try:
            self.praw_submission.mod.remove()
            logging.debug(f"Removed: {self.submission_id}")

            # Update button appearance after removal
            self.remove_button.setStyleSheet("background-color: red;")
            self.remove_button.setText("Removed")
        except prawcore.exceptions.Forbidden:
            logging.error(f"Forbidden: You do not have permission to remove submission {self.submission_id}")
        except Exception as e:
            logging.exception(f"Unexpected error while removing submission {self.submission_id}: {e}")


if __name__ == '__main__':
    app = QApplication(sys.argv)
    # Set the application-wide stylesheet
    app.setStyleSheet("""
        QMainWindow, QWidget {
            background-color: #121212;
            color: white;
        }
        QPushButton { 
            color: white; 
            background-color: #1e1e1e; 
            border: 1px solid #333333; 
            padding: 5px;
        }
        QLineEdit {
            color: white;
            background-color: #1e1e1e;
            border: 1px solid #333333;
            padding: 5px;
        }
        QLabel {
            color: white;
        }
        QMessageBox {
            color: white; 
            background-color: #121212;
        }
        QTableWidget {
            background-color: #1e1e1e;
            color: white;
            gridline-color: #333333;
        }
        QHeaderView::section {
            background-color: #1e1e1e;
            color: white;
            border: 1px solid #333333;
        }
    """)



    main_win = MainWindow(subreddit=default_subreddit)
    main_win.show()
    
    # Force the main window to update
    main_win.update()
    QApplication.processEvents()

    sys.exit(app.exec_())