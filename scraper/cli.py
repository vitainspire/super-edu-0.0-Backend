"""
Command-line interface for TS SCERT Books Scraper
"""

import argparse
import sys
from pathlib import Path

from ts_scert_scraper import TSScertScraper
from utils import (
    create_summary_report, 
    save_json, 
    load_json,
    ProgressTracker,
    format_size
)
from config import (
    BASE_URL,
    DOWNLOADS_DIR,
    METADATA_DIR,
    METADATA_FILENAME
)


def cmd_scrape(args):
    """Scrape books from the website"""
    print("Starting TS SCERT Books Scraper...")
    print(f"Target URL: {args.url}")
    print("-" * 60)
    
    scraper = TSScertScraper(base_url=args.url)
    
    # Scrape all books
    books = scraper.scrape_all_books()
    
    if not books:
        print("No books found. The website structure may have changed.")
        return
    
    # Save metadata
    output_path = METADATA_DIR / args.output
    scraper.save_metadata(str(output_path))
    
    # Create summary report
    report = create_summary_report(books)
    report_path = METADATA_DIR / "summary_report.json"
    save_json(report, report_path)
    
    # Print summary
    print("\n" + "=" * 60)
    print("SCRAPING COMPLETE")
    print("=" * 60)
    print(f"Total books found: {report['total_books']}")
    print(f"Total PDFs found: {report['total_pdfs']}")
    print(f"\nMetadata saved to: {output_path}")
    print(f"Summary report saved to: {report_path}")
    
    if report['books_by_class']:
        print("\nBooks by Class:")
        for class_name, count in sorted(report['books_by_class'].items()):
            print(f"  {class_name}: {count} books")
    
    if report['books_by_subject']:
        print("\nBooks by Subject:")
        for subject, count in sorted(report['books_by_subject'].items()):
            print(f"  {subject}: {count} books")


def cmd_download(args):
    """Download PDFs from scraped metadata"""
    metadata_path = METADATA_DIR / args.metadata
    
    if not metadata_path.exists():
        print(f"Error: Metadata file not found: {metadata_path}")
        print("Please run 'scrape' command first.")
        return
    
    print(f"Loading metadata from: {metadata_path}")
    books_data = load_json(metadata_path)
    
    scraper = TSScertScraper()
    
    # Filter by class if specified
    if args.class_number:
        books_data = [
            book for book in books_data
            if f"class {args.class_number}" in book['title'].lower()
        ]
        print(f"Filtered to Class {args.class_number} books")
    
    # Count total PDFs
    total_pdfs = sum(len(book.get('pdfs', [])) for book in books_data)
    
    if total_pdfs == 0:
        print("No PDFs found to download.")
        return
    
    print(f"Found {total_pdfs} PDFs to download")
    print(f"Download directory: {args.output}")
    print("-" * 60)
    
    # Download PDFs
    scraper.books_data = books_data
    scraper.download_all_pdfs(args.output)
    
    print("\n" + "=" * 60)
    print("DOWNLOAD COMPLETE")
    print("=" * 60)


def cmd_list(args):
    """List scraped books"""
    metadata_path = METADATA_DIR / args.metadata
    
    if not metadata_path.exists():
        print(f"Error: Metadata file not found: {metadata_path}")
        print("Please run 'scrape' command first.")
        return
    
    books_data = load_json(metadata_path)
    
    # Filter by class if specified
    if args.class_number:
        books_data = [
            book for book in books_data
            if f"class {args.class_number}" in book['title'].lower()
        ]
    
    print(f"\nFound {len(books_data)} books\n")
    print("=" * 80)
    
    for idx, book in enumerate(books_data, 1):
        print(f"{idx}. {book['title']}")
        print(f"   URL: {book['url']}")
        print(f"   PDFs: {len(book.get('pdfs', []))}")
        
        if args.verbose:
            metadata = book.get('metadata', {})
            if metadata.get('class'):
                print(f"   Class: {metadata['class']}")
            if metadata.get('subject'):
                print(f"   Subject: {metadata['subject']}")
            if book.get('pdfs'):
                print(f"   PDF Links:")
                for pdf in book['pdfs'][:3]:  # Show first 3 PDFs
                    print(f"     - {pdf.get('title', 'Untitled')}")
                if len(book['pdfs']) > 3:
                    print(f"     ... and {len(book['pdfs']) - 3} more")
        
        print("-" * 80)


def cmd_info(args):
    """Show information about scraped data"""
    metadata_path = METADATA_DIR / args.metadata
    
    if not metadata_path.exists():
        print(f"Error: Metadata file not found: {metadata_path}")
        return
    
    books_data = load_json(metadata_path)
    report = create_summary_report(books_data)
    
    print("\n" + "=" * 60)
    print("SCRAPER DATA INFORMATION")
    print("=" * 60)
    print(f"Total books: {report['total_books']}")
    print(f"Total PDFs: {report['total_pdfs']}")
    print(f"Scraped at: {books_data[0].get('scraped_at', 'Unknown') if books_data else 'Unknown'}")
    
    print("\nBooks by Class:")
    if report['books_by_class']:
        for class_name, count in sorted(report['books_by_class'].items()):
            print(f"  {class_name}: {count} books")
    else:
        print("  No class information available")
    
    print("\nBooks by Subject:")
    if report['books_by_subject']:
        for subject, count in sorted(report['books_by_subject'].items()):
            print(f"  {subject}: {count} books")
    else:
        print("  No subject information available")
    
    # Check for downloaded files
    if DOWNLOADS_DIR.exists():
        downloaded_files = list(DOWNLOADS_DIR.rglob("*.pdf"))
        total_size = sum(f.stat().st_size for f in downloaded_files)
        print(f"\nDownloaded Files:")
        print(f"  PDF files: {len(downloaded_files)}")
        print(f"  Total size: {format_size(total_size)}")


def main():
    parser = argparse.ArgumentParser(
        description='TS SCERT Books Scraper - Extract educational books and PDFs',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scrape all books
  python cli.py scrape
  
  # Scrape from custom URL
  python cli.py scrape --url https://example.com/books/
  
  # Download all PDFs
  python cli.py download
  
  # Download only Class 5 PDFs
  python cli.py download --class 5
  
  # List all scraped books
  python cli.py list
  
  # List Class 8 books with details
  python cli.py list --class 8 --verbose
  
  # Show scraper statistics
  python cli.py info
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Scrape command
    scrape_parser = subparsers.add_parser('scrape', help='Scrape books from website')
    scrape_parser.add_argument('--url', default=BASE_URL, help='Base URL to scrape')
    scrape_parser.add_argument('--output', default=METADATA_FILENAME, 
                              help='Output filename for metadata')
    
    # Download command
    download_parser = subparsers.add_parser('download', help='Download PDFs from scraped data')
    download_parser.add_argument('--metadata', default=METADATA_FILENAME,
                                help='Metadata file to read')
    download_parser.add_argument('--output', default=str(DOWNLOADS_DIR),
                                help='Download directory')
    download_parser.add_argument('--class', dest='class_number', type=int,
                                help='Download only specific class (e.g., 5 for Class 5)')
    
    # List command
    list_parser = subparsers.add_parser('list', help='List scraped books')
    list_parser.add_argument('--metadata', default=METADATA_FILENAME,
                            help='Metadata file to read')
    list_parser.add_argument('--class', dest='class_number', type=int,
                            help='Filter by class number')
    list_parser.add_argument('--verbose', '-v', action='store_true',
                            help='Show detailed information')
    
    # Info command
    info_parser = subparsers.add_parser('info', help='Show scraper statistics')
    info_parser.add_argument('--metadata', default=METADATA_FILENAME,
                            help='Metadata file to read')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # Execute command
    try:
        if args.command == 'scrape':
            cmd_scrape(args)
        elif args.command == 'download':
            cmd_download(args)
        elif args.command == 'list':
            cmd_list(args)
        elif args.command == 'info':
            cmd_info(args)
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
