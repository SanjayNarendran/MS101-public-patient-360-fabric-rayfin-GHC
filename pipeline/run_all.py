"""Run steps 5 and 6 of the README in order: download the public data, build it, and load it into Fabric.

    python pipeline/run_all.py [--tissue-slides] [--skip-raw]

Each step is the script you would run by hand, run with this interpreter; the first failure stops the run. Downloads
skip files that already exist, so a failed run can be started again. Needs fabric.env (scripts/setup_fabric.py) and
an Azure CLI login for the upload. scripts/cloud_load.py runs this on an Azure VM.
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def steps(tissue_slides: bool, skip_raw: bool) -> list[list[str]]:
    plan = [["download_sources.py"], ["download_imaging.py"]]
    if tissue_slides:
        plan.append(["download_imaging.py", "--tissue-slides"])
    plan += [["fetch_clinical_genomics.py"], ["build_synthea.py"], ["build_slides.py"]]
    if tissue_slides:
        plan.append(["build_slides.py", "--tissue"])
    plan += [["build_radiology.py"], ["build_tables.py"], ["upload_onelake.py", *(["--skip-raw"] if skip_raw else [])]]
    return plan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tissue-slides", action="store_true", help="also download and tile the frozen sections")
    ap.add_argument("--skip-raw", action="store_true", help="do not copy the original SVS and DICOM files to OneLake")
    args = ap.parse_args()
    for script, *opts in steps(args.tissue_slides, args.skip_raw):
        print(f"=== {script} {' '.join(opts)}".rstrip(), flush=True)
        if subprocess.run([sys.executable, "-u", str(HERE / script), *opts]).returncode:
            raise SystemExit(f"{script} failed; fix the cause and run this again")
    print("=== done")


if __name__ == "__main__":
    main()
