"""Self-check for fork additions: organize, migrate, cooldown jitter, log, retry backoff.
Run: python3 test_self.py  (no network needed)"""
import os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mitene_download as m

fail = []
def ok(name, cond):
    print(f"{'✅' if cond else '❌'} {name}")
    if not cond: fail.append(name)

# 1. organize_dest: tookAt -> YYYY-MM subdir
with tempfile.TemporaryDirectory() as d:
    p = m.organize_dest(d, "2024-05-01T12-34-56")
    ok("organize creates YYYY-MM dir", os.path.isdir(p) and p == os.path.join(d, "2024-05"))

# 2. migrate: flat file -> month dir
with tempfile.TemporaryDirectory() as d:
    flat = os.path.join(d, "2024-05-01T12-34-56-photo.jpg")
    open(flat, "w").write("x")
    m.migrate_flat_files(d)
    ok("migrate moves flat -> YYYY-MM", os.path.exists(os.path.join(d, "2024-05", "2024-05-01T12-34-56-photo.jpg")) and not os.path.exists(flat))

# 3. log line format
with tempfile.TemporaryDirectory() as d:
    m.append_log(d, {"new": 3, "skip": 10, "error": 1}, "https://mitene.us/f/abc")
    line = open(os.path.join(d, "download.log")).read().strip()
    ok("log has added/skipped/failed", "added=3 skipped=10 failed=1" in line and line.startswith("[20"))

# 4. jittered_cooldown respects scale (0 -> fast, 0.2 -> ~0.1-0.3s)
t0 = time.monotonic(); m.jittered_cooldown(0); t1 = time.monotonic()
ok("cooldown=0 is instant", (t1 - t0) < 0.05)
t0 = time.monotonic(); m.jittered_cooldown(0.2); t1 = time.monotonic()
ok("cooldown=0.2 sleeps ~0.1-0.3s", 0.05 <= (t1 - t0) <= 0.6)

# 5. download_media skip path (existing file -> 'skip', no network)
import asyncio
with tempfile.TemporaryDirectory() as d:
    f = os.path.join(d, "x.jpg"); open(f, "w").write("x")
    res = asyncio.run(m.download_media(None, "http://none", f, "x", False, 0))  # type: ignore[arg-type]
    ok("existing file returns ('skip', None)", isinstance(res, tuple) and res[0] == "skip")

# 6. months/since filter logic (same predicate as in async_main)
def passes_filter(month, months_filter, since_filter):
    if months_filter is not None and month not in months_filter:
        return False
    if since_filter is not None and month < since_filter:
        return False
    return True
ok("months filter includes only selected", passes_filter("2024-05", {"2024-05", "2024-06"}, None) and not passes_filter("2024-04", {"2024-05", "2024-06"}, None))
ok("since filter >= boundary", passes_filter("2024-01", None, "2024-01") and not passes_filter("2023-12", None, "2024-01"))

# 7. error log writes per-file detail
with tempfile.TemporaryDirectory() as d:
    m.append_error_log(d, "https://mitene.us/f/abc", ["photo1: HTTP 429", "video2: timeout"])
    log = open(os.path.join(d, "errors.log")).read()
    ok("errors.log has header + per-file lines", "failed=2" in log and "photo1: HTTP 429" in log and "video2: timeout" in log)
    m.append_error_log(d, "https://mitene.us/f/abc", [])
    ok("no errors -> no errors.log change", open(os.path.join(d, "errors.log")).read().count("#") == 1)

# 8. estimate_run: counts + size probing
import io, contextlib
med = [
    {"contentType": "image/jpeg", "size": 3_000_000, "tookAt": "2024-05-01T12-34-56", "expiringUrl": "https://x/photo.jpg"},
    {"contentType": "video/mp4", "fileSize": 100_000_000, "tookAt": "2024-06-01T12-34-56", "expiringUrl": "https://x/vid.mp4"},
    {"contentType": "image/jpeg", "tookAt": "2024-07-01T12-34-56", "expiringUrl": "https://x/p.jpg"},  # unknown size
]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.estimate_run(med, 0.3, 100)
out = buf.getvalue()
ok("estimate counts photos/videos", "相片 2" in out and "影片 1" in out)
ok("estimate shows duration", "小時" in out and "評估" in out)

# 8b. estimate_run excludes already-downloaded (YYYY-MM dir exists)
with tempfile.TemporaryDirectory() as d:
    os.makedirs(os.path.join(d, "2024-05"))
    open(os.path.join(d, "2024-05", "2024-05-01T12-34-56-photo.jpg"), "w").write("x")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.estimate_run(med, 0.3, 100, d)
    out2 = buf.getvalue()
    ok("estimate excludes downloaded (已下載 1)", "已下載 1" in out2 and "待下載 2" in out2)

print("\n" + ("ALL PASS ✅" if not fail else f"FAILED: {fail}"))
sys.exit(1 if fail else 0)