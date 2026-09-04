#!/usr/bin/env python3
"""One-time dedupe cleanup for mitene-backup: delete exact-name duplicates.

Groups files by (folder_id, name); keeps one per group, deletes the rest ONLY
when md5Checksum matches the kept file (or all sizes match when md5 is absent).
After cleanup, invalidates .album_counts.json entries for touched years so the
sync runner re-probes instead of trusting stale "complete" counts.

Usage: python3 dedupe_cleanup.py [--commit]   (default = dry run)
"""
import json, os, sys, time, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gdrive_utils import _service, first_id

COMMIT = "--commit" in sys.argv
MITENE_DIR = os.path.expanduser("~/mitene_download")
CACHE = os.path.join(MITENE_DIR, ".album_counts.json")

def walk_folders(svc, fid, depth=0):
    """Yield every folder id under fid (including fid itself)."""
    yield fid
    page = None
    while True:
        r = svc.files().list(q=f"'{fid}' in parents and trashed=false",
                             fields="nextPageToken,files(id,mimeType)", pageSize=1000,
                             pageToken=page).execute()
        for f in r.get("files", []):
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                yield from walk_folders(svc, f["id"], depth + 1)
        page = r.get("nextPageToken")
        if not page:
            break

def list_files(svc, fid):
    out = []
    page = None
    while True:
        r = svc.files().list(q=f"'{fid}' in parents and trashed=false",
                             fields="nextPageToken,files(id,name,md5Checksum,size)",
                             pageSize=1000, pageToken=page).execute()
        out += r.get("files", [])
        page = r.get("nextPageToken")
        if not page:
            break
    return out

def main():
    svc = _service()
    root = first_id(svc, "name='mitene-backup' and mimeType='application/vnd.google-apps.folder' and trashed=false")
    if not root:
        print("mitene-backup not found"); return 1
    folders = list(walk_folders(svc, root))
    print(f"folders: {len(folders)}")
    groups = 0
    to_delete = []      # (file_id, name, size)
    skipped_groups = []
    touched_years = set()
    for fid in folders:
        files = list_files(svc, fid)
        by_name = {}
        for f in files:
            by_name.setdefault(f["name"], []).append(f)
        for name, fs in by_name.items():
            if len(fs) < 2:
                continue
            groups += 1
            keep = fs[0]
            md5s = {f.get("md5Checksum") for f in fs}
            ok = None
            if keep.get("md5Checksum") and len(md5s) == 1:
                ok = True
            elif not any(f.get("md5Checksum") for f in fs):
                sizes = {f.get("size") for f in fs}
                ok = len(sizes) == 1
            if not ok:
                skipped_groups.append((name, [f.get("md5Checksum") for f in fs]))
                continue
            for f in fs[1:]:
                to_delete.append((f["id"], name, int(f.get("size") or 0)))
    total_size = sum(s for _, _, s in to_delete)
    print(f"groups with dupes: {groups} | files to delete: {len(to_delete)} | ~{total_size/1e9:.2f} GB")
    if skipped_groups:
        print(f"SKIPPED (md5 mismatch within same name): {len(skipped_groups)}")
        for name, md5s in skipped_groups[:5]:
            print("  ", name, md5s)
    if not COMMIT:
        print("DRY RUN - no changes. Re-run with --commit to delete.")
        return 0
    # commit
    deleted = 0
    for i, (fid, name, _) in enumerate(to_delete):
        try:
            svc.files().delete(fileId=fid).execute()
            deleted += 1
            if i % 200 == 0:
                print(f"  deleted {deleted}/{len(to_delete)}", flush=True)
                time.sleep(1)
        except Exception as e:
            print(f"  FAIL {name}: {e}", flush=True)
        time.sleep(0.05)
    # invalidate affected years in cache (year = first 4 chars of folder path unknown here;
    # simply drop entries whose gdrive count will now be stale: all years)
    cache = {}
    try:
        cache = json.load(open(CACHE))
    except Exception:
        pass
    for y in list(cache.keys()):
        cache[y]["updated"] = "2000-01-01"
    json.dump(cache, open(CACHE, "w"), indent=1)
    print(f"done: deleted {deleted} duplicates; cache invalidated (runner will re-probe)")
    return 0

if __name__ == "__main__":
    sys.exit(main())