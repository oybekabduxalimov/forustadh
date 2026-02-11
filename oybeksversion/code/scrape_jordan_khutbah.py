from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas as rl_canvas

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAVE_ARABIC_TOOLS = True
except ImportError:
    HAVE_ARABIC_TOOLS = False

import sys

if sys.version_info < (3, 10):
    raise RuntimeError("Ustadh Humoyun, please use Python +3.10!")


ARABIC_RE = re.compile(r"[\u0600-\u06FF]")

def contains_arabic(s: str) -> bool:
    return bool(ARABIC_RE.search(s or ""))

def shape_rtl_arabic(s: str) -> str:
    # Arabic needs shaping + bidi reordering for correct display in PDFs
    # If tools aren't available, return original (but we will avoid rendering Arabic then)
    if not HAVE_ARABIC_TOOLS:
        return s
    reshaped = arabic_reshaper.reshape(s)
    return get_display(reshaped)

def wrap_text(s: str, width: int) -> list[str]:
    # Simple wrapper preserving your old wrapping logic
    return textwrap.wrap(s, width=width) if s else [""]

LIST_URL = (
    "https://awqaf.gov.jo/AR/Pages/%D8%AE%D8%B7%D8%A8_%D8%A7%D9%84%D8%AC%D9%85%D8%B9%D8%A9"
    "?View=5333"
)


COUNTRY = "Jordan"
AUTHORITY_EN = "Ministry of Awqaf and Islamic Affairs and Holy Places (Jordan)"
AUTHORITY_AR = "وزارة الأوقاف والشؤون والمقدسات الإسلامية - الأردن"

DATE_RE = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})\.pdf$", re.IGNORECASE)


@dataclass
class SermonLink:
    date_iso: str            # YYYY-MM-DD
    source_url: str
    inferred_title: str      # extracted from PDF (preferred) or fallback


def _requests_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; RA-assignment-scraper/1.0; +https://example.com)",
        "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar,en;q=0.8",
    })
    return s


def parse_listing_for_year(html: str, year: int) -> List[Tuple[str, str]]:
    """
    Returns list of (date_iso, pdf_url) from the listing page for a given year.
    Strategy: collect all hrefs ending in .pdf and parse Gregorian date from filename suffix _d-m-yyyy.pdf.
    """
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().endswith(".pdf"):
            continue
        full = urljoin(LIST_URL, href)
        m = DATE_RE.search(full)
        if not m:
            continue
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y != year:
            continue
        date_iso = f"{y:04d}-{mth:02d}-{d:02d}"
        links.append((date_iso, full))

    # Deduplicate by date (keep first occurrence)
    dedup = {}
    for date_iso, url in links:
        dedup.setdefault(date_iso, url)

    out = sorted(dedup.items(), key=lambda x: x[0])
    return out


def download_file(session: requests.Session, url: str, path: Path, sleep_seconds: float = 0.5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 64):
                if chunk:
                    f.write(chunk)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_title_from_pdf(pdf_path: Path) -> Optional[str]:
    """
    Best-effort title extraction from page 1.
    Many Jordan PDFs include a line like:
      "عنوان خطبة الجمعة الموحد )TITLE("
    Note RTL parentheses can appear reversed.
    """
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                return None
            text = pdf.pages[0].extract_text() or ""
    except Exception:
        return None

    text = " ".join(text.split())
    if not text:
        return None

    # Try pattern: ) TITLE (
    candidates = re.findall(r"\)\s*([^()]{3,200}?)\s*\(", text)
    if candidates:
        # Choose the first candidate that contains Arabic letters or looks like a real title
        for c in candidates:
            c2 = c.strip()
            if re.search(r"[\u0600-\u06FF]", c2) or len(c2) >= 8:
                return c2

    # Fallback: look for word "عنوان" and take subsequent words
    idx = text.find("عنوان")
    if idx != -1:
        tail = text[idx: idx + 300]
        # remove common header words
        tail = re.sub(r"عنوان\s+خطبة\s+الجمعة\s+الموحد", "", tail).strip()
        tail = tail.strip(":-–— ")
        if len(tail) >= 8:
            return tail[:120]

    return None


def sanitize_filename_component(s: str, max_len: int = 90) -> str:
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    # Remove characters that break Windows/macOS filesystems
    s = re.sub(r'[\\/:"*?<>|]+', "", s)
    s = s.strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s or "untitled"


def make_cover_pdf(
    cover_path: Path,
    *,
    date_iso: str,
    title: str,
    source_url: str,
    retrieved_at: str,
) -> None:
    """
    Create a 1-page cover PDF with required metadata inside the PDF.
    Preserves the original layout, but renders Arabic correctly using an embedded TTF font.
    """
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    c = rl_canvas.Canvas(str(cover_path), pagesize=LETTER)
    width, height = LETTER

    left = 54
    y = height - 72
    line_h = 14
    value_x = left + 140
    value_right_x = width - left

    # Register Arabic font if present
    arabic_font_name = None
    font_path = Path(__file__).resolve().parent / "fonts" / "Amiri-Regular.ttf"
    if font_path.exists() and HAVE_ARABIC_TOOLS:
        arabic_font_name = "Amiri"
        pdfmetrics.registerFont(TTFont(arabic_font_name, str(font_path)))

    c.setFont("Helvetica-Bold", 14)
    c.drawString(left, y, "Friday Sermon (Khutbah) | For Ustadh Humoyun | By Oybek Abdukhalimov")
    y -= 2 * line_h

    c.setFont("Helvetica", 11)

    issuing_value = f"{AUTHORITY_EN} / {AUTHORITY_AR}"
    fields = [
        ("Country", COUNTRY),
        ("Issuing authority", issuing_value),
        ("Sermon date (Gregorian)", date_iso),
        ("Sermon title (extracted)", title),
        ("Source URL", source_url),
        ("Retrieved at", retrieved_at),
    ]

    for k, v in fields:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(left, y, f"{k}:")
        y -= 0  # keep your spacing behavior

        # Special-case issuing authority to avoid mixed LTR/RTL on one line
        if k == "Issuing authority" and " / " in v:
            en_part, ar_part = v.split(" / ", 1)

            # English line (same as before)
            c.setFont("Helvetica", 11)
            for line in wrap_text(en_part, width=95):
                c.drawString(value_x, y, line)
                y -= line_h
                if y < 72:
                    c.showPage()
                    c.setFont("Helvetica", 11)
                    y = height - 72

            # Arabic line (only if we can render it; otherwise omit to avoid black squares)
            if arabic_font_name and contains_arabic(ar_part):
                c.setFont(arabic_font_name, 12)
                for line in wrap_text(ar_part, width=70):
                    c.drawRightString(value_right_x, y, shape_rtl_arabic(line))
                    y -= line_h
                    if y < 72:
                        c.showPage()
                        c.setFont(arabic_font_name, 12)
                        y = height - 72

            y -= 6
            continue

        # General rendering: Arabic values use Arabic font + RTL shaping (no squares)
        if arabic_font_name and contains_arabic(v):
            c.setFont(arabic_font_name, 12)
            for line in wrap_text(v, width=70):
                c.drawRightString(value_right_x, y, shape_rtl_arabic(line))
                y -= line_h
                if y < 72:
                    c.showPage()
                    c.setFont(arabic_font_name, 12)
                    y = height - 72
        else:
            # If Arabic font isn't available, avoid printing Arabic characters (prevents squares)
            if not arabic_font_name and contains_arabic(v):
                v = "[Arabic text omitted: font not available]"

            c.setFont("Helvetica", 11)
            for line in wrap_text(v, width=95):
                c.drawString(value_x, y, line)
                y -= line_h
                if y < 72:
                    c.showPage()
                    c.setFont("Helvetica", 11)
                    y = height - 72

        y -= 6

    c.setFont("Helvetica-Oblique", 10)
    note = (
        "Note: I always prioritize beauty and conciseness. I didn't want to put long ugly label with all information. Thus, I created additional meta-page that provides all details before you dive into the khutbah! Hope you like it ;)"
    )
    for line in wrap_text(note, width=110):
        c.drawString(left, y, line)
        y -= line_h

    c.showPage()
    c.save()


def merge_cover_with_original(cover_pdf: Path, original_pdf: Path, out_pdf: Path) -> int:
    reader_cover = PdfReader(str(cover_pdf))
    reader_orig = PdfReader(str(original_pdf))
    writer = PdfWriter()

    for p in reader_cover.pages:
        writer.add_page(p)
    for p in reader_orig.pages:
        writer.add_page(p)

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with open(out_pdf, "wb") as f:
        writer.write(f)

    return len(reader_cover.pages) + len(reader_orig.pages)


def write_manifest_csv(rows: List[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def build_zip(out_dir: Path, zip_path: Path) -> None:
    import zipfile
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in out_dir.rglob("*"):
            if p.is_file():
                z.write(p, arcname=p.relative_to(out_dir))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025, help="Gregorian year (e.g., 2025)")
    ap.add_argument("--out", type=str, default="out_jordan", help="Output directory")
    ap.add_argument("--sleep", type=float, default=0.5, help="Delay between downloads (seconds)")
    ap.add_argument("--zip-name", type=str, default="", help="Optional ZIP filename (default auto)")
    args = ap.parse_args()

    year = args.year
    out_dir = Path(args.out).resolve()
    originals_dir = out_dir / "original_pdfs"
    final_dir = out_dir / "pdfs"
    docs_dir = out_dir / "docs"

    session = _requests_session()
    resp = session.get(LIST_URL, timeout=60)
    resp.raise_for_status()

    pairs = parse_listing_for_year(resp.text, year)
    if len(pairs) < 50:
        print(f"[WARN] Found {len(pairs)} PDFs for {year}. Check the listing page or year.", file=sys.stderr)

    manifest_rows = []
    retrieved_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    for date_iso, pdf_url in pairs:
        # Download original
        tmp_pdf = originals_dir / f"{date_iso}.pdf"
        download_file(session, pdf_url, tmp_pdf, sleep_seconds=args.sleep)

        # Extract title
        title = extract_title_from_pdf(tmp_pdf)
        if not title:
            # fallback from URL filename (remove date suffix)
            base = Path(pdf_url).name
            base = re.sub(DATE_RE, "", base)
            base = base.replace("_", " ")
            title = base.strip() or "untitled"
        title_clean = sanitize_filename_component(title)

        # Cover + merge
        cover_pdf = out_dir / "tmp" / f"cover_{date_iso}.pdf"
        make_cover_pdf(
            cover_pdf,
            date_iso=date_iso,
            title=title,
            source_url=pdf_url,
            retrieved_at=retrieved_at,
        )

        out_pdf = final_dir / f"{date_iso} - {title_clean}.pdf"
        total_pages = merge_cover_with_original(cover_pdf, tmp_pdf, out_pdf)

        # Manifest row
        manifest_rows.append({
            "country": COUNTRY,
            "issuing_authority_en": AUTHORITY_EN,
            "issuing_authority_ar": AUTHORITY_AR,
            "date_gregorian": date_iso,
            "title_extracted": title,
            "source_url": pdf_url,
            "local_filename": str(out_pdf.relative_to(out_dir)).replace("\\", "/"),
            "sha256": sha256_file(out_pdf),
            "pages_total": total_pages,
            "retrieved_at_utc": retrieved_at,
        })

        print(f"[OK] {date_iso} | {title_clean}")

    write_manifest_csv(manifest_rows, out_dir / "manifest.csv")

    # Write brief 1-page documentation stub
    doc_text = f"""RA Assignment — Friday Sermon Corpus (Jordan, {year})

Country chosen: Jordan
Issuing authority: {AUTHORITY_EN} / {AUTHORITY_AR}

Why Jordan (and how I got here):
Before committing to one country, I did a quick screening of several Muslim-majority countries that *might* meet the project constraints (standardized nationwide sermon + official online publication + at least a full year available). My goal was to find a source that is:
(1) official, (2) stable over time, (3) easy to scrape reproducibly, and (4) already “PDF-native” (so I don’t need to convert HTML to PDF (I am not lazy though)).

A few examples from the screening phase (and what was missing):
- Malaysia (JAKIM e-Khutbah): sermons are often published as web pages (HTML) rather than weekly PDFs. This is great for reading, but it adds extra steps (HTML parsing and/or HTML→PDF conversion) and increases formatting variability.
- Morocco (Habous): sermons are accessible online, but commonly as page content rather than a clean “one sermon = one PDF file” archive; it’s doable, but less aligned with the deliverable requirement of a PDF per sermon.
- UAE (Awqaf): an official archive exists and PDFs are available, but the workflow tends to be more dynamic (year selectors / detail pages / non-date filenames), meaning the scraper must traverse more layers to reliably recover date + title for every entry.
- Oman (MRA): PDFs/Word versions are available weekly, but calendar formats and labeling can vary (e.g., Hijri-first presentation). That’s solvable, but requires extra conversion/verification steps to ensure correct Gregorian YYYY-MM-DD labeling.

Why Jordan won in the end :)
- Jordan’s archive provides direct PDF links on an official government domain.
- PDF filenames include an explicit Gregorian date pattern (d-m-yyyy.pdf), which makes it unusually scrape-friendly and reduces risk of mislabeling.
- The dataset is naturally “weekly,” and the archive supports collecting a full year with minimal special-case logic.
- In short: it’s the cleanest path to a reliable, reproducible 52+ sermon corpus with strong traceability ;) 

Source page:
- {LIST_URL}

What was collected:
- All sermon PDFs on the source page whose filenames end with *-*-{year}.pdf
- Each output file is a PDF named: YYYY-MM-DD - <Title>.pdf
- A metadata cover page is prepended to each PDF containing: source URL, country, authority, date, title, retrieval time.
- A manifest.csv is generated (including sha256 hashes) for verification and reproducibility.

Challenges & solutions (what broke, and how I fixed it):
1) Titles are not consistently listed on the archive page.
   - Solution: extract the title programmatically from page 1 of each PDF (fallback to filename if needed).

2) Duplicate links or repeated entries can appear on listing-style pages.
   - Solution: deduplicate by sermon date and sort chronologically to ensure a clean weekly sequence.

3) Arabic text on a generated cover page can render as “black squares” (font/RTL issue).
   - Solution: register an Arabic TTF font (e.g., Amiri) and apply Arabic reshaping + bidi display so Arabic titles/authority render correctly.

4) Filenames can break on different operating systems (slashes, colons, very long titles).
   - Solution: sanitize titles to remove forbidden characters and trim length while keeping the filename meaningful.

5) Reproducibility + respectful scraping behavior :)
   - Solution: use a single requests session with clear headers and an optional delay; keep everything parameterized (year/output folder); log outputs via manifest + hashes.

How to reproduce:
- Python 3.10+ (tested locally)
- Install requirements: pip install -r requirements.txt
- Run: python scrape_jordan_khutbah.py --year {year} --out Humoyunversion

Outputs:
- original_pdfs/: raw downloaded PDFs (unchanged)
- pdfs/: final submission-ready PDFs (metadata cover page + original pages)
- manifest.csv: metadata + sha256 hashes
- docs/documentation.txt: this file
"""
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "documentation.txt").write_text(doc_text, encoding="utf-8")

    # Copy code + requirements into output (so ZIP contains everything)
    # (When you run locally, ensure this script and requirements.txt are in the same folder.)
    try:
        this_file = Path(__file__).resolve()
        (out_dir / "code").mkdir(parents=True, exist_ok=True)
        (out_dir / "code" / "scrape_jordan_khutbah.py").write_text(this_file.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())