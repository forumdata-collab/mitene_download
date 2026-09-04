#!/usr/bin/env python3
"""Sync ~/mitene_download/out → R2 (ai-images/mitene-backup/). Incremental:
skips objects already present with matching size. Prints summary on stdout
(used by cron in no_agent mode: empty = silent, but we want a brief line).
"""
import os, sys, boto3
from pathlib import Path

OUT = Path.home() / "mitene_download" / "out"
BUCKET = "ai-images"
PREFIX = "mitene-backup"

def main():
    if not OUT.is_dir():
        print("out dir missing, nothing to sync")
        return 0
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )

    # existing objects: key -> size
    existing = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{PREFIX}/"):
        for obj in page.get("Contents", []):
            existing[obj["Key"]] = obj["Size"]

    uploaded, skipped, failed = 0, 0, 0
    for f in sorted(OUT.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(OUT).as_posix()
        key = f"{PREFIX}/{rel}"
        size = f.stat().st_size
        if key in existing and existing[key] == size:
            skipped += 1
            continue
        try:
            s3.upload_file(str(f), BUCKET, key)
            existing[key] = size
            uploaded += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL {key}: {e}", file=sys.stderr)

    total = sum(1 for f in OUT.rglob("*") if f.is_file())
    print(f"R2 sync: {uploaded} uploaded, {skipped} skipped, {failed} failed (total {total} files)")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
