"""
Utility functions for the scraper
"""

import re
import json
import hashlib
from typing import List, Dict, Optional
from pathlib import Path
from datetime import datetime


def sanitize_filename(filename: str, max_length: int = 255) -> str:
    """
    Sanitize filename for file system compatibility
    
    Args:
        filename: Original filename
        max_length: Maximum length for filename
    
    Returns:
        Sanitized filename
    """
    # Remove invalid characters
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    
    # Remove leading/trailing spaces and dots
    filename = filename.strip('. ')
    
    # Replace multiple spaces with single space
    filename = re.sub(r'\s+', ' ', filename)
    
    # Truncate if too long
    if len(filename) > max_length:
        name, ext = Path(filename).stem, Path(filename).suffix
        filename = name[:max_length - len(ext)] + ext
    
    return filename or 'unnamed'


def extract_class_number(text: str) -> Optional[int]:
    """
    Extract class/grade number from text
    
    Args:
        text: Input text
    
    Returns:
        Class number if found, None otherwise
    """
    patterns = [
        r'class\s*(\d+)',
        r'grade\s*(\d+)',
        r'(\d+)(?:st|nd|rd|th)\s*(?:class|grade)',
    ]
    
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            return int(match.group(1))
    
    return None


def extract_subject(text: str, subject_keywords: List[str]) -> Optional[str]:
    """
    Extract subject name from text
    
    Args:
        text: Input text
        subject_keywords: List of subject keywords to search for
    
    Returns:
        Subject name if found, None otherwise
    """
    text_lower = text.lower()
    
    for keyword in subject_keywords:
        if keyword in text_lower:
            return keyword.title()
    
    return None


def generate_file_hash(filepath: Path) -> str:
    """
    Generate SHA256 hash of a file
    
    Args:
        filepath: Path to file
    
    Returns:
        Hex digest of file hash
    """
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def save_json(data: any, filepath: Path, indent: int = 2):
    """
    Save data to JSON file
    
    Args:
        data: Data to save
        filepath: Output file path
        indent: JSON indentation level
    """
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)


def load_json(filepath: Path) -> any:
    """
    Load data from JSON file
    
    Args:
        filepath: Input file path
    
    Returns:
        Loaded data
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def format_size(size_bytes: int) -> str:
    """
    Format size in bytes to human-readable format
    
    Args:
        size_bytes: Size in bytes
    
    Returns:
        Formatted size string
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def format_duration(seconds: float) -> str:
    """
    Format duration in seconds to human-readable format
    
    Args:
        seconds: Duration in seconds
    
    Returns:
        Formatted duration string
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}m"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}h"


def create_summary_report(books_data: List[Dict]) -> Dict[str, any]:
    """
    Create a summary report from scraped books data
    
    Args:
        books_data: List of book data dictionaries
    
    Returns:
        Summary report dictionary
    """
    report = {
        'total_books': len(books_data),
        'total_pdfs': sum(len(book.get('pdfs', [])) for book in books_data),
        'books_by_class': {},
        'books_by_subject': {},
        'timestamp': datetime.now().isoformat(),
    }
    
    # Count by class
    for book in books_data:
        class_num = extract_class_number(book.get('title', ''))
        if class_num:
            class_key = f"Class {class_num}"
            report['books_by_class'][class_key] = report['books_by_class'].get(class_key, 0) + 1
    
    # Count by subject
    for book in books_data:
        title = book.get('title', '').lower()
        for subject in ['mathematics', 'science', 'english', 'social', 'hindi', 'telugu']:
            if subject in title:
                subject_key = subject.title()
                report['books_by_subject'][subject_key] = report['books_by_subject'].get(subject_key, 0) + 1
                break
    
    return report


def validate_url(url: str) -> bool:
    """
    Validate if a string is a valid URL
    
    Args:
        url: URL string to validate
    
    Returns:
        True if valid, False otherwise
    """
    url_pattern = re.compile(
        r'^https?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain
        r'localhost|'  # localhost
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # or IP
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    
    return url_pattern.match(url) is not None


def deduplicate_books(books: List[Dict], key: str = 'url') -> List[Dict]:
    """
    Remove duplicate books based on a key
    
    Args:
        books: List of book dictionaries
        key: Key to use for deduplication
    
    Returns:
        Deduplicated list of books
    """
    seen = set()
    unique_books = []
    
    for book in books:
        value = book.get(key)
        if value and value not in seen:
            seen.add(value)
            unique_books.append(book)
    
    return unique_books


def merge_book_data(existing_data: List[Dict], new_data: List[Dict]) -> List[Dict]:
    """
    Merge new book data with existing data, updating existing entries
    
    Args:
        existing_data: Existing book data
        new_data: New book data to merge
    
    Returns:
        Merged book data
    """
    # Create a dictionary keyed by URL for easy lookup
    merged = {book['url']: book for book in existing_data}
    
    # Update with new data
    for book in new_data:
        url = book['url']
        if url in merged:
            # Update existing entry
            merged[url].update(book)
            merged[url]['updated_at'] = datetime.now().isoformat()
        else:
            # Add new entry
            merged[url] = book
    
    return list(merged.values())


class ProgressTracker:
    """Simple progress tracker for scraping operations"""
    
    def __init__(self, total: int):
        self.total = total
        self.current = 0
        self.start_time = datetime.now()
    
    def update(self, increment: int = 1):
        """Update progress"""
        self.current += increment
        self._print_progress()
    
    def _print_progress(self):
        """Print progress bar"""
        if self.total == 0:
            return
        
        percent = (self.current / self.total) * 100
        bar_length = 50
        filled = int(bar_length * self.current / self.total)
        bar = '█' * filled + '░' * (bar_length - filled)
        
        elapsed = (datetime.now() - self.start_time).total_seconds()
        if self.current > 0:
            eta = (elapsed / self.current) * (self.total - self.current)
            eta_str = format_duration(eta)
        else:
            eta_str = "?"
        
        print(f'\r[{bar}] {percent:.1f}% ({self.current}/{self.total}) ETA: {eta_str}', end='')
    
    def finish(self):
        """Complete progress tracking"""
        print()  # New line
        elapsed = (datetime.now() - self.start_time).total_seconds()
        print(f"Completed in {format_duration(elapsed)}")
