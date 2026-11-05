#!/bin/bash
# Runs on the loader VM that scripts/cloud_load.py creates: clones the repo and runs pipeline/run_all.py with the
# VM's managed identity. cloud-init has installed git, Python, Java, the Azure CLI and AzCopy, and written fabric.env.
# Usage: cloud_load.sh <git url> <git ref> [pipeline/run_all.py options]
set -euo pipefail
export HOME=/root
trap 'if [ $? -eq 0 ]; then echo DONE; else echo FAILED; fi > /var/lib/p360/state' EXIT
echo RUNNING > /var/lib/p360/state
exec >> /var/log/p360-loader.log 2>&1

git clone -q "$1" /opt/p360
cd /opt/p360
git checkout -q "$2"
shift 2
cp /var/lib/p360/fabric.env fabric.env
python3 -m venv .venv
.venv/bin/pip install -q -r pipeline/requirements.txt

# The identity endpoint can take a moment after boot.
for _ in 1 2 3 4 5; do az login --identity --allow-no-subscriptions -o none && break; sleep 30; done
az account show -o none
.venv/bin/python -u pipeline/run_all.py "$@"
