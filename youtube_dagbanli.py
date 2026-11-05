#!/usr/bin/env python3
"""Local-first incremental YouTube channel archive manager."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

APP_NAME = "youtube-dagbanli"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
CHANNELS_FILE = CONFIG_DIR / "channels.json"
ARCHIVE_FILE = CONFIG_DIR / "downloaded-archive.txt"
AUDIO_ARCHIVE_FILE = CONFIG_DIR / "audio-archive.txt"
DEFAULT_LIBRARY = Path.home() / "Videos" / "YouTube Dagbanli"


def ensure_state() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        save_json(CONFIG_FILE, {"library": str(DEFAULT_LIBRARY), "audio": False, "subtitles": False})
    if not CHANNELS_FILE.exists():
        save_json(CHANNELS_FILE, {"channels": []})
    for path in (ARCHIVE_FILE, AUDIO_ARCHIVE_FILE):
        path.touch(exist_ok=True)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def get_config() -> dict:
    ensure_state()
    return load_json(CONFIG_FILE, {"library": str(DEFAULT_LIBRARY), "audio": False, "subtitles": False})


def get_channels() -> list[dict]:
    ensure_state()
    return load_json(CHANNELS_FILE, {"channels": []}).get("channels", [])


def library_path() -> Path:
    return Path(get_config()["library"]).expanduser()


def channel_slug(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if path.endswith("/videos"):
        path = path[:-7].rstrip("/")
    path = path.replace("@", "")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", path).strip("-._")
    return slug or "youtube-channel"


def normalise_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Please provide a channel URL.")
    if value.startswith("@"):
        value = "https://www.youtube.com/" + value
    elif not value.startswith(("https://", "http://")):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.netloc.lower() not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        raise ValueError("Please provide a YouTube channel URL.")
    if parsed.netloc.lower() == "youtu.be":
        raise ValueError("Please provide a channel URL, not an individual video URL.")
    path = parsed.path.rstrip("/")
    if not path.endswith("/videos") and (path.startswith("/@") or path.startswith("/channel/") or path.startswith("/c/") or path.startswith("/user/")):
        path += "/videos"
    return f"https://www.youtube.com{path}"


def require_program(name: str) -> str:
    found = shutil.which(name)
    if not found:
        print(f"Missing dependency: {name}. Install it and try again.", file=sys.stderr)
        if name == "yt-dlp":
            print("Suggested: pipx install yt-dlp", file=sys.stderr)
        elif name == "ffmpeg":
            print("Suggested: sudo apt install ffmpeg", file=sys.stderr)
        raise SystemExit(2)
    return found


def add_channel(args: argparse.Namespace) -> None:
    try:
        url = normalise_url(args.url)
    except ValueError as exc:
        raise SystemExit(str(exc))
    channels = get_channels()
    key = channel_slug(url)
    for channel in channels:
        if channel["url"] == url or channel["key"] == key:
            print(f"Already in your channel list: {channel['name']}")
            return
    name = args.name.strip() if args.name else key.replace("-", " ").replace("_", " ").title()
    channels.append({"key": key, "name": name, "url": url, "added_at": datetime.now(timezone.utc).isoformat()})
    save_json(CHANNELS_FILE, {"channels": channels})
    print(f"Added: {name}")
    print("Run 'ydl sync' to download its videos.")


def remove_channel(args: argparse.Namespace) -> None:
    channels = get_channels()
    needle = args.channel.casefold()
    remaining = [c for c in channels if needle not in {c["key"].casefold(), c["name"].casefold(), c["url"].casefold()}]
    if len(remaining) == len(channels):
        print(f"No channel matched: {args.channel}")
        raise SystemExit(1)
    save_json(CHANNELS_FILE, {"channels": remaining})
    print(f"Removed {len(channels) - len(remaining)} channel entry. Existing downloaded files are not deleted.")


def list_channels(_: argparse.Namespace) -> None:
    channels = get_channels()
    if not channels:
        print("No channels yet. Add one with: ydl add <channel-url>")
        return
    for index, channel in enumerate(channels, 1):
        print(f"{index}. {channel['name']}  [{channel['key']}]\n   {channel['url']}")


def run_ytdlp(command: list[str]) -> int:
    print("\n$ " + " ".join(subprocess.list2cmdline([part]) for part in command), flush=True)
    try:
        return subprocess.run(command, check=False).returncode
    except KeyboardInterrupt:
        print("\nInterrupted. yt-dlp can resume on the next sync.")
        return 130


def sync_channels(args: argparse.Namespace) -> None:
    yt_dlp = require_program("yt-dlp")
    library = library_path()
    library.mkdir(parents=True, exist_ok=True)
    channels = get_channels()
    if args.channel:
        needle = args.channel.casefold()
        channels = [c for c in channels if needle in {c["key"].casefold(), c["name"].casefold()}]
        if not channels:
            raise SystemExit(f"No configured channel matched: {args.channel}")
    if not channels:
        raise SystemExit("No channels configured. Add one with: ydl add <channel-url>")

    config = get_config()
    failures = []
    for channel in channels:
        print(f"\n{'=' * 68}\nChannel: {channel['name']}\n{'=' * 68}", flush=True)
        output = str(library / channel["key"] / "%(title)s [%(id)s].%(ext)s")
        base = [
            yt_dlp, "--yes-playlist", "--ignore-errors", "--continue",
            "--no-overwrites", "--download-archive", str(ARCHIVE_FILE),
            "--format", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "--merge-output-format", "mp4",
            "--write-info-json", "--write-thumbnail",
            "--windows-filenames", "--trim-filenames", "180",
            "--output", output,
        ]
        if config.get("subtitles"):
            base += ["--write-subs", "--write-auto-subs", "--sub-langs", "dag,en.*,en", "--sub-format", "vtt/best"]
        if args.limit:
            base += ["--playlist-end", str(args.limit)]
        code = run_ytdlp(base + [channel["url"]])
        if code not in (0, 1):
            failures.append(channel["name"])

        if config.get("audio"):
            audio_command = [
                yt_dlp, "--yes-playlist", "--ignore-errors", "--continue",
                "--no-overwrites", "--download-archive", str(AUDIO_ARCHIVE_FILE),
                "--format", "bestaudio/best", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
                "--write-info-json", "--output", output, "--windows-filenames",
                "--trim-filenames", "180",
            ]
            if args.limit:
                audio_command += ["--playlist-end", str(args.limit)]
            audio_code = run_ytdlp(audio_command + [channel["url"]])
            if audio_code not in (0, 1):
                failures.append(channel["name"] + " (audio)")

    build_catalogue(library)
    print("\nSync finished.")
    print(f"Library: {library}")
    if failures:
        print("Some operations reported errors: " + ", ".join(failures))
        print("Check the output above; rerun 'ydl sync' to retry incomplete items.")
    else:
        print("No fatal errors reported.")


def build_catalogue(library: Path) -> int:
    fields = [
        "channel", "title", "video_id", "url", "upload_date", "release_timestamp",
        "duration_seconds", "duration", "language", "description", "view_count",
        "uploader", "webpage_url", "video_file", "audio_file", "thumbnail_file", "metadata_file",
    ]
    rows = []
    for info_path in library.rglob("*.info.json"):
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        video_id = info.get("id", "")
        stem = info_path.name[:-len(".info.json")]
        folder = info_path.parent
        video = next((p for p in folder.glob(stem + ".*") if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}), None)
        audio = next(iter(folder.glob(stem + ".mp3")), None)
        thumb = next((p for p in folder.glob(stem + ".*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}), None)
        duration = info.get("duration")
        rows.append({
            "channel": info.get("channel") or folder.name,
            "title": info.get("title", ""),
            "video_id": video_id,
            "url": info.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else ""),
            "upload_date": info.get("upload_date", ""),
            "release_timestamp": info.get("release_timestamp", ""),
            "duration_seconds": duration if duration is not None else "",
            "duration": info.get("duration_string", ""),
            "language": info.get("language", ""),
            "description": info.get("description", ""),
            "view_count": info.get("view_count", ""),
            "uploader": info.get("uploader", ""),
            "webpage_url": info.get("original_url") or info.get("webpage_url", ""),
            "video_file": str(video) if video else "",
            "audio_file": str(audio) if audio else "",
            "thumbnail_file": str(thumb) if thumb else "",
            "metadata_file": str(info_path),
        })
    rows.sort(key=lambda row: (row["channel"].casefold(), row["upload_date"], row["title"].casefold()))
    target = library / "catalogue.csv"
    library.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def catalogue_command(_: argparse.Namespace) -> None:
    count = build_catalogue(library_path())
    print(f"Catalogue rebuilt: {count} videos")
    print(library_path() / "catalogue.csv")


def status_command(_: argparse.Namespace) -> None:
    library = library_path()
    channels = get_channels()
    archive_count = sum(1 for line in ARCHIVE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()) if ARCHIVE_FILE.exists() else 0
    metadata_count = sum(1 for _ in library.rglob("*.info.json")) if library.exists() else 0
    print(f"Configured channels: {len(channels)}")
    print(f"Archived video IDs: {archive_count}")
    print(f"Metadata files in library: {metadata_count}")
    print(f"Library: {library}")
    print(f"Configuration: {CONFIG_DIR}")


def config_command(args: argparse.Namespace) -> None:
    config = get_config()
    changed = False
    if args.library:
        config["library"] = str(Path(args.library).expanduser().resolve())
        changed = True
    if args.audio is not None:
        config["audio"] = args.audio
        changed = True
    if args.subtitles is not None:
        config["subtitles"] = args.subtitles
        changed = True
    if changed:
        save_json(CONFIG_FILE, config)
        print("Configuration updated.")
    print(json.dumps(config, indent=2))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ydl",
        description="Manage an incremental local archive of YouTube channels.",
        epilog="Example: ydl add https://www.youtube.com/@learndagbani9379/videos && ydl sync",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Add a YouTube channel")
    add.add_argument("url", help="YouTube channel URL")
    add.add_argument("--name", help="Optional friendly name")
    add.set_defaults(func=add_channel)

    remove = sub.add_parser("remove", help="Remove a configured channel (does not delete files)")
    remove.add_argument("channel", help="Channel name, key, or exact URL")
    remove.set_defaults(func=remove_channel)

    listing = sub.add_parser("list", help="List configured channels")
    listing.set_defaults(func=list_channels)

    sync = sub.add_parser("sync", help="Download new videos from configured channels")
    sync.add_argument("--channel", help="Only sync the matching channel name or key")
    sync.add_argument("--limit", type=int, help="Limit each channel to its first N listed videos (useful for testing)")
    sync.set_defaults(func=sync_channels)

    cat = sub.add_parser("catalogue", help="Rebuild catalogue.csv from saved metadata")
    cat.set_defaults(func=catalogue_command)

    status = sub.add_parser("status", help="Show archive and library status")
    status.set_defaults(func=status_command)

    cfg = sub.add_parser("config", help="View or change settings")
    cfg.add_argument("--library", help="Set media library directory")
    audio = cfg.add_mutually_exclusive_group()
    audio.add_argument("--audio", dest="audio", action="store_true", help="Enable MP3 extraction")
    audio.add_argument("--no-audio", dest="audio", action="store_false", help="Disable MP3 extraction")
    subtitles = cfg.add_mutually_exclusive_group()
    subtitles.add_argument("--subtitles", dest="subtitles", action="store_true", help="Enable subtitle collection")
    subtitles.add_argument("--no-subtitles", dest="subtitles", action="store_false", help="Disable subtitle collection")
    cfg.set_defaults(func=config_command, audio=None, subtitles=None)
    return parser


def main() -> None:
    parser = make_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
