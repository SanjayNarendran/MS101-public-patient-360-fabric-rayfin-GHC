"""Rank TCGA patients that have every modality, one shortlist per cancer type.

A candidate must have, in public open-access sources:
  - a pre-treatment CT or MR series in NCI Imaging Data Commons (IDC)
  - a diagnostic H&E whole-slide image in the GDC
  - a pathology report in TCGA-Reports (Kefeli et al. 2024)
  - a PanCancer Atlas sample in cBioPortal (mutations, copy number, subtype, survival)

Clinical depth (treatments, follow-ups, recurrence) and genomic interest (known driver
mutations) push a patient up the list. Output: work/candidates.csv

Usage: python pipeline/select_cohort.py
"""
from __future__ import annotations

import json
import pathlib

import pandas as pd
import requests
from idc_index import IDCClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
GDC = "https://api.gdc.cancer.gov"
CBIO = "https://www.cbioportal.org/api"

PROJECTS = {
    "tcga_kirc": "kirc", "tcga_ov": "ov", "tcga_brca": "brca", "tcga_blca": "blca",
    "tcga_lihc": "lihc", "tcga_ucec": "ucec", "tcga_cesc": "cesc", "tcga_stad": "stad",
    "tcga_lusc": "lusc", "tcga_kirp": "kirp", "tcga_luad": "luad", "tcga_coad": "coadread",
    "tcga_esca": "esca", "tcga_kich": "kich", "tcga_prad": "prad",
}

DRIVERS = {
    "TP53", "KRAS", "PIK3CA", "EGFR", "BRAF", "VHL", "PBRM1", "SETD2", "BAP1", "BRCA1", "BRCA2",
    "PTEN", "ARID1A", "APC", "CTNNB1", "NRAS", "FGFR3", "KMT2D", "KEAP1", "STK11", "RB1",
    "CDKN2A", "NFE2L2", "ERBB2", "MET", "IDH1", "SMAD4", "FBXW7", "SPOP", "FOXA1", "GATA3",
    "MAP3K7", "CDH1", "TERT", "AXIN1", "POLE", "KDM6A", "ELF3", "MTOR", "NF1", "ATM",
}


def idc_candidates() -> pd.DataFrame:
    client = IDCClient()
    query = """
    WITH rad AS (
      SELECT collection_id, PatientID, SeriesInstanceUID, StudyInstanceUID, Modality,
             SeriesDescription, BodyPartExamined, instanceCount, series_size_MB
      FROM index
      WHERE collection_id LIKE 'tcga_%' AND Modality IN ('CT','MR') AND instanceCount >= 40
    ),
    sm AS (SELECT DISTINCT PatientID FROM index WHERE collection_id LIKE 'tcga_%' AND Modality = 'SM')
    SELECT rad.* FROM rad JOIN sm USING (PatientID)
    """
    df = client.sql_query(query)
    return df[df["collection_id"].isin(PROJECTS)]


def report_patients() -> set[str]:
    reports = pd.read_csv(WORK / "sources" / "TCGA_Reports.csv", usecols=["patient_filename"])
    return set(reports["patient_filename"].str.slice(0, 12))


def gdc_cases(barcodes: list[str]) -> dict[str, dict]:
    fields = [
        "submitter_id", "project.project_id", "demographic.vital_status", "demographic.gender",
        "diagnoses.ajcc_pathologic_stage", "diagnoses.treatments.treatment_type",
        "diagnoses.treatments.therapeutic_agents", "follow_ups.days_to_follow_up",
        "follow_ups.progression_or_recurrence", "files.data_type", "files.experimental_strategy",
    ]
    out: dict[str, dict] = {}
    for i in range(0, len(barcodes), 200):
        chunk = barcodes[i : i + 200]
        body = {
            "filters": {"op": "in", "content": {"field": "submitter_id", "value": chunk}},
            "fields": ",".join(fields),
            "size": len(chunk),
        }
        resp = requests.post(f"{GDC}/cases", json=body, timeout=120)
        resp.raise_for_status()
        for hit in resp.json()["data"]["hits"]:
            out[hit["submitter_id"]] = hit
    return out


def cbio_patients(study: str) -> pd.DataFrame:
    url = f"{CBIO}/studies/{study}_tcga_pan_can_atlas_2018/clinical-data"
    rows = requests.get(url, params={"clinicalDataType": "SAMPLE", "projection": "SUMMARY"}, timeout=120).json()
    df = pd.DataFrame(rows).pivot_table(index="patientId", columns="clinicalAttributeId", values="value", aggfunc="first")
    return df


def cbio_drivers(study: str) -> dict[str, list[str]]:
    sid = f"{study}_tcga_pan_can_atlas_2018"
    body = {"sampleListId": f"{sid}_all"}
    url = f"{CBIO}/molecular-profiles/{sid}_mutations/mutations/fetch"
    rows = requests.post(url, params={"projection": "DETAILED"}, json=body, timeout=300).json()
    hits: dict[str, list[str]] = {}
    for r in rows:
        gene = r.get("gene", {}).get("hugoGeneSymbol")
        if gene in DRIVERS and r.get("mutationType") != "Silent":
            hits.setdefault(r["patientId"], []).append(f"{gene} {r.get('proteinChange', '')}")
    return hits


def main() -> None:
    WORK.mkdir(exist_ok=True)
    rad = idc_candidates()
    have_report = report_patients()
    rad = rad[rad["PatientID"].isin(have_report)]
    gdc = gdc_cases(sorted(rad["PatientID"].unique()))

    rows = []
    for collection, study in PROJECTS.items():
        sub = rad[rad["collection_id"] == collection]
        if sub.empty:
            continue
        clin = cbio_patients(study)
        drivers = cbio_drivers(study)
        for pid, series in sub.groupby("PatientID"):
            case = gdc.get(pid)
            if not case or pid not in clin.index:
                continue
            files = case.get("files", [])
            dx = sum(1 for f in files if f.get("experimental_strategy") == "Diagnostic Slide")
            if dx == 0:
                continue
            treatments = [t for d in case.get("diagnoses", []) for t in d.get("treatments", []) or []]
            agents = sorted({t.get("therapeutic_agents") for t in treatments if t.get("therapeutic_agents")})
            fups = case.get("follow_ups", []) or []
            recur = any((f.get("progression_or_recurrence") or "").lower() == "yes" for f in fups)
            c = clin.loc[pid]
            best = series.sort_values(["instanceCount"], ascending=False).iloc[0]
            score = (
                min(len(treatments), 8) + 2 * len(agents) + min(len(fups), 6)
                + 3 * recur + 2 * min(len(drivers.get(pid, [])), 4)
                + (2 if series["Modality"].nunique() > 1 else 0)
            )
            rows.append({
                "collection": collection, "patient": pid, "score": score,
                "stage": (case.get("diagnoses") or [{}])[0].get("ajcc_pathologic_stage"),
                "vital": case.get("demographic", {}).get("vital_status"),
                "sex": case.get("demographic", {}).get("gender"),
                "n_treat": len(treatments), "agents": "; ".join(agents), "n_fup": len(fups),
                "recur": recur, "drivers": "; ".join(drivers.get(pid, [])[:6]),
                "subtype": c.get("SUBTYPE"), "tmb": c.get("TMB_NONSYNONYMOUS"),
                "msi": c.get("MSI_SCORE_MANTIS"), "dx_slides": dx,
                "n_series": len(series), "modalities": ",".join(sorted(series["Modality"].unique())),
                "best_series": best["SeriesInstanceUID"], "best_desc": best["SeriesDescription"],
                "best_n": int(best["instanceCount"]), "best_mb": float(best["series_size_MB"]),
            })
    out = pd.DataFrame(rows).sort_values(["collection", "score"], ascending=[True, False])
    out.to_csv(WORK / "candidates.csv", index=False)
    print(out.groupby("collection").head(4).to_string(max_colwidth=50))


if __name__ == "__main__":
    main()
