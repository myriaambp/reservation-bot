"""Shared logging utilities for reservation bot."""

import json
from pathlib import Path

LOG_FILE = Path(__file__).parent / "reservations_log.json"


def load_log():
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return []


def save_log(entries):
    LOG_FILE.write_text(json.dumps(entries, indent=2))


def log_entry(entry):
    entries = load_log()
    entries.append(entry)
    save_log(entries)
