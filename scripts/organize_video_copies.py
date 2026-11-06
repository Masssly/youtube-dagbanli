#!/usr/bin/env python3
"""Copy just the video files from ydl's flat per-channel library into a
separate folder, organized into one subfolder per channel.

Read-only on the source - never touches ydl's own library layout. Never
deletes anything on either side, so a video stays here even after the
channel removes it from YouTube: ydl itself already never deletes a file
it has downloaded, and this script only ever adds, never removes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SOURCE_LIBRARY = Path.home() / "Videos" / "YouTube Dagbanli"
DEST_LIBRARY = Path.home() / "Videos" / "Dagbanli Videos"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov"}


def main() -> None:
    if not SOURCE_LIBRARY.exists():
        raise SystemExit(f"Source library not found: {SOURCE_LIBRARY}")

    copied = 0
    skipped = 0
    for channel_dir in sorted(p for p in SOURCE_LIBRARY.iterdir() if p.is_dir()):
        dest_channel_dir = DEST_LIBRARY / channel_dir.name
        dest_channel_dir.mkdir(parents=True, exist_ok=True)

        for video_file in sorted(channel_dir.iterdir()):
            if video_file.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            dest_file = dest_channel_dir / video_file.name
            if dest_file.exists():
                skipped += 1
                continue
            shutil.copy2(video_file, dest_file)
            copied += 1
            print(f"Copied: {channel_dir.name}/{video_file.name}")

    print(f"\nDone. {copied} new video(s) copied, {skipped} already present.")
    print(f"Organized copies: {DEST_LIBRARY}")


if __name__ == "__main__":
    main()
