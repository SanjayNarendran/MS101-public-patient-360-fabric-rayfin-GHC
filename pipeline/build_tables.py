"""Shape everything into typed tables, one folder per OneLake silo.

work/tables/clinical/*.parquet   EHR + cancer registry (real TCGA clinical, synthetic Synthea background)
work/tables/genomics/*.parquet   sequencing core (mutations, copy number, expression, immune landscape)
work/tables/imaging/*.parquet    PACS + digital pathology catalog (slides, radiology studies and series)

Dates: the GDC publishes the real year of diagnosis and every event as days from diagnosis. The day
and month of diagnosis are synthetic (see build_synthea.py), so all dates keep their real intervals.
TCIA shifts radiology dates per patient; the baseline scan is aligned to 7 days before diagnosis.

Usage: python pipeline/build_tables.py
"""
from __future__ import annotations

import json
import pathlib
from datetime import date, timedelta

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
OUT = WORK / "tables"
IMAGING = WORK / "lakehouse" / "imaging"
MISSING = (None, "", "Not Reported", "not reported", "Unknown", "unknown", "Not Applicable", "not applicable")

CANCER_NAMES = {
    "KIRC": "Clear cell renal cell carcinoma", "BRCA": "Invasive breast carcinoma", "LUAD": "Lung adenocarcinoma",
    "LUSC": "Lung squamous cell carcinoma", "OV": "High-grade serous ovarian carcinoma", "LIHC": "Hepatocellular carcinoma",
    "BLCA": "Muscle-invasive urothelial bladder carcinoma", "UCEC": "Endometrial carcinoma", "CESC": "Cervical squamous cell carcinoma",
    "STAD": "Gastric adenocarcinoma", "COAD": "Colon adenocarcinoma", "ESCA": "Oesophageal adenocarcinoma",
    "KICH": "Chromophobe renal cell carcinoma", "KIRP": "Papillary renal cell carcinoma", "PRAD": "Prostate adenocarcinoma",
}
ORGANS = {
    "KIRC": "Kidney", "BRCA": "Breast", "LUAD": "Lung", "LUSC": "Lung", "OV": "Ovary", "LIHC": "Liver", "BLCA": "Bladder",
    "UCEC": "Uterus", "CESC": "Cervix", "STAD": "Stomach", "COAD": "Colon", "ESCA": "Oesophagus", "KICH": "Kidney",
    "KIRP": "Kidney", "PRAD": "Prostate",
}
REPORT_PART = 4000
SYNTHETIC_CONDITION_DROP = ("gingival", "tooth", "teeth", "burn", "dental")
EMPTY_OK = [
    ("clinical", "allergy", ["patient_id", "encounter_id", "onset", "description", "category", "reaction", "severity"]),
    ("genomics", "structural_variant", ["patient_id", "site1Gene", "site2Gene", "eventInfo", "variantClass"]),
]
NAME_OVERRIDES = {"TCGA-KM-8441": "Ruth", "TCGA-VS-A9V3": "Joan"}
CIBERSORT = [
    "B Cells Naive", "B Cells Memory", "Plasma Cells", "T Cells CD8", "T Cells CD4 Naive", "T Cells CD4 Memory Resting",
    "T Cells CD4 Memory Activated", "T Cells Follicular Helper", "T Cells Regulatory Tregs", "T Cells gamma delta",
    "NK Cells Resting", "NK Cells Activated", "Monocytes", "Macrophages M0", "Macrophages M1", "Macrophages M2",
    "Dendritic Cells Resting", "Dendritic Cells Activated", "Mast Cells Resting", "Mast Cells Activated", "Eosinophils",
    "Neutrophils",
]


def v(x):
    """Normalise GDC 'not reported' style values to None and lists to '; '-joined text."""
    if isinstance(x, list):
        x = [i for i in x if i not in MISSING]
        return "; ".join(map(str, x)) or None
    return None if x in MISSING else x


def num(x):
    try:
        return float(x) if x not in MISSING else None
    except (TypeError, ValueError):
        return None


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def day_to_date(dx: date, day) -> str | None:
    d = num(day)
    return iso(dx + timedelta(days=int(d))) if d is not None else None


def cohort() -> list[dict]:
    return json.loads((ROOT / "pipeline" / "cohort.json").read_text())["patients"]


def build() -> dict[str, dict[str, pd.DataFrame]]:
    t: dict[str, dict[str, list]] = {"clinical": {}, "genomics": {}, "imaging": {}}
    add = lambda silo, table, row: t[silo].setdefault(table, []).append(row)
    imaging_index = json.loads((WORK / "imaging_index.json").read_text())
    reference = json.loads((WORK / "fetched" / "_reference.json").read_text())

    for p in cohort():
        pid = p["id"]
        f = json.loads((WORK / "fetched" / f"{pid}.json").read_text())
        s = json.loads((WORK / "synthea" / f"{pid}.json").read_text())
        case, attrs, imm = f["gdcCase"], f["attributes"], f["immune"]
        code = p["project"].split("-")[1]
        dx_date = date.fromisoformat(s["diagnosisDate"])
        demo = case.get("demographic") or {}
        dxs = case["diagnoses"]
        primary = next((d for d in dxs if d.get("diagnosis_is_primary_disease") and d.get("year_of_diagnosis")), dxs[0])
        fups = case.get("follow_ups") or []
        oca = [o for fu in fups for o in (fu.get("other_clinical_attributes") or [])]
        exp = case.get("exposures") or []
        fh = case.get("family_histories") or []
        tob = next((e for e in exp if e.get("exposure_type") == "Tobacco"), {})
        occupation = next((v(e.get("occupation_type")) for e in exp if e.get("occupation_type")), None)
        last_day = max([num(fu.get("days_to_follow_up")) or 0 for fu in fups] + [num(demo.get("days_to_death")) or 0]
                       + [num(d.get("days_to_last_follow_up")) or 0 for d in dxs])
        first = NAME_OVERRIDES.get(pid, s["person"]["first"])
        grade = next((v(d.get("tumor_grade")) for d in dxs if v(d.get("tumor_grade"))), None)
        gleason = next((d.get("gleason_score") for d in dxs if d.get("gleason_score")), None)

        add("clinical", "patient", {
            "patient_id": pid,
            "display_name": f"{first} {s['person']['last']}",
            "project_id": p["project"],
            "cancer_code": code,
            "cancer_type": CANCER_NAMES[code],
            "organ": ORGANS[code],
            "sex": (demo.get("sex_at_birth") or "").title() or None,
            "age_at_diagnosis": int(primary["age_at_diagnosis"] // 365.25),
            "race": (v(demo.get("race")) or "").capitalize().replace("American indian", "American Indian").replace("alaska native", "Alaska Native").replace("african american", "African American") or None,
            "ethnicity": (v(demo.get("ethnicity")) or "").capitalize().replace("latino", "Latino").replace("hispanic", "Hispanic") or None,
            "country": v(demo.get("country_of_residence_at_enrollment")),
            "vital_status": demo.get("vital_status"),
            "cause_of_death": v(demo.get("cause_of_death")),
            "diagnosis_date": iso(dx_date),
            "year_of_diagnosis": int(primary["year_of_diagnosis"]),
            "days_to_death": num(demo.get("days_to_death")),
            "death_date": day_to_date(dx_date, demo.get("days_to_death")),
            "days_to_last_contact": last_day,
            "last_contact_date": day_to_date(dx_date, last_day),
            "primary_diagnosis": v(primary.get("primary_diagnosis")),
            "subtype_detailed": attrs.get("CANCER_TYPE_DETAILED"),
            "tissue_of_origin": v(primary.get("tissue_or_organ_of_origin")),
            "site_of_resection": v(primary.get("site_of_resection_or_biopsy")),
            "laterality": v(primary.get("laterality")),
            "icd10": v(primary.get("icd_10_code")),
            "morphology": v(primary.get("morphology")),
            "stage": v(primary.get("ajcc_pathologic_stage")) or next((v(d.get("figo_stage")) for d in dxs if v(d.get("figo_stage"))), None)
                     or v(primary.get("ajcc_clinical_stage")),
            "stage_system": "AJCC " + (primary.get("ajcc_staging_system_edition") or "") if v(primary.get("ajcc_pathologic_stage"))
                            else ("FIGO" if any(v(d.get("figo_stage")) for d in dxs) else None),
            "t_stage": v(primary.get("ajcc_pathologic_t")) or v(primary.get("ajcc_clinical_t")),
            "n_stage": v(primary.get("ajcc_pathologic_n")) or v(primary.get("ajcc_clinical_n")),
            "m_stage": v(primary.get("ajcc_pathologic_m")) or v(primary.get("ajcc_clinical_m")),
            "grade": grade,
            "gleason_score": num(gleason),
            "residual_disease": next((v(d.get("residual_disease")) for d in dxs if v(d.get("residual_disease"))), None),
            "method_of_diagnosis": next((v(d.get("method_of_diagnosis")) for d in dxs if v(d.get("method_of_diagnosis"))), None),
            "child_pugh": next((v(d.get("child_pugh_classification")) for d in dxs if v(d.get("child_pugh_classification"))), None),
            "fibrosis": next((v(d.get("ishak_fibrosis_score")) for d in dxs if v(d.get("ishak_fibrosis_score"))), None),
            "prior_malignancy": v(primary.get("prior_malignancy")),
            "smoking_status": v(tob.get("tobacco_smoking_status")),
            "pack_years": num(tob.get("pack_years_smoked")),
            "smoking_quit_year": num(tob.get("tobacco_smoking_quit_year")),
            "occupation": occupation,
            "bmi": next((num(o.get("bmi")) for o in oca if o.get("bmi")), None),
            "weight_kg": next((num(o.get("weight")) for o in oca if o.get("weight")), None),
            "height_cm": next((num(o.get("height")) for o in oca if o.get("height")), None),
            "menopause_status": next((v(o.get("menopause_status")) for o in oca if v(o.get("menopause_status"))), None),
            "risk_factors": "; ".join(sorted({x for o in oca for x in (o.get("risk_factors") or [])})) or None,
            "comorbidities": "; ".join(sorted({x for o in oca for x in (o.get("comorbidities") or [])})) or None,
            "family_history": "; ".join(
                f"{'Relative with ' if (h.get('relative_with_cancer_history') or '').lower() == 'yes' else 'No relative with '}"
                f"{(h.get('relationship_primary_diagnosis') or 'cancer').lower()}" for h in fh
            ) or None,
            "os_months": num(attrs.get("OS_MONTHS")),
            "pfs_months": num(attrs.get("PFS_MONTHS")),
            "gdc_case_id": case["id"],
            "gdc_url": f"https://portal.gdc.cancer.gov/cases/{case['id']}",
            "synthetic_name": True,
        })

        for i, d in enumerate(dxs):
            add("clinical", "diagnosis", {
                "patient_id": pid, "seq": i,
                "classification": v(d.get("classification_of_tumor")),
                "is_primary": bool(d.get("diagnosis_is_primary_disease")),
                "primary_diagnosis": v(d.get("primary_diagnosis")),
                "site": v(d.get("tissue_or_organ_of_origin")),
                "day": num(d.get("days_to_diagnosis")),
                "date": day_to_date(dx_date, d.get("days_to_diagnosis")),
                "stage": v(d.get("ajcc_pathologic_stage")) or v(d.get("figo_stage")),
                "sites_of_involvement": v(d.get("sites_of_involvement")),
                "method": v(d.get("method_of_diagnosis")),
            })
            for j, tr in enumerate(d.get("treatments") or []):
                given = (tr.get("treatment_or_therapy") or "").lower() == "yes"
                ttype = v(tr.get("treatment_type")) or ""
                start = num(tr.get("days_to_treatment_start"))
                estimated = False
                if start is None and given and ttype.lower().startswith("surgery") and d.get("diagnosis_is_primary_disease"):
                    start, estimated = 0.0, True
                add("clinical", "treatment", {
                    "patient_id": pid, "diagnosis_seq": i, "seq": j,
                    "given": given,
                    "treatment_type": ttype or None,
                    "agent": v(tr.get("therapeutic_agents")),
                    "intent": v(tr.get("treatment_intent_type")),
                    "disease_status": v(tr.get("initial_disease_status")),
                    "outcome": v(tr.get("treatment_outcome")),
                    "start_day": start,
                    "end_day": num(tr.get("days_to_treatment_end")),
                    "start_date": day_to_date(dx_date, start),
                    "end_date": day_to_date(dx_date, tr.get("days_to_treatment_end")),
                    "start_estimated": estimated,
                    "cycles": num(tr.get("number_of_cycles")),
                    "dose": num(tr.get("treatment_dose")),
                    "dose_units": v(tr.get("treatment_dose_units")),
                    "fractions": num(tr.get("number_of_fractions")),
                    "route": v(tr.get("route_of_administration")),
                    "anatomic_site": v(tr.get("treatment_anatomic_sites")),
                    "clinical_trial": v(tr.get("clinical_trial_indicator")),
                    "concurrent_radiation": v(tr.get("chemo_concurrent_to_radiation")),
                })

        for fu in fups:
            day = num(fu.get("days_to_follow_up"))
            add("clinical", "follow_up", {
                "patient_id": pid, "day": day, "date": day_to_date(dx_date, day),
                "timepoint": v(fu.get("timepoint_category")),
                "disease_response": v(fu.get("disease_response")),
                "progression": v(fu.get("progression_or_recurrence")),
                "progression_type": v(fu.get("progression_or_recurrence_type")),
                "progression_site": v(fu.get("progression_or_recurrence_anatomic_site")),
                "days_to_progression": num(fu.get("days_to_progression")) or num(fu.get("days_to_recurrence")),
                "evidence": v(fu.get("evidence_of_progression_type")) or v(fu.get("evidence_of_recurrence_type")),
                "ecog": num(fu.get("ecog_performance_status")),
                "karnofsky": num(fu.get("karnofsky_performance_status")),
                "imaging_type": v(fu.get("imaging_type")),
                "imaging_findings": v(fu.get("imaging_findings")),
            })
            for m in fu.get("molecular_tests") or []:
                test_day = num(m.get("days_to_test"))
                if test_day is None:
                    test_day = 0.0 if v(m.get("timepoint_category")) in ("Initial Diagnosis", "Preoperative", None) else day
                row = {
                    "patient_id": pid, "day": test_day, "date": day_to_date(dx_date, test_day),
                    "test": v(m.get("laboratory_test")) or v(m.get("antigen")) or v(m.get("gene_symbol")),
                    "gene": v(m.get("gene_symbol")),
                    "method": v(m.get("molecular_analysis_method")),
                    "result": v(m.get("test_result")),
                    "value": num(m.get("test_value")),
                    "units": v(m.get("test_units")),
                    "ref_low": num(m.get("blood_test_normal_range_lower")),
                    "ref_high": num(m.get("blood_test_normal_range_upper")),
                    "intensity": v(m.get("staining_intensity_value")),
                    "exon": v(m.get("exon")),
                    "variant_type": v(m.get("variant_type")),
                    "timepoint": v(m.get("timepoint_category")),
                }
                add("clinical", "biomarker_test", row)
            for o in fu.get("other_clinical_attributes") or []:
                for key, label, unit in [
                    ("fev1_ref_pre_bronch_percent", "FEV1 % predicted (pre-bronchodilator)", "%"),
                    ("fev1_ref_post_bronch_percent", "FEV1 % predicted (post-bronchodilator)", "%"),
                    ("dlco_ref_predictive_percent", "DLCO % predicted", "%"),
                ]:
                    if o.get(key) is not None:
                        add("clinical", "observation", {
                            "patient_id": pid, "encounter_id": None, "date": iso(dx_date), "day": 0.0, "category": "laboratory",
                            "code": None, "name": label, "value": num(o[key]), "value_text": None, "units": unit,
                            "source": "TCGA", "synthetic": False,
                        })

        # Synthetic primary-care background (Synthea), ending at diagnosis. Every item keeps the id of the
        # visit it was recorded at, so a visit can be opened with everything that happened during it.
        names = lambda key: {r["Id"]: r for r in s.get(key, [])}
        orgs, providers, payers = names("organizations"), names("providers"), names("payers")
        clean = lambda t: "".join(ch for ch in str(t or "") if not ch.isdigit()).strip() or None
        for e in s["encounters"]:
            start, stop = pd.Timestamp(e["START"]), pd.Timestamp(e["STOP"]) if e.get("STOP") else None
            prov = providers.get(e.get("PROVIDER"), {})
            add("clinical", "encounter", {
                "patient_id": pid, "encounter_id": e["Id"], "start": e["START"][:10],
                "stop": (e.get("STOP") or "")[:10] or None,
                "duration_minutes": round((stop - start).total_seconds() / 60) if stop is not None else None,
                "encounter_class": e.get("ENCOUNTERCLASS"), "code": str(e.get("CODE")), "description": e.get("DESCRIPTION"),
                "reason": e.get("REASONDESCRIPTION"),
                "provider": clean(prov.get("NAME")), "provider_specialty": (prov.get("SPECIALITY") or "").capitalize() or None,
                "organization": (orgs.get(e.get("ORGANIZATION"), {}).get("NAME") or "").title() or None,
                "payer": payers.get(e.get("PAYER"), {}).get("NAME"),
                "base_cost": num(e.get("BASE_ENCOUNTER_COST")), "total_cost": num(e.get("TOTAL_CLAIM_COST")),
                "payer_coverage": num(e.get("PAYER_COVERAGE")), "synthetic": True,
            })
        for c in s["conditions"]:
            if any(w in c["DESCRIPTION"].lower() for w in SYNTHETIC_CONDITION_DROP):
                continue
            add("clinical", "condition", {
                "patient_id": pid, "encounter_id": c.get("ENCOUNTER"), "onset": c["START"][:10],
                "resolved": (c.get("STOP") or "")[:10] or None,
                "code": str(c.get("CODE")), "description": c["DESCRIPTION"], "source": "Synthea", "synthetic": True,
            })
        for c in s.get("careplans", []):
            add("clinical", "careplan", {
                "patient_id": pid, "encounter_id": c.get("ENCOUNTER"), "start": c["START"][:10],
                "stop": (c.get("STOP") or "")[:10] or None, "description": c["DESCRIPTION"],
                "reason": c.get("REASONDESCRIPTION"), "synthetic": True,
            })
        for c in filter(None, [
            *[{"d": x, "kind": "Risk factor"} for o in oca for x in (o.get("risk_factors") or [])],
            *[{"d": x, "kind": "Comorbidity"} for o in oca for x in (o.get("comorbidities") or [])],
        ]):
            add("clinical", "condition", {
                "patient_id": pid, "onset": None, "resolved": None, "code": None,
                "description": f"{c['d']} ({c['kind'].lower()})", "source": "TCGA", "synthetic": False,
            })
        for m in s["medications"]:
            add("clinical", "medication", {
                "patient_id": pid, "encounter_id": m.get("ENCOUNTER"), "start": m["START"][:10],
                "stop": (m.get("STOP") or "")[:10] or None,
                "code": str(m.get("CODE")), "description": m["DESCRIPTION"], "reason": m.get("REASONDESCRIPTION"),
                "dispenses": num(m.get("DISPENSES")), "total_cost": num(m.get("TOTALCOST")), "synthetic": True,
            })
        for m in s["immunizations"]:
            add("clinical", "immunization", {
                "patient_id": pid, "encounter_id": m.get("ENCOUNTER"), "date": m["DATE"][:10],
                "description": m["DESCRIPTION"], "synthetic": True,
            })
        for a in s["allergies"]:
            add("clinical", "allergy", {
                "patient_id": pid, "encounter_id": a.get("ENCOUNTER"), "onset": a["START"][:10], "description": a["DESCRIPTION"],
                "category": a.get("CATEGORY"), "reaction": a.get("DESCRIPTION1"), "severity": a.get("SEVERITY1"),
                "synthetic": True,
            })
        for pr in s["procedures"]:
            add("clinical", "procedure", {
                "patient_id": pid, "encounter_id": pr.get("ENCOUNTER"), "date": pr["START"][:10],
                "description": pr["DESCRIPTION"], "reason": pr.get("REASONDESCRIPTION"), "synthetic": True,
            })
        for o in s["observations"]:
            val = num(o.get("VALUE")) if o.get("TYPE") == "numeric" else None
            add("clinical", "observation", {
                "patient_id": pid, "encounter_id": o.get("ENCOUNTER"), "date": o["DATE"][:10],
                "day": float((date.fromisoformat(o["DATE"][:10]) - dx_date).days),
                "category": o.get("CATEGORY") or "survey", "code": o.get("CODE"), "name": o["DESCRIPTION"],
                "value": val, "value_text": None if val is not None else o.get("VALUE"), "units": o.get("UNITS"),
                "source": "Synthea", "synthetic": True,
            })

        # SQL analytics endpoints expose strings as varchar(8000), so long reports are stored in parts.
        pdf = f.get("reportPdf") or {}
        text = f.get("reportText") or ""
        for part, i in enumerate(range(0, max(len(text), 1), REPORT_PART)):
            add("clinical", "pathology_report", {
                "patient_id": pid, "part": part, "report_text": text[i : i + REPORT_PART] or None,
                "pdf_path": f"reports/{pid}.pdf" if pdf else None, "pdf_bytes": pdf.get("bytes"),
                "gdc_file_id": pdf.get("fileId"),
                "text_source": "TCGA-Reports (Kefeli et al. 2024), OCR of the GDC pathology report, CC BY 4.0",
            })

        # Genomics silo.
        sample = f"{pid}-01"
        for m in f["mutations"]:
            alt, ref = m.get("tumorAltCount"), m.get("tumorRefCount")
            vaf = alt / (alt + ref) if isinstance(alt, int) and isinstance(ref, int) and alt + ref > 0 and alt >= 0 else None
            add("genomics", "somatic_mutation", {
                "patient_id": pid, "sample_id": sample, "gene": m["gene"], "protein_change": m.get("proteinChange"),
                "mutation_type": m.get("mutationType"), "variant_type": m.get("variantType"),
                "chromosome": str(m.get("chromosome")), "start_pos": m.get("start"), "end_pos": m.get("end"),
                "ref_allele": m.get("ref"), "alt_allele": m.get("alt"), "vaf": round(vaf, 4) if vaf is not None else None,
                "is_driver_gene": m["isDriverGene"],
            })
        for seg in f["segments"]:
            add("genomics", "copy_number_segment", {
                "patient_id": pid, "chromosome": str(seg["chromosome"]), "start_pos": seg["start"], "end_pos": seg["end"],
                "probes": seg["probes"], "segment_mean": seg["segmentMean"],
            })
        for c in f["cna"]:
            add("genomics", "gene_copy_number", {
                "patient_id": pid, "gene": c["gene"], "alteration": c["alteration"],
                "call": {2: "Amplification", -2: "Deep deletion"}.get(c["alteration"], str(c["alteration"])),
                "is_driver_gene": c["isDriverGene"],
            })
        for e in f["expression"]:
            add("genomics", "gene_expression", {"patient_id": pid, "gene": e["gene"], "z_score": round(e["zScore"], 3)})
        for sv in f["fusions"]:
            add("genomics", "structural_variant", {"patient_id": pid, **sv})

        study = f["study"]
        pct = lambda metric, val: (
            round(100 * sum(1 for x in reference[study].get(metric, []) if x <= val) / len(reference[study][metric]), 1)
            if val is not None and reference[study].get(metric) else None
        )
        tmb, fga = num(attrs.get("TMB_NONSYNONYMOUS")), num(attrs.get("FRACTION_GENOME_ALTERED"))
        msi = num(attrs.get("MSI_SCORE_MANTIS"))
        add("genomics", "molecular_profile", {
            "patient_id": pid, "sample_id": sample,
            "subtype": attrs.get("SUBTYPE"),
            "tmb": tmb, "tmb_percentile": pct("TMB_NONSYNONYMOUS", tmb),
            "fraction_genome_altered": fga, "fga_percentile": pct("FRACTION_GENOME_ALTERED", fga),
            "aneuploidy_score": num(attrs.get("ANEUPLOIDY_SCORE")),
            "msi_mantis": msi, "msi_sensor": num(attrs.get("MSI_SENSOR_SCORE")),
            "msi_status": ("MSI-high" if msi is not None and msi >= 0.4 else "Microsatellite stable") if msi is not None else None,
            "mutation_count": len(f["mutations"]),
            "leukocyte_fraction": num(imm.get("Leukocyte Fraction")),
            "proliferation": num(imm.get("Proliferation")),
            "ifn_gamma_response": num(imm.get("IFN-gamma Response")),
            "tgf_beta_response": num(imm.get("TGF-beta Response")),
            "wound_healing": num(imm.get("Wound Healing")),
            "macrophage_regulation": num(imm.get("Macrophage Regulation")),
            "lymphocyte_infiltration": num(imm.get("Lymphocyte Infiltration Signature Score")),
            "hrd_score": num(imm.get("Homologous Recombination Defects")),
            "tcr_richness": num(imm.get("TCR Richness")),
            "cohort_study": study,
            "cohort_size": len(reference[study].get("TMB_NONSYNONYMOUS", [])),
        })
        for cell in CIBERSORT:
            if imm.get(cell) is not None:
                add("genomics", "immune_cell_fraction", {"patient_id": pid, "cell_type": cell, "fraction": num(imm.get(cell))})

        # Imaging silo.
        entry = imaging_index[pid]
        label = json.loads((IMAGING / "slides" / pid.lower() / "label.json").read_text())
        til = label.get("til") or {}
        add("imaging", "pathology_slide", {
            "patient_id": pid, "slide_id": label["slideId"], "barcode": label["barcode"], "stain": "H&E",
            "specimen": "FFPE diagnostic section", "role": "diagnostic", "section": "DX1",
            "sample_type": "Primary tumour", "preparation": "Formalin-fixed, paraffin-embedded",
            "sort_order": 0, "day": 0.0, "date": iso(dx_date),
            "vendor": label.get("vendor"), "objective_power": label.get("objectivePower"), "mpp": label.get("mpp"),
            "width": label["width"], "height": label["height"], "levels": label["levels"],
            "tile_count": label["tileCount"], "tiles_bytes": label["tilesBytes"], "raw_bytes": label["rawSizeBytes"],
            "dzi_path": label["dzi"], "thumbnail_path": label["thumbnail"], "raw_path": label["raw"],
            "gdc_file_id": label["gdcFileId"], "source_url": label["sourceUrl"],
            "hotspots": json.dumps(label["hotspots"] + (til.get("hotspots") or [])),
            "til_overlay_path": f"slides/{label['slideId']}/til.png" if til else None,
            "til_x": til.get("x"), "til_y": til.get("y"), "til_width": til.get("width"),
            "til_positive_fraction": til.get("tilPositiveFraction"), "til_mean_probability": til.get("meanProbability"),
            "til_algorithm": til.get("algorithm"),
        })
        # Frozen sections: TS (top) and BS (bottom) bracket the tissue portion that was sequenced.
        frozen = sorted((IMAGING / "slides" / pid.lower()).glob("*/label.json"),
                        key=lambda q: (q.parent.name.startswith("11"), "-b" in q.parent.name, q.parent.name))
        for order, tpath in enumerate(frozen, start=1):
            fs = json.loads(tpath.read_text())
            code = fs["barcode"].split("-")
            sample, slide_code = code[3], code[5]
            normal = sample.startswith("11")
            where = {"T": "top", "B": "bottom"}.get(slide_code[0], "")
            portion = "normal tissue" if normal else "sequenced tumour"
            add("imaging", "pathology_slide", {
                "patient_id": pid, "slide_id": fs["slideId"], "barcode": fs["barcode"], "stain": "H&E",
                "specimen": f"Frozen section, {where} of the {portion} portion" if where else f"Frozen section of the {portion} portion",
                "role": "frozen", "section": slide_code,
                "sample_type": "Adjacent normal tissue" if normal else "Primary tumour",
                "preparation": "Frozen section", "sort_order": order + (100 if normal else 0), "day": 0.0, "date": iso(dx_date),
                "vendor": fs.get("vendor"), "objective_power": fs.get("objectivePower"), "mpp": fs.get("mpp"),
                "width": fs["width"], "height": fs["height"], "levels": fs["levels"],
                "tile_count": fs["tileCount"], "tiles_bytes": fs["tilesBytes"], "raw_bytes": fs["rawSizeBytes"],
                "dzi_path": fs["dzi"], "thumbnail_path": fs["thumbnail"], "raw_path": fs["raw"],
                "gdc_file_id": fs["gdcFileId"], "source_url": fs["sourceUrl"],
                "hotspots": json.dumps(fs["hotspots"]), "til_overlay_path": None,
                "til_x": None, "til_y": None, "til_width": None,
                "til_positive_fraction": None, "til_mean_probability": None, "til_algorithm": None,
            })
        primary_series = next(x for x in entry["series"] if x["role"] == "primary")
        offset = (dx_date - timedelta(days=7)) - date.fromisoformat(primary_series["date"])
        for st in entry["radiologyStudies"]:
            shifted = date.fromisoformat(st["StudyDate"]) + offset
            add("imaging", "radiology_study", {
                "patient_id": pid, "study_uid": st["StudyInstanceUID"], "date": iso(shifted),
                "day": float((shifted - dx_date).days), "date_deidentified": st["StudyDate"],
                "modalities": st["modalities"], "series_count": int(st["series"]), "images": int(st["images"]),
                "body_part": st.get("bodyPart"),
            })
        for meta_path in sorted((IMAGING / "radiology" / pid.lower()).glob("*/series.json")):
            m = json.loads(meta_path.read_text())
            shifted = date.fromisoformat(m["studyDateDeidentified"]) + offset
            ai = m.get("aiSegmentation") or {}
            add("imaging", "radiology_series", {
                "patient_id": pid, "series_key": m["key"], "role": m["role"], "series_uid": m["seriesUid"],
                "study_uid": m["studyUid"], "date": iso(shifted), "day": float((shifted - dx_date).days),
                "modality": m["modality"], "description": m["description"], "body_part": m.get("bodyPart"),
                "manufacturer": m.get("manufacturer"), "model": m.get("model"),
                "slice_thickness_mm": m.get("sliceThicknessMm"), "kvp": m.get("kvp"), "contrast": m.get("contrast"),
                "field_strength_t": m.get("fieldStrengthT"), "images": m["images"],
                "dims": json.dumps(m["dims"]), "spacing_mm": json.dumps(m["spacingMm"]),
                "volume_path": m["volume"], "labels_path": m.get("labels"), "labels_lut_path": m.get("labelsLut"),
                "thumbnail_path": m["thumbnail"], "volume_bytes": m["volumeBytes"],
                "raw_files": m["rawFiles"], "raw_bytes": m["rawBytes"], "raw_path": m["rawPath"],
                "ai_algorithm": ai.get("algorithm"), "ai_description": ai.get("description"),
                "ai_measures": json.dumps(ai.get("measures")) if ai else None,
                "source_url": m["sourceUrl"],
            })

    summaries = json.loads((ROOT / "pipeline" / "summaries.json").read_text())
    for pid, sm in summaries["patients"].items():
        t["clinical"].setdefault("patient_summary", []).append({
            "patient_id": pid, "headline": sm["headline"], "summary": sm["summary"],
            "points_json": json.dumps(sm["points"]), "generated_by": summaries["generated_by"],
        })

    for study, metrics in reference.items():
        for metric, values in metrics.items():
            q = pd.Series(values).quantile([i / 100 for i in range(101)]).round(4).tolist() if values else []
            t["genomics"].setdefault("cohort_reference", []).append(
                {"cohort_study": study, "metric": metric, "n": len(values), "quantiles_json": json.dumps(q)}
            )
    out = {silo: {name: pd.DataFrame(rows) for name, rows in tables.items()} for silo, tables in t.items()}
    # The app queries these even when the cohort has no rows for them.
    for silo, name, cols in EMPTY_OK:
        out[silo].setdefault(name, pd.DataFrame({c: pd.Series(dtype="string") for c in cols}))
    return out


def main() -> None:
    for silo, tables in build().items():
        folder = OUT / silo
        folder.mkdir(parents=True, exist_ok=True)
        for name, df in tables.items():
            df.to_parquet(folder / f"{name}.parquet", index=False)
            print(f"{silo}.{name}: {len(df)} rows, {len(df.columns)} columns")


if __name__ == "__main__":
    main()
