"""Download medias from https://mitene.us/ or https://family-album.com/ to keep a local backup.

Fork (forumdata-collab) additions over upstream v0.7.0:
  - --organize: year-month subdirectories (YYYY-MM/) using the photo timestamp (default on)
  - --cooldown: jittered sleep + exponential backoff retry on 429/5xx to avoid IP bans (default on)
  - --log: per-run summary appended to download.log in the destination directory
  - --gdrive-upload: after download, `rclone move <dest> <remote>:<folder>` (needs rclone configured)
"""

__version__ = "0.9.1"

import argparse
import asyncio
import datetime
import getpass
import glob
import json
import mimetypes
import os
import pathlib
import random
import subprocess
import sys
import time
import urllib.parse
from typing import Awaitable, List

import aiohttp


async def gather_with_concurrency(n: int, *tasks: Awaitable) -> List:
  """Like asyncio.gather but limit the number of concurent tasks. Returns task results."""
  semaphore = asyncio.Semaphore(n)

  async def sem_task(task: Awaitable):
    async with semaphore:
      return await task

  return await asyncio.gather(*(sem_task(task) for task in tasks))


def jittered_cooldown(cooldown: float) -> None:
  """Small random sleep so a batch doesn't look like a scripted hammer."""
  time.sleep(cooldown * random.uniform(0.5, 1.5))


async def download_media(
  session: aiohttp.ClientSession,
  url: str,
  destination_filename: str,
  media_name: str,
  verbose: bool,
  cooldown: float,
  stream_command: str = None,
  stream_daemon: subprocess.Popen = None,
) -> tuple:
  """Download one media from URL. Returns ('new'|'skip', None) or ('error', 'name: message')."""
  if os.path.exists(destination_filename):
    if os.environ.get("MITENE_DEBUG"):
      print(f"DEBUG skip-L63: {destination_filename}", flush=True)
    if verbose:
      print(f"{media_name} already downloaded ✔️", flush=True)
    return ("skip", None)
  last_err = None
  for attempt in range(4):  # exponential backoff: 0s, 2s, 4s, 8s
    try:
      async with session.get(url) as r:
        if r.status in (429, 500, 502, 503, 504):
          last_err = f"HTTP {r.status}"
          await asyncio.sleep(2 ** attempt * random.uniform(1, 2))  # 429/5xx -> back off
          continue
        r.raise_for_status()
        expected = r.headers.get("Content-Length")
        tmp = destination_filename + "." + media_name + ".tmp"  # unique per media (parallel-safe)
        with open(tmp, "wb") as f:
          written = 0
          async for chunk in r.content.iter_chunked(1024 * 1024):
            f.write(chunk)
            written += len(chunk)
        # integrity: complete file must match advertised Content-Length
        if expected is not None and written != int(expected):
          last_err = f"size mismatch (got {written}, expected {expected})"
          os.unlink(tmp)
          await asyncio.sleep(2 ** attempt * random.uniform(1, 2))
          continue
      if os.path.exists(destination_filename):
        if os.environ.get("MITENE_DEBUG"):
          print(f"DEBUG skip-L88: {destination_filename}", flush=True)
        os.unlink(tmp)  # another task landed the same file first
        return ("skip", None)
      os.rename(tmp, destination_filename)
      if stream_daemon:
        stream_daemon.stdin.write(destination_filename + "\n")
        stream_daemon.stdin.flush()
        # DON'T unlink here — daemon uploads from disk, then unlinks on success.
        # Unlinking first = race: daemon's os.path.exists() fails → silent data loss.
      elif stream_command:
        try:
          subprocess.run(stream_command.replace("{file}", destination_filename),
                         shell=True, check=True, capture_output=True, text=True)
          try:
            os.unlink(destination_filename)  # stream helper may already have removed it
          except OSError:
            pass
        except subprocess.CalledProcessError as e:
          last_err = f"stream failed: {e.stderr.strip()[:120]}"
          return ("error", f"{media_name}: {last_err}")
      if verbose:
        print(f"Downloading {media_name} ⏳", flush=True)
      if cooldown:
        jittered_cooldown(cooldown)
      return ("new", None)
    except Exception as e:  # network error
      last_err = str(e)
      if os.path.exists(destination_filename):
        if os.environ.get("MITENE_DEBUG"):
          print(f"DEBUG skip-L116: {destination_filename} err={str(last_err)[:80]}", flush=True)
        return ("skip", None)  # another task finished this file meanwhile
      await asyncio.sleep(2 ** attempt * random.uniform(1, 2))
  try:
    os.unlink(destination_filename + ".tmp")
  except OSError:
    pass
  print(f"❌ {media_name} failed after retries: {last_err}", file=sys.stderr, flush=True)
  return ("error", f"{media_name}: {last_err}")


def organize_dest(destination_directory: str, took_at: str) -> str:
  """Return YYYY-MM subdirectory path (in place of flat dir)."""
  month = took_at[:7]  # "2024-05-01T..." -> "2024-05"
  d = os.path.join(destination_directory, month)
  os.makedirs(d, exist_ok=True)
  return d


def migrate_flat_files(destination_directory: str) -> None:
  """Move legacy flat files (TookAt-xxx.ext) into YYYY-MM/ subdirs, once."""
  for old in glob.glob(os.path.join(destination_directory, "*")):
    name = os.path.basename(old)
    if not os.path.isfile(old) or os.path.splitext(name)[1] == ".md":
      continue
    month = name[:7]  # TookAt prefix, e.g. 2024-05-01T12-34-56-...
    if len(month) != 7 or month[4] != "-":
      continue
    dest_dir = os.path.join(destination_directory, month)
    os.makedirs(dest_dir, exist_ok=True)
    new = os.path.join(dest_dir, name)
    if not os.path.exists(new):
      os.rename(old, new)
      print(f"migrated {name} -> {month}/", flush=True)


def append_log(destination_directory: str, stats: dict, album_url: str) -> None:
  """Append one line to download.log with per-run summary."""
  line = (f"[{datetime.datetime.now().isoformat(timespec='seconds')}] "
          f"url={album_url} added={stats['new']} skipped={stats['skip']} "
          f"failed={stats['error']} -> {destination_directory}/")
  with open(os.path.join(destination_directory, "download.log"), "a", encoding="utf-8") as f:
    f.write(line + "\n")
  print(line, flush=True)


def append_error_log(destination_directory: str, album_url: str, errors: list) -> None:
  """Append one line per failed file to errors.log for post-run debugging."""
  if not errors:
    return
  path = os.path.join(destination_directory, "errors.log")
  with open(path, "a", encoding="utf-8") as f:
    f.write(f"# {datetime.datetime.now().isoformat(timespec='seconds')} url={album_url} failed={len(errors)}\n")
    for err in errors:
      f.write(f"- {err}\n")
  print(f"❌ {len(errors)} failures logged -> errors.log", file=sys.stderr, flush=True)


async def estimate_run(medias: list, cooldown: float, bandwidth_mbps: float, destination_directory: str = "out", session: aiohttp.ClientSession = None) -> None:
  """Pre-run estimate: file count, total size, expected duration (already-downloaded excluded)."""
  photos = videos = 0
  total_bytes = 0
  already = 0
  for m in medias:
    took = m.get("tookAt", "")
    url = m.get("expiringUrl", m.get("expiringVideoUrl", ""))
    name = urllib.parse.urlparse(url).path.split("/")[-1]
    name = f"{took}-{name}".replace(":", "")
    if not os.path.splitext(name)[1]:
      if ext := mimetypes.guess_extension(m.get("contentType", "")):
        name = name + ext
    month = took[:7]
    if os.path.exists(os.path.join(destination_directory, month, name)):
      already += 1
      continue
    if "video" in (m.get("contentType") or "").lower():
      videos += 1
    else:
      photos += 1

  total_files = photos + videos

  # probe a sample of URLs via HEAD to get real Content-Length, extrapolate the rest
  sample_bytes = []
  if session is not None and total_files > 0:
    sample = [m for m in medias
              if not os.path.exists(os.path.join(destination_directory, m.get("tookAt", "")[:7], _media_name(m)))]
    sample = sample[:min(10, len(sample))]  # ponytail: 10-sample, raise if accuracy matters
    async def _probe(m):
      # download endpoint = original file (expiringUrl is smartphone thumbnail tier)
      u = f"{m.get('_album', '')}/media_files/{m['uuid']}/download"
      try:
        async with session.head(u, allow_redirects=True) as r:
          cl = r.headers.get("Content-Length")
          return int(cl) if cl else None
      except Exception:
        return None
    sample_bytes = [b for b in await asyncio.gather(*[_probe(m) for m in sample]) if b]

  if sample_bytes:
    avg = sum(sample_bytes) / len(sample_bytes)
    total_bytes = avg * total_files
    size_note = f"抽樣 {len(sample_bytes)} 檔平均 {avg/1e6:.1f} MB/檔 × {total_files}"
  else:
    total_bytes = total_files * 3 * 1024 * 1024
    size_note = f"無 size 數據, 用 3MB/檔粗估"

  speed = bandwidth_mbps * 1e6 / 8  # bytes/sec
  est_sec = total_files * cooldown + total_bytes / speed
  print(f"\n📊 執行前評估 (--dry-run, 未下載任何檔案)")
  print(f"   相簿總數: {len(medias)} 檔 (已下載 {already} / 待下載 {total_files})")
  print(f"   待下載: {total_files} 檔 (相片 {photos} / 影片 {videos})")
  print(f"   待下載大小: ≈ {total_bytes/1e9:.2f} GB ({size_note})")
  print(f"   預計耗時: @{bandwidth_mbps:.0f} Mbps + {cooldown}s cooldown ≈ {est_sec/3600:.1f} 小時")
  print(f"   實際可用: --bandwidth-mbps 改頻寬 · --cooldown 改冷卻 · --months/--since 收窄範圍\n")


def _media_name(m):
  """Reconstruct the on-disk filename for a media dict (mirrors async_main logic)."""
  took = m.get("tookAt", "")
  url = m.get("expiringUrl", m.get("expiringVideoUrl", ""))
  name = urllib.parse.urlparse(url).path.split("/")[-1]
  name = f"{took}-{name}".replace(":", "")
  if not os.path.splitext(name)[1]:
    if ext := mimetypes.guess_extension(m.get("contentType", "")):
      name = name + ext
  return name


async def ensure_login(session: aiohttp.ClientSession, args) -> None:
  """Login if the album is password-protected. Idempotent; call before probing pages."""
  r = await session.get(f"{args.album_url}?page=1")
  response_text = await r.text()
  if "Please enter your password" not in response_text:
    return
  if not args.password:
    try:
      args.password = getpass.getpass("Album is password protected. Enter password: ")
    except (EOFError, KeyboardInterrupt):
      print(file=sys.stderr)
      print("Cannot read password from terminal, please specify password with --password", file=sys.stderr)
      sys.exit(1)
    if not args.password:
      print("Password cannot be empty", file=sys.stderr)
      sys.exit(1)
  authenticity_token = response_text.split('name="authenticity_token" value="')[1].split('"')[0]
  assert authenticity_token, "Could not parse authenticity token"
  r = await session.post(
    f"{args.album_url}/login",
    data={"session[password]": args.password, "authenticity_token": authenticity_token},
  )
  if r.url.path.endswith("/login"):
    print("Could not authenticate, maybe password is incorrect", file=sys.stderr)
    sys.exit(1)


async def async_main() -> None:
  parser = argparse.ArgumentParser(prog="mitene_download", description=__doc__)
  parser.add_argument("album_url", help="URL obtained by inviting a family member for the web version.")
  parser.add_argument("--destination-directory", default="out")
  parser.add_argument("-p", "--password")
  parser.add_argument("-v", "--verbose", action="store_true")
  parser.add_argument("--nocomments", action="store_true", help="Skip downloading comment files (.md).")
  parser.add_argument("--no-organize", action="store_true", help="Disable YYYY-MM subdirectories (flat output like upstream).")
  parser.add_argument("--cooldown", type=float, default=0.3, help="Jittered sleep seconds between downloads (0 disables).")
  parser.add_argument("--gdrive-upload", metavar="REMOTE:FOLDER", help="After download, rclone move files to GDrive (e.g. gdrive:mitene-backup).")
  parser.add_argument("--months", help="Only download these YYYY-MM months, comma-separated (e.g. 2024-05,2024-06).")
  parser.add_argument("--since", help="Only download months >= this YYYY-MM (e.g. 2024-01 = last 2 years).")
  parser.add_argument("--dry-run", action="store_true", help="Estimate only: count files, total size, expected duration. Downloads nothing.")
  parser.add_argument("--bandwidth-mbps", type=float, default=100.0, help="Assumed download bandwidth for --dry-run estimate (default 100).")
  parser.add_argument("--concurrency", type=int, default=4, help="Max parallel downloads (default 4; lower for huge videos).")
  parser.add_argument("--password-file", help="Read password from a file (avoids exposing it in process list).")
  parser.add_argument("--interactive", action="store_true", help="Estimate first, then ask which time range to sync before downloading.")
  parser.add_argument("--stream-command", help="Run this shell command after each successful download; {file} is replaced with the file path (e.g. 'rclone move {file} gdrive:mitene-backup'). VM keeps no local copy.")
  parser.add_argument("--stream-daemon", help="Long-lived uploader: spawn once, feed each downloaded file path on stdin (one per line). Much faster than --stream-command per-file spawn (e.g. 'python3 stream_gdrive.py --daemon').")
  args = parser.parse_args()

  if args.password_file:
    try:
      args.password = open(args.password_file).read().strip()
    except OSError as e:
      print(f"❌ Cannot read password file: {e}", file=sys.stderr)
      sys.exit(2)

  months_filter = None
  if args.months:
    months_filter = set(m.strip() for m in args.months.split(",") if m.strip())
  since_filter = args.since

  os.makedirs(args.destination_directory, exist_ok=True)
  # cleanup temp files from previous run, if interrupted
  for tmp_file in glob.glob(os.path.join(args.destination_directory, "**", "*.tmp"), recursive=True):
    os.unlink(tmp_file)

  if not args.no_organize:
    migrate_flat_files(args.destination_directory)

  stats = {"new": 0, "skip": 0, "error": 0}
  errors = []  # (media_name, error_message) for errors.log
  download_coroutines = []
  all_medias = []  # for --dry-run estimate
  seen_uuids = set()  # album lists some media twice; dedupe
  stream_daemon = None
  if args.stream_daemon:
    stream_daemon = subprocess.Popen(args.stream_daemon, shell=True, stdin=subprocess.PIPE, text=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  async with aiohttp.ClientSession(
    timeout=aiohttp.ClientTimeout(total=datetime.timedelta(minutes=30).total_seconds())
  ) as session:
    page = 1
    oldest = None  # oldest target month (for early exit); set in the search block below
    # ensure logged-in BEFORE binary search probes (login is only triggered on page 1)
    await ensure_login(session, args)
    # when filtering by months, binary-search the first page of the earliest target
    # month (album is time-descending, one month per page-range) to avoid scanning
    # the whole album. ponytail: assumes strictly descending tookAt.
    if (months_filter or since_filter) and not args.no_organize:
        lo, hi = 1, 2000
        # start at the NEWEST target month (album is time-descending); early-exit at the oldest
        target = max(months_filter) if months_filter else since_filter
        oldest = min(months_filter) if months_filter else since_filter
        found = None
        while lo <= hi and not found:
            mid = (lo + hi) // 2
            try:
                probe = await session.get(f"{args.album_url}?page={mid}")
                pt = await probe.text()
                pd = json.loads(pt.split(";gon.media=")[1].split(";gon.familyUserIdToColorMap=")[0])
                files = pd.get("mediaFiles") or []
                if not files:
                    hi = mid - 1
                    continue
                pm = min(m["tookAt"][:7] for m in files)
                if pm > target:
                    lo = mid + 1
                elif pm < target:
                    hi = mid - 1
                else:
                    found = mid
                    # walk back to the first page of this month
                    while found > 1:
                        probe = await session.get(f"{args.album_url}?page={found - 1}")
                        pt = await probe.text()
                        pd = json.loads(pt.split(";gon.media=")[1].split(";gon.familyUserIdToColorMap=")[0])
                        pf = pd.get("mediaFiles") or []
                        if pf and min(m["tookAt"][:7] for m in pf) == target:
                            found -= 1
                        else:
                            break
            except Exception:
                break
        if found:
            page = found
    while True:
      r = await session.get(f"{args.album_url}?page={page}")
      response_text = await r.text()
      if page == 1 and "Please enter your password" in response_text:
        await ensure_login(session, args)
        continue

      page_text = response_text.split(";gon.media=")[1].split(";gon.familyUserIdToColorMap=")[0]
      data = json.loads(page_text)

      page += 1
      if not data["mediaFiles"]:
        break
      # early exit: album is time-descending; once a page is entirely older than
      # the target range, nothing further can match
      page_min = min(m["tookAt"][:7] for m in data["mediaFiles"])
      if months_filter is not None:
        if page_min < oldest:
          break
      elif since_filter is not None and page_min < since_filter:
        break
      for media in data["mediaFiles"]:
        media["_album"] = args.album_url  # for estimate HEAD probes
        month = media["tookAt"][:7]
        if months_filter is not None and month not in months_filter:
          continue
        if since_filter is not None and month < since_filter:
          continue
        if media["uuid"] in seen_uuids:
          continue  # album sometimes lists the same media twice (parallel-safe dedupe)
        seen_uuids.add(media["uuid"])
        filename = urllib.parse.urlparse(media.get("expiringVideoUrl", media["expiringUrl"])).path.split("/")[-1]
        took = media["tookAt"][:10].replace("-","")  # 211029
        filename = f"{took}-{filename[:6]}"  # YYMMDD + 6-char CDN basename
        if not os.path.splitext(filename)[1]:
          if ext := mimetypes.guess_extension(media["contentType"]):
            filename = filename + ext
        if args.no_organize:
          dest_dir = args.destination_directory
        else:
          dest_dir = organize_dest(args.destination_directory, media["tookAt"])
        destination_filename = os.path.join(dest_dir, filename)

        # --dry-run: collect media for estimate, skip download work
        if args.dry_run:
          all_medias.append(media)
          continue

        download_coroutines.append(
          download_media(
            session,
            f"{args.album_url}/media_files/{media['uuid']}/download",
            destination_filename,
            media["uuid"],
            args.verbose,
            args.cooldown,
            args.stream_command,
            stream_daemon,
          )
        )

        if not args.nocomments and media["comments"]:
          comment_text = "".join(
            f"**{comment['user']['nickname']}**: {comment['body']}\n\n"
            for comment in media["comments"]
            if not comment["isDeleted"]
          )
          comment_file = pathlib.Path(os.path.splitext(destination_filename)[0] + ".md")
          if not (comment_file.exists() and comment_file.read_text(encoding="utf-8") == comment_text):
            comment_file.write_text(comment_text, encoding="utf-8")

    if args.dry_run or args.interactive:
      await estimate_run(all_medias, args.cooldown, args.bandwidth_mbps, args.destination_directory, session)
      if not args.interactive:
        return

      # interactive: user picks time range before download
      months = sorted({m["tookAt"][:7] for m in all_medias})
      print("📅 相簿涵蓋月份:", ", ".join(months[:12]) + (" ..." if len(months) > 12 else ""))
      print("   1) 全部下載")
      print("   2) 揀月份 (輸入如 2024-05,2024-06)")
      print("   3) 最近 N 個月 (輸入數字)")
      choice = input("> ").strip()
      if choice == "1":
        pass
      elif choice == "2":
        pick = input("月份 (逗號分隔): ").strip()
        if pick:
          months_filter = set(m.strip() for m in pick.split(",") if m.strip())
      elif choice.isdigit():
        n = int(choice)
        recent = sorted(set(months))[-n:]
        months_filter = set(recent)
        since_filter = None
      else:
        print("❌ 唔明輸入, 取消", file=sys.stderr)
        sys.exit(2)
      print(f"✅ 範圍: {'全部' if months_filter is None and since_filter is None else str(months_filter or since_filter)}")
      # rebuild coroutines for the chosen range
      download_coroutines = []
      for media in all_medias:
        month = media["tookAt"][:7]
        if months_filter is not None and month not in months_filter:
          continue
        if since_filter is not None and month < since_filter:
          continue
        filename = urllib.parse.urlparse(media.get("expiringVideoUrl", media["expiringUrl"])).path.split("/")[-1]
        took = media["tookAt"][:10].replace("-","")  # 211029
        filename = f"{took}-{filename[:6]}"  # YYMMDD + 6-char CDN basename
        if not os.path.splitext(filename)[1]:
          if ext := mimetypes.guess_extension(media["contentType"]):
            filename = filename + ext
        dest_dir = args.destination_directory if args.no_organize else organize_dest(args.destination_directory, media["tookAt"])
        download_coroutines.append(download_media(
          session, f"{args.album_url}/media_files/{media['uuid']}/download",
          os.path.join(dest_dir, filename), media["uuid"], args.verbose, args.cooldown, args.stream_command, stream_daemon))
      if not download_coroutines:
        print("⚠️ 呢個範圍冇檔案要下載", file=sys.stderr)
        return
      print(f"📥 開始下載 {len(download_coroutines)} 個檔案...", flush=True)

    results = await gather_with_concurrency(args.concurrency, *download_coroutines)
    if os.environ.get("MITENE_DEBUG"):
      print(f"DEBUG coroutines={len(download_coroutines)} results={len(results)}", flush=True)
    errors = []
    for status, detail in results:
      stats[status] += 1
      if status == "error":
        errors.append(detail)
      if os.environ.get("MITENE_DEBUG") and status == "skip" and stats["skip"] <= 3:
        print(f"DEBUG skip-status: {detail}", flush=True)
    if os.environ.get("MITENE_DEBUG"):
      print("DEBUG results:", {k: sum(1 for s, _ in results if s == k) for k in ("new", "skip", "error")}, flush=True)

  append_log(args.destination_directory, stats, args.album_url)
  append_error_log(args.destination_directory, args.album_url, errors)

  # close daemon stdin, wait for remaining uploads to drain
  if stream_daemon:
    stream_daemon.stdin.close()
    stream_daemon.wait(timeout=3600)

  if args.gdrive_upload:
    cmd = ["rclone", "move", args.destination_directory, args.gdrive_upload,
           "--include", "*.jpg", "--include", "*.jpeg", "--include", "*.png",
           "--include", "*.gif", "--include", "*.mp4", "--include", "*.mov",
           "--include", "*.md", "--include", "download.log", "--include", "errors.log", "-v"]
    print(f"📤 Uploading to {args.gdrive_upload} via rclone...", flush=True)
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
      print("❌ rclone move failed", file=sys.stderr)
      sys.exit(proc.returncode)
    print("✅ Upload complete", flush=True)

  if stats["error"] > 0:
    print(f"❌ {stats['error']} file(s) failed — see errors.log", file=sys.stderr)
    sys.exit(1)


def main() -> None:
  loop = asyncio.get_event_loop()
  loop.run_until_complete(async_main())


if __name__ == "__main__":
  main()
