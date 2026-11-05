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
CAPTURED_VIDEOS_RANGE = "'Captured videos'!A:H"
SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
R2_PUBLIC_BASE_URL = "https://pub-442a1ed98c1a44938b232fa937be8956.r2.dev"
TIME_BUDGET_SECONDS = 5 * 3600 + 40 * 60  # stop starting new videos with ~20min to spare in the 6h job limit
MANUAL_SUB_LANGS = "all"
AUTO_SUB_LANGS = "en,dag"
COOKIES_PATH = "/tmp/yt-cookies.txt"
INTER_VIDEO_DELAY_SECONDS = 8  # spaces out requests; a burst is what triggered re-challenge in testing

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


def get_sheet_credentials() -> service_account.Credentials:
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SHEET_SCOPES)
    creds.refresh(GoogleAuthRequest())
    return creds


def read_sheet_channels(creds: service_account.Credentials) -> list[dict]:
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


def log_captured_video(
    creds: service_account.Credentials, channel_name: str, title: str, video_id: str, files: list[str]
) -> None:
    def link_for(marker: str) -> str:
        key = next((f for f in files if marker in f), None)
        return f"{R2_PUBLIC_BASE_URL}/{key}" if key else ""

    row = [
        channel_name,
        title,
        video_id,
        link_for("/video."),
        link_for("/audio."),
        link_for("/metadata.json"),
        link_for("/thumbnail."),
        datetime.now(timezone.utc).isoformat(),
    ]
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    resp = requests.post(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{CAPTURED_VIDEOS_RANGE}:append",
        params={"valueInputOption": "RAW"},
        headers={"Authorization": f"Bearer {creds.token}"},
        json={"values": [row]},
        timeout=30,
    )
    resp.raise_for_status()


def write_cookies() -> None:
    cookies = os.environ.get("YOUTUBE_COOKIES", "")
    if not cookies:
        raise SystemExit("YOUTUBE_COOKIES secret is empty or not set")
    Path(COOKIES_PATH).write_text(cookies, encoding="utf-8")


def ytdlp_base_args() -> list[str]:
    return ["yt-dlp", "--cookies", COOKIES_PATH, "--remote-components", "ejs:github", "--no-warnings"]


class BotCheckTriggered(Exception):
    """Raised when YouTube's 'sign in to confirm' gate reappears mid-run.

    In testing this showed up on one video, then kept failing on every later
    video and every later run against the same cookies, even with no requests
    in between - it stuck, rather than being specific to that one video or to
    request bursts. A fresh cookie export cleared it immediately. So treat one
    occurrence as possibly a quirk, but two in a row as the cookie session
    itself having gone bad - see the two-in-a-row check below.
    """


def run_ytdlp(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        stderr = proc.stderr.lower()
        if COOKIE_EXPIRED_MARKER in stderr:
            raise BotCheckTriggered(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "bot-check triggered")
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


def download_video(video_id: str, workdir: Path) -> None:
    """Three yt-dlp invocations, each with its own output basename.

    An earlier version tried to cut this to two by requesting both the merged
    video and standalone audio in one invocation via a comma-separated format
    spec sharing one output template. That collided: both outputs resolved to
    the same filename (manual.webm), so audio silently overwrote video on disk
    and the video was never uploaded. Distinct basenames make that impossible."""
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    run_ytdlp(ytdlp_base_args() + [
        "-f", "bv*+ba/b", "--merge-output-format", "mp4",
        "--write-info-json", "--write-thumbnail",
        "--write-subs", "--sub-langs", MANUAL_SUB_LANGS, "--sub-format", "vtt/best",
        "--no-playlist",
        "-o", str(workdir / "video.%(ext)s"),
        video_url,
    ])

    run_ytdlp(ytdlp_base_args() + [
        "-f", "bestaudio", "--no-playlist",
        "-o", str(workdir / "audio.%(ext)s"),
        video_url,
    ])

    run_ytdlp(ytdlp_base_args() + [
        "--skip-download", "--write-auto-sub", "--sub-langs", AUTO_SUB_LANGS, "--sub-format", "vtt/best",
        "--no-playlist",
        "-o", str(workdir / "auto.%(ext)s"),
        video_url,
    ])


def has_video_stream(path: Path) -> bool:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() != ""


def upload_video_artifacts(s3, bucket: str, prefix: str, workdir: Path) -> list[str]:
    uploaded = []

    def put(local: Path, key: str) -> None:
        s3.upload_file(str(local), bucket, key)
        uploaded.append(key)

    info_json = next(workdir.glob("video.info.json"), None)
    info = json.loads(info_json.read_text(encoding="utf-8")) if info_json else {}

    video_file = next((p for p in workdir.glob("video.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm"}), None)
    if video_file and not has_video_stream(video_file):
        # yt-dlp can resolve "bv*+ba/b" down to an audio-only stream with no error
        # at all when the real video formats fail to resolve (seen in testing: a
        # byte-identical duplicate of the separately-downloaded audio file, silently
        # accepted as "the video"). Never upload that as if it were valid.
        raise RuntimeError(f"downloaded 'video' file has no video stream ({video_file.name}, {video_file.stat().st_size} bytes)")
    if video_file:
        put(video_file, f"{prefix}video{video_file.suffix}")

    audio_file = next((p for p in workdir.glob("audio.*") if p.suffix.lower() != ".json"), None)
    if audio_file:
        put(audio_file, f"{prefix}audio{audio_file.suffix}")

    thumb_file = next((p for p in workdir.glob("video.*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}), None)
    if thumb_file:
        put(thumb_file, f"{prefix}thumbnail{thumb_file.suffix}")

    if info_json:
        put(info_json, f"{prefix}metadata.json")
        description = info.get("description")
        if description:
            desc_path = workdir / "description.txt"
            desc_path.write_text(description, encoding="utf-8")
            put(desc_path, f"{prefix}description.txt")

    for sub_file in workdir.glob("video.*.vtt"):
        lang = sub_file.name[len("video."):-len(".vtt")]
        put(sub_file, f"{prefix}subtitles/{lang}.vtt")

    for sub_file in workdir.glob("auto.*.vtt"):
        lang = sub_file.name[len("auto."):-len(".vtt")]
        put(sub_file, f"{prefix}subtitles/{lang}.auto.vtt")

    return uploaded


# Bumped once: every manifest written before this had no guarantee the "video" file
# it listed actually contained a video stream (two separate bugs did exactly that -
# a filename collision, and yt-dlp silently settling for an audio-only format with
# no error). A manifest missing this exact version is treated as incomplete, which
# forces every existing video to be re-validated and re-uploaded once.
MANIFEST_SCHEMA_VERSION = 2


def read_manifest(s3, bucket: str, prefix: str) -> dict | None:
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"{prefix}manifest.json")
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return None
        raise
    return json.loads(obj["Body"].read())


def process_video(s3, bucket: str, slug: str, video_id: str, workdir_root: Path) -> tuple[str, dict | None]:
    """Returns ('new'|'skipped', extra) - extra carries {title, files} for 'new', else None."""
    prefix = f"channels/{slug}/videos/{video_id}/"
    existing = read_manifest(s3, bucket, prefix)
    if existing is not None and existing.get("schema_version") == MANIFEST_SCHEMA_VERSION:
        return "skipped", None

    workdir = workdir_root / video_id
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        download_video(video_id, workdir)
        uploaded = upload_video_artifacts(s3, bucket, prefix, workdir)
        info_json = next(workdir.glob("video.info.json"), None)
        title = json.loads(info_json.read_text(encoding="utf-8")).get("title", video_id) if info_json else video_id
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "video_id": video_id,
            "channel_slug": slug,
            "title": title,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "files": uploaded,
        }
        manifest_path = workdir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        s3.upload_file(str(manifest_path), bucket, f"{prefix}manifest.json")
        return "new", {"title": title, "files": uploaded}
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

    sheet_creds = get_sheet_credentials()
    channels = [c for c in read_sheet_channels(sheet_creds) if c["active"]]
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

        consecutive_bot_checks = 0
        for video_id in video_ids:
            if time.monotonic() - start > TIME_BUDGET_SECONDS:
                result.deferred += 1
                continue
            try:
                outcome, extra = process_video(s3, bucket, slug, video_id, workdir_root)
                consecutive_bot_checks = 0
                if outcome == "new":
                    result.new += 1
                    log(f"  {video_id}: downloaded and uploaded")
                    try:
                        log_captured_video(sheet_creds, channel["name"], extra["title"], video_id, extra["files"])
                    except Exception as exc:
                        log(f"  {video_id}: uploaded, but could not log to the sheet: {exc}")
                    time.sleep(INTER_VIDEO_DELAY_SECONDS)
                else:
                    result.skipped += 1
            except BotCheckTriggered as exc:
                consecutive_bot_checks += 1
                result.errors.append(f"{video_id}: bot-check ({exc})")
                log(f"  {video_id}: bot-check triggered - {exc}")
                # One bot-check hit can be a quirk of that one video; two in a row is
                # the session itself failing (expired/invalid cookies), so stop there.
                if consecutive_bot_checks >= 2:
                    aborted_reason = (
                        "YouTube's bot-check triggered on two videos in a row - this looks "
                        f"like the cookie session itself, not a single video: {exc}"
                    )
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
