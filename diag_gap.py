#!/usr/bin/env python3
"""Diagnose mitene backfill gap: which album files are missing on GDrive.
Compares dry-run file list (album) vs gdrive month folder listing.
Usage: python3 diag_gap.py 2024
"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

HOME = os.path.expanduser("~")
MITENE_DIR = os.path.join(HOME, "mitene_download")
VENV_PY = "/home/ubuntu/.hermes/hermes-agent/venv/bin/python3"

def load_env():
    env = {}
    for line in open(os.path.join(MITENE_DIR, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env

def _drive():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    tok = json.load(open(os.path.join(HOME, ".hermes/google_token.json")))
    creds = Credentials(token=tok.get("token"), refresh_token=tok.get("refresh_token"),
                        client_id=tok.get("client_id"), client_secret=tok.get("client_secret"),
                        token_uri="https://oauth2.googleapis.com/token")
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def _first_id(svc, q):
    r = svc.files().list(q=q, pageSize=1, fields="files(id)").execute()
    return r["files"][0]["id"] if r.get("files") else None

def gdrive_month_names(svc, month_folder_id):
    names, page = set(), None
    while True:
        r = svc.files().list(q=f"'{month_folder_id}' in parents and trashed=false",
                             fields="nextPageToken,files(name)", pageSize=1000, pageToken=page).execute()
        for f in r.get("files", []):
            if f.get("mimeType") != "application/vnd.google-apps.folder":
                names.add(f["name"])
        page = r.get("nextPageToken")
        if not page:
            break
    return names

def main():
    year = sys.argv[1]
    env = load_env()
    url, pwd = env["MITENE_URL"], env["MITENE_PASSWORD"]
    pf = tempfile.NamedTemporaryFile(delete=False, mode="w")
    pf.write(pwd); pf.close(); os.chmod(pf.name, 0o600)

    months = ",".join(f"{year}-{m:02d}" for m in range(1, 13))
    # verbose run: parse "Downloading <id> ⏳" or final names? Use dry-run + download listing.
    # dry-run prints 待下載 count only. We need names: run a normal listing via --verbose dry? 
    # Use the downloader in dry-run verbose mode to capture media titles.
    out = subprocess.run([VENV_PY, os.path.join(MITENE_DIR, "mitene_download.py"), url,
                          "--months", months, "--dry-run", "--password-file", pf.name, "--verbose"],
                         capture_output=True, text=True, cwd=MITENE_DIR, timeout=1200)
    os.unlink(pf.name)
    # dry-run verbose may not print names; fall back to comparing counts per month via GDrive only.
    print(f"dry-run stdout tail:\n{out.stdout[-400:]}")

    # GDrive per-month counts
    svc = _drive()
    root = _first_id(svc, "name='mitene-backup' and mimeType='application/vnd.google-apps.folder' and trashed=false")
    yid = _first_id(svc, f"name='{year}' and mimeType='application/vnd.google-apps.folder' and '{root}' in parents and trashed=false")
    if not yid:
        print(f"no year folder {year}")
        return
    months_folders, page = {}, None
    while True:
        r = svc.files().list(q=f"'{yid}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder'",
                             fields="nextPageToken,files(name,id)", pageSize=100, pageToken=page).execute()
        for f in r.get("files", []):
            months_folders[f["name"]] = f["id"]
        page = r.get("nextPageToken")
        if not page:
            break
    total = 0
    for m in sorted(months_folders):
        n = len(gdrive_month_names(svc, months_folders[m]))
        total += n
        print(f"  {m}: {n}")
    print(f"  {year} TOTAL on gdrive: {total}")

if __name__ == "__main__":
    main()
