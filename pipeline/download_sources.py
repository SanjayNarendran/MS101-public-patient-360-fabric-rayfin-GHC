"""Download the reference files the pipeline reads, from their original publishers, into work/sources/.

  TCGA_Reports.csv          TCGA-Reports (Kefeli et al. 2024), OCR text of the TCGA pathology reports.
                            Mendeley Data, doi:10.17632/hyg5xkznpx.1, CC BY 4.0
  panimmune/*               The Immune Landscape of Cancer (Thorsson et al. 2018), the study's data files as
                            published by the NCI Genomic Data Commons (open access):
                            https://gdc.cancer.gov/about-data/publications/panimmune
  synthea-with-dependencies.jar
                            Synthea (Walonoski et al. 2018), Apache-2.0, from its GitHub releases; it generates the
                            synthetic primary-care history (work/synthea/)

Everything else (clinical records, slides, report PDFs, genomics, CT and MR) is fetched by the other pipeline scripts
from the GDC, cBioPortal and NCI Imaging Data Commons APIs. Files already present are skipped.

Usage: python pipeline/download_sources.py
"""
from __future__ import annotations

import hashlib
import pathlib
import shutil
import time
import zipfile

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = ROOT / "work" / "sources"
SYNTHEA = ROOT / "work" / "synthea"

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(max_retries=Retry(
    total=4, connect=0, read=4, backoff_factor=2, allowed_methods=None, status_forcelist=[429, 500, 502, 503, 504],
)))
# Connect and read timeouts: fail fast when a host is unreachable, but allow slow transfers.
TIMEOUT = (6, 120)
# Landing pages for a manual download if a direct link fails.
LANDING = {
    "TCGA_Reports.csv.zip": "https://doi.org/10.17632/hyg5xkznpx.1",
    "synthea-with-dependencies.jar": "https://github.com/synthetichealth/synthea/releases",
}

TCGA_REPORTS = (
    "https://data.mendeley.com/public-files/datasets/hyg5xkznpx/files/60abe141-9352-4a54-943c-3d015eabefea/file_downloaded",
    "bbbc0ee0c75d8feb0bb246b4fa40e8e29bba9775d4a9e4f41e0b73a7955e1c12",
)

# GDC file ids from the PanImmune publication page.
PANIMMUNE = {
    "TCGA_all_leuk_estimate.masked.20170107.tsv": "6f75c9d7-5134-4ed1-b8f3-72856c98a4e8",
    "TCGA.Kallisto.fullIDs.cibersort.relative.tsv": "b3df502e-3594-46ef-9f94-d041a20a0b9a",
    "Scores_160_Signatures.tsv.gz": "80a82092-161d-4615-9d96-e858f113618d",
    "mitcr_sampleStatistics_20160714.tsv": "da5724d2-2ae7-4632-8a9d-4b08496a9d20",
    "TCGA.HRD_withSampleID.txt": "66dd07d7-6366-4774-83c3-5ad1e22b177e",
}

# The Synthea build this demo was made with; a newer build still works but gives different synthetic histories.
SYNTHEA_JAR = (
    "https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar",
    "20098ec70c24c715ea874b19140983634711cc4986196105b5b0a9ca72a4b644",
)


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: pathlib.Path, expected: str | None = None) -> None:
    if dest.exists():
        print(f"have {dest.relative_to(ROOT)}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"get  {dest.relative_to(ROOT)} <- {url}", flush=True)
    for attempt in range(2):
        try:
            with HTTP.get(url, stream=True, timeout=TIMEOUT) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    shutil.copyfileobj(r.raw, f, 1 << 20)
            break
        except requests.ConnectionError as e:
            if attempt == 1:
                page = LANDING.get(dest.name, "https://gdc.cancer.gov/about-data/publications/panimmune")
                raise SystemExit(f"Could not download {dest.name} ({e.__class__.__name__}). Download it from {page} "
                                 f"and save it as {dest.relative_to(ROOT)}, then run this again.") from None
            time.sleep(5 * (attempt + 1))
    if expected and sha256(tmp) != expected:
        print(f"  warning: {dest.name} differs from the file this demo was built with (SHA-256 mismatch)")
    tmp.rename(dest)


def main() -> None:
    reports_zip = SOURCES / "TCGA_Reports.csv.zip"
    fetch(TCGA_REPORTS[0], reports_zip, TCGA_REPORTS[1])
    if not (SOURCES / "TCGA_Reports.csv").exists():
        with zipfile.ZipFile(reports_zip) as z:
            z.extract("TCGA_Reports.csv", SOURCES)

    for name, file_id in PANIMMUNE.items():
        fetch(f"https://api.gdc.cancer.gov/data/{file_id}", SOURCES / "panimmune" / name)

    fetch(SYNTHEA_JAR[0], SYNTHEA / "synthea-with-dependencies.jar", SYNTHEA_JAR[1])
    print("sources ready")


if __name__ == "__main__":
    main()
