# Reddit Image and Video Browser

A lightweight Python desktop application that allows you to browse images and videos from Reddit. Built with [PyQt5](https://pypi.org/project/PyQt5/), [PRAW](https://praw.readthedocs.io/en/latest/), and [vlc](https://www.olivieraubert.net/vlc/python-bindings-doc/), this app displays media content from subreddits or user profiles and offers basic moderation tools if you’re a moderator.

## Features

- **Subreddit & User Browsing:** Load posts from your favorite subreddits or view posts from a specific Reddit user.
- **Paginated Gallery:** Navigate through submissions using Previous/Next page buttons.
- **Bulk Download:** Download the media for the next 100 posts at once.
- **Interactive Thumbnails:** Click on thumbnails to open the corresponding Reddit post in your browser.
- **Moderator Tools:** If you have moderator privileges, approve or remove submissions directly from the app.
- **Automatic Media Processing:** Handles image, video, GIF, and RedGIFs media URLs, including necessary workarounds for redirects and API calls.

## Requirements

- Python 3.6+
- [PyQt5](https://pypi.org/project/PyQt5/)
- [PRAW](https://pypi.org/project/praw/)
- [requests](https://pypi.org/project/requests/)
- [python-vlc](https://pypi.org/project/python-vlc/)

> **Tip:** Consider using a virtual environment to manage dependencies.

## Installation

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/yourusername/your-repo-name.git
   cd your-repo-name
   ```

2. **Install Dependencies:**

   You can install the required packages with pip:

   ```bash
   pip install PyQt5 praw requests python-vlc
   ```

   Alternatively, if a `requirements.txt` file is provided:

   ```bash
   pip install -r requirements.txt
   ```

3. **Run the Application:**

   Navigate to the source directory and run the program:

   ```bash
   python src/red-image-browser.py
   ```

## Configuration

Before running the program, you need to provide your Reddit API credentials through a configuration file.

### Creating a Config File

- **Interactive Setup:**  
  When the app starts, if `config.json` is missing, you will be prompted in the terminal to enter your credentials. The required keys include:
  - `client_id`
  - `client_secret`
  - `redirect_uri` (default: `http://localhost:8080`)
  - `refresh_token` (if you don’t have one, leave it blank to follow the interactive flow)
  - `user_agent` (default: `red-image-browser/1.0`)
  - `default_subreddit` (e.g., `pics`)

- **Manual Setup:**  
  Alternatively, create a file named `config.json` in the same directory (next to `red-image-browser.py`) with the following format:

  ```json
  {
      "client_id": "YOUR_CLIENT_ID",
      "client_secret": "YOUR_CLIENT_SECRET",
      "redirect_uri": "http://localhost:8080",
      "refresh_token": "YOUR_REFRESH_TOKEN_IF_AVAILABLE",
      "user_agent": "red-image-browser/1.0",
      "default_subreddit": "pics"
  }
  ```

## Reddit Authentication Setup

To work with Reddit’s API, you need to create a Reddit application to obtain your authentication credentials:

1. **Create a Reddit App:**
   - Visit [Reddit Apps](https://www.reddit.com/prefs/apps).
   - Scroll down and click on **"Create App"** or **"Create Another App"**.
   - Fill out the form:
     - **Name:** Choose a name for your app.
     - **App Type:** Select **script**.
     - **Redirect URI:** Use `http://localhost:8080` (or another URI if you customize it).
   - Click **Create App**.

2. **Configure Your App:**
   - Copy your **client_id** (displayed under the app name) and **client_secret**.
   - Insert these into your `config.json` file or provide them during the interactive configuration.

3. **Refresh Token Process:**
   - If you leave the refresh token field blank, the program will guide you through obtaining one by opening a browser window. After authorizing the app, copy and paste the redirected URL into the terminal as prompted.

## Usage

### Launching the App

Run the following command:

bash
python src/red-image-browser.py


### User Interface Controls

- **Subreddit Input & "Load Subreddit" Button:**  
  Type the name of a subreddit in the text field and click "Load Subreddit" (or press Enter) to fetch and display the latest posts from that subreddit.

- **User Input & "Load User" Button:**  
  Enter a Reddit username and click "Load User" to display posts made by that user. A "Back to Subreddit" button will appear to allow you to return to your previous subreddit view.

- **Thumbnail Interactions:**  
  - **Click on an Image:** Opens the full Reddit post in your default web browser.
  - **Click on the Author:** The author’s name (displayed on the thumbnail) is clickable. Tapping it will load posts from that user.
  
- **Navigation Buttons:**
  - **Previous Page:** Loads the previous set of submissions.
  - **Next Page:** Loads the next set of submissions. If no local cache is available for the next page, it fetches additional posts.
  - **Download Next 100:** Initiates a bulk download of media (images/videos) for the next 100 submissions. Media files are stored in a local cache to improve performance in subsequent views.

- **Moderator Buttons (for Subreddit Moderators):**
  - **Approve:** For users with moderator privileges, this button will approve a submission.
  - **Remove:** Allows moderators to remove a submission.

## Troubleshooting

- **Configuration Issues:**  
  Ensure that your `config.json` file is correctly formatted and placed in the same directory as `red-image-browser.py`.

- **Reddit API Errors:**  
  If you encounter errors regarding scopes or authentication, double-check your Reddit app settings and credentials. The program will prompt for a new refresh token if necessary.

- **Media Not Loading:**  
  If images or videos do not load, verify your internet connection and that the provided media URLs are accessible.

## Contributing

Contributions are welcome! Feel free to fork this repository and submit pull requests. Please ensure your code adheres to the project's style and add tests if applicable.

## License

Distributed under the MIT License. See `LICENSE` for more information.