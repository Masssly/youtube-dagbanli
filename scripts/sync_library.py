#!/usr/bin/env python3
"""Sync active channels from the control sheet into the R2 archive."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import boto3
import requests
from botocore.exceptions import ClientError
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

SHEET_RANGE = "Sheet1!A2:D5000"
SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
TIME_BUDGET_SECONDS = 5 * 3600 + 40 * 60  # stop starting new videos with ~20min to spare in the 6h job limit
MANUAL_SUB_LANGS = "all"
AUTO_SUB_LANGS = "en,dag"
COOKIES_PATH = "/tmp/yt-cookies.txt"

COOKIE_EXPIRED_MARKER = "sign in to confirm"


@dataclass
class ChannelResult:
    name: str
    slug: str
    new: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    deferred: int = 0


def log(msg: str) -> None:
    print(msg, flush=True)


def channel_slug(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if path.endswith("/videos"):
        path = path[: -len("/videos")].rstrip("/")
    path = path.replace("@", "")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", path).strip("-._")
    return slug or "channel"


def read_sheet_channels() -> list[dict]:
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SHEET_SCOPES)
    creds.refresh(GoogleAuthRequest())

    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    resp = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{SHEET_RANGE}",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json().get("values", [])

    channels = []
    for row in rows:
        name = (row[0] if len(row) > 0 else "").strip()
        url = (row[1] if len(row) > 1 else "").strip()
        active = (row[2] if len(row) > 2 else "").strip().upper() == "TRUE"
        permission = (row[3] if len(row) > 3 else "").strip()
        if not name or not url:
            continue
        channels.append({"name": name, "url": url, "active": active, "permission": permission})
    return channels


def write_cookies() -> None:
    cookies = os.environ.get("YOUTUBE_COOKIES", "")
    if not cookies:
        raise SystemExit("YOUTUBE_COOKIES secret is empty or not set")
    Path(COOKIES_PATH).write_text(cookies, encoding="utf-8")


def ytdlp_base_args() -> list[str]:
    return ["yt-dlp", "--cookies", COOKIES_PATH, "--remote-components", "ejs:github", "--no-warnings"]


class CookiesExpired(Exception):
    pass


def run_ytdlp(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        stderr = proc.stderr.lower()
        if COOKIE_EXPIRED_MARKER in stderr:
            raise CookiesExpired(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "cookies expired")
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"yt-dlp exited {proc.returncode}")


def list_channel_video_ids(url: str) -> list[str]:
    proc = subprocess.run(
        ytdlp_base_args() + ["--flat-playlist", "--simulate", "--dump-json", url],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "could not list channel videos")
    ids = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        ids.append(json.loads(line)["id"])
    return ids


def object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def download_video(video_id: str, workdir: Path) -> None:
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    run_ytdlp(ytdlp_base_args() + [
        "-f", "bv*+ba/b", "--merge-output-format", "mp4",
        "--write-info-json", "--write-thumbnail",
        "--write-subs", "--sub-langs", MANUAL_SUB_LANGS, "--sub-format", "vtt/best",
        "--no-playlist",
        "-o", str(workdir / "manual.%(ext)s"),
        video_url,
    ])

    run_ytdlp(ytdlp_base_args() + [
        "--skip-download", "--write-auto-sub", "--sub-langs", AUTO_SUB_LANGS, "--sub-format", "vtt/best",
        "--no-playlist",
        "-o", str(workdir / "auto.%(ext)s"),
        video_url,
    ])

    run_ytdlp(ytdlp_base_args() + [
        "-f", "bestaudio", "--no-playlist",
        "-o", str(workdir / "audio.%(ext)s"),
        video_url,
    ])


def upload_video_artifacts(s3, bucket: str, prefix: str, workdir: Path) -> list[str]:
    uploaded = []

    def put(local: Path, key: str) -> None:
        s3.upload_file(str(local), bucket, key)
        uploaded.append(key)

    info_json = next(workdir.glob("manual.info.json"), None)
    info = json.loads(info_json.read_text(encoding="utf-8")) if info_json else {}

    video_file = next((p for p in workdir.glob("manual.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm"}), None)
    if video_file:
        put(video_file, f"{prefix}video{video_file.suffix}")

    audio_file = next((p for p in workdir.glob("audio.*") if p.name != "audio.info.json"), None)
    if audio_file:
        put(audio_file, f"{prefix}audio{audio_file.suffix}")

    thumb_file = next((p for p in workdir.glob("manual.*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}), None)
    if thumb_file:
        put(thumb_file, f"{prefix}thumbnail{thumb_file.suffix}")

    if info_json:
        put(info_json, f"{prefix}metadata.json")
        description = info.get("description")
        if description:
            desc_path = workdir / "description.txt"
            desc_path.write_text(description, encoding="utf-8")
            put(desc_path, f"{prefix}description.txt")

    for sub_file in workdir.glob("manual.*.vtt"):
        lang = sub_file.name[len("manual."):-len(".vtt")]
        put(sub_file, f"{prefix}subtitles/{lang}.vtt")

    for sub_file in workdir.glob("auto.*.vtt"):
        lang = sub_file.name[len("auto."):-len(".vtt")]
        put(sub_file, f"{prefix}subtitles/{lang}.auto.vtt")

    return uploaded


def process_video(s3, bucket: str, slug: str, video_id: str, workdir_root: Path) -> str:
    """Returns 'new' or 'skipped'."""
    prefix = f"channels/{slug}/videos/{video_id}/"
    if object_exists(s3, bucket, f"{prefix}manifest.json"):
        return "skipped"

    workdir = workdir_root / video_id
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        download_video(video_id, workdir)
        uploaded = upload_video_artifacts(s3, bucket, prefix, workdir)
        manifest = {
            "video_id": video_id,
            "channel_slug": slug,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "files": uploaded,
        }
        manifest_path = workdir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        s3.upload_file(str(manifest_path), bucket, f"{prefix}manifest.json")
        return "new"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    start = time.monotonic()
    write_cookies()

    bucket = os.environ["R2_BUCKET"]
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    channels = [c for c in read_sheet_channels() if c["active"]]
    log(f"{len(channels)} active channel(s) in the sheet.")

    results: list[ChannelResult] = []
    aborted_reason = None
    workdir_root = Path("/tmp/sync-work")
    workdir_root.mkdir(parents=True, exist_ok=True)

    for channel in channels:
        slug = channel_slug(channel["url"])
        result = ChannelResult(name=channel["name"], slug=slug)
        results.append(result)
        log(f"\n=== {channel['name']} ({slug}) ===")

        try:
            video_ids = list_channel_video_ids(channel["url"])
        except Exception as exc:
            result.errors.append(f"could not list videos: {exc}")
            continue

        log(f"{len(video_ids)} video(s) found.")

        for video_id in video_ids:
            if time.monotonic() - start > TIME_BUDGET_SECONDS:
                result.deferred += 1
                continue
            try:
                outcome = process_video(s3, bucket, slug, video_id, workdir_root)
                if outcome == "new":
                    result.new += 1
                    log(f"  {video_id}: downloaded and uploaded")
                else:
                    result.skipped += 1
            except CookiesExpired as exc:
                aborted_reason = f"YOUTUBE_COOKIES secret appears expired: {exc}"
                result.errors.append(str(exc))
                break
            except Exception as exc:
                result.errors.append(f"{video_id}: {exc}")
                log(f"  {video_id}: ERROR - {exc}")
        if aborted_reason:
            break

    write_report(results, aborted_reason)

    if aborted_reason:
        print(f"::error::{aborted_reason}", file=sys.stderr)
        sys.exit(1)


def write_report(results: list[ChannelResult], aborted_reason: str | None) -> None:
    lines = ["## Sync report\n", "| Channel | New | Skipped | Deferred | Errors |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.name} | {r.new} | {r.skipped} | {r.deferred} | {len(r.errors)} |")
    if aborted_reason:
        lines.append(f"\n**Run stopped early:** {aborted_reason}")
    for r in results:
        for err in r.errors:
            lines.append(f"\n- `{r.slug}`: {err}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(text)
    log("\n" + text)


if __name__ == "__main__":
    main()
