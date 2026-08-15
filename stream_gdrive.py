#!/usr/bin/env python3
"""Stream helper: upload one file to Google Drive, delete local on success.
Used as --stream-command 'python3 stream_gdrive.py {file}' to keep VM disk lean.
Structure: mitene-backup/YYYY/MM/<filename>  (YYYY-MM parsed from file path, e.g. out/2021-10/...)
Folder ids cached in .stream_folder_id (JSON: {root, yyyy, mm})."""
import sys, os, json, re

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

def main():
    if len(sys.argv) < 2:
        print("usage: stream_gdrive.py <file>", file=sys.stderr); sys.exit(2)
    f = sys.argv[1]
    if not os.path.exists(f):
        print(f"❌ {f} not found", file=sys.stderr); sys.exit(2)

    svc = _service()
    cache = load_cache()

    # root: mitene-backup
    root = cache.get("root")
    if not root:
        root = find_or_create(svc, PARENT_NAME)
        cache["root"] = root
        save_cache(cache)

    # YYYY/MM from path segment like out/2021-10/xxx.jpg
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

    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(f, resumable=True)
    svc.files().create(body={"name": os.path.basename(f), "parents": [folder_id]},
                       media_body=media, fields="id").execute()
    os.unlink(f)
    print(f"✅ streamed {os.path.basename(f)}", flush=True)

if __name__ == "__main__":
    main()