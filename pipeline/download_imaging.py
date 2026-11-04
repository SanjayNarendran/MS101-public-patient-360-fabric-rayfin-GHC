"""Download the raw imaging for the cohort.

  - the first diagnostic H&E slide (SVS) per patient from the NCI Genomic Data Commons
  - with --tissue-slides, the frozen-section slides as well (top and bottom of the sequenced portion, normal tissue)
  - the chosen CT/MR series (DICOM) from NCI Imaging Data Commons, plus the patient's
    full radiology study list (metadata only) for the timeline
  - Stony Brook TIL maps (DICOM SEG) from IDC when the diagnostic slide has one
  - AI segmentations (DICOM SEG) of the chosen CT/MR series from IDC
IDC series are limited to licences that allow commercial use (CC BY, not NC or ND).

Output: work/raw/slides/*.svs, work/raw/dicom/<patient>/<series-uid>/*.dcm,
        work/raw/til/<patient>/*.dcm, work/imaging_index.json

Usage: python pipeline/download_imaging.py [--skip-slides] [--skip-dicom]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import requests
from idc_index import IDCClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "work" / "raw"
GDC = "https://api.gdc.cancer.gov"
# Only IDC series whose licence allows reuse, including commercial use (CC BY 3.0 and 4.0 at the time of writing):
# no non-commercial (NC) or no-derivatives (ND) data.
OPEN_LICENCE = "license_short_name LIKE 'CC BY%' AND license_short_name NOT LIKE '%NC%' AND license_short_name NOT LIKE '%ND%'"


def cohort() -> list[dict]:
    return json.loads((ROOT / "pipeline" / "cohort.json").read_text())["patients"]


def slide_files(ids: list[str]) -> dict[str, dict]:
    body = {
        "filters": {"op": "and", "content": [
            {"op": "in", "content": {"field": "cases.submitter_id", "value": ids}},
            {"op": "=", "content": {"field": "experimental_strategy", "value": "Diagnostic Slide"}},
            {"op": "=", "content": {"field": "data_format", "value": "SVS"}},
            {"op": "=", "content": {"field": "access", "value": "open"}},
        ]},
        "fields": "file_id,file_name,file_size,cases.submitter_id",
        "size": 200,
    }
    hits = requests.post(f"{GDC}/files", json=body, timeout=120).json()["data"]["hits"]
    best: dict[str, dict] = {}
    for h in sorted(hits, key=lambda h: h["file_name"]):
        pid = h["cases"][0]["submitter_id"]
        best.setdefault(pid, h)  # DX1 sorts first
    return best


def tissue_slide_files(ids: list[str]) -> dict[str, list[dict]]:
    """Frozen sections (TCGA "Tissue Slide"): top and bottom of the portion that was sequenced, and normal tissue."""
    body = {
        "filters": {"op": "and", "content": [
            {"op": "in", "content": {"field": "cases.submitter_id", "value": ids}},
            {"op": "=", "content": {"field": "experimental_strategy", "value": "Tissue Slide"}},
            {"op": "=", "content": {"field": "data_format", "value": "SVS"}},
            {"op": "=", "content": {"field": "access", "value": "open"}},
        ]},
        "fields": "file_id,file_name,file_size,cases.submitter_id",
        "size": 500,
    }
    hits = requests.post(f"{GDC}/files", json=body, timeout=120).json()["data"]["hits"]
    out: dict[str, list[dict]] = {}
    for h in sorted(hits, key=lambda h: h["file_name"]):
        out.setdefault(h["cases"][0]["submitter_id"], []).append(h)
    return out


def fetch_slide(hit: dict) -> str:
    dest = RAW / "slides" / hit["file_name"]
    if dest.exists() and dest.stat().st_size == hit["file_size"]:
        return f"skip {dest.name}"
    tmp = dest.with_suffix(".part")
    for attempt in range(5):
        try:
            urllib.request.urlretrieve(f"{GDC}/data/{hit['file_id']}", tmp)
            break
        except OSError as err:
            if attempt == 4:
                raise
            print(f"retry {dest.name}: {err}", flush=True)
            time.sleep(5 * (attempt + 1))
    tmp.rename(dest)
    return f"done {dest.name} ({hit['file_size'] / 1e6:.0f} MB)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-slides", action="store_true")
    ap.add_argument("--skip-dicom", action="store_true")
    ap.add_argument("--tissue-slides", action="store_true",
                    help="add the frozen-section slides to work/imaging_index.json and download only those")
    args = ap.parse_args()
    patients = cohort()
    ids = [p["id"] for p in patients]
    (RAW / "slides").mkdir(parents=True, exist_ok=True)

    if args.tissue_slides:
        index_path = ROOT / "work" / "imaging_index.json"
        index = json.loads(index_path.read_text())
        tissue = tissue_slide_files(ids)
        for pid in ids:
            index[pid]["tissueSlides"] = tissue.get(pid, [])
        index_path.write_text(json.dumps(index, indent=2, default=str))
        hits = [h for pid in ids for h in tissue.get(pid, [])]
        print(f"{len(hits)} tissue slides, {sum(h['file_size'] for h in hits) / 1e9:.1f} GB", flush=True)
        with ThreadPoolExecutor(4) as pool:
            for msg in pool.map(fetch_slide, hits):
                print(msg, flush=True)
        return

    slides = slide_files(ids)
    client = IDCClient()
    quoted = ",".join(repr(i) for i in ids)
    studies = client.sql_query(f"""
        SELECT PatientID, StudyInstanceUID, SeriesInstanceUID, StudyDate, Modality, SeriesDescription,
               BodyPartExamined, instanceCount, series_size_MB
        FROM index WHERE PatientID IN ({quoted}) AND Modality IN ('CT','MR','PT','SEG','SM')
          AND {OPEN_LICENCE}
        ORDER BY PatientID, StudyDate
    """)

    client.fetch_index("seg_index")
    segs = client.sql_query(f"""
        SELECT s.segmented_SeriesInstanceUID AS target, s.SeriesInstanceUID AS seg, i.SeriesDescription AS description,
               s.AlgorithmName AS algorithm, s.total_segments AS segments
        FROM seg_index s JOIN index i ON i.SeriesInstanceUID = s.SeriesInstanceUID
        WHERE i.PatientID IN ({quoted}) AND i.SeriesDescription NOT LIKE '%TIL%' AND {OPEN_LICENCE.replace('license', 'i.license')}
    """)

    index: dict[str, dict] = {}
    to_download: list[str] = []
    for p in patients:
        rows = studies[studies["PatientID"] == p["id"]]
        chosen = []
        for want in p["series"]:
            m = rows[(rows["SeriesDescription"].str.strip() == want["desc"].strip())
                     & (rows["StudyDate"].astype(str) == want["date"])]
            m = m.sort_values("instanceCount", ascending=False)
            if m.empty:
                raise SystemExit(f"{p['id']}: no series '{want['desc']}' on {want['date']}")
            s = m.iloc[0]
            chosen.append({"role": want["role"], "seriesUid": s["SeriesInstanceUID"],
                           "studyUid": s["StudyInstanceUID"], "date": want["date"],
                           "modality": s["Modality"], "description": s["SeriesDescription"],
                           "instances": int(s["instanceCount"]),
                           "aiSegmentations": segs[segs["target"] == s["SeriesInstanceUID"]]
                           .drop(columns="target").to_dict("records")})
            to_download.append(s["SeriesInstanceUID"])
        til = rows[(rows["Modality"] == "SEG") & rows["SeriesDescription"].str.contains("TIL", na=False)]
        index[p["id"]] = {
            "slide": slides.get(p["id"]),
            "series": chosen,
            "tilSeries": til[["SeriesInstanceUID", "SeriesDescription"]].to_dict("records"),
            "radiologyStudies": (
                rows[rows["Modality"].isin(["CT", "MR", "PT"])]
                .groupby(["StudyInstanceUID", "StudyDate"], as_index=False)
                .agg(modalities=("Modality", lambda m: ",".join(sorted(set(m)))),
                     series=("SeriesInstanceUID", "count"), images=("instanceCount", "sum"),
                     bodyPart=("BodyPartExamined", "first"))
                .assign(StudyDate=lambda d: d["StudyDate"].astype(str))
                .to_dict("records")
            ),
        }
    (ROOT / "work" / "imaging_index.json").write_text(json.dumps(index, indent=2, default=str))
    print(f"index written; {len(to_download)} series, {sum(1 for v in index.values() if v['slide'])} slides")

    if not args.skip_dicom:
        for pid, entry in index.items():
            for s in entry["series"]:
                dest = RAW / "dicom" / pid / s["seriesUid"]
                if not (dest.exists() and len(list(dest.rglob("*.dcm"))) >= s["instances"]):
                    dest.mkdir(parents=True, exist_ok=True)
                    client.download_from_selection(seriesInstanceUID=[s["seriesUid"]], downloadDir=str(dest),
                                                   dirTemplate=None, quiet=True)
                for seg in s["aiSegmentations"]:
                    sdest = RAW / "seg" / pid / seg["seg"]
                    if not sdest.exists():
                        sdest.mkdir(parents=True)
                        client.download_from_selection(seriesInstanceUID=[seg["seg"]], downloadDir=str(sdest),
                                                       dirTemplate=None, quiet=True)
            for t in entry["tilSeries"]:
                dest = RAW / "til" / pid
                dest.mkdir(parents=True, exist_ok=True)
                client.download_from_selection(seriesInstanceUID=[t["SeriesInstanceUID"]], downloadDir=str(dest),
                                               dirTemplate=None, quiet=True)
            print(f"dicom {pid}", flush=True)

    if not args.skip_slides:
        with ThreadPoolExecutor(4) as pool:
            for msg in pool.map(fetch_slide, [s for s in slides.values()]):
                print(msg, flush=True)


if __name__ == "__main__":
    main()
