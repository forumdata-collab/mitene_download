#!/usr/bin/env python3
"""Per-year album vs GDrive counts -> .album_counts.json (single source of truth).

Consumed by: web UI check button (mitene_web run_check) and sync_year.py runner.
One full pass ~8-10 min (linear in album size, same as a single whole-album scan).
"""
import json, os, sys, tempfile, datetime, fcntl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sync_year import (load_env, months_for_year, gdrive_year_count, album_year_count,
                       MITENE_DIR, PWD)

YEARS = list(range(2021, 2027))
CACHE = os.path.join(MITENE_DIR, ".album_counts.json")
LOCK = "/tmp/mitene_sync.lock"

def main():
    env = load_env()
    url = env.get("MITENE_URL", "")
    if not url:
        print("missing MITENE_URL in .env", file=sys.stderr)
        return 1
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("sync already running (lock held), check skipped", file=sys.stderr)
        return 0

    pf = tempfile.NamedTemporaryFile(delete=False, mode="w")
    pf.write(PWD)
    pf.close()
    os.chmod(pf.name, 0o600)
    try:
        cache = {}
        try:
            cache = json.load(open(CACHE))
        except Exception:
            pass
        print(f"{'年':>4} {'相簿':>7} {'GDrive':>7} {'偏差':>7}  狀態", flush=True)
        for y in YEARS:
            album = album_year_count(y, pf.name)
            if album < 0:
                print(f"{y}: dry-run parse failed", file=sys.stderr)
                return 1
            g = gdrive_year_count(y)
            cache[str(y)] = {"album": album, "gdrive": g,
                             "updated": datetime.date.today().isoformat()}
            print(f"{y:>4} {album:>7} {g:>7} {max(0, album-g):>7}  "
                  f"{'OK' if g >= album else '待續'}", flush=True)
        json.dump(cache, open(CACHE, "w"), indent=1)
        print(f"\ncache -> {CACHE}", flush=True)
    finally:
        os.unlink(pf.name)
    return 0

if __name__ == "__main__":
    sys.exit(main())