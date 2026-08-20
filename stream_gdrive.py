#!/usr/bin/env python3
"""Stream helper: upload one file to Google Drive, delete local on success.
Used as --stream-command 'python3 stream_gdrive.py {file}' to keep VM disk lean.
Structure: mitene-backup/YYYY/MM/<filename>  (YYYY-MM parsed from file path, e.g. out/2021-10/...)
Folder ids cached in .stream_folder_id (JSON: {root, yyyy, mm})."""
import sys, os, json, re, socket

# ponytail: global socket timeout; a hung GDrive request would otherwise block
# the daemon forever, fill the pipe and wedge the whole download pipeline.
socket.setdefaulttimeout(120)

def _service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    tok = json.load(open(os.path.expanduser("~/.hermes/google_token.json")))
    creds = Credentials(token=tok.get("token"), refresh_token=tok.get("refresh_token"),
                        client_id=tok.get("client_id"), client_secret=tok.get("client_secret"),
                        token_uri="https://oauth2.googleapis.com/token")
    return build("drive", "v3", credentials=creds, cache_discovery=False)

PARENT_NAME = "mitene-backup"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stream_folder_id")

def load_cache():
    try:
        return json.load(open(CACHE))
    except Exception:
        return {}

def save_cache(c):
    json.dump(c, open(CACHE, "w"))

def find_or_create(svc, name, parent=None, mime="application/vnd.google-apps.folder"):
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

def _folder_listing(svc, folder_id):
    """name -> size map for one GDrive folder (paginated). Cached per process."""
    if folder_id in _LISTING_CACHE:
        return _LISTING_CACHE[folder_id]
    listing = {}
    page = None
    seen = set()
    for _ in range(50):  # ponytail: hard cap; a repeated pageToken would otherwise loop forever
        req = svc.files().list(q=f"'{folder_id}' in parents and trashed=false",
                               fields="nextPageToken,files(name,size)", pageSize=1000,
                               pageToken=page)
        r = req.execute()
        for f in r.get("files", []):
            listing[f["name"]] = f.get("size")
        page = r.get("nextPageToken")
        if not page or page in seen:
            break
        seen.add(page)
    _LISTING_CACHE[folder_id] = listing
    return listing


_LISTING_CACHE = {}


def upload_one(svc, f):
    """Upload a single file, return (ok, msg)."""
    if not os.path.exists(f):
        return False, f"not found: {f}"
    cache = load_cache()
    root = cache.get("root")
    if not root:
        root = find_or_create(svc, PARENT_NAME)
        cache["root"] = root
        save_cache(cache)
    m = re.search(r"/(\d{4})-(\d{2})/", f)
    if m:
        y, mo = m.group(1), m.group(2)
        y_id = cache.get(y)
        if not y_id:
            y_id = find_or_create(svc, y, root)
            cache[y] = y_id
            save_cache(cache)
        m_id = cache.get(f"{y}-{mo}")
        if not m_id:
            m_id = find_or_create(svc, mo, y_id)
            cache[f"{y}-{mo}"] = m_id
            save_cache(cache)
        folder_id = m_id
    else:
        folder_id = root
    # dedupe: same name + same size already on GDrive -> safe to drop local copy
    listing = _folder_listing(svc, folder_id)
    name = os.path.basename(f)
    local_size = str(os.path.getsize(f))
    if listing.get(name) == local_size:
        os.unlink(f)
        return True, f"{name} (exists, skipped)"
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(f, resumable=True)
    svc.files().create(body={"name": name, "parents": [folder_id]},
                       media_body=media, fields="id").execute()
    os.unlink(f)
    return True, name

def main():
    if len(sys.argv) < 2:
        print("usage: stream_gdrive.py <file> | stream_gdrive.py --daemon", file=sys.stderr); sys.exit(2)
    if "--daemon" in sys.argv:
        # long-lived uploader: one OAuth service, reads file paths from stdin, one per line.
        svc = _service()
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            f = line.strip()
            if not f:
                continue
            try:
                ok, msg = upload_one(svc, f)
                print(f"✅ streamed {msg}" if ok else f"❌ {msg}", flush=True)
            except Exception as e:
                print(f"❌ {f}: {e}", flush=True)
        return
    f = sys.argv[1]
    svc = _service()
    ok, msg = upload_one(svc, f)
    print(f"✅ streamed {msg}" if ok else f"❌ {msg}", flush=True)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()