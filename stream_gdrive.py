#!/usr/bin/env python3
"""Stream helper: upload one file to Google Drive, delete local on success.
Used as --stream-daemon 'python3 stream_gdrive.py --daemon' to keep VM disk lean.
Structure: mitene-backup/YYYY/MM/<filename>  (YYYY-MM parsed from file path, e.g. out/2021-10/...)
Folder ids cached in .stream_folder_id (JSON: {root, yyyy, mm}).

Dedupe criteria: name + size (+ md5 when GDrive has it). A file is skipped
only when name AND size AND md5 all match the GDrive copy — same name+size
with a different md5 means content changed and is re-uploaded (loudly)."""
import sys, os, json, re, socket

# ponytail: global socket timeout; a hung GDrive request would otherwise block
# the daemon forever, fill the pipe and wedge the whole download pipeline.
socket.setdefaulttimeout(120)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gdrive_utils import _service, find_or_create, folder_listing, local_md5

PARENT_NAME = "mitene-backup"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stream_folder_id")

def load_cache():
    try:
        return json.load(open(CACHE))
    except Exception:
        return {}

def save_cache(c):
    json.dump(c, open(CACHE, "w"))


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
    name = os.path.basename(f)
    local_size = str(os.path.getsize(f))
    listing = folder_listing(svc, folder_id)
    remote = listing.get(name)
    if remote is not None:
        if remote["size"] == local_size:
            # same name + same size: confirm with md5 when GDrive exposes it
            if remote["md5"]:
                local_digest = local_md5(f)
                if remote["md5"] == local_digest:
                    os.unlink(f)
                    return True, f"{name} (exists, skipped)"
                # same name+size but DIFFERENT content: local is authoritative
                # (fresh from album) — re-upload so the good copy wins; the
                # stale GDrive twin is flagged for dedupe_cleanup later.
                from googleapiclient.http import MediaFileUpload
                media = MediaFileUpload(f, resumable=True)
                svc.files().create(body={"name": name, "parents": [folder_id]},
                                   media_body=media, fields="id").execute()
                listing[name] = {"size": local_size, "md5": local_digest}
                os.unlink(f)
                return True, f"{name} (⚠ md5 DIFFERS vs stale gdrive copy {remote['md5'][:8]}, re-uploaded)"
            os.unlink(f)
            return True, f"{name} (exists, skipped size-only)"
        print(f"⚠ {name}: same name, size differs (gdrive={remote['size']} local={local_size}), uploading", flush=True)
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(f, resumable=True)
    svc.files().create(body={"name": name, "parents": [folder_id]},
                       media_body=media, fields="id").execute()
    # update in-process listing cache so a duplicate filename later in the
    # same daemon run is recognized and skipped instead of double-uploaded
    listing[name] = {"size": local_size, "md5": local_md5(f) if remote is None or not remote.get("md5") else remote["md5"]}
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