#!/usr/bin/env python3
"""Targeted backfill: specifically download+upload the exact missing files
(gap list from diag_full.py) using full album scan (no --months binary search,
which skips month-boundary pages = beginning-of-month files).

Flow:
1. Scan album ALL pages (time-desc) with proper login, collect {filename -> {uuid, url, month, tookAt}}
2. Read gap lists: /tmp/diag_2022.log /tmp/diag_2023.log /tmp/diag_2024.log
   (lines "   20240101-xxxx.jpg  [local-nohave]")
3. For each missing filename, download {album}/media_files/{uuid}/download
   -> out/{month}/{filename} (organize by tookAt month)
4. Stream to GDrive via stream_gdrive.py --daemon (auto-skips existing)
5. Verify local cleaned; report summary
"""
import asyncio, json, os, re, subprocess, sys, tempfile, mimetypes
from pathlib import Path
import aiohttp

HOME = os.path.expanduser("~")
MITENE_DIR = os.path.join(HOME, "mitene_download")
VENV_PY = "/home/ubuntu/.hermes/hermes-agent/venv/bin/python3"
STREAM = os.path.join(MITENE_DIR, "stream_gdrive.py")

def load_env():
    env = {}
    for line in open(os.path.join(MITENE_DIR, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env

ENV = load_env()
URL, PWD = ENV["MITENE_URL"], ENV["MITENE_PASSWORD"]

def parse_gap_filenames(log_path):
    """Extract missing filenames from diag log lines: '   20240101-xxxx.jpg  [local-nohave]'"""
    out = set()
    if not os.path.exists(log_path):
        return out
    for line in open(log_path, encoding="utf-8", errors="replace"):
        m = re.match(r"\s+(20\d{6}-[A-Za-z0-9]+\.\w+)\s+\[local-nohave\]", line)
        if m:
            out.add(m.group(1))
    return out

async def scan_album(session):
    """Full album scan: {filename -> {uuid, url, month}} (no binary search)."""
    idx = {}
    async with session.get(f"{URL}?page=1") as r:
        txt = await r.text()
    if "Please enter your password" in txt:
        token = txt.split('name="authenticity_token" value="')[1].split('"')[0]
        await session.post(f"{URL}/login",
                           data={"session[password]": PWD, "authenticity_token": token})
    page, seen = 1, set()
    while True:
        async with session.get(f"{URL}?page={page}") as r:
            txt = await r.text()
        if "Please enter your password" in txt:
            token = txt.split('name="authenticity_token" value="')[1].split('"')[0]
            await session.post(f"{URL}/login",
                               data={"session[password]": PWD, "authenticity_token": token})
            async with session.get(f"{URL}?page={page}") as r:
                txt = await r.text()
        try:
            pd = json.loads(txt.split(";gon.media=")[1].split(";gon.familyUserIdToColorMap=")[0])
        except Exception as e:
            print(f"parse fail page {page}: {e}", file=sys.stderr)
            break
        files = pd.get("mediaFiles") or []
        if not files:
            break
        for m in files:
            uuid = m["uuid"]
            if uuid in seen:
                continue
            seen.add(uuid)
            src = m.get("expiringVideoUrl") or m.get("expiringUrl") or ""
            base = os.path.basename(src.split("?")[0])
            took = m["tookAt"][:10].replace("-", "")
            fn = f"{took}-{base[:6]}"
            if not os.path.splitext(fn)[1]:
                ext = mimetypes.guess_extension(m.get("contentType", ""))
                if ext:
                    fn += ext
            idx[fn] = {"uuid": uuid, "url": f"{URL}/media_files/{uuid}/download",
                       "month": m["tookAt"][:7]}
        page += 1
    return idx

async def download_one(session, meta, dest):
    """Download with retries + size check. Returns True on success."""
    for attempt in range(4):
        try:
            async with session.get(meta["url"]) as r:
                if r.status in (429, 500, 502, 503, 504):
                    await asyncio.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                expected = r.headers.get("Content-Length")
                tmp = dest + f".{attempt}.tmp"
                written = 0
                with open(tmp, "wb") as f:
                    async for chunk in r.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
                        written += len(chunk)
                if expected is not None and written != int(expected):
                    os.unlink(tmp)
                    await asyncio.sleep(2 ** attempt)
                    continue
                os.rename(tmp, dest)
                return True
        except Exception as e:
            await asyncio.sleep(2 ** attempt)
    return False

async def main():
    gaps = set()
    years = sys.argv[1:] or ("2021", "2022", "2023", "2024", "2025", "2026")
    for y in years:
        gaps |= parse_gap_filenames(f"/tmp/diag_{y}.log")
    print(f"gap filenames to backfill: {len(gaps)}", flush=True)
    if not gaps:
        print("no gaps to fill", flush=True)
        return 0

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        print("scanning album (full)...", flush=True)
        idx = await scan_album(session)
        print(f"album index built: {len(idx)} files", flush=True)

        missing_meta = []
        no_idx = []
        for fn in sorted(gaps):
            if fn in idx:
                missing_meta.append((fn, idx[fn]))
            else:
                no_idx.append(fn)
        print(f"resolved {len(missing_meta)} / {len(gaps)} (unresolvable: {len(no_idx)})", flush=True)
        if no_idx:
            print("  unresolvable:", no_idx[:10], flush=True)

        # download
        out_root = Path(MITENE_DIR) / "out"
        downloaded, failed = [], []
        for fn, meta in missing_meta:
            month_dir = out_root / meta["month"]
            month_dir.mkdir(parents=True, exist_ok=True)
            dest = month_dir / fn
            if os.path.exists(dest):
                downloaded.append(dest)
                continue
            ok = await download_one(session, meta, str(dest))
            if ok:
                downloaded.append(dest)
            else:
                failed.append(fn)
            print(f"  {'✅' if ok else '❌'} {fn}", flush=True)

        print(f"\ndownloaded: {len(downloaded)}, failed: {len(failed)}", flush=True)

        # stream to GDrive
        if downloaded:
            print("streaming to GDrive...", flush=True)
            up = subprocess.run([VENV_PY, STREAM, "--daemon"],
                                input="\n".join(str(f) for f in downloaded),
                                text=True, capture_output=True, cwd=MITENE_DIR)
            print(f"stream done rc={up.returncode}", flush=True)
            if up.stderr.strip():
                print("stderr:", up.stderr[-300:], flush=True)

        # verify local clean
        remaining = [f for f in downloaded if os.path.exists(f)]
        print(f"local remaining after stream (should be 0): {len(remaining)}", flush=True)
        if failed:
            print("FAILED (retry next time):", failed[:20], flush=True)
        return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))