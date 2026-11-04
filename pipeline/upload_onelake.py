"""Load the three silos into OneLake and build the shortcut-only patient360 Lakehouse.

  clinical_lakehouse  Files/reports/*.pdf, Files/raw/{gdc,synthea}/...   Tables/<clinical tables>
  genomics_lakehouse  Files/raw/{cbioportal,panimmune}/...                 Tables/<genomics tables>
  imaging_lakehouse   Files/slides, Files/radiology, Files/raw/{slides,dicom,til,seg}   Tables/<imaging tables>
  patient360          Tables/* and Files/{clinical,genomics,imaging}: OneLake shortcuts only, no data

Delta tables are written with delta-rs. Files go up with azcopy. Each silo also gets a
`<silo>_inventory` table describing what it stores, for the app's data map.
Uses your Azure CLI login (`az login`) and the IDs in fabric.env (scripts/setup_fabric.py).

Usage: python pipeline/upload_onelake.py [--skip-files] [--skip-raw] [--skip-tables] [--skip-shortcuts]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import time

import pandas as pd
import pyarrow as pa
import requests
from azure.identity import AzureCliCredential
from deltalake import write_deltalake

import fabric_config

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
CONFIG = fabric_config.load()
WORKSPACE_ID = CONFIG["WORKSPACE_ID"]
LAKEHOUSES = {"clinical": CONFIG["CLINICAL_LH"], "genomics": CONFIG["GENOMICS_LH"], "imaging": CONFIG["IMAGING_LH"]}
PATIENT360 = CONFIG["PATIENT360_LH"]
SQL_ENDPOINT = CONFIG["PATIENT360_SQL"]
FABRIC = "https://api.fabric.microsoft.com/v1"
DATE_COLUMNS = {"date", "start_date", "end_date", "diagnosis_date", "death_date", "last_contact_date",
                "onset", "resolved", "start", "stop"}
CREDENTIAL = AzureCliCredential()


def token(scope: str) -> str:
    return CREDENTIAL.get_token(scope).token


def azcopy(src: pathlib.Path | str, silo_id: str, dest: str, *extra: str) -> None:
    url = f"https://onelake.blob.fabric.microsoft.com/{WORKSPACE_ID}/{silo_id}/Files/{dest}"
    cmd = ["azcopy", "copy", str(src), url, "--recursive", "--overwrite=ifSourceNewer",
           "--trusted-microsoft-suffixes=onelake.blob.fabric.microsoft.com",
           "--log-level=ERROR", "--output-level=essential", *extra]
    env = {**os.environ, "AZCOPY_AUTO_LOGIN_TYPE": "AZCLI"}
    subprocess.run(cmd, check=True, env=env)


def upload_files(include_raw: bool) -> None:
    img = WORK / "lakehouse" / "imaging"
    azcopy(img / "slides", LAKEHOUSES["imaging"], "", "--include-pattern=*.jpg", "--content-type=image/jpeg")
    azcopy(img / "slides", LAKEHOUSES["imaging"], "", "--include-pattern=*.dzi;*.json;*.png")
    azcopy(img / "radiology", LAKEHOUSES["imaging"], "", "--include-pattern=*.nii.gz;*.json;*.jpg")
    azcopy(WORK / "raw" / "reports", LAKEHOUSES["clinical"], "", "--content-type=application/pdf")

    staging = WORK / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "clinical" / "gdc").mkdir(parents=True)
    (staging / "genomics" / "cbioportal").mkdir(parents=True)
    for f in (WORK / "raw" / "clinical").glob("*.json"):
        shutil.copy(f, staging / "clinical" / "gdc" / f.name)
    for f in (WORK / "fetched").glob("TCGA-*.json"):
        data = json.loads(f.read_text())
        slim = {k: data[k] for k in ("id", "study", "attributes", "mutations", "segments", "cna", "expression", "fusions", "immune")}
        (staging / "genomics" / "cbioportal" / f.name).write_text(json.dumps(slim))
    shutil.copy(WORK / "fetched" / "_reference.json", staging / "genomics" / "cbioportal" / "_reference.json")
    synthea = staging / "clinical" / "synthea"
    for d in (WORK / "synthea" / "out").iterdir():
        shutil.copytree(d / "csv", synthea / d.name)
    azcopy(staging / "clinical" / "*", LAKEHOUSES["clinical"], "raw/")
    azcopy(staging / "genomics" / "*", LAKEHOUSES["genomics"], "raw/")
    azcopy(WORK / "sources" / "panimmune", LAKEHOUSES["genomics"], "raw/")

    if include_raw:
        azcopy(WORK / "raw" / "slides", LAKEHOUSES["imaging"], "raw/", "--include-pattern=*.svs")
        for kind in ("dicom", "til", "seg"):
            azcopy(WORK / "raw" / kind, LAKEHOUSES["imaging"], "raw/")


def arrow_table(df: pd.DataFrame) -> pa.Table:
    df = df.copy()
    for col in df.columns:
        if col in DATE_COLUMNS:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    table = pa.Table.from_pandas(df, preserve_index=False)
    fields = []
    for field in table.schema:
        if pa.types.is_null(field.type):
            fields.append(pa.field(field.name, pa.string()))
        elif pa.types.is_large_string(field.type):
            fields.append(pa.field(field.name, pa.string()))
        else:
            fields.append(field)
    return table.cast(pa.schema(fields))


def write_table(silo: str, name: str, df: pd.DataFrame) -> None:
    uri = f"abfss://{WORKSPACE_ID}@onelake.dfs.fabric.microsoft.com/{LAKEHOUSES[silo]}/Tables/{name}"
    opts = {"bearer_token": token("https://storage.azure.com/.default"), "use_fabric_endpoint": "true"}
    write_deltalake(uri, arrow_table(df), mode="overwrite", schema_mode="overwrite", storage_options=opts)


def folder_stats(path: pathlib.Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def inventory(silo: str, tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = [{"silo": f"{silo}_lakehouse", "kind": "table", "name": name, "rows": len(df), "files": None,
             "bytes": int(df.memory_usage(deep=True).sum()), "description": None} for name, df in tables.items()]
    folders = {
        "clinical": [("Files/reports", WORK / "raw" / "reports", "Scanned pathology reports (PDF, GDC)"),
                     ("Files/raw/gdc", WORK / "raw" / "clinical", "GDC clinical records (JSON)"),
                     ("Files/raw/synthea", WORK / "synthea" / "out", "Synthea output (CSV)")],
        "genomics": [("Files/raw/cbioportal", WORK / "fetched", "cBioPortal and GDC extracts (JSON)"),
                     ("Files/raw/panimmune", WORK / "sources" / "panimmune", "Immune landscape data files (GDC)")],
        "imaging": [("Files/raw/slides", WORK / "raw" / "slides", "Original whole-slide images (Aperio SVS)"),
                    ("Files/raw/dicom", WORK / "raw" / "dicom", "Original CT and MR series (DICOM)"),
                    ("Files/raw/seg", WORK / "raw" / "seg", "AI segmentations (DICOM SEG)"),
                    ("Files/raw/til", WORK / "raw" / "til", "AI TIL maps (DICOM SEG)"),
                    ("Files/slides", WORK / "lakehouse" / "imaging" / "slides", "Deep Zoom tile pyramids (JPEG)"),
                    ("Files/radiology", WORK / "lakehouse" / "imaging" / "radiology", "Browser-ready volumes (NIfTI)")],
    }[silo]
    for name, path, desc in folders:
        n, b = folder_stats(path) if path.is_dir() else (1, path.stat().st_size)
        rows.append({"silo": f"{silo}_lakehouse", "kind": "files", "name": name, "rows": None, "files": n, "bytes": b, "description": desc})
    return pd.DataFrame(rows)


def load_tables() -> list[tuple[str, str]]:
    written = []
    for silo in LAKEHOUSES:
        tables = {p.stem: pd.read_parquet(p) for p in sorted((WORK / "tables" / silo).glob("*.parquet"))}
        tables[f"{silo}_inventory"] = inventory(silo, tables)
        for name, df in tables.items():
            write_table(silo, name, df)
            written.append((silo, name))
            print(f"{silo}_lakehouse.{name}: {len(df)} rows", flush=True)
    return written


def fabric(method: str, path: str, **kw) -> requests.Response:
    headers = {"Authorization": f"Bearer {token('https://api.fabric.microsoft.com/.default')}"}
    resp = requests.request(method, f"{FABRIC}{path}", headers=headers, timeout=120, **kw)
    if resp.status_code >= 400:
        raise SystemExit(f"{method} {path}: {resp.status_code} {resp.text}")
    return resp


def wait(resp: requests.Response) -> None:
    op = resp.headers.get("Location")
    while resp.status_code == 202 and op:
        time.sleep(3)
        r = requests.get(op, headers={"Authorization": f"Bearer {token('https://api.fabric.microsoft.com/.default')}"}, timeout=60)
        status = r.json().get("status")
        if status in ("Succeeded", "Failed", "Completed"):
            print(f"operation {status}")
            return


def shortcuts(tables: list[tuple[str, str]]) -> None:
    requests_ = [{"path": "Tables", "name": name,
                  "target": {"oneLake": {"workspaceId": WORKSPACE_ID, "itemId": LAKEHOUSES[silo], "path": f"Tables/{name}"}}}
                 for silo, name in tables]
    requests_ += [{"path": "Files", "name": silo,
                   "target": {"oneLake": {"workspaceId": WORKSPACE_ID, "itemId": item, "path": "Files"}}}
                  for silo, item in LAKEHOUSES.items()]
    resp = fabric("POST", f"/workspaces/{WORKSPACE_ID}/items/{PATIENT360}/shortcuts/bulkCreate?shortcutConflictPolicy=CreateOrOverwrite",
                  json={"createShortcutRequests": requests_})
    wait(resp)
    print(f"{len(requests_)} shortcuts in patient360")
    resp = fabric("POST", f"/workspaces/{WORKSPACE_ID}/sqlEndpoints/{SQL_ENDPOINT}/refreshMetadata", json={})
    wait(resp)
    print("SQL endpoint metadata refreshed")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-files", action="store_true")
    ap.add_argument("--skip-raw", action="store_true")
    ap.add_argument("--skip-tables", action="store_true")
    ap.add_argument("--skip-shortcuts", action="store_true")
    args = ap.parse_args()
    if not args.skip_files:
        upload_files(include_raw=not args.skip_raw)
    tables = [] if args.skip_tables else load_tables()
    if args.skip_tables:
        tables = [(silo, p.stem) for silo in LAKEHOUSES for p in sorted((WORK / "tables" / silo).glob("*.parquet"))] \
            + [(s, f"{s}_inventory") for s in LAKEHOUSES]
    if not args.skip_shortcuts:
        shortcuts(tables)


if __name__ == "__main__":
    main()
