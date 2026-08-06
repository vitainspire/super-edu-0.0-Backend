# TS SCERT Books Scraper

A Python scraper to extract book information and PDFs from the TS SCERT books website (ncertbooks.guru/ts-scert-books/).

## Features

- 🔍 Scrapes all book listings from the main page
- 📚 Extracts book metadata (title, class, subject, etc.)
- 📄 Finds and downloads PDF links
- 💾 Saves metadata to JSON for easy processing
- 🔄 Retry logic with exponential backoff
- 📊 Progress logging
- 🎯 Filter books by class/grade

## Installation

```bash
cd backend/scraper
pip install -r requirements.txt
```

## Usage

### Basic Scraping (Metadata Only)

```python
from ts_scert_scraper import TSScertScraper

scraper = TSScertScraper()

# Scrape all books metadata
books = scraper.scrape_all_books()

# Save to JSON
scraper.save_metadata("books_data.json")
```

### Download PDFs

```python
scraper = TSScertScraper()
books = scraper.scrape_all_books()

# Download all PDFs to 'downloads' folder
scraper.download_all_pdfs("downloads")
```

### Filter by Class

```python
scraper = TSScertScraper()
books = scraper.scrape_all_books()

# Get only Class 5 books
class_5_books = scraper.get_books_by_class(5)
print(f"Found {len(class_5_books)} books for Class 5")
```

### Command Line Usage

```bash
python ts_scert_scraper.py
```

## Output Format

The scraper generates a JSON file with the following structure:

```json
[
  {
    "title": "Class 5 Mathematics",
    "url": "https://www.ncertbooks.guru/...",
    "metadata": {
      "title": "Class 5 Mathematics",
      "class": "Class 5",
      "subject": "Mathematics",
      "language": "English",
      "publisher": "TS SCERT",
      "description": "..."
    },
    "pdfs": [
      {
        "title": "Download PDF",
        "url": "https://...",
        "filename": "class5_math.pdf",
        "local_path": "downloads/class5_math.pdf"
      }
    ],
    "scraped_at": "2026-07-16 10:30:00"
  }
]
```

## Advanced Usage

### Custom Base URL

```python
scraper = TSScertScraper(base_url="https://example.com/books/")
```

### Download Single PDF

```python
scraper = TSScertScraper()
filepath = scraper.download_pdf(
    "https://example.com/book.pdf",
    output_dir="my_books"
)
```

### Process Results

```python
scraper = TSScertScraper()
books = scraper.scrape_all_books()

# Count books by class
from collections import Counter
classes = [book['metadata']['class'] for book in books if book['metadata']['class']]
print(Counter(classes))

# Get all PDF URLs
all_pdfs = []
for book in books:
    all_pdfs.extend([pdf['url'] for pdf in book['pdfs']])
```

## Configuration

The scraper includes several configurable options:

- **Retry Logic**: 3 attempts with exponential backoff
- **Timeout**: 30 seconds for page fetches, 60 seconds for PDF downloads
- **Rate Limiting**: 1 second delay between requests
- **User Agent**: Mozilla/5.0 (configurable in code)

## Error Handling

The scraper includes comprehensive error handling:
- Network timeouts and failures
- Invalid URLs
- Missing content
- File system errors
- Partial failures (continues with remaining items)

## Logging

Logs are written to console with the following format:
```
2026-07-16 10:30:00 - ts_scert_scraper - INFO - Fetching https://...
```

Adjust logging level in code:
```python
logging.basicConfig(level=logging.DEBUG)  # More verbose
logging.basicConfig(level=logging.WARNING)  # Less verbose
```

## Notes

- Be respectful of the website's resources (built-in delays)
- PDFs can be large - ensure adequate disk space
- Some books may have multiple PDFs (chapters, solutions, etc.)
- The scraper adapts to common HTML patterns but may need adjustments for specific site layouts

## Troubleshooting

### No books found
- Check if the website structure has changed
- Verify the base URL is correct
- Check logs for HTTP errors

### PDFs not downloading
- Verify PDF URLs are direct links
- Check internet connectivity
- Ensure write permissions for output directory

### Timeout errors
- Increase timeout values in code
- Check internet connection speed
- Website may be slow or blocking requests

## Legal Notice

This scraper is for educational purposes. Ensure you have permission to scrape and download content from the target website. Respect copyright and terms of service.
