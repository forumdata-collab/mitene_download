#!/usr/bin/env python3
"""Half-year-batched mitene sync with per-month download->upload->verify (v0.9.3).

Day pacing: one 6-month half per day (2021 H1 -> H2 -> 2022 H1 ... 2026 H2).
Inside a half, each month runs as: download month -> upload all files ->
verify local empty (daemon unlinks only on success, so leftovers stay on disk)
-> delete local -> next month. State tracks finished months, so an interrupted
half resumes without re-downloading completed months. Complete years are
skipped via .album_counts.json. flock guards against concurrent check/sync.
"""
import json, os, subprocess, sys, datetime, tempfile, re, fcntl

HOME = os.path.expanduser("~")
MITENE_DIR = os.path.join(HOME, "mitene_download")
VENV_PY = "/home/ubuntu/.hermes/hermes-agent/venv/bin/python3"
STATE = os.path.join(MITENE_DIR, ".year_sync.json")
CACHE = os.path.join(MITENE_DIR, ".album_counts.json")
LOCK = "/tmp/mitene_sync.lock"
LOG = os.path.join(MITENE_DIR, "logs")
if not os.path.isdir(LOG):
    os.makedirs(LOG)

def load_counts():
    try:
        return json.load(open(CACHE))
    except Exception:
        return {}

def load_env():
    env = {}
    for line in open(os.path.join(MITENE_DIR, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env

ENV = load_env()
URL = ENV.get("MITENE_URL", "")
PWD = ENV.get("MITENE_PASSWORD", "")

def load_state():
    try:
        s = json.load(open(STATE))
        if "year" in s and "half" not in s:  # migrate old hour-budget state
            s["half"] = (s["year"] - 2021) * 2 + 1
        return s
    except Exception:
        return {"day": datetime.date.today().isoformat(), "half": 1}

def save_state(s):
    json.dump(s, open(STATE, "w"))

def half_to_year(half):
    return 2021 + (half - 1) // 2

def months_for_half(half):
    year = half_to_year(half)
    h = 1 + (half - 1) % 2
    start = 1 if h == 1 else 7
    return ",".join(f"{year}-{m:02d}" for m in range(start, start + 6))

def months_for_year(year):
    return ",".join(f"{year}-{m:02d}" for m in range(1, 13))

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

def _count_folder(svc, fid):
    """Count all non-folder files under a folder (recursive, paginated)."""
    total = 0
    page = None
    while True:
        r = svc.files().list(q=f"'{fid}' in parents and trashed=false",
                             fields="nextPageToken,files(id,mimeType)", pageSize=1000,
                             pageToken=page).execute()
        for f in r.get("files", []):
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                total += _count_folder(svc, f["id"])
            else:
                total += 1
        page = r.get("nextPageToken")
        if not page:
            break
    return total


def gdrive_year_count(year):
    """Total files under mitene-backup/<year> (recursive, paginated)."""
    svc = _drive()
    root = _first_id(svc, "name='mitene-backup' and mimeType='application/vnd.google-apps.folder' and trashed=false")
    if not root:
        return 0
    yid = _first_id(svc, f"name='{year}' and mimeType='application/vnd.google-apps.folder' and '{root}' in parents and trashed=false")
    if not yid:
        return 0
    return _count_folder(svc, yid)

def album_year_count(year, pwd_path):
    """Dry-run count of a year (pending downloads when local is empty)."""
    out = subprocess.run([VENV_PY, os.path.join(MITENE_DIR, "mitene_download.py"), URL,
                          "--months", months_for_year(year), "--dry-run",
                          "--password-file", pwd_path],
                         capture_output=True, text=True, cwd=MITENE_DIR, timeout=1200)
    m = re.search(r"待下載: (\d+)", out.stdout)
    return int(m.group(1)) if m else -1

def run_month(month, pwd_path):
    """download month -> upload all files -> verify local empty (nothing lost).
    Returns True only when every file made it to GDrive (daemon unlinks on success,
    so leftovers = failures, which are KEPT on disk, never deleted)."""
    dl = subprocess.call(["timeout", "-k", "30", "20h", VENV_PY,
                          os.path.join(MITENE_DIR, "mitene_download.py"), URL,
                          "--months", month, "--password-file", pwd_path,
                          "--cooldown", "0.4", "--verbose"], cwd=MITENE_DIR)
    if dl != 0:
        print(f"  {month} download rc={dl}", flush=True)
        return False
    month_dir = os.path.join(MITENE_DIR, "out", month)
    files = []
    if os.path.isdir(month_dir):
        files = sorted(os.path.join(month_dir, f) for f in os.listdir(month_dir)
                       if os.path.isfile(os.path.join(month_dir, f)))
    if files:
        up = subprocess.run([VENV_PY, os.path.join(MITENE_DIR, "stream_gdrive.py"), "--daemon"],
                            input="\n".join(files), text=True, capture_output=True,
                            cwd=MITENE_DIR)
        print(f"  {month} uploaded {len(files)} files", flush=True)
        if up.stderr.strip():
            print("   stderr:", up.stderr.strip()[:200], flush=True)
    remaining = []
    if os.path.isdir(month_dir):
        remaining = [f for f in os.listdir(month_dir)
                     if os.path.isfile(os.path.join(month_dir, f))]
    if remaining:
        print(f"  {month} VERIFY FAIL: {len(remaining)} files still local (kept, NOT deleted)", flush=True)
        return False
    print(f"  {month} OK (uploaded + verified, local clean)", flush=True)
    return True


def main():
    if not URL or not PWD:
        print("missing MITENE_URL/MITENE_PASSWORD in .env", file=sys.stderr)
        return 1
    state = load_state()
    half = state.get("half", 1)

    pwd_file = tempfile.NamedTemporaryFile(delete=False, mode="w")
    pwd_file.write(PWD)
    pwd_file.close()
    os.chmod(pwd_file.name, 0o600)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another sync/check running (lock held), skipping", flush=True)
        os.unlink(pwd_file.name)
        return 0
    counts = load_counts()
    try:
        # advance past complete years (cache first, probe only when stale)
        while half <= 12:
            year = half_to_year(half)
            c = counts.get(str(year))
            if c and c.get("updated", "") >= (datetime.date.today() - datetime.timedelta(days=7)).isoformat() \
                    and c.get("gdrive", 0) >= c.get("album", 0):
                print(f"year {year}: cached complete ({c['gdrive']}/{c['album']}), skipping", flush=True)
                half += 2  # both halves of a complete year
                continue
            album_n = album_year_count(year, pwd_file.name)
            g_n = gdrive_year_count(year)
            if album_n < 0:
                print(f"dry-run parse failed for {year}, aborting", flush=True)
                return 1
            print(f"year {year}: album≈{album_n} GDrive={g_n}", flush=True)
            counts[str(year)] = {"album": album_n, "gdrive": g_n,
                                 "updated": datetime.date.today().isoformat()}
            if g_n >= album_n:
                print(f"  {year} already complete, skipping", flush=True)
                half += 2
                continue
            break
        json.dump(counts, open(CACHE, "w"), indent=1)
        if half > 12:
            print("all halves done (2021-2026 complete)", flush=True)
            return 0
        state["half"] = half
        save_state(state)

        months = months_for_half(half).split(",")
        year = half_to_year(half)
        done = set(state.get("months_done", []))
        header = (f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M} "
                  f"half {half} ({year} H{(half-1)%2+1}, months {months}) ===")
        print(header, flush=True)
        for month in months:
            if month in done:
                print(f"  {month} already done, skip", flush=True)
                continue
            if not run_month(month, pwd_file.name):
                print(f"month {month} failed — {len(done)} done, retry same month tomorrow "
                      f"(local files kept, nothing lost)", flush=True)
                break
            done.add(month)
            state["months_done"] = sorted(done)
            save_state(state)
        else:
            # all 6 months of the half verified clean -> advance
            state["half"] = half + 1
            state.pop("months_done", None)
            save_state(state)
            print(f"half {half} done -> next half {state['half']}", flush=True)
        # refresh cache with live GDrive counts after the run
        counts[str(year)] = {"album": counts.get(str(year), {}).get("album", 0),
                             "gdrive": gdrive_year_count(year),
                             "updated": datetime.date.today().isoformat()}
        json.dump(counts, open(CACHE, "w"), indent=1)
    finally:
        os.unlink(pwd_file.name)
    return 0

if __name__ == "__main__":
    sys.exit(main())