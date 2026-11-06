"""Run steps 5 and 6 of the README on a temporary Azure VM instead of this machine.

    python scripts/cloud_load.py up --location <region> [--size Standard_D16s_v5] [-- <pipeline/run_all.py options>]
    python scripts/cloud_load.py status
    python scripts/cloud_load.py down

up creates a resource group, tagged as made by this script, with one Ubuntu VM. The VM has a system-assigned managed
identity, no public inbound ports, a throwaway SSH key whose private half is never kept, and auto-shutdown as a safety net.
up adds the identity to the workspace in fabric.env as Contributor. The VM clones this repository and runs
pipeline/run_all.py (scripts/cloud_load.sh). up then waits: on success it runs down; on failure it keeps the VM so
status can show the log. down uses the workspace and principal recorded in resource-group tags, deletes their role
assignments and verifies removal before deleting the group. Removing workspace roles requires a workspace Admin.

Uses your Azure CLI login and its current subscription (`az account set` to change it); your credentials stay on this
machine. The VM clones the origin remote at the current commit, so push it first (or pass --repo and --ref): local
changes that are not pushed are not used.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from setup_fabric import API, Fabric

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
import fabric_config  # noqa: E402

AZ = shutil.which("az") or "az"
VM = "p360-loader"
TAG_KEY, TAG_VALUE = "p360-loader", "cloud_load.py"
WORKSPACE_TAG, PRINCIPAL_TAG = "p360-workspace", "p360-principal"
PENDING = "pending"
SHUTDOWN_AFTER = datetime.timedelta(hours=6)
# Microsoft (Release signing): signs the two apt repositories below.
MICROSOFT_KEY = "BC528686B50D79E339D3721CEB3E94ADBE1229CF"
STATUS_SCRIPT = r"""
if systemctl is-failed --quiet p360-loader; then
    state=FAILED
elif [ -s /var/lib/p360/state ]; then
    state=$(cat /var/lib/p360/state)
    if [ "$state" = RUNNING ] && ! systemctl is-active --quiet p360-loader; then state=FAILED; fi
else
    boot=$(cloud-init status 2>&1)
    printf '%s\n' "$boot" | tail -c 1000
    case "$boot" in
        *error*|*degraded*|*disabled*) state=FAILED ;;
        *) state=STARTING ;;
    esac
fi
tail -c 2000 /var/log/p360-loader.log 2>/dev/null || true
printf '\nP360_STATE=%s\n' "$state"
"""


def az(*args: str) -> str:
    r = subprocess.run([AZ, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise SystemExit(f"az {' '.join(args[:2])} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def cloud_config(repo: str, ref: str, fabric_env: str, run_args: list[str]) -> str:
    def file(path: str, text: str, mode: str) -> dict:
        data = base64.b64encode(text.replace("\r\n", "\n").encode()).decode()
        return {"path": path, "permissions": mode, "encoding": "b64", "content": data}

    script = (ROOT / "scripts" / "cloud_load.sh").read_text(encoding="utf-8")
    config = {
        "apt": {"sources": {
            "microsoft-prod": {"source": "deb [arch=amd64] https://packages.microsoft.com/ubuntu/24.04/prod noble main",
                               "keyid": MICROSOFT_KEY},
            "azure-cli": {"source": "deb [arch=amd64] https://packages.microsoft.com/repos/azure-cli/ noble main",
                          "keyid": MICROSOFT_KEY},
        }},
        "package_update": True,
        "packages": ["git", "python3-venv", "default-jre-headless", "azure-cli", "azcopy"],
        "write_files": [file("/var/lib/p360/fabric.env", fabric_env, "0600"),
                        file("/var/lib/p360/cloud_load.sh", script, "0755")],
        # systemd-run returns at once, so cloud-init finishes and the run carries on as its own service.
        "runcmd": [["systemd-run", f"--unit={VM}", "/var/lib/p360/cloud_load.sh", repo, ref, *run_args]],
    }
    return "#cloud-config\n" + json.dumps(config, indent=1)


def status_text(rg: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        script = pathlib.Path(tmp) / "status.sh"
        script.write_text(STATUS_SCRIPT, encoding="utf-8", newline="\n")
        out = az("vm", "run-command", "invoke", "-g", rg, "-n", VM, "--command-id", "RunShellScript",
                 "--scripts", f"@{script}", "-o", "json")
    message = json.loads(out)["value"][0]["message"].replace("\r\n", "\n")
    stdout = message.split("[stdout]\n", 1)[-1].split("[stderr]", 1)[0].strip()
    # Run Command retains only the last 4 KiB, so the state must follow the bounded log.
    log, marker, state = stdout.rpartition("P360_STATE=")
    state = state.strip()
    if not marker or state not in ("STARTING", "RUNNING", "DONE", "FAILED"):
        raise SystemExit("Could not read the loader state. The VM is retained; inspect it with `status` or Azure Run Command.")
    return f"{state}\n{log.strip()}".strip()


def up(args: argparse.Namespace) -> None:
    cfg = fabric_config.load()
    rg = args.resource_group
    if az("group", "exists", "-n", rg) == "true":
        raise SystemExit(f"Resource group {rg} already exists: check it with `status`, or remove it with `down`.")
    repo = args.repo or git("remote", "get-url", "origin")
    invalid_repo = "Pass a public HTTPS repository URL without credentials, query parameters or fragments."
    try:
        url = urlsplit(repo)
    except ValueError:
        raise SystemExit(invalid_repo) from None
    if url.scheme != "https" or not url.hostname or url.username is not None or url.query or url.fragment:
        raise SystemExit(invalid_repo)
    ref = args.ref or git("rev-parse", "HEAD")
    run_args = [a for a in args.run_all_args if a != "--"]
    print(f"The VM will run {repo} at {ref} (pushed commits only), with run_all.py {' '.join(run_args)}".rstrip())
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072).public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()

    az("group", "create", "-n", rg, "-l", args.location, "--tags", f"{TAG_KEY}={TAG_VALUE}",
       f"{WORKSPACE_TAG}={cfg['WORKSPACE_ID']}", f"{PRINCIPAL_TAG}={PENDING}", "-o", "none")
    print(f"Created resource group {rg}. If a later step fails, remove it with `down`.")
    with tempfile.TemporaryDirectory() as tmp:
        init = pathlib.Path(tmp) / "cloud-init.yaml"
        init.write_text(cloud_config(repo, ref, "".join(f"{k}={cfg[k]}\n" for k in fabric_config.KEYS if k in cfg), run_args),
                        encoding="utf-8", newline="\n")
        az("vm", "create", "-g", rg, "-n", VM, "--image", "Ubuntu2404", "--size", args.size,
           "--os-disk-size-gb", "256", "--assign-identity", "--nsg-rule", "NONE", "--public-ip-sku", "Standard",
           "--admin-username", "azureuser", "--ssh-key-values", key, "--custom-data", str(init),
           "--tags", f"{TAG_KEY}={TAG_VALUE}", "-o", "none")
    stop_at = datetime.datetime.now(datetime.timezone.utc) + SHUTDOWN_AFTER
    az("vm", "auto-shutdown", "-g", rg, "-n", VM, "--time", stop_at.strftime("%H%M"), "-o", "none")
    principal = az("vm", "show", "-g", rg, "-n", VM, "--query", "identity.principalId", "-o", "tsv")
    if not principal:
        raise SystemExit("The VM has no managed identity. No Fabric grant was attempted; remove the group with `down`.")
    # Persist cleanup context before a grant can succeed, even if its response is lost.
    az("group", "update", "-n", rg, "--set", f"tags.{PRINCIPAL_TAG}={principal}", "-o", "none")
    Fabric().post(f"/workspaces/{cfg['WORKSPACE_ID']}/roleAssignments",
                  {"principal": {"id": principal, "type": "ServicePrincipal"}, "role": "Contributor"})
    print(f"VM {VM} is loading; it shuts down at {stop_at:%H:%M} UTC if still running.")
    wait(rg)


def wait(rg: str) -> None:
    print("Waiting for the run. Ctrl+C stops waiting; the VM carries on (`status`, `down`).")
    deadline = time.monotonic() + SHUTDOWN_AFTER.total_seconds()
    try:
        while time.monotonic() < deadline:
            time.sleep(120)
            lines = status_text(rg).splitlines() or ["starting"]
            print(f"{time.strftime('%H:%M')}  {lines[0]}  {lines[-1] if len(lines) > 1 else ''}", flush=True)
            if lines[0] == "DONE":
                down(rg)
                return
            if lines[0] == "FAILED":
                print("\n".join(lines))
                raise SystemExit("The run failed. The VM is kept for `status`; remove it with `down`, then run `up` again.")
        raise SystemExit("Timed out waiting for the loader. Check `status` and use `down` to remove its resources.")
    except KeyboardInterrupt:
        raise SystemExit("Stopped waiting. The VM is still running: check it with `status`, remove it with `down`.")


def down(rg: str) -> None:
    tags = json.loads(az("group", "show", "-n", rg, "--query", "tags", "-o", "json") or "null") or {}
    if tags.get(TAG_KEY) != TAG_VALUE:
        raise SystemExit(f"Resource group {rg} was not created by cloud_load.py; leaving it alone.")
    ws, principal = tags.get(WORKSPACE_TAG), tags.get(PRINCIPAL_TAG)
    if not ws or not principal:
        raise SystemExit(f"Resource group {rg} is missing cleanup metadata; leaving it intact for manual inspection.")
    if principal != PENDING:
        fab = Fabric()
        path = f"/workspaces/{ws}/roleAssignments"
        assignments = [a for a in fab.get_all(path) if a["principal"]["id"].lower() == principal.lower()]
        for assignment in assignments:
            r = requests.delete(f"{API}{path}/{assignment['id']}", headers=fab._headers(), timeout=60)
            if r.status_code not in (200, 404):
                raise SystemExit(f"Could not remove the VM's workspace role ({r.status_code}): {r.text}. "
                                 f"Keeping resource group {rg}; retry `down` after resolving the error.")
        if any(a["principal"]["id"].lower() == principal.lower() for a in fab.get_all(path)):
            raise SystemExit(f"The VM still has a workspace role; keeping resource group {rg}. Retry `down`.")
    az("group", "delete", "-n", rg, "--yes", "-o", "none")
    print(f"Removed resource group {rg}" + (" after verifying the VM's workspace roles are absent."
                                         if principal != PENDING else "; no Fabric grant was attempted."))


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--resource-group", "-g", default="p360-loader")
    ap = argparse.ArgumentParser(description="Run README steps 5 and 6 on a temporary Azure VM.")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("up", parents=[common], help="create the VM, start the run and wait for it")
    p.add_argument("--location", "-l", required=True, help="Azure region; your Fabric capacity's region is best")
    p.add_argument("--size", default="Standard_D16s_v5", help="x64 VM size (default: 16 vCPUs; needs quota in the region)")
    p.add_argument("--repo", help="public git URL the VM clones (default: the origin remote)")
    p.add_argument("--ref", help="commit or branch to run (default: the current commit)")
    p.add_argument("run_all_args", nargs=argparse.REMAINDER, help="after --: options for pipeline/run_all.py")
    sub.add_parser("status", parents=[common], help="show the run's state and the end of its log")
    sub.add_parser("down", parents=[common], help="remove the VM's workspace role and the resource group")
    args = ap.parse_args()
    if args.command == "up":
        up(args)
    elif args.command == "status":
        print(status_text(args.resource_group))
    else:
        down(args.resource_group)


if __name__ == "__main__":
    main()
