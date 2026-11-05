# YouTube Dagbanli

A local-first command-line tool for building an organised, incremental archive of YouTube learning materials. It is designed for Dagbanli language resources, but works with any public YouTube channel.

**No API keys, paid services, or cloud database.** It uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) and FFmpeg on your own computer.

## Features

- Add, remove, and list channels.
- Download a channel's videos and later fetch only new uploads.
- Keep a persistent download archive to prevent duplicate downloads.
- Save video metadata (JSON) and thumbnails.
- Optionally collect subtitles and extract audio.
- Rebuild a CSV catalogue from metadata at any time.
- Keep configuration and media outside the Git repository.

## Requirements

- Ubuntu or another Linux distribution
- Python 3.10+
- `yt-dlp`
- FFmpeg (required for merging streams and audio extraction)

Install system dependencies:

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg pipx
pipx ensurepath
pipx install yt-dlp
```

Restart your terminal after `pipx ensurepath` if the `ydl` command is not found.

## Install

Clone the repository and install the command:

```bash
git clone https://github.com/Masssly/youtube-dagbanli.git
cd youtube-dagbanli
./install.sh
```

The installer creates an isolated Python environment and places the `ydl` launcher in `~/.local/bin`. Make sure that directory is on your PATH.

## Quick start

Add the Learn Dagbani channel:

```bash
ydl add https://www.youtube.com/@learndagbani9379/videos
ydl list
ydl sync
```

The first sync downloads the channel's available videos. Later syncs check for new uploads and skip videos already recorded in the archive.

## Commands

```text
ydl add <channel-url>       Add a channel
ydl remove <channel>        Remove a channel by name or URL
ydl list                    List configured channels
ydl sync                    Check all channels and download new videos
ydl sync --channel <name>   Sync one channel
ydl catalogue               Rebuild the CSV catalogue
ydl status                  Show library paths and download counts
ydl config                  Show current configuration
ydl config --library PATH   Change the media library directory
ydl config --audio          Enable MP3 extraction on future downloads
ydl config --subtitles      Enable subtitle collection on future downloads
ydl config --no-audio       Disable audio extraction
ydl config --no-subtitles   Disable subtitle collection
```

Use `ydl <command> --help` for command-specific options.

## Where files are stored

By default, all user data is stored outside the repository:

```text
~/.config/youtube-dagbanli/
  config.json
  channels.json
  downloaded-archive.txt

~/Videos/YouTube Dagbanli/
  <Channel name>/
    <Video title> [<video id>].mp4
    <Video title> [<video id>].info.json
    <Video title> [<video id>].jpg
    <Video title> [<video id>].mp3       (when audio extraction is enabled)
    <Video title> [<video id>].en.vtt    (when subtitles are enabled)
  catalogue.csv
```

The archive file stores YouTube video IDs, so running sync repeatedly does not download the same video again. Do not delete it unless you intentionally want to rebuild the archive.

## Scheduling regular checks

You can run `ydl sync` whenever you want. To run it automatically every day, use cron:

```bash
crontab -e
```

Add this line (adjust the path if your `ydl` command is installed elsewhere):

```cron
0 9 * * * $HOME/.local/bin/ydl sync >> $HOME/.local/share/youtube-dagbanli/sync.log 2>&1
```

This runs a check every day at 09:00. The computer must be on and connected to the internet.

## Notes

- The initial channel scan can take time. Large channels may contain many videos.
- YouTube may rate-limit requests or require cookies for some age-restricted or private content. This tool does not bypass access controls.
- Respect copyright, creator permissions, YouTube's Terms of Service, and applicable law. Download and reuse content only when you have the right to do so.
- This project is not affiliated with YouTube or Google.
