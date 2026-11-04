"""Generate a synthetic primary-care history (Synthea) that ends at each patient's real diagnosis.

TCGA has no routine EHR data (labs, vitals, medications, encounters). To show what a full record
looks like, each real patient gets a Synthea-generated background matched on sex and age at
diagnosis, ending on the diagnosis date. Everything from Synthea is marked synthetic in the app.

Rules that keep the record coherent with the real data:
  - the synthetic person must be alive on the diagnosis date and have no cancer of any kind
  - smoking observations are dropped when TCGA records the real smoking history
  - body weight and height are rescaled so the last synthetic value matches the real one at
    diagnosis, when TCGA records it (BMI is recomputed)

Output: work/synthea/out/<id>/csv/*.csv and work/synthea/<id>.json (the patient's background)

Usage: python pipeline/build_synthea.py [--only TCGA-XX-XXXX] [--extract-only]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
JAR = WORK / "synthea" / "synthea-with-dependencies.jar"
CANCER_WORDS = ("neoplasm", "carcinoma", "cancer", "malignant", "tumor", "tumour", "leukemia", "lymphoma", "metasta")
SMOKING_CODES = {"72166-2"}
# Clinical observations: vitals, labs, exams and screening questionnaires (incl. social-needs screening).
# QALY/DALY bookkeeping and identity items (address, race, language) are dropped.
OBS_CATEGORIES = {"vital-signs", "laboratory", "exam", "survey"}
OBS_DROP = ("Address", "Race", "Hispanic or Latino", "Preferred language", "QALY", "DALY", "QOLS")
CONDITION_KEEP_FINDINGS = ("obesity", "prediabetes", "hypertension", "hyperlipidemia", "anemia", "body mass index 30")
CONDITION_DROP = ("gunshot", "bullet", "abuse", "violence", "intimate partner", "overdose", "misuse", "laceration",
                  "concussion", "sprain", "fracture", "injury", "dental", "gingiv", "caries", "tooth", "teeth", "burn")
WEIGHT, HEIGHT, BMI = "29463-7", "8302-2", "39156-5"


def diagnosis_date(pid: str, year: int) -> date:
    """Real year, deterministic synthetic day: the GDC publishes only the year of diagnosis."""
    day = int(hashlib.sha256(pid.encode()).hexdigest(), 16) % 330 + 15
    return date(year, 1, 1) + timedelta(days=day)


def real_facts(case: dict) -> dict:
    dx = [d for d in case["diagnoses"] if d.get("diagnosis_is_primary_disease")] or case["diagnoses"]
    fups = case.get("follow_ups") or []
    attrs = [o for f in fups for o in (f.get("other_clinical_attributes") or [])]
    at_dx = [o for o in attrs if o.get("timepoint_category") == "Initial Diagnosis"]
    weight = next((o["weight"] for o in at_dx if o.get("weight")), None)
    height = next((o["height"] for o in at_dx if o.get("height")), None)
    smoking = next((e.get("tobacco_smoking_status") for e in case.get("exposures") or []
                    if e.get("tobacco_smoking_status") not in (None, "Unknown", "Not Reported")), None)
    sex = (case.get("demographic") or {}).get("sex_at_birth") or (case.get("demographic") or {}).get("gender")
    return {
        "sex": "F" if str(sex).lower().startswith("f") else "M",
        "ageYears": int(dx[0]["age_at_diagnosis"] // 365.25),
        "year": int(dx[0]["year_of_diagnosis"]),
        "weightKg": weight,
        "heightCm": height,
        "smoking": smoking,
    }


def run_synthea(pid: str, facts: dict, dx: date, seed: int) -> pathlib.Path:
    out = WORK / "synthea" / "out" / pid
    if out.exists():
        shutil.rmtree(out)
    cmd = [
        "java", "-jar", str(JAR), "-p", "1", "-s", str(seed), "-cs", str(seed),
        "-g", facts["sex"], "-a", f"{facts['ageYears']}-{facts['ageYears']}",
        "-r", dx.strftime("%Y%m%d"), "-e", dx.strftime("%Y%m%d"),
        "--exporter.baseDirectory", str(out),
        "--exporter.csv.export", "true", "--exporter.fhir.export", "false",
        "--exporter.years_of_history", "0", "--generate.only_alive_patients", "true",
        "Massachusetts",
    ]
    subprocess.run(cmd, check=True, capture_output=True, cwd=WORK / "synthea")
    return out / "csv"


def acceptable(csv: pathlib.Path) -> tuple[bool, str]:
    patients = pd.read_csv(csv / "patients.csv")
    if len(patients) != 1:
        return False, f"{len(patients)} patients"
    if isinstance(patients.iloc[0].get("DEATHDATE"), str):
        return False, "died"
    conditions = pd.read_csv(csv / "conditions.csv")
    bad = conditions[conditions["DESCRIPTION"].str.lower().str.contains("|".join(CANCER_WORDS))]
    if len(bad):
        return False, f"synthetic cancer: {bad.iloc[0]['DESCRIPTION']}"
    return True, "ok"


def calibrate(obs: pd.DataFrame, code: str, target: float | None) -> pd.DataFrame:
    if target is None:
        return obs
    rows = obs["CODE"] == code
    if not rows.any():
        return obs
    vals = pd.to_numeric(obs.loc[rows, "VALUE"], errors="coerce")
    last = vals.iloc[-1]
    if last and last > 0:
        obs.loc[rows, "VALUE"] = (vals * (target / last)).round(1).astype(str)
    return obs


def keep_condition(description: str) -> bool:
    d = description.lower()
    if any(w in d for w in CONDITION_DROP):
        return False
    if "(disorder)" in d:
        return True
    return any(w in d for w in CONDITION_KEEP_FINDINGS)


def background(pid: str, csv: pathlib.Path, facts: dict, dx: date) -> dict:
    p = pd.read_csv(csv / "patients.csv").iloc[0]
    strip = lambda s: "".join(ch for ch in str(s) if not ch.isdigit()).strip()

    obs = pd.read_csv(csv / "observations.csv").sort_values("DATE")
    obs = obs[obs["CATEGORY"].isin(OBS_CATEGORIES)
              & ~obs["DESCRIPTION"].str.startswith(OBS_DROP, na=False)]
    if facts["smoking"]:
        obs = obs[~obs["CODE"].isin(SMOKING_CODES)]
    obs = calibrate(obs, WEIGHT, facts["weightKg"])
    obs = calibrate(obs, HEIGHT, facts["heightCm"])
    if facts["weightKg"] or facts["heightCm"]:
        w = obs[obs["CODE"] == WEIGHT].set_index("DATE")["VALUE"].astype(float)
        h = obs[obs["CODE"] == HEIGHT].set_index("DATE")["VALUE"].astype(float)
        bmi_rows = obs["CODE"] == BMI
        for i in obs[bmi_rows].index:
            d = obs.at[i, "DATE"]
            wv = w[w.index <= d].iloc[-1] if (w.index <= d).any() else None
            hv = h[h.index <= d].iloc[-1] if (h.index <= d).any() else None
            if wv and hv:
                obs.at[i, "VALUE"] = f"{wv / (hv / 100) ** 2:.1f}"

    def table(name: str, cols: list[str]) -> list[dict]:
        if not (csv / f"{name}.csv").exists():
            return []
        df = pd.read_csv(csv / f"{name}.csv")
        keep = [c for c in cols if c in df.columns]
        return json.loads(df[keep].to_json(orient="records"))

    return {
        "id": pid,
        "diagnosisDate": dx.isoformat(),
        "realFacts": facts,
        "person": {
            "first": strip(p["FIRST"]), "last": strip(p["LAST"]), "birthDate": p["BIRTHDATE"],
            "maritalStatus": p.get("MARITAL"), "city": p.get("CITY"), "state": p.get("STATE"),
            "county": p.get("COUNTY"),
        },
        "encounters": table("encounters", ["Id", "START", "STOP", "ENCOUNTERCLASS", "CODE", "DESCRIPTION", "REASONDESCRIPTION",
                                           "ORGANIZATION", "PROVIDER", "PAYER", "BASE_ENCOUNTER_COST", "TOTAL_CLAIM_COST",
                                           "PAYER_COVERAGE"]),
        "conditions": [c for c in table("conditions", ["START", "STOP", "ENCOUNTER", "CODE", "DESCRIPTION"])
                       if keep_condition(c["DESCRIPTION"])],
        "medications": table("medications", ["START", "STOP", "ENCOUNTER", "CODE", "DESCRIPTION", "REASONDESCRIPTION",
                                             "DISPENSES", "TOTALCOST", "PAYER_COVERAGE"]),
        "procedures": table("procedures", ["START", "STOP", "ENCOUNTER", "CODE", "DESCRIPTION", "REASONDESCRIPTION", "BASE_COST"]),
        "immunizations": table("immunizations", ["DATE", "ENCOUNTER", "CODE", "DESCRIPTION"]),
        "allergies": table("allergies", ["START", "STOP", "ENCOUNTER", "CODE", "DESCRIPTION", "TYPE", "CATEGORY", "REACTION1",
                                         "DESCRIPTION1", "SEVERITY1"]),
        "careplans": table("careplans", ["START", "STOP", "ENCOUNTER", "CODE", "DESCRIPTION", "REASONDESCRIPTION"]),
        "observations": json.loads(obs[["DATE", "ENCOUNTER", "CATEGORY", "CODE", "DESCRIPTION", "VALUE", "UNITS", "TYPE"]]
                                   .to_json(orient="records")),
        "organizations": table("organizations", ["Id", "NAME", "CITY"]),
        "providers": table("providers", ["Id", "NAME", "SPECIALITY"]),
        "payers": table("payers", ["Id", "NAME"]),
    }


def build(pid: str) -> str:
    case = json.loads((WORK / "raw" / "clinical" / f"{pid}.json").read_text())
    facts = real_facts(case)
    dx = diagnosis_date(pid, facts["year"])
    base = int(hashlib.sha256(pid.encode()).hexdigest(), 16) % 100_000
    for attempt in range(25):
        csv = run_synthea(pid, facts, dx, base + attempt)
        ok, why = acceptable(csv)
        if ok:
            data = background(pid, csv, facts, dx)
            (WORK / "synthea" / f"{pid}.json").write_text(json.dumps(data, indent=1))
            return f"{pid}: {data['person']['first']} {data['person']['last']}, seed {base + attempt}, " \
                   f"{len(data['encounters'])} encounters, {len(data['observations'])} observations"
        print(f"{pid}: retry ({why})", flush=True)
    raise SystemExit(f"{pid}: no acceptable synthetic background")


def extract(pid: str) -> str:
    """Rebuild the patient's background JSON from the Synthea output already on disk."""
    case = json.loads((WORK / "raw" / "clinical" / f"{pid}.json").read_text())
    facts = real_facts(case)
    dx = diagnosis_date(pid, facts["year"])
    data = background(pid, WORK / "synthea" / "out" / pid / "csv", facts, dx)
    (WORK / "synthea" / f"{pid}.json").write_text(json.dumps(data, indent=1))
    return f"{pid}: {len(data['encounters'])} encounters, {len(data['observations'])} observations"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--extract-only", action="store_true", help="re-read existing Synthea output; do not regenerate")
    args = ap.parse_args()
    ids = [p["id"] for p in json.loads((ROOT / "pipeline" / "cohort.json").read_text())["patients"]]
    if args.only:
        ids = [i for i in ids if i in args.only]
    with ThreadPoolExecutor(4) as pool:
        for msg in pool.map(extract if args.extract_only else build, ids):
            print(msg, flush=True)


if __name__ == "__main__":
    main()
