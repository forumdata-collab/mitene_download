"""Download medias from https://mitene.us/ or https://family-album.com/ to keep a local backup.

Fork (forumdata-collab) additions over upstream v0.7.0:
  - --organize: year-month subdirectories (YYYY-MM/) using the photo timestamp (default on)
  - --cooldown: jittered sleep + exponential backoff retry on 429/5xx to avoid IP bans (default on)
  - --log: per-run summary appended to download.log in the destination directory
  - --gdrive-upload: after download, `rclone move <dest> <remote>:<folder>` (needs rclone configured)
"""

__version__ = "0.8.0"

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
) -> str:
  """Download one media from URL. Returns 'new' | 'skip' | 'error'."""
  if os.path.exists(destination_filename):
    if verbose:
      print(f"{media_name} already downloaded ✔️", flush=True)
    return "skip"
  last_err = None
  for attempt in range(4):  # exponential backoff: 0s, 2s, 4s, 8s
    try:
      async with session.get(url) as r:
        if r.status in (429, 500, 502, 503, 504):
          last_err = f"HTTP {r.status}"
          await asyncio.sleep(2 ** attempt * random.uniform(1, 2))  # 429/5xx -> back off
          continue
        r.raise_for_status()
        with open(destination_filename + ".tmp", "wb") as f:
          async for chunk in r.content.iter_chunked(1024 * 1024):
            f.write(chunk)
      os.rename(destination_filename + ".tmp", destination_filename)
      if verbose:
        print(f"Downloading {media_name} ⏳", flush=True)
      if cooldown:
        jittered_cooldown(cooldown)
      return "new"
    except Exception as e:  # network error
      last_err = str(e)
      await asyncio.sleep(2 ** attempt * random.uniform(1, 2))
  try:
    os.unlink(destination_filename + ".tmp")
  except OSError:
    pass
  print(f"❌ {media_name} failed after retries: {last_err}", file=sys.stderr, flush=True)
  return "error"


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
  args = parser.parse_args()

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
  download_coroutines = []
  async with aiohttp.ClientSession(
    timeout=aiohttp.ClientTimeout(total=datetime.timedelta(minutes=30).total_seconds())
  ) as session:
    page = 1
    while True:
      r = await session.get(f"{args.album_url}?page={page}")
      response_text = await r.text()
      if page == 1 and "Please enter your password" in response_text:
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
        continue

      page_text = response_text.split(";gon.media=")[1].split(";gon.familyUserIdToColorMap=")[0]
      data = json.loads(page_text)

      page += 1
      if not data["mediaFiles"]:
        break
      for media in data["mediaFiles"]:
        month = media["tookAt"][:7]
        if months_filter is not None and month not in months_filter:
          continue
        if since_filter is not None and month < since_filter:
          continue
        filename = urllib.parse.urlparse(media.get("expiringVideoUrl", media["expiringUrl"])).path.split("/")[-1]
        filename = f"{media['tookAt']}-{filename}".replace(":", "")
        if not os.path.splitext(filename)[1]:
          if ext := mimetypes.guess_extension(media["contentType"]):
            filename = filename + ext
        if args.no_organize:
          dest_dir = args.destination_directory
        else:
          dest_dir = organize_dest(args.destination_directory, media["tookAt"])
        destination_filename = os.path.join(dest_dir, filename)

        download_coroutines.append(
          download_media(
            session,
            f"{args.album_url}/media_files/{media['uuid']}/download",
            destination_filename,
            media["uuid"],
            args.verbose,
            args.cooldown,
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

    results = await gather_with_concurrency(4, *download_coroutines)
    for res in results:
      if res in stats:
        stats[res] += 1

  append_log(args.destination_directory, stats, args.album_url)

  if args.gdrive_upload:
    cmd = ["rclone", "move", args.destination_directory, args.gdrive_upload,
           "--include", "*.jpg", "--include", "*.jpeg", "--include", "*.png",
           "--include", "*.gif", "--include", "*.mp4", "--include", "*.mov",
           "--include", "*.md", "--include", "download.log", "-v"]
    print(f"📤 Uploading to {args.gdrive_upload} via rclone...", flush=True)
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
      print("❌ rclone move failed", file=sys.stderr)
      sys.exit(proc.returncode)
    print("✅ Upload complete", flush=True)


def main() -> None:
  loop = asyncio.get_event_loop()
  loop.run_until_complete(async_main())


if __name__ == "__main__":
  main()
