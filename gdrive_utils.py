#!/usr/bin/env python3
"""Shared Google Drive helpers for mitene backup scripts.

Consolidates credential loading, service building, folder operations,
and file listing (with size + md5) that were previously duplicated across
stream_gdrive.py, sync_year.py, backfill_years.py, count_gdrive.py,
check_years.py, diag_full.py, dedupe_cleanup.py.
"""
import json, os

HOME = os.path.expanduser("~")
MITENE_DIR = os.path.join(HOME, "mitene_download")
CACHE_PATH = os.path.join(MITENE_DIR, ".stream_folder_id")
ALBUM_COUNTS = os.path.join(MITENE_DIR, ".album_counts.json")

# ---------------------------------------------------------------------------
# Credentials & service
# ---------------------------------------------------------------------------

def _service():
    """Build a Google Drive v3 service from ~/.hermes/google_token.json."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    tok = json.load(open(os.path.join(HOME, ".hermes/google_token.json")))
    creds = Credentials(
        token=tok.get("token"), refresh_token=tok.get("refresh_token"),
        client_id=tok.get("client_id"), client_secret=tok.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

# Re-export with legacy names so existing callers keep working
_drive = _service

# ---------------------------------------------------------------------------
# Folder lookup & creation
# ---------------------------------------------------------------------------

def first_id(svc, q):
    """Return the id of the first file matching query q, or None."""
    r = svc.files().list(q=q, pageSize=1, fields="files(id)").execute()
    return r["files"][0]["id"] if r.get("files") else None

# Legacy alias
_first_id = first_id

FOLDER_MIME = "application/vnd.google-apps.folder"

def find_or_create(svc, name, parent=None, mime=FOLDER_MIME):
    """Find an existing folder (or file) by name, or create it."""
    q = f"name='{name}' and mimeType='{mime}' and trashed=false"
    if parent:
        q += f" and '{parent}' in parents"
    r = svc.files().list(q=q, pageSize=1, fields="files(id)").execute()
    if r.get("files"):
        return r["files"][0]["id"]
    body = {"name": name, "mimeType": mime}
    if parent:
        body["parents"] = [parent]
    return svc.files().create(body=body, fields="id").execute()["id"]

# ---------------------------------------------------------------------------
# Folder content listing  (name -> {size, md5})
# ---------------------------------------------------------------------------

_LISTING_CACHE = {}

def folder_listing(svc, folder_id):
    """Return {name: {size: str, md5: str|None}} for all files in a folder.

    Cached per process to avoid repeated API calls during a single run.
    Includes md5Checksum (free with files.list) so callers can verify
    content identity without extra API calls.
    """
    if folder_id in _LISTING_CACHE:
        return _LISTING_CACHE[folder_id]
    listing = {}
    page = None
    seen = set()
    for _ in range(50):  # hard cap: repeated nextPageToken → infinite loop guard
        r = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken,files(name,size,md5Checksum)",
            pageSize=1000, pageToken=page,
        ).execute()
        for f in r.get("files", []):
            listing[f["name"]] = {
                "size": f.get("size"),
                "md5": f.get("md5Checksum"),
            }
        page = r.get("nextPageToken")
        if not page or page in seen:
            break
        seen.add(page)
    _LISTING_CACHE[folder_id] = listing
    return listing

def folder_file_names(svc, folder_id):
    """Return set of filenames in a folder (non-recursive, non-folder)."""
    names, page = set(), None
    while True:
        r = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken,files(name,mimeType)", pageSize=1000, pageToken=page,
        ).execute()
        for f in r.get("files", []):
            if f.get("mimeType") != FOLDER_MIME:
                names.add(f["name"])
        page = r.get("nextPageToken")
        if not page:
            break
    return names

# ---------------------------------------------------------------------------
# Recursive file counting
# ---------------------------------------------------------------------------

def count_folder(svc, folder_id):
    """Count all non-folder files under folder_id (recursive, paginated)."""
    total = 0
    page = None
    while True:
        r = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken,files(id,mimeType)", pageSize=1000, pageToken=page,
        ).execute()
        for f in r.get("files", []):
            if f.get("mimeType") == FOLDER_MIME:
                total += count_folder(svc, f["id"])
            else:
                total += 1
        page = r.get("nextPageToken")
        if not page:
            break
    return total

# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------

def local_md5(path, chunk=1024 * 1024):
    """Compute MD5 hex digest of a local file (1 MB chunks)."""
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        while data := f.read(chunk):
            h.update(data)
    return h.hexdigest()

# ---------------------------------------------------------------------------
# Album counts cache
# ---------------------------------------------------------------------------

def load_album_counts():
    try:
        return json.load(open(ALBUM_COUNTS))
    except Exception:
        return {}

def save_album_counts(cache):
    json.dump(cache, open(ALBUM_COUNTS, "w"), indent=1)
