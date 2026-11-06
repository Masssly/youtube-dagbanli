#!/usr/bin/env python3
"""Run on the local ydl machine: pull active channels from the control sheet,
add any new ones, run a sync, then push the rebuilt catalogue back to the sheet.

Separate from ydl itself - this only wraps it as a subprocess, so ydl's own
code and behavior are untouched.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

SHEET_ID = "19QDNPhJnkTIGiflxk0J8i_6BbMt2GFtLbMK4k1pBtf4"
CHANNELS_RANGE = "Sheet1!A2:D5000"
CATALOGUE_RANGE = "'Local catalogue'!A1:Q5000"
SERVICE_ACCOUNT_PATH = Path.home() / ".config" / "youtube-dagbanli" / "service-account.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
LIBRARY_CSV = Path.home() / "Videos" / "YouTube Dagbanli" / "catalogue.csv"


def log(msg: str) -> None:
    print(msg, flush=True)


def get_credentials() -> service_account.Credentials:
    if not SERVICE_ACCOUNT_PATH.exists():
        raise SystemExit(f"Missing {SERVICE_ACCOUNT_PATH} - copy the service account JSON key there first.")
    creds = service_account.Credentials.from_service_account_file(str(SERVICE_ACCOUNT_PATH), scopes=SCOPES)
    creds.refresh(GoogleAuthRequest())
    return creds


def sheet_get(creds: service_account.Credentials, a1_range: str) -> list[list[str]]:
    resp = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{a1_range}",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("values", [])


def sheet_clear(creds: service_account.Credentials, a1_range: str) -> None:
    resp = requests.post(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{a1_range}:clear",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=30,
    )
    resp.raise_for_status()


def sheet_put(creds: service_account.Credentials, a1_range: str, values: list[list[str]]) -> None:
    resp = requests.put(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{a1_range}",
        params={"valueInputOption": "RAW"},
        headers={"Authorization": f"Bearer {creds.token}"},
        json={"values": values},
        timeout=30,
    )
    resp.raise_for_status()


def existing_channel_urls() -> set[str]:
    proc = subprocess.run(["ydl", "list"], capture_output=True, text=True)
    return {line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("http")}


def add_new_channels(creds: service_account.Credentials) -> None:
    rows = sheet_get(creds, CHANNELS_RANGE)
    current = existing_channel_urls()
    for row in rows:
        name = row[0].strip() if len(row) > 0 else ""
        url = row[1].strip() if len(row) > 1 else ""
        active = row[2].strip().upper() == "TRUE" if len(row) > 2 else False
        if not name or not url or not active or url in current:
            continue
        log(f"Adding channel from the sheet: {name}")
        subprocess.run(["ydl", "add", url, "--name", name], check=False)


def push_catalogue(creds: service_account.Credentials) -> None:
    if not LIBRARY_CSV.exists():
        log(f"No catalogue yet at {LIBRARY_CSV}, skipping sheet push.")
        return
    with open(LIBRARY_CSV, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    sheet_clear(creds, CATALOGUE_RANGE)
    if rows:
        sheet_put(creds, "'Local catalogue'!A1", rows)
    log(f"Pushed {max(len(rows) - 1, 0)} video row(s) to the 'Local catalogue' tab.")


def main() -> None:
    creds = get_credentials()
    add_new_channels(creds)

    log("Running ydl sync...")
    subprocess.run(["ydl", "sync"], check=False)
    subprocess.run(["ydl", "catalogue"], check=False)

    push_catalogue(creds)


if __name__ == "__main__":
    main()
