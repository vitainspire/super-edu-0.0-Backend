"""
Configuration file for TS SCERT Books Scraper
"""

import os
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).parent

# URLs
BASE_URL = "https://www.ncertbooks.guru/ts-scert-books/"

# Output directories
OUTPUT_DIR = BASE_DIR / "output"
DOWNLOADS_DIR = OUTPUT_DIR / "downloads"
METADATA_DIR = OUTPUT_DIR / "metadata"
LOGS_DIR = OUTPUT_DIR / "logs"

# Create directories if they don't exist
for directory in [OUTPUT_DIR, DOWNLOADS_DIR, METADATA_DIR, LOGS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# Scraper settings
SCRAPER_CONFIG = {
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'timeout': 30,
    'download_timeout': 120,
    'retry_attempts': 3,
    'retry_delay': 2,  # seconds
    'request_delay': 1,  # seconds between requests
    'max_pages': 100,
    'chunk_size': 8192,  # bytes for download
}

# Logging configuration
LOGGING_CONFIG = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'detailed': {
            'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        },
        'simple': {
            'format': '%(levelname)s - %(message)s'
        }
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'INFO',
            'formatter': 'simple',
            'stream': 'ext://sys.stdout'
        },
        'file': {
            'class': 'logging.FileHandler',
            'level': 'DEBUG',
            'formatter': 'detailed',
            'filename': str(LOGS_DIR / 'scraper.log'),
            'mode': 'a'
        }
    },
    'root': {
        'level': 'DEBUG',
        'handlers': ['console', 'file']
    }
}

# Book classification patterns
CLASS_PATTERNS = [
    r'class\s*(\d+)',
    r'grade\s*(\d+)',
    r'(\d+)th\s*class',
    r'(\d+)st\s*class',
    r'(\d+)nd\s*class',
    r'(\d+)rd\s*class',
]

SUBJECT_KEYWORDS = [
    'mathematics', 'math', 'maths',
    'science', 'physics', 'chemistry', 'biology',
    'english', 'hindi', 'telugu', 'urdu',
    'social', 'history', 'geography', 'civics',
    'economics', 'political science',
    'environmental studies', 'evs',
    'computer science', 'cs',
]

# File naming
METADATA_FILENAME = "books_metadata.json"
BOOKS_LIST_FILENAME = "books_list.json"
ERRORS_FILENAME = "errors.json"
