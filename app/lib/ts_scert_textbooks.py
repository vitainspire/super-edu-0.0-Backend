"""Download textbook PDFs from the SCERT Telangana e-Textbooks page
(Publications > Our Books > eTextBooks) for a given class, medium and subject.

Ported from a standalone script the admin already hand-tested for classes 1-5.
Two quirks of this specific government site drove the design here:

1. The eTextbooks page (DisplayContent.aspx?encry=...) silently serves a
   short, unrelated "generic content" fallback (FLN/pre-primary repository
   links) to any request that doesn't carry the session cookie the site sets
   on its homepage. A plain GET on the page URL alone reproduces this -- you
   have to GET the homepage first with a requests.Session() so the cookie is
   attached, then GET the eTextbooks page with that same session. If that
   cookie dance ever stops working (site changes), fetch_etextbooks_html
   notices ("customtable" missing from the HTML) and raises instead of
   silently returning the wrong content.

2. The per-class tables are hand-maintained HTML with real bugs (a duplicated
   "Second Language" header on some classes, orphan rows with an empty Medium
   cell inserted between real rows to hold "Part1/Part2" variant links,
   inconsistent rowspans). Rather than hardcode "column 5 is Maths", each
   table is reconstructed into a full grid using the standard HTML
   rowspan/colspan algorithm (the same one browsers use), and then every
   non-empty link cell in whichever row's Medium column matches is read off.
   The anchor's own text (e.g. "5EM_MAT") -- not the column header -- is what
   is trustworthy here, so it is kept in the filename for traceability even
   though it is not otherwise used for matching.

This module is a pure library: no FastAPI, no threading. app/lib/textbook_scrape_jobs.py
wraps it the same way app/lib/syllabus_pdf_jobs.py wraps vision_extraction.py.
"""

import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

BASE_URL = "https://scert.telangana.gov.in/"
ETEXTBOOKS_URL = BASE_URL + "DisplayContent.aspx?encry=ammkNW4/gx+NeApstGPX+A=="
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Only TS has a working scraper. AP/CBSE/ICSE are listed so the frontend can
# show them as disabled "coming soon" options without a second source of truth.
SUPPORTED_BOARDS = {"TS"}
ALL_BOARDS = [
    {"code": "TS", "label": "Telangana (TS)", "enabled": True},
    {"code": "AP", "label": "Andhra Pradesh (AP)", "enabled": False},
    {"code": "CBSE", "label": "CBSE", "enabled": False},
    {"code": "ICSE", "label": "ICSE", "enabled": False},
]

DRIVE_ID_RE = re.compile(r"/d/([a-zA-Z0-9_-]+)")


class ScrapeError(Exception):
    """Base for failures that should surface as a clean job/route error, not a 500."""


class SiteShapeChanged(ScrapeError):
    """The eTextbooks page didn't return the real class tables."""


class NoClassTable(ScrapeError):
    """No customtable on the page maps to the requested class."""


class NoMediumRow(ScrapeError):
    """The class table has no row whose Medium column matches."""


class NoSubjectMatch(ScrapeError):
    """The medium row has links, but none under a header matching the subject."""


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.get(BASE_URL, timeout=30)
    return session


def fetch_etextbooks_html(session: requests.Session) -> str:
    resp = session.get(ETEXTBOOKS_URL, headers={"Referer": BASE_URL}, timeout=30)
    resp.raise_for_status()
    if "customtable" not in resp.text:
        raise SiteShapeChanged(
            "The SCERT eTextbooks page didn't return the real class tables -- got "
            "the generic-content fallback instead. The site's session-cookie "
            "behavior may have changed."
        )
    return resp.text


def table_to_grid(table: Tag) -> tuple[list[str], list[list]]:
    """Reconstruct an HTML table's rowspan/colspan into a full grid, so each
    row has exactly len(headers) cells regardless of how many <td>s it
    actually declares."""
    headers = [th.get_text(strip=True) for th in table.find("thead").find_all("th")]
    seen: dict[str, int] = {}
    deduped_headers = []
    for h in headers:
        seen[h] = seen.get(h, 0) + 1
        deduped_headers.append(h if seen[h] == 1 else f"{h} ({seen[h]})")

    n_cols = len(deduped_headers)
    carryover: list[Optional[tuple[int, Tag]]] = [None] * n_cols

    grid: list[list] = []
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        cell_iter = iter(cells)
        row = []
        for col in range(n_cols):
            if carryover[col] is not None:
                remaining, cell = carryover[col]
                row.append(cell)
                carryover[col] = (remaining - 1, cell) if remaining > 1 else None
                continue
            cell = next(cell_iter, None)
            rowspan = int(cell.get("rowspan", 1)) if cell is not None else 1
            row.append(cell)
            if rowspan > 1:
                carryover[col] = (rowspan - 1, cell)
        grid.append(row)
    return deduped_headers, grid


def class_number(first_cell: Optional[Tag]) -> Optional[int]:
    if first_cell is None:
        return None
    match = re.search(r"\d+", first_cell.get_text())
    return int(match.group()) if match else None


def find_medium_row(headers: list[str], grid: list[list], medium: str) -> Optional[list]:
    medium_col = headers.index("Medium") if "Medium" in headers else 1
    for row in grid:
        cell = row[medium_col]
        if cell is not None and cell.get_text(strip=True).lower() == medium.lower():
            return row
    return None


def collect_links(headers: list[str], row: list, skip_cols: set[str]) -> list[tuple[str, str, str]]:
    """Returns (subject_column, href, anchor_text) for every non-empty link
    cell in the row, excluding the Class/Medium columns."""
    out = []
    for header, cell in zip(headers, row):
        if header in skip_cols or cell is None:
            continue
        for a in cell.find_all("a"):
            href = a.get("href", "").strip()
            if href:
                out.append((header, href, a.get_text(strip=True)))
    return out


def download_drive_file(session: requests.Session, file_id: str, dest: Path, max_attempts: int = 3) -> None:
    import gdown

    for attempt in range(1, max_attempts + 1):
        try:
            gdown.download(id=file_id, output=str(dest), quiet=True)
            return
        except Exception:  # noqa: BLE001 -- retried below
            if attempt < max_attempts:
                time.sleep(5)
            else:
                raise RuntimeError(f"failed to download Drive file {file_id} after {max_attempts} attempts")


def download_direct_file(session: requests.Session, href: str, dest: Path, max_attempts: int = 3) -> None:
    url = urljoin(ETEXTBOOKS_URL, href.replace("\\", "/"))
    url = quote(url, safe=":/?&=%")

    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, headers={"Referer": ETEXTBOOKS_URL}, stream=True, timeout=60)
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
            return
        except Exception as exc:  # noqa: BLE001 -- retried below
            last_exc = exc
            if attempt < max_attempts:
                time.sleep(5)
    raise last_exc


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "-", name).strip()


def _class_table(soup: BeautifulSoup, klass: int) -> Tag:
    for table in soup.find_all("table", class_="customtable"):
        headers, grid = table_to_grid(table)
        if not grid:
            continue
        if class_number(grid[0][0]) == klass:
            return table
    raise NoClassTable(f"No table on the eTextbooks page matches Class {klass}.")


def list_subjects(klass: int, medium: str) -> list[dict]:
    """No download -- opens the page, finds the class table, finds the medium
    row, and returns every non-empty link cell's header/label. Powers the
    admin's Subject dropdown so it reflects what is actually on the page
    (headers vary by class: "Second Language", "Second Language (2)", some
    classes have no Science) instead of guessing free text."""
    session = get_session()
    html = fetch_etextbooks_html(session)
    soup = BeautifulSoup(html, "html.parser")

    table = _class_table(soup, klass)
    headers, grid = table_to_grid(table)
    row = find_medium_row(headers, grid, medium)
    if row is None:
        raise NoMediumRow(f"Class {klass} has no '{medium}' medium row on the eTextbooks page.")

    links = collect_links(headers, row, skip_cols={"Class", "Medium"})
    seen: dict[str, str] = {}
    for header, _href, label in links:
        seen.setdefault(header, label)
    return [{"header": header, "sampleLabel": label} for header, label in seen.items()]


def download_textbooks(
    klass: int,
    medium: str,
    subject_query: str,
    dest_dir: Path,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> list[dict]:
    """The per-request entry point. A fresh requests.Session() is created
    inside this call -- never a module-level singleton, since concurrent jobs
    (different admins, or a retry) must not share cookies/connection state.

    Filters the medium row's links to headers matching subject_query
    case-insensitively (substring match, so "second language" matches both
    "Second Language" and "Second Language (2)"). Downloads each match into
    dest_dir, skipping a file that already exists there -- resumability
    scoped to this job's own directory, so two jobs never skip each other's
    files.
    """
    session = get_session()
    if progress_cb:
        progress_cb(0, 1, "Reading the SCERT class table…")
    html = fetch_etextbooks_html(session)
    soup = BeautifulSoup(html, "html.parser")

    table = _class_table(soup, klass)
    headers, grid = table_to_grid(table)
    row = find_medium_row(headers, grid, medium)
    if row is None:
        raise NoMediumRow(f"Class {klass} has no '{medium}' medium row on the eTextbooks page.")

    links = collect_links(headers, row, skip_cols={"Class", "Medium"})
    query = subject_query.strip().lower()
    matches = [(h, href, label) for h, href, label in links if query in h.lower()]
    if not matches:
        available = sorted({h for h, _href, _label in links})
        raise NoSubjectMatch(
            f"No subject on this page matches '{subject_query}' for Class {klass} ({medium}). "
            f"Available: {', '.join(available) if available else 'none'}."
        )

    results: list[dict] = []
    total = len(matches)
    for done, (subject, href, label) in enumerate(matches):
        if progress_cb:
            progress_cb(done, total, f"Downloading {subject} ({label})…")

        fname = safe_filename(f"Class {klass} - {subject} - {label}.pdf")
        dest = dest_dir / fname
        if not dest.exists():
            drive_match = DRIVE_ID_RE.search(href)
            if drive_match:
                download_drive_file(session, drive_match.group(1), dest)
            else:
                download_direct_file(session, href, dest)

        results.append({
            "subject": subject,
            "label": label,
            "filename": fname,
            "path": str(dest),
            "sizeBytes": dest.stat().st_size if dest.exists() else 0,
        })

    if progress_cb:
        progress_cb(total, total, f"{total} file(s) downloaded")
    return results
