#!/bin/bash
# Double-click this file to sync data and launch the Review GUI.

cd "$(dirname "$0")"

echo "=== Job Search Toolkit ==="

echo ""
echo "[1/3] Pulling latest data from the cloud..."
git pull

echo ""
echo "[2/3] Syncing jobs_index.csv..."
python3 scripts/sync_index.py 2>/dev/null || python3 scripts/sync_index.py --rebuild

echo ""
echo "[3/3] Launching the Review GUI..."
echo "      Once the browser opens, visit http://localhost:8501"
echo "      Close this window to stop the service"
echo ""
streamlit run scripts/review_gui.py
