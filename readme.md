# RA Assignment — Friday Sermon Corpus (Jordan)

This script collects one full year of **standardized Friday sermons (khutbah) PDFs** from the official Jordanian authority, and produces a clean, reproducible corpus with metadata embedded inside each PDF. :)

---

## Outputs (Folder Structure)

After running, you will get an output directory like `Humoyunsversion` containing:

* `original_pdfs/`
  Raw PDFs downloaded from the source website (unchanged).

* `pdfs/`
  Submission-ready PDFs named as:
  `YYYY-MM-DD - <Title>.pdf`
  Each file contains a **generated metadata cover page** + the original sermon pages.

* `manifest.csv`
  A machine-readable table including sermon date, extracted title, source URL, local filename, sha256 hash, and retrieval timestamp.

* `docs/documentation.txt`
  Short 1-page writeup of country choice, challenges, and solutions.

* `code/scrape_jordan_khutbah.py`
  All codes written inside there.

---

## Requirements

* **Python:** 3.10+ (recommended)
* Packages are listed in `requirements.txt`

> Note: For Arabic text to render correctly on the metadata cover page (no black squares), the script uses:
>
> * `arabic-reshaper`
> * `python-bidi`
> * an Arabic TTF font file (e.g., Amiri)

---

## Setup

### 1) Create and activate a virtual environment

```bash
python -m venv researchenv
source researchenv/bin/activate   # Windows: researchenv\Scripts\activate
```

### 2) Install Dependencies

```bash
pip install -r requirements.txt
```

### 3) Ensure Arabic font file exists here

```text
fonts/Amiri-Regular.ttf
```

### 4) Finally, run

```bash
python scrape_jordan_khutbah.py --year 2025 --out Humoyunsversion
```
