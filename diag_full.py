#!/usr/bin/env python3
"""THOROUGH gap diagnosis: exact filenames present in album (mitene) but
missing from GDrive, per month. Also lists local out/ leftovers.

How it works:
1. Logs into mitene album, walks ALL pages (time-descending), collects every
   media's tookAt + computed destination filename (same logic as mitene_download.py).
2. For each GDrive month folder under mitene-backup/<year>, lists existing filenames.
3. Diff per month -> files in album but not on GDrive (= the true gap).
4. Reports also which gaps have local out/<month>/ leftovers (retry candidate).
"""
import asyncio, json, os, re, sys, tempfile
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

def gdrive_month_names(svc, folder_id):
    names, page = set(), None
    while True:
        r = svc.files().list(q=f"'{folder_id}' in parents and trashed=false",
                             fields="nextPageToken,files(name,mimeType)", pageSize=1000, pageToken=page).execute()
        for f in r.get("files", []):
            if f.get("mimeType") != "application/vnd.google-apps.folder":
                names.add(f["name"])
        page = r.get("nextPageToken")
        if not page:
            break
    return names

# ---- mitene album scraping (async) ----
import aiohttp

async def fetch_album_filenames(url, password, months_filter=None):
    """Return {month: set(filename)} by walking all album pages (time-desc)."""
    out = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        # ensure_login: same flow as mitene_download.py
        async with s.get(f"{url}?page=1") as r:
            txt = await r.text()
        if "Please enter your password" in txt:
            token = txt.split('name="authenticity_token" value="')[1].split('"')[0]
            await s.post(f"{url}/login",
                         data={"session[password]": password, "authenticity_token": token})
        page = 1
        seen = set()
        oldest_target = min(months_filter) if months_filter else None
        while True:
            async with s.get(f"{url}?page={page}") as r:
                txt = await r.text()
            if "Please enter your password" in txt:
                token = txt.split('name="authenticity_token" value="')[1].split('"')[0]
                await s.post(f"{url}/login",
                             data={"session[password]": password, "authenticity_token": token})
                async with s.get(f"{url}?page={page}") as r:
                    txt = await r.text()
            try:
                pd = json.loads(txt.split(";gon.media=")[1].split(";gon.familyUserIdToColorMap=")[0])
            except Exception as e:
                print(f"parse fail page {page}: {e}", file=sys.stderr)
                break
            files = pd.get("mediaFiles") or []
            if not files:
                break
            # early exit: album is time-descending; once the NEWEST file on this page
            # is older than the oldest target month, nothing further can match
            page_max = max(m["tookAt"][:7] for m in files)
            if months_filter and page_max < oldest_target:
                break
            for m in files:
                uuid = m["uuid"]
                if uuid in seen:
                    continue
                seen.add(uuid)
                month = m["tookAt"][:7]
                if months_filter and month not in months_filter:
                    continue
                src = m.get("expiringVideoUrl") or m.get("expiringUrl") or ""
                base = os.path.basename(src.split("?")[0])
                took = m["tookAt"][:10].replace("-", "")
                fn = f"{took}-{base[:6]}"
                if not os.path.splitext(fn)[1]:
                    import mimetypes
                    ext = mimetypes.guess_extension(m.get("contentType", ""))
                    if ext:
                        fn += ext
                out.setdefault(month, set()).add(fn)
            page += 1
    return out

def main():
    year = sys.argv[1] if len(sys.argv) > 1 else "2024"
    env = load_env()
    url, pwd = env["MITENE_URL"], env["MITENE_PASSWORD"]
    months = {f"{year}-{m:02d}" for m in range(1, 13)}

    print(f"=== scraping album {year} (all pages) ===", flush=True)
    album = asyncio.run(fetch_album_filenames(url, pwd, months_filter=months))
    print(f"album months found: {len(album)}", flush=True)

    print("=== listing GDrive month folders ===", flush=True)
    svc = _drive()
    root = _first_id(svc, "name='mitene-backup' and mimeType='application/vnd.google-apps.folder' and trashed=false")
    yid = _first_id(svc, f"name='{year}' and mimeType='application/vnd.google-apps.folder' and '{root}' in parents and trashed=false")
    if not yid:
        print(f"NO year folder for {year}")
        return
    mf = {}
    page = None
    while True:
        r = svc.files().list(q=f"'{yid}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder'",
                             fields="nextPageToken,files(name,id)", pageSize=100, pageToken=page).execute()
        for f in r.get("files", []):
            mf[f["name"]] = f["id"]
        page = r.get("nextPageToken")
        if not page:
            break
    gdrive = {}
    for mn, fid in mf.items():
        # GDrive month folders are named "01".."12" (no year prefix)
        gdrive[f"{year}-{mn}"] = gdrive_month_names(svc, fid)

    # local leftovers
    out_dir = Path(MITENE_DIR) / "out"
    local = {}
    for d in sorted(out_dir.glob(f"{year}-*")):
        if d.is_dir():
            local[d.name] = {f.name for f in d.iterdir() if f.is_file()}

    print(f"\n=== GAP ANALYSIS {year} ===", flush=True)
    total_gap = 0
    for month in sorted(months):
        a = album.get(month, set())
        g = gdrive.get(month, set())
        l = local.get(month, set())
        missing = a - g
        if missing:
            total_gap += len(missing)
            print(f"\n{month}: album={len(a)} gdrive={len(g)} MISSING={len(missing)}", flush=True)
            for fn in sorted(missing):
                loc = "LOCAL-HAS" if fn in l else "local-nohave"
                print(f"   {fn}  [{loc}]", flush=True)
    print(f"\nTOTAL missing: {total_gap}", flush=True)

    # also check gdrive has files album doesn't (dup/foreign)
    extra = 0
    for month in sorted(months):
        g = gdrive.get(month, set())
        a = album.get(month, set())
        e = g - a
        if e:
            extra += len(e)
            print(f"{month}: gdrive-only files = {len(e)} (first 5: {sorted(e)[:5]})", flush=True)
    print(f"gdrive-only total: {extra}", flush=True)

if __name__ == "__main__":
    main()
