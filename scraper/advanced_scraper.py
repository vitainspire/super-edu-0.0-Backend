"""
Advanced TS SCERT Books Scraper with Selenium support for JavaScript-heavy sites
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Optional Selenium support for JavaScript-rendered content
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class AdvancedScraper:
    """Advanced scraper with Selenium fallback for JavaScript content"""
    
    def __init__(self, base_url: str, use_selenium: bool = False):
        self.base_url = base_url
        self.use_selenium = use_selenium and SELENIUM_AVAILABLE
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        self.driver = None
        
        if self.use_selenium:
            self._init_selenium()
    
    def _init_selenium(self):
        """Initialize Selenium WebDriver"""
        try:
            chrome_options = Options()
            chrome_options.add_argument('--headless')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            self.driver = webdriver.Chrome(options=chrome_options)
            logger.info("Selenium WebDriver initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Selenium: {e}")
            self.use_selenium = False
    
    def fetch_with_selenium(self, url: str, wait_time: int = 10) -> Optional[str]:
        """Fetch page content using Selenium"""
        try:
            self.driver.get(url)
            WebDriverWait(self.driver, wait_time).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(2)  # Additional wait for dynamic content
            return self.driver.page_source
        except Exception as e:
            logger.error(f"Selenium fetch error for {url}: {e}")
            return None
    
    def fetch_page(self, url: str) -> Optional[BeautifulSoup]:
        """Fetch page with fallback to Selenium if needed"""
        # Try regular requests first
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            html = response.text
        except Exception as e:
            logger.warning(f"Regular fetch failed for {url}: {e}")
            if self.use_selenium:
                logger.info("Trying with Selenium...")
                html = self.fetch_with_selenium(url)
                if not html:
                    return None
            else:
                return None
        
        return BeautifulSoup(html, 'html.parser')
    
    def extract_all_links(self, soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
        """Extract all relevant links from page"""
        links = []
        seen_urls = set()
        
        for link in soup.find_all('a', href=True):
            href = link['href']
            full_url = urljoin(base_url, href)
            
            if full_url not in seen_urls:
                links.append({
                    'text': link.get_text(strip=True),
                    'url': full_url,
                    'title': link.get('title', ''),
                    'class': ' '.join(link.get('class', []))
                })
                seen_urls.add(full_url)
        
        return links
    
    def find_pdf_links(self, links: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Filter PDF links from all links"""
        pdf_links = []
        for link in links:
            url = link['url'].lower()
            text = link['text'].lower()
            
            if url.endswith('.pdf') or '.pdf?' in url or 'pdf' in text or 'download' in text:
                pdf_links.append(link)
        
        return pdf_links
    
    def extract_text_content(self, soup: BeautifulSoup) -> Dict[str, any]:
        """Extract structured text content from page"""
        content = {
            'title': '',
            'headings': [],
            'paragraphs': [],
            'lists': []
        }
        
        # Extract title
        title = soup.find('h1')
        if title:
            content['title'] = title.get_text(strip=True)
        
        # Extract headings
        for heading in soup.find_all(['h1', 'h2', 'h3', 'h4']):
            content['headings'].append({
                'level': heading.name,
                'text': heading.get_text(strip=True)
            })
        
        # Extract paragraphs
        for para in soup.find_all('p'):
            text = para.get_text(strip=True)
            if text and len(text) > 20:  # Filter out empty or very short paragraphs
                content['paragraphs'].append(text)
        
        # Extract lists
        for ul in soup.find_all(['ul', 'ol']):
            items = [li.get_text(strip=True) for li in ul.find_all('li')]
            if items:
                content['lists'].append(items)
        
        return content
    
    def download_file(self, url: str, output_path: str) -> bool:
        """Download file with progress tracking"""
        try:
            response = self.session.get(url, stream=True, timeout=120)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            
            with open(output_path, 'wb') as f:
                if total_size == 0:
                    f.write(response.content)
                else:
                    downloaded = 0
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size:
                                percent = (downloaded / total_size) * 100
                                sys.stdout.write(f"\rDownloading: {percent:.1f}%")
                                sys.stdout.flush()
            
            print()  # New line after progress
            logger.info(f"Downloaded: {output_path}")
            return True
            
        except Exception as e:
            logger.error(f"Download failed for {url}: {e}")
            return False
    
    def scrape_book_page(self, url: str) -> Dict[str, any]:
        """Scrape a single book page"""
        logger.info(f"Scraping: {url}")
        
        soup = self.fetch_page(url)
        if not soup:
            return None
        
        # Extract all data
        all_links = self.extract_all_links(soup, url)
        pdf_links = self.find_pdf_links(all_links)
        content = self.extract_text_content(soup)
        
        return {
            'url': url,
            'content': content,
            'pdf_links': pdf_links,
            'all_links': all_links[:50],  # Limit to first 50 links
            'scraped_at': time.strftime('%Y-%m-%d %H:%M:%S')
        }
    
    def close(self):
        """Clean up resources"""
        if self.driver:
            self.driver.quit()
            logger.info("Selenium WebDriver closed")


def main():
    parser = argparse.ArgumentParser(description='Advanced TS SCERT Books Scraper')
    parser.add_argument('--url', default='https://www.ncertbooks.guru/ts-scert-books/',
                        help='Base URL to scrape')
    parser.add_argument('--output', default='scraped_data.json',
                        help='Output JSON file')
    parser.add_argument('--download-pdfs', action='store_true',
                        help='Download all found PDFs')
    parser.add_argument('--download-dir', default='downloads',
                        help='Directory for downloaded PDFs')
    parser.add_argument('--use-selenium', action='store_true',
                        help='Use Selenium for JavaScript-heavy pages')
    parser.add_argument('--max-pages', type=int, default=50,
                        help='Maximum number of pages to scrape')
    
    args = parser.parse_args()
    
    # Initialize scraper
    scraper = AdvancedScraper(args.url, use_selenium=args.use_selenium)
    
    try:
        # Scrape main page
        logger.info(f"Starting scrape of {args.url}")
        main_data = scraper.scrape_book_page(args.url)
        
        if not main_data:
            logger.error("Failed to scrape main page")
            return
        
        # Save main page data
        results = [main_data]
        
        # Find book pages (links that might lead to individual books)
        book_links = [
            link for link in main_data['all_links']
            if any(keyword in link['text'].lower() for keyword in ['class', 'grade', 'book'])
        ]
        
        logger.info(f"Found {len(book_links)} potential book pages")
        
        # Scrape individual book pages
        for idx, book_link in enumerate(book_links[:args.max_pages], 1):
            logger.info(f"Processing {idx}/{min(len(book_links), args.max_pages)}")
            book_data = scraper.scrape_book_page(book_link['url'])
            if book_data:
                results.append(book_data)
            time.sleep(1)  # Be respectful
        
        # Save results
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Results saved to {args.output}")
        
        # Download PDFs if requested
        if args.download_pdfs:
            Path(args.download_dir).mkdir(parents=True, exist_ok=True)
            
            all_pdfs = []
            for result in results:
                all_pdfs.extend(result.get('pdf_links', []))
            
            logger.info(f"Downloading {len(all_pdfs)} PDFs...")
            
            for idx, pdf in enumerate(all_pdfs, 1):
                filename = os.path.basename(urlparse(pdf['url']).path) or f'book_{idx}.pdf'
                output_path = os.path.join(args.download_dir, filename)
                
                logger.info(f"Downloading {idx}/{len(all_pdfs)}: {filename}")
                scraper.download_file(pdf['url'], output_path)
                time.sleep(1)
        
        # Print summary
        total_pdfs = sum(len(r.get('pdf_links', [])) for r in results)
        print(f"\n{'='*60}")
        print(f"Scraping Complete!")
        print(f"Pages scraped: {len(results)}")
        print(f"PDFs found: {total_pdfs}")
        print(f"Results saved to: {args.output}")
        print(f"{'='*60}\n")
        
    finally:
        scraper.close()


if __name__ == '__main__':
    main()
