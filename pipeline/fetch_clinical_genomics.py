"""Fetch clinical, genomic and report data for the cohort from open-access APIs.

Sources (all open access, no login):
  - NCI Genomic Data Commons (GDC) API: full clinical record (diagnoses, treatments, follow-ups,
    molecular tests, exposures, family history) and the scanned pathology report PDF
  - cBioPortal, TCGA PanCancer Atlas 2018 studies: somatic mutations, copy-number segments,
    GISTIC gene calls, structural variants, mRNA expression z-scores for a gene panel, sample
    attributes (subtype, TMB, MSI, aneuploidy), and per-cancer distributions for context
  - Thorsson et al. 2018, The Immune Landscape of Cancer: leukocyte fraction, immune signature scores, CIBERSORT,
    TCR richness and HRD, from the study's data files on the GDC (work/sources/panimmune/)
  - TCGA-Reports (Kefeli et al. 2024, CC BY 4.0): OCR text of the pathology report (work/sources/TCGA_Reports.csv)

Run pipeline/download_sources.py first for the two work/sources/ inputs.

Output: work/raw/clinical/<id>.json, work/raw/reports/<id>.pdf, work/fetched/<id>.json,
        work/fetched/_reference.json

Usage: python pipeline/fetch_clinical_genomics.py
"""
from __future__ import annotations

import json
import math
import pathlib
import re
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
GDC = "https://api.gdc.cancer.gov"
CBIO = "https://www.cbioportal.org/api"

# Lineage markers, therapy targets, immune checkpoints, DNA repair and antibody-drug conjugate targets.
GENE_PANEL = [
    "ERBB2", "ESR1", "PGR", "AR", "EGFR", "MET", "ALK", "FGFR3", "KIT", "VEGFA", "CA9", "MKI67",
    "CD274", "PDCD1", "CTLA4", "LAG3", "CD8A", "GZMB",
    "TACSTD2", "NECTIN4", "FOLR1", "CLDN18", "CD276", "DLL3", "MSLN",
    "KLK3", "FOLH1", "MUC16", "CEACAM5", "AFP", "GATA3", "NKX2-1", "TP63", "KRT5", "PAX8", "WT1", "CDX2",
    "MLH1", "MSH2", "MSH6", "PMS2", "BRCA1", "BRCA2", "CDKN2A", "PTEN",
]

DRIVER_GENES = {
    "TP53", "KRAS", "NRAS", "HRAS", "PIK3CA", "PIK3R1", "EGFR", "ERBB2", "ERBB3", "BRAF", "MET", "ALK", "ROS1",
    "RET", "VHL", "PBRM1", "SETD2", "BAP1", "KDM5C", "BRCA1", "BRCA2", "PALB2", "ATM", "ATR", "CHEK2",
    "PTEN", "ARID1A", "ARID1B", "ARID2", "APC", "CTNNB1", "AXIN1", "FGFR1", "FGFR2", "FGFR3", "KMT2D",
    "KMT2C", "KEAP1", "STK11", "NFE2L2", "RB1", "CDKN2A", "CDKN1A", "SMAD4", "SMAD2", "TGFBR2", "FBXW7",
    "SPOP", "FOXA1", "GATA3", "MAP3K1", "CDH1", "TERT", "POLE", "POLD1", "KDM6A", "ELF3", "MTOR", "TSC1",
    "TSC2", "NF1", "NF2", "IDH1", "IDH2", "MLH1", "MSH2", "MSH6", "PMS2", "CREBBP", "EP300", "NOTCH1",
    "FAT1", "ZFHX3", "CTCF", "RNF43", "SOX9", "ACVR2A", "PPP2R1A", "TP63", "SOX2", "MYC", "CCNE1",
    "CCND1", "MDM2", "CDK4", "AKT1", "ESR1", "AR", "STAG2", "TET2", "ASXL1", "DNMT3A", "PIK3CB",
}

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(max_retries=Retry(
    total=6, backoff_factor=2, allowed_methods=None, status_forcelist=[429, 500, 502, 503, 504],
)))

COHORT_METRICS = ["TMB_NONSYNONYMOUS", "FRACTION_GENOME_ALTERED", "ANEUPLOIDY_SCORE", "MSI_SCORE_MANTIS"]


def cohort() -> list[dict]:
    return json.loads((ROOT / "pipeline" / "cohort.json").read_text())["patients"]


def gdc_case(pid: str) -> dict:
    body = {
        "filters": {"op": "in", "content": {"field": "submitter_id", "value": [pid]}},
        "expand": ",".join([
            "demographic", "diagnoses", "diagnoses.treatments", "diagnoses.pathology_details",
            "follow_ups", "follow_ups.molecular_tests", "follow_ups.other_clinical_attributes",
            "exposures", "family_histories", "project",
        ]),
        "size": 1,
    }
    resp = HTTP.post(f"{GDC}/cases", json=body, timeout=120)
    resp.raise_for_status()
    return resp.json()["data"]["hits"][0]


def gdc_report(pid: str) -> dict | None:
    body = {
        "filters": {"op": "and", "content": [
            {"op": "in", "content": {"field": "cases.submitter_id", "value": [pid]}},
            {"op": "=", "content": {"field": "data_type", "value": "Pathology Report"}},
        ]},
        "fields": "file_id,file_name,file_size,access",
        "size": 5,
    }
    hits = HTTP.post(f"{GDC}/files", json=body, timeout=60).json()["data"]["hits"]
    hits = [h for h in hits if h.get("access") == "open"]
    if not hits:
        return None
    hit = hits[0]
    dest = WORK / "raw" / "reports" / f"{pid}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(HTTP.get(f"{GDC}/data/{hit['file_id']}", timeout=300).content)
    return {"fileId": hit["file_id"], "fileName": hit["file_name"], "bytes": dest.stat().st_size}


def cbio_get(path: str, **params) -> list | dict:
    resp = HTTP.get(f"{CBIO}{path}", params=params, timeout=300)
    resp.raise_for_status()
    return resp.json()


def cbio_post(path: str, body: dict | list, **params) -> list:
    resp = HTTP.post(f"{CBIO}{path}", params=params, json=body, timeout=300)
    resp.raise_for_status()
    return resp.json()


def entrez_ids(symbols: list[str]) -> dict[int, str]:
    rows = cbio_post("/genes/fetch", symbols, geneIdType="HUGO_GENE_SYMBOL")
    return {r["entrezGeneId"]: r["hugoGeneSymbol"] for r in rows}


def clean(value):
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


# Thorsson et al. 2018 scored each tumour on these expression signatures (Scores_160_Signatures, "SetName").
IMMUNE_SIGNATURES = {
    "Wound Healing": "CHANG_CORE_SERUM_RESPONSE_UP",
    "Macrophage Regulation": "CSF1_response",
    "Lymphocyte Infiltration Signature Score": "LIexpression_score",
    "IFN-gamma Response": "Module3_IFN_score",
    "TGF-beta Response": "TGFB_score_21050467",
    "Proliferation": "Module11_Prolif_score",
}

CIBERSORT_CELLS = [
    "B Cells Naive", "B Cells Memory", "Plasma Cells", "T Cells CD8", "T Cells CD4 Naive", "T Cells CD4 Memory Resting",
    "T Cells CD4 Memory Activated", "T Cells Follicular Helper", "T Cells Regulatory Tregs", "T Cells gamma delta",
    "NK Cells Resting", "NK Cells Activated", "Monocytes", "Macrophages M0", "Macrophages M1", "Macrophages M2",
    "Dendritic Cells Resting", "Dendritic Cells Activated", "Mast Cells Resting", "Mast Cells Activated", "Eosinophils",
    "Neutrophils",
]


def load_panimmune(patients: list[str]) -> dict[str, dict]:
    """Immune landscape values per patient (primary tumour), from the GDC copies of the Thorsson 2018 data files.

    Keys follow the paper's names, which build_tables.py maps to columns.
    """
    src = WORK / "sources" / "panimmune"
    primary = lambda barcode: barcode[13:15] == "01"  # noqa: E731
    out: dict[str, dict] = {p: {} for p in patients}

    leuk = pd.read_csv(src / "TCGA_all_leuk_estimate.masked.20170107.tsv", sep="\t", header=None, names=["study", "sample", "value"])
    for row in leuk[leuk["sample"].map(primary)].itertuples():
        if row.sample[:12] in out:
            out[row.sample[:12]]["Leukocyte Fraction"] = float(row.value)

    sig = pd.read_csv(src / "Scores_160_Signatures.tsv.gz", sep="\t").drop(columns="Source").set_index("SetName")
    for barcode in sig.columns:
        if primary(barcode) and barcode[:12] in out:
            for name, set_name in IMMUNE_SIGNATURES.items():
                out[barcode[:12]][name] = clean(float(sig.at[set_name, barcode]))

    cib = pd.read_csv(src / "TCGA.Kallisto.fullIDs.cibersort.relative.tsv", sep="\t")
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    cells = {c: name for c in cib.columns for name in CIBERSORT_CELLS if norm(c) == norm(name)}
    for row in cib.to_dict("records"):
        barcode = row["SampleID"].replace(".", "-")
        if primary(barcode) and barcode[:12] in out:
            for c, name in cells.items():
                out[barcode[:12]][name] = clean(float(row[c]))

    tcr = pd.read_csv(src / "mitcr_sampleStatistics_20160714.tsv", sep="\t")
    for row in tcr[tcr["SampleTypeLetterCode"] == "TP"].itertuples():
        if row.ParticipantBarcode in out:
            out[row.ParticipantBarcode]["TCR Richness"] = clean(float(row.numClones))

    hrd = pd.read_csv(src / "TCGA.HRD_withSampleID.txt", sep="\t")
    for row in hrd.itertuples():
        if primary(row.sampleID) and row.sampleID[:12] in out:
            out[row.sampleID[:12]]["Homologous Recombination Defects"] = clean(float(row.HRD))
    return out


def fetch_patient(p: dict, immune: dict[str, dict], reports: pd.DataFrame, genes: dict[int, str]) -> dict:
    pid, study = p["id"], f"{p['cbio']}_tcga_pan_can_atlas_2018"
    sample = f"{pid}-01"
    case = gdc_case(pid)
    (WORK / "raw" / "clinical").mkdir(parents=True, exist_ok=True)
    (WORK / "raw" / "clinical" / f"{pid}.json").write_text(json.dumps(case, indent=2))

    attrs = {}
    for typ in ("PATIENT", "SAMPLE"):
        url = f"/studies/{study}/{'patients' if typ == 'PATIENT' else 'samples'}/{pid if typ == 'PATIENT' else sample}/clinical-data"
        for r in cbio_get(url):
            attrs[r["clinicalAttributeId"]] = r["value"]

    mutations = cbio_post(
        f"/molecular-profiles/{study}_mutations/mutations/fetch",
        {"sampleIds": [sample]}, projection="DETAILED",
    )
    muts = [{
        "gene": m["gene"]["hugoGeneSymbol"],
        "proteinChange": m.get("proteinChange"),
        "mutationType": m.get("mutationType"),
        "variantType": m.get("variantType"),
        "chromosome": m.get("chr"),
        "start": m.get("startPosition"),
        "end": m.get("endPosition"),
        "ref": m.get("referenceAllele"),
        "alt": m.get("variantAllele"),
        "tumorAltCount": m.get("tumorAltCount"),
        "tumorRefCount": m.get("tumorRefCount"),
        "isDriverGene": m["gene"]["hugoGeneSymbol"] in DRIVER_GENES,
    } for m in mutations]

    segments = [{
        "chromosome": s["chromosome"], "start": s["start"], "end": s["end"],
        "probes": s["numberOfProbes"], "segmentMean": s["segmentMean"],
    } for s in cbio_get(f"/studies/{study}/samples/{sample}/copy-number-segments")]

    cna = cbio_post(
        f"/molecular-profiles/{study}_gistic/discrete-copy-number/fetch",
        {"sampleIds": [sample]}, discreteCopyNumberEventType="HOMDEL_AND_AMP", projection="DETAILED",
    )
    cna_calls = [{
        "gene": c["gene"]["hugoGeneSymbol"], "alteration": c["alteration"],
        "isDriverGene": c["gene"]["hugoGeneSymbol"] in DRIVER_GENES,
    } for c in cna]

    expression = []
    try:
        rows = cbio_post(
            f"/molecular-profiles/{study}_rna_seq_v2_mrna_median_all_sample_Zscores/molecular-data/fetch",
            {"sampleIds": [sample], "entrezGeneIds": list(genes)},
        )
        expression = [{"gene": genes[r["entrezGeneId"]], "zScore": r["value"]} for r in rows]
    except requests.HTTPError:
        pass

    fusions = []
    try:
        sv = cbio_post("/structural-variant/fetch", {
            "molecularProfileIds": [f"{study}_structural_variants"],
            "sampleMolecularIdentifiers": [{"molecularProfileId": f"{study}_structural_variants", "sampleId": sample}],
        })
        fusions = [{
            "site1Gene": s.get("site1HugoSymbol"), "site2Gene": s.get("site2HugoSymbol"),
            "eventInfo": s.get("eventInfo"), "variantClass": s.get("variantClass"),
        } for s in sv]
    except requests.HTTPError:
        pass

    immune_row = immune.get(pid, {})

    rep = reports[reports["patient_filename"].str.startswith(pid)]
    report_text = rep.iloc[0]["text"] if len(rep) else None

    return {
        "id": pid,
        "project": p["project"],
        "study": study,
        "gdcCase": case,
        "attributes": attrs,
        "mutations": muts,
        "segments": segments,
        "cna": cna_calls,
        "expression": expression,
        "fusions": fusions,
        "immune": immune_row,
        "reportText": report_text,
        "reportPdf": gdc_report(pid),
    }


def reference(studies: list[str]) -> dict:
    """Per-cancer distributions of TMB, genome altered, aneuploidy and MSI for context."""
    out = {}
    for study in studies:
        rows = cbio_get(f"/studies/{study}/clinical-data", clinicalDataType="SAMPLE")
        df = pd.DataFrame(rows)
        df = df[df["clinicalAttributeId"].isin(COHORT_METRICS)]
        metrics = {}
        for metric, grp in df.groupby("clinicalAttributeId"):
            vals = pd.to_numeric(grp["value"], errors="coerce").dropna().sort_values()
            metrics[metric] = [round(float(v), 4) for v in vals]
        out[study] = metrics
    return out


def main() -> None:
    patients = cohort()
    (WORK / "fetched").mkdir(parents=True, exist_ok=True)
    immune = load_panimmune([p["id"] for p in patients])
    reports = pd.read_csv(WORK / "sources" / "TCGA_Reports.csv")
    genes = entrez_ids(GENE_PANEL)

    def run(p: dict) -> str:
        data = fetch_patient(p, immune, reports, genes)
        (WORK / "fetched" / f"{p['id']}.json").write_text(json.dumps(data, indent=2, default=str))
        return f"{p['id']}: {len(data['mutations'])} mutations, {len(data['segments'])} segments, " \
               f"{len(data['cna'])} CNA, {len(data['expression'])} expr, {len(data['fusions'])} SV, " \
               f"report {'yes' if data['reportText'] else 'no'}, pdf {'yes' if data['reportPdf'] else 'no'}"

    with ThreadPoolExecutor(5) as pool:
        for msg in pool.map(run, patients):
            print(msg, flush=True)

    studies = sorted({f"{p['cbio']}_tcga_pan_can_atlas_2018" for p in patients})
    (WORK / "fetched" / "_reference.json").write_text(json.dumps(reference(studies)))
    print("reference distributions written")


if __name__ == "__main__":
    main()
