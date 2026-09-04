#!/usr/bin/env python3
"""Backfill 2022/2023/2024 gaps + verify no duplicates on GDrive.
Safe: doesn't touch .year_sync.json state. Uses existing mitene_download.py
(download → stream_gdrive auto-skips existing → verify → clean).
After all years, scans GDrive for duplicate filenames within each month folder.
"""
import json, os, subprocess, sys, tempfile, datetime, time

HOME = os.path.expanduser("~")
MITENE_DIR = os.path.join(HOME, "mitene_download")
VENV_PY = "/home/ubuntu/.hermes/hermes-agent/venv/bin/python3"
LOCK = "/tmp/mitene_sync.lock"

def load_env():
    env = {}
    for line in open(os.path.join(MITENE_DIR, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env

ENV = load_env()
URL = ENV["MITENE_URL"]
PWD = ENV["MITENE_PASSWORD"]

TARGET_YEARS = [2022, 2023, 2024]

# --- GDrive helpers (from sync_year.py) ---
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

def gdrive_month_files(svc, month_folder_id):
    """Return {name: id} for all files in a gdrive folder (non-recursive)."""
    result, page = {}, None
    while True:
        r = svc.files().list(q=f"'{month_folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'",
                             fields="nextPageToken,files(name,id,size)",
                             pageSize=1000, pageToken=page).execute()
        for f in r.get("files", []):
            result.setdefault(f["name"], []).append(f["id"])
        page = r.get("nextPageToken")
        if not page:
            break
    return result

def count_duplicates(gdrive_files):
    """Return {name: [ids]} for files with same name appearing >1 time."""
    return {n: ids for n, ids in gdrive_files.items() if len(ids) > 1}

# --- Core ---
def run_month(month, pwd_file):
    """Download month → upload all → verify local empty."""
    dl = subprocess.call(["timeout", "-k", "30", "20h", VENV_PY,
                          os.path.join(MITENE_DIR, "mitene_download.py"), URL,
                          "--months", month, "--password-file", pwd_file,
                          "--cooldown", "0.4", "--verbose"], cwd=MITENE_DIR)
    if dl != 0:
        print(f"  {month} download rc={dl}", flush=True)
        return False

    month_dir = os.path.join(MITENE_DIR, "out", month)
    files = sorted(os.path.join(month_dir, f) for f in os.listdir(month_dir)
                   if os.path.isfile(os.path.join(month_dir, f))) if os.path.isdir(month_dir) else []

    if files:
        up = subprocess.run([VENV_PY, os.path.join(MITENE_DIR, "stream_gdrive.py"), "--daemon"],
                            input="\n".join(files), text=True, capture_output=True, cwd=MITENE_DIR)
        print(f"  {month} uploaded {len(files)} files", flush=True)
        if up.stderr.strip():
            print(f"   stderr: {up.stderr.strip()[:200]}", flush=True)

    remaining = [f for f in os.listdir(month_dir)
                 if os.path.isfile(os.path.join(month_dir, f))] if os.path.isdir(month_dir) else []
    if remaining:
        print(f"  {month} VERIFY FAIL: {len(remaining)} files still local", flush=True)
        return False
    print(f"  {month} OK (uploaded + verified, local clean)", flush=True)
    return True

def main():
    import fcntl
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another sync running, skipping backfill", flush=True)
        return 0

    pwd_file = tempfile.NamedTemporaryFile(delete=False, mode="w")
    pwd_file.write(PWD)
    pwd_file.close()
    os.chmod(pwd_file.name, 0o600)

    now = datetime.datetime.now()
    print(f"=== backfill {TARGET_YEARS} starting {now:%Y-%m-%d %H:%M} ===", flush=True)
    try:
        for year in TARGET_YEARS:
            months = [f"{year}-{m:02d}" for m in range(1, 13)]
            print(f"\n--- {year} ({len(months)} months) ---", flush=True)
            for month in months:
                ok = run_month(month, pwd_file.name)
                if not ok:
                    print(f"  month {month} failed — keeping rest for retry", flush=True)
        print(f"\n=== backfill done {datetime.datetime.now():%Y-%m-%d %H:%M} ===", flush=True)
    finally:
        os.unlink(pwd_file.name)

    # --- Verify: count per year + scan for duplicates ---
    print("\n=== verification ===", flush=True)
    counts = {}
    dupes_total = 0
    try:
        svc = _drive()
        root = _first_id(svc, "name='mitene-backup' and mimeType='application/vnd.google-apps.folder' and trashed=false")
        if not root:
            print("ERROR: mitene-backup folder not found", flush=True)
            return 1
        for year in TARGET_YEARS:
            yid = _first_id(svc, f"name='{year}' and mimeType='application/vnd.google-apps.folder' and '{root}' in parents and trashed=false")
            if not yid:
                print(f"  {year}: no year folder", flush=True)
                continue
            # count month folders + files + dupes
            year_dupes = {}
            month_folders, page = [], None
            while True:
                r = svc.files().list(q=f"'{yid}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder'",
                                     fields="nextPageToken,files(name,id)", pageSize=100, pageToken=page).execute()
                month_folders.extend(r.get("files", []))
                page = r.get("nextPageToken")
                if not page:
                    break
            total = 0
            for mf in sorted(month_folders, key=lambda x: x["name"]):
                gfiles = gdrive_month_files(svc, mf["id"])
                dups = count_duplicates(gfiles)
                total += len(gfiles)
                if dups:
                    year_dupes[mf["name"]] = {n: len(ids) for n, ids in dups.items()}
            dupes_total += sum(sum(v.values()) for v in year_dupes.values())
            counts[year] = total
            print(f"  {year}: {total} files", flush=True)
            if year_dupes:
                print(f"    ⚠️ DUPLICATES:", flush=True)
                for mn, dn in sorted(year_dupes.items()):
                    for fn, cnt in sorted(dn.items()):
                        print(f"      {mn}/{fn} x{cnt}", flush=True)
    except Exception as e:
        print(f"verification scan error: {e}", flush=True)

    # Refresh album_counts cache
    cache_path = os.path.join(MITENE_DIR, ".album_counts.json")
    cache = {}
    try:
        cache = json.load(open(cache_path))
    except Exception:
        pass
    for year in TARGET_YEARS:
        if year in counts:
            cache[str(year)] = {"album": cache.get(str(year), {}).get("album", 0),
                                "gdrive": counts[year],
                                "updated": datetime.date.today().isoformat()}
    json.dump(cache, open(cache_path, "w"), indent=1)
    print(f"\ncounts: {counts}", flush=True)
    print(f"duplicates found: {dupes_total}", flush=True)
    print(f"cache updated: {cache_path}", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
