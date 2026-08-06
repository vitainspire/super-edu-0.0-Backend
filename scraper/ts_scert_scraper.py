"""
TS SCERT Books Scraper
Scrapes book information and PDFs from ncertbooks.guru/ts-scert-books/
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import time
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse
import logging
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TSScertScraper:
    """Scraper for TS SCERT educational books"""
    
    def __init__(self, base_url: str = "https://www.ncertbooks.guru/ts-scert-books/"):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        self.books_data = []
        
    def fetch_page(self, url: str, retries: int = 3) -> Optional[BeautifulSoup]:
        """Fetch and parse a webpage with retry logic"""
        for attempt in range(retries):
            try:
                logger.info(f"Fetching {url} (attempt {attempt + 1}/{retries})")
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return BeautifulSoup(response.content, 'html.parser')
            except requests.RequestException as e:
                logger.error(f"Error fetching {url}: {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                else:
                    return None
        return None
    
    def extract_book_links(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        """Extract book links from the main page"""
        books = []
        
        # Look for common patterns in educational book sites
        # Pattern 1: Links in list items
        for item in soup.find_all(['li', 'div', 'article'], class_=lambda x: x and any(
            keyword in str(x).lower() for keyword in ['book', 'class', 'grade', 'subject']
        )):
            link = item.find('a')
            if link and link.get('href'):
                books.append({
                    'title': link.get_text(strip=True),
                    'url': urljoin(self.base_url, link['href']),
                    'description': item.get_text(strip=True)
                })
        
        # Pattern 2: Direct links with specific attributes
        for link in soup.find_all('a', href=True):
            href = link['href']
            text = link.get_text(strip=True)
            
            # Check if it's a book-related link
            if any(keyword in text.lower() for keyword in ['class', 'grade', 'book', 'pdf', 'download']):
                full_url = urljoin(self.base_url, href)
                if full_url not in [b['url'] for b in books]:
                    books.append({
                        'title': text,
                        'url': full_url,
                        'description': text
                    })
        
        logger.info(f"Found {len(books)} book links")
        return books
    
    def extract_pdf_links(self, soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
        """Extract PDF download links from a book page"""
        pdfs = []
        
        # Look for PDF links
        for link in soup.find_all('a', href=True):
            href = link['href']
            if href.endswith('.pdf') or 'pdf' in href.lower():
                pdfs.append({
                    'title': link.get_text(strip=True),
                    'url': urljoin(page_url, href),
                    'filename': os.path.basename(urlparse(href).path)
                })
        
        # Look for download buttons/links
        for element in soup.find_all(['a', 'button'], class_=lambda x: x and 'download' in str(x).lower()):
            if element.name == 'a' and element.get('href'):
                href = element['href']
                pdfs.append({
                    'title': element.get_text(strip=True),
                    'url': urljoin(page_url, href),
                    'filename': os.path.basename(urlparse(href).path) or 'download.pdf'
                })
        
        logger.info(f"Found {len(pdfs)} PDF links on {page_url}")
        return pdfs
    
    def extract_book_metadata(self, soup: BeautifulSoup) -> Dict[str, any]:
        """Extract metadata from a book page"""
        metadata = {
            'title': '',
            'class': '',
            'subject': '',
            'language': '',
            'publisher': 'TS SCERT',
            'description': ''
        }
        
        # Try to extract title
        title_tag = soup.find(['h1', 'h2'], class_=lambda x: x and 'title' in str(x).lower())
        if title_tag:
            metadata['title'] = title_tag.get_text(strip=True)
        elif soup.find('h1'):
            metadata['title'] = soup.find('h1').get_text(strip=True)
        
        # Try to extract class/grade
        text_content = soup.get_text().lower()
        for i in range(1, 13):
            if f'class {i}' in text_content or f'grade {i}' in text_content:
                metadata['class'] = f'Class {i}'
                break
        
        # Extract description
        description_tag = soup.find(['p', 'div'], class_=lambda x: x and 'description' in str(x).lower())
        if description_tag:
            metadata['description'] = description_tag.get_text(strip=True)
        
        return metadata
    
    def scrape_all_books(self) -> List[Dict[str, any]]:
        """Scrape all books from the main page"""
        logger.info("Starting to scrape TS SCERT books")
        
        # Fetch main page
        main_soup = self.fetch_page(self.base_url)
        if not main_soup:
            logger.error("Failed to fetch main page")
            return []
        
        # Extract book links
        book_links = self.extract_book_links(main_soup)
        
        # Process each book
        for idx, book_link in enumerate(book_links, 1):
            logger.info(f"Processing book {idx}/{len(book_links)}: {book_link['title']}")
            
            book_soup = self.fetch_page(book_link['url'])
            if not book_soup:
                continue
            
            # Extract metadata
            metadata = self.extract_book_metadata(book_soup)
            
            # Extract PDF links
            pdf_links = self.extract_pdf_links(book_soup, book_link['url'])
            
            book_data = {
                'title': metadata['title'] or book_link['title'],
                'url': book_link['url'],
                'metadata': metadata,
                'pdfs': pdf_links,
                'scraped_at': time.strftime('%Y-%m-%d %H:%M:%S')
            }
            
            self.books_data.append(book_data)
            
            # Be respectful - add delay between requests
            time.sleep(1)
        
        logger.info(f"Scraping completed. Total books: {len(self.books_data)}")
        return self.books_data
    
    def download_pdf(self, pdf_url: str, output_dir: str = "downloads") -> Optional[str]:
        """Download a PDF file"""
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            
            logger.info(f"Downloading PDF: {pdf_url}")
            response = self.session.get(pdf_url, timeout=60, stream=True)
            response.raise_for_status()
            
            # Get filename from URL or Content-Disposition header
            filename = os.path.basename(urlparse(pdf_url).path)
            if not filename.endswith('.pdf'):
                filename = f"{filename}.pdf"
            
            filepath = os.path.join(output_dir, filename)
            
            # Download with progress
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size:
                            progress = (downloaded / total_size) * 100
                            logger.debug(f"Download progress: {progress:.1f}%")
            
            logger.info(f"Downloaded to: {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"Error downloading {pdf_url}: {e}")
            return None
    
    def download_all_pdfs(self, output_dir: str = "downloads"):
        """Download all PDFs from scraped books"""
        total_pdfs = sum(len(book['pdfs']) for book in self.books_data)
        logger.info(f"Starting to download {total_pdfs} PDFs")
        
        downloaded = 0
        for book in self.books_data:
            book_dir = os.path.join(output_dir, self._sanitize_filename(book['title']))
            
            for pdf in book['pdfs']:
                filepath = self.download_pdf(pdf['url'], book_dir)
                if filepath:
                    downloaded += 1
                    pdf['local_path'] = filepath
                
                time.sleep(1)  # Be respectful
        
        logger.info(f"Downloaded {downloaded}/{total_pdfs} PDFs successfully")
    
    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for file system"""
        # Remove invalid characters
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, '_')
        return filename.strip()
    
    def save_metadata(self, output_file: str = "books_metadata.json"):
        """Save scraped metadata to JSON file"""
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(self.books_data, f, indent=2, ensure_ascii=False)
            logger.info(f"Metadata saved to: {output_file}")
        except Exception as e:
            logger.error(f"Error saving metadata: {e}")
    
    def get_books_by_class(self, class_number: int) -> List[Dict[str, any]]:
        """Filter books by class number"""
        return [
            book for book in self.books_data 
            if f'class {class_number}' in book['title'].lower() or 
               f'class {class_number}' in book['metadata'].get('class', '').lower()
        ]


def main():
    """Main function to run the scraper"""
    scraper = TSScertScraper()
    
    # Scrape all books
    books = scraper.scrape_all_books()
    
    # Save metadata
    scraper.save_metadata("ts_scert_books_metadata.json")
    
    # Optionally download PDFs (commented out by default)
    # scraper.download_all_pdfs("ts_scert_books")
    
    # Print summary
    print(f"\n{'='*50}")
    print(f"Scraping completed!")
    print(f"Total books found: {len(books)}")
    print(f"{'='*50}\n")
    
    # Print sample data
    for book in books[:3]:
        print(f"Title: {book['title']}")
        print(f"URL: {book['url']}")
        print(f"PDFs: {len(book['pdfs'])}")
        print("-" * 50)


if __name__ == "__main__":
    main()
