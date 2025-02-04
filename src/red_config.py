#!/usr/bin/env python3
"""
Reddit Configuration Manager

This module provides helper functions for creating and loading a configuration
file (config.json) containing Reddit API credentials. It also provides methods
to obtain a new refresh token and update the configuration with it.

Usage Example:
    from reddit_config import load_config, get_new_refresh_token, update_config_with_new_token

    config_path = "./config.json"
    config = load_config(config_path)

    # ... Initialize your PRAW Reddit instance with config values ...

    # If you need a new refresh token:
    new_token = get_new_refresh_token(reddit, requested_scopes)
    if new_token:
        update_config_with_new_token(config, config_path, new_token)
"""

import os
import json
import logging
import sys
import webbrowser
import re
from urllib.parse import urlparse, parse_qs, quote

import praw
import prawcore.exceptions

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt='%Y-%m-%d %H:%M:%S'
    )

def create_config_file(config_path):
    """
    Creates a new configuration file (config.json) with user-provided values.
    Prompts the user to input their Reddit API credentials and saves them.
    """
    print("config.json not found. Let's create one with your Reddit API credentials.")
    print("Please visit 'https://www.reddit.com/prefs/apps' to create an application if you haven't already.")
    
    client_id = input("Enter Reddit client_id: ").strip()
    client_secret = input("Enter Reddit client_secret: ").strip()
    redirect_uri = input("Enter redirect URI [default: http://localhost:8080]: ").strip() or "http://localhost:8080"
    print("If you already have a Reddit refresh token, enter it now. Otherwise, leave this blank.")
    refresh_token = input("Enter Reddit refresh_token (or leave blank if you don't have one yet): ").strip()
    user_agent = input("Enter user_agent [default: red-image-browser/1.0]: ").strip() or "red-image-browser/1.0"
    default_subreddit = input("Enter default subreddit [default: pics]: ").strip() or "pics"

    config_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "refresh_token": refresh_token,
        "user_agent": user_agent,
        "default_subreddit": default_subreddit
    }

    try:
        with open(config_path, 'w') as config_file:
            json.dump(config_data, config_file, indent=4)
        print(f"Created new configuration file at {config_path}.")
        if not refresh_token:
            print("Since you left the refresh token blank, the application will now guide you through obtaining one.")
            print("When a browser window opens, log into Reddit and authorize the app using the provided redirect URI (http://localhost:8080).")
            print("Then copy the authorization code from your browser back here when prompted.")
            print("If you encounter any issues, please consult Reddit's OAuth2 documentation.")
    except Exception as e:
        logger.exception("Failed to create config.json: " + str(e))
        sys.exit(1)

def load_config(config_path):
    """
    Loads the configuration from config.json.
    If the file does not exist, it is created interactively.
    """
    if not os.path.exists(config_path):
        create_config_file(config_path)
    try:
        with open(config_path, 'r') as config_file:
            config = json.load(config_file)
        return config
    except Exception as e:
        logger.exception("Error reading configuration file: " + str(e))
        sys.exit(1)

def get_new_refresh_token(reddit, requested_scopes):
    """
    Attempts to obtain a new Reddit refresh token.

    Parameters:
        reddit (praw.Reddit): An instance of the Reddit API client.
        requested_scopes (list): A list of scopes required for the application.

    Returns:
        str or None: The new refresh token if successfully obtained, otherwise None.
    """
    logger.info("Requesting new refresh token with proper scopes.")
    auth_url = reddit.auth.url(requested_scopes, 'uniqueKey', 'permanent')
    logger.info(f"Please visit this URL to authorize the application: {auth_url}")
    webbrowser.open(auth_url)

    redirected_url = input("After authorization, paste the full redirected URL here: ")
    parsed_url = urlparse(redirected_url)
    query_params = parse_qs(parsed_url.query)
    auth_code = query_params.get("code", [None])[0]

    if not auth_code:
        logger.error("Authorization code not found in the provided URL.")
        return None

    try:
        refresh_token = reddit.auth.authorize(auth_code)
        logger.info("Successfully obtained new refresh token.")
        return refresh_token
    except prawcore.exceptions.PrawcoreException as e:
        logger.error(f"Error obtaining new refresh token: {e}")
        return None

def update_config_with_new_token(config, config_path, new_token):
    """
    Updates the existing configuration file with the new refresh token.

    Parameters:
        config (dict): The current configuration dictionary.
        config_path (str): Path to the config.json file.
        new_token (str): The newly obtained refresh token.
    """
    config['refresh_token'] = new_token
    try:
        with open(config_path, 'w') as config_file:
            json.dump(config, config_file, indent=4)
        logger.info("Updated config.json with new refresh token.")
    except Exception as e:
        logger.exception("Failed to update config.json: " + str(e))

if __name__ == "__main__":
    # For quick testing of the configuration manager
    config_file_path = os.path.join(os.path.dirname(__file__), "config.json")
    config = load_config(config_file_path)
    print("Loaded configuration:")
    print(json.dumps(config, indent=4))