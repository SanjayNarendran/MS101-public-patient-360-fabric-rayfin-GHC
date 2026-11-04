"""Prepare a Microsoft Fabric workspace for this app and record its IDs.

    python scripts/setup_fabric.py --workspace "<workspace name>" [--create] [--capacity-id <id>] [--dry-run]

What it does, using your Azure CLI login (`az login`) and the public Fabric REST API:
  1. Finds the workspace by display name. With --create it creates it if it does not exist.
  2. Makes sure the workspace is on an active Fabric capacity (assigns --capacity-id when it has none).
  3. Finds or creates the four Lakehouses: the three silos (clinical_lakehouse, genomics_lakehouse,
     imaging_lakehouse) and patient360, which will hold only OneLake shortcuts. Waits for patient360's SQL
     analytics endpoint, which the app queries.
  4. Writes the IDs to:
       fabric.env                              read by pipeline/upload_onelake.py
       rayfin/functions/src/function_app.ts    the <fabric-config> block, read by the deployed functions
       .env.production.local                   the workspace link in the app header
     Keep them out of git: fabric.env and *.local are ignored; reset the function block to its zero GUIDs before
     committing.

--dry-run only reads: it reports what it found and what it would create, and writes nothing.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time

import requests
from azure.identity import AzureCliCredential

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = "https://api.fabric.microsoft.com/v1"
FUNCTION_APP = ROOT / "rayfin" / "functions" / "src" / "function_app.ts"
ENV_FILE = ROOT / "fabric.env"
VITE_ENV = ROOT / ".env.production.local"
LAKEHOUSES = {"clinical": "clinical_lakehouse", "genomics": "genomics_lakehouse", "imaging": "imaging_lakehouse",
              "patient360": "patient360"}


class Fabric:
    def __init__(self) -> None:
        self.cred = AzureCliCredential()

    def _headers(self) -> dict:
        token = self.cred.get_token("https://api.fabric.microsoft.com/.default").token
        return {"Authorization": f"Bearer {token}"}

    def get(self, path: str) -> dict:
        r = requests.get(f"{API}{path}", headers=self._headers(), timeout=60)
        r.raise_for_status()
        return r.json()

    def get_all(self, path: str) -> list[dict]:
        items, url = [], f"{API}{path}"
        while url:
            r = requests.get(url, headers=self._headers(), timeout=60)
            r.raise_for_status()
            body = r.json()
            items += body.get("value", [])
            token = body.get("continuationToken")
            url = f"{API}{path}{'&' if '?' in path else '?'}continuationToken={token}" if token else None
        return items

    def post(self, path: str, payload: dict) -> dict:
        r = requests.post(f"{API}{path}", json=payload, headers=self._headers(), timeout=60)
        if r.status_code == 202 and r.headers.get("Location"):
            return self._wait(r.headers["Location"])
        if not r.ok:
            raise SystemExit(f"POST {path} failed ({r.status_code}): {r.text}")
        return r.json() if r.content else {}

    def _wait(self, url: str) -> dict:
        for _ in range(60):
            time.sleep(3)
            r = requests.get(url, headers=self._headers(), timeout=60)
            if r.ok and r.json().get("status") in ("Succeeded", "Failed"):
                status = r.json()
                if status["status"] == "Failed":
                    raise SystemExit(f"Fabric operation failed: {status}")
                result = requests.get(f"{url}/result", headers=self._headers(), timeout=60)
                return result.json() if result.ok and result.content else {}
        raise SystemExit("Timed out waiting for a Fabric operation")


def capacity_list(capacities: dict) -> str:
    rows = [f"  {c['id']}  {c['displayName']}  {c['sku']}  {c['region']}  {c['state']}" for c in capacities.values()]
    return "\n".join(rows) or "  (none visible to you)"


def sql_endpoint(fab: Fabric, ws_id: str, lh_id: str) -> dict:
    """patient360's SQL analytics endpoint, once Fabric has provisioned it (about a minute for a new Lakehouse)."""
    for _ in range(40):
        props = fab.get(f"/workspaces/{ws_id}/lakehouses/{lh_id}").get("properties", {}).get("sqlEndpointProperties") or {}
        if props.get("provisioningStatus") == "Success" and props.get("connectionString"):
            return props
        time.sleep(6)
    raise SystemExit("patient360's SQL analytics endpoint is not ready yet. Wait a minute and run this again.")


def write_config(ws: dict, ids: dict[str, str], sql: dict) -> None:
    ENV_FILE.write_text(
        "# Written by scripts/setup_fabric.py. Not committed (see .gitignore).\n"
        f"WORKSPACE_NAME={ws['displayName']}\nWORKSPACE_ID={ws['id']}\n"
        f"CLINICAL_LH={ids['clinical']}\nGENOMICS_LH={ids['genomics']}\nIMAGING_LH={ids['imaging']}\n"
        f"PATIENT360_LH={ids['patient360']}\nPATIENT360_SQL={sql['id']}\nPATIENT360_SQL_SERVER={sql['connectionString']}\n"
    )
    VITE_ENV.write_text(
        "# Written by scripts/setup_fabric.py: the workspace linked from the app header. Not committed.\n"
        f"VITE_FABRIC_WORKSPACE_ID={ws['id']}\nVITE_FABRIC_WORKSPACE_NAME={ws['displayName']}\n"
    )

    def q(v: str) -> str:
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"

    block = (
        "// <fabric-config>\n"
        f"const WORKSPACE_ID = {q(ws['id'])};\n"
        f"const WORKSPACE_NAME = {q(ws['displayName'])};\n"
        f"const CLINICAL_LH = {q(ids['clinical'])};\n"
        f"const GENOMICS_LH = {q(ids['genomics'])};\n"
        f"const IMAGING_LH = {q(ids['imaging'])};\n"
        f"const PATIENT360_LH = {q(ids['patient360'])};\n"
        f"const SQL_SERVER = {q(sql['connectionString'])};\n"
        "// </fabric-config>"
    )
    src = FUNCTION_APP.read_text()
    new, n = re.subn(r"// <fabric-config>.*?// </fabric-config>", lambda _: block, src, flags=re.S)
    if n != 1:
        raise SystemExit(f"Could not find the <fabric-config> block in {FUNCTION_APP}")
    FUNCTION_APP.write_text(new)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True, help="Fabric workspace display name")
    ap.add_argument("--create", action="store_true", help="create the workspace if it does not exist")
    ap.add_argument("--capacity-id", help="Fabric capacity to assign if the workspace has none")
    ap.add_argument("--dry-run", action="store_true", help="read only; report without creating or writing anything")
    args = ap.parse_args()

    fab = Fabric()
    capacities = {c["id"]: c for c in fab.get_all("/capacities")}
    ws = next((w for w in fab.get_all("/workspaces") if w["displayName"].lower() == args.workspace.lower()), None)

    if ws is None:
        if not args.create:
            raise SystemExit(f'Workspace "{args.workspace}" not found. Re-run with --create to create it.')
        if not args.capacity_id:
            raise SystemExit(f"Creating a workspace needs --capacity-id. Capacities you can use:\n{capacity_list(capacities)}")
        print(f'Workspace "{args.workspace}" not found; {"would create" if args.dry_run else "creating"} it '
              f"on capacity {args.capacity_id}.")
        if args.dry_run:
            return
        ws = fab.post("/workspaces", {"displayName": args.workspace, "capacityId": args.capacity_id})
    print(f"Workspace   {ws['displayName']}  {ws['id']}")

    cap = capacities.get(ws.get("capacityId") or "")
    if cap is None:
        if not args.capacity_id:
            raise SystemExit(f"The workspace has no Fabric capacity. Re-run with --capacity-id. Capacities:\n"
                             f"{capacity_list(capacities)}")
        print(f"Capacity    {'would assign' if args.dry_run else 'assigning'} {args.capacity_id}")
        if not args.dry_run:
            fab.post(f"/workspaces/{ws['id']}/assignToCapacity", {"capacityId": args.capacity_id})
    else:
        print(f"Capacity    {cap['displayName']}  {cap['sku']}  {cap['region']}  {cap['state']}")
        if cap["state"] != "Active":
            raise SystemExit(f"Capacity {cap['displayName']} is {cap['state']} (paused). Resume it in the Azure portal "
                             "(Microsoft Fabric capacity > Resume), wait a minute, then run this again.")

    existing = {x["displayName"]: x for x in fab.get_all(f"/workspaces/{ws['id']}/lakehouses")}
    ids: dict[str, str] = {}
    for key, name in LAKEHOUSES.items():
        lh = existing.get(name)
        if lh is None:
            print(f"Lakehouse   {name} not found; {'would create' if args.dry_run else 'creating'} it")
            if args.dry_run:
                continue
            lh = fab.post(f"/workspaces/{ws['id']}/lakehouses", {"displayName": name})
            if "id" not in lh:
                lh = next(x for x in fab.get_all(f"/workspaces/{ws['id']}/lakehouses") if x["displayName"] == name)
        print(f"Lakehouse   {name}  {lh['id']}")
        ids[key] = lh["id"]

    if args.dry_run:
        print("Dry run: nothing written.")
        return
    sql = sql_endpoint(fab, ws["id"], ids["patient360"])
    print(f"SQL         {sql['connectionString']}")
    write_config(ws, ids, sql)
    print(f"Wrote {ENV_FILE.name}, {VITE_ENV.name} and the config block in {FUNCTION_APP.relative_to(ROOT)}")
    print("Next: download and build the data (README steps 5 and 6), then deploy (step 7).")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        sys.exit(f"Fabric API error: {e.response.status_code} {e.response.text}")
