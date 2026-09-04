#!/usr/bin/env python3
"""Quick GDrive file counts per year (recursive) — no album scan."""
import json, os, sys

def _drive():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    tok = json.load(open(os.path.expanduser('~/.hermes/google_token.json')))
    creds = Credentials(token=tok.get("token"), refresh_token=tok.get("refresh_token"),
                        client_id=tok.get("client_id"), client_secret=tok.get("client_secret"),
                        token_uri="https://oauth2.googleapis.com/token")
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def _first_id(svc, q):
    r = svc.files().list(q=q, pageSize=1, fields="files(id)").execute()
    return r["files"][0]["id"] if r.get("files") else None

def count_folder(svc, fid):
    total, page = 0, None
    while True:
        r = svc.files().list(q=f"'{fid}' in parents and trashed=false",
                             fields="nextPageToken,files(id,mimeType)", pageSize=1000, pageToken=page).execute()
        for f in r.get("files", []):
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                total += count_folder(svc, f["id"])
            else:
                total += 1
        page = r.get("nextPageToken")
        if not page:
            break
    return total

svc = _drive()
root = _first_id(svc, "name='mitene-backup' and mimeType='application/vnd.google-apps.folder' and trashed=false")
ALBUM = {2021: 866, 2022: 8021, 2023: 10749, 2024: 10785, 2025: 6995, 2026: 4750}
for y in range(2021, 2027):
    yid = _first_id(svc, f"name='{y}' and mimeType='application/vnd.google-apps.folder' and '{root}' in parents and trashed=false")
    n = count_folder(svc, yid) if yid else 0
    a = ALBUM.get(y, 0)
    print(f"{y}: gdrive={n} album={a} gap={a-n}")
