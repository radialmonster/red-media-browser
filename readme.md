# Red Image Browser

This program is a Python-based desktop application that allows users to browse and view images and videos from a specified subreddit. It provides a user-friendly interface to navigate through the submissions, display thumbnails, and interact with the content.

## Features

- Load submissions from a specified subreddit
- Display thumbnails of images and videos in a grid layout
- Navigate through pages of submissions using Previous and Next buttons
- Open the original post on Reddit by clicking on a thumbnail
- Support for multiple images in a single submission (gallery)
- Caching mechanism to store fetched submissions for faster loading
- Moderation features for subreddit moderators (approve or remove submissions)

## Requirements

- Python 3.x
- PyQt5
- PRAW (Python Reddit API Wrapper)
- requests

## Installation

1. Clone the repository:
   ```
   git clone https://github.com/radialmonster/red-image-browser.git
   ```

2. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Create a `config.json` file in the project directory with your Reddit API credentials:
   ```json
   {
     "client_id": "your-client-id",
     "client_secret": "your-client-secret",
     "refresh_token": "your-refresh-token",
     "user_agent": "your-user-agent"
   }
   ```

   Replace the placeholders with your actual Reddit API credentials.

## Usage

1. Run the program:
   ```
   python red-image-browser.py
   ```

2. Enter the name of the subreddit you want to browse in the input field and click the "Load Subreddit" button.

3. The program will fetch the submissions from the specified subreddit and display them in a grid layout.

4. Use the Previous and Next buttons to navigate through pages of submissions.

5. Click on a thumbnail to open the original post on Reddit in your default web browser.

6. If you are a moderator of the subreddit, you will see "Approve" and "Remove" buttons below each submission thumbnail, allowing you to moderate the submissions directly from the application.

## Caching

The program implements a caching mechanism to store fetched submissions locally. This allows for faster loading of previously viewed submissions. The cached data is stored in the `cache` directory within the project folder.

## Logging

The program includes logging functionality to help with debugging and monitoring. Log messages are displayed in the console and stored in a log file. You can adjust the logging level in the code to control the verbosity of the logs.

## Contributing

Contributions to the project are welcome! If you find any bugs, have suggestions for improvements, or want to add new features, please open an issue or submit a pull request on the GitHub repository.

## License

This project is licensed under the [MIT License](LICENSE).