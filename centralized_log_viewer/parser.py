#!/usr/bin/env python3
"""
Log Parser and Filtering Utilities

Provides functions to:
- Parse structured log lines into components (timestamp, level, message)
- Apply filtering by log level, regex match, and time ranges
- Load and aggregate lines from multiple log files
- Convert relative time shortcuts (e.g., '15m', '2h') into datetime ranges

Used by the log query CLI tool to search and extract meaningful logs.
"""
import re
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - (\w+) - (.*)$")


def parse_log_line(line: str):
    """
    Parse a single log line into structured components.

    Expected log format:
        YYYY-MM-DD HH:MM:SS,mmm - LEVEL - message

    Args:
        line (str): Raw log line as a string.

    Returns:
        dict | None: Parsed components as a dictionary:
            {
                "timestamp": datetime,
                "level": str,
                "message": str,
                "raw": str
            }
        Returns None if the line doesn't match the expected format.
    """

    match = LOG_LINE_RE.match(line)
    if not match:
        return None
    try:
        timestamp_str, level, message = match.groups()
        timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S,%f")
        return {
            "timestamp": timestamp,
            "level": level,
            "message": message,
            "raw": line
        }
    except Exception:
        return None


def filter_log_lines(lines: List[str], *,
                     level: Optional[str] = None,
                     regex: Optional[str] = None,
                     start: Optional[datetime] = None,
                     end: Optional[datetime] = None) -> List[str]:
    """
    Filter log lines based on level, regex match, and time window.

    Args:
        lines (List[str]): List of raw log lines to process.
        level (Optional[str]): Log level to match (e.g., "ERROR", "INFO").
        regex (Optional[str]): Regex pattern to apply to the message field.
        start (Optional[datetime]): Lower bound for timestamp filtering (inclusive).
        end (Optional[datetime]): Upper bound for timestamp filtering (inclusive).

    Returns:
        List[str]: List of raw lines that match all specified filters.
    """

    pattern = re.compile(regex) if regex else None
    filtered = []

    for line in lines:
        parsed = parse_log_line(line)
        if not parsed:
            continue

        if level and parsed["level"].upper() != level.upper():
            continue
        if pattern and not pattern.search(parsed["message"]):
            continue
        if start and parsed["timestamp"] < start:
            continue
        if end and parsed["timestamp"] > end:
            continue

        filtered.append(parsed["raw"])

    return filtered


def parse_timerange(shortcut: str) -> Tuple[datetime, datetime]:
    """
    Parse a relative time shortcut (like '15m', '2h', '1d') into a datetime range.

    Args:
        shortcut (str): Relative time string (e.g., '30m', '2h', '1d').

    Returns:
        Tuple[datetime, datetime]: Start and end datetime, where end is `now`.

    Raises:
        ValueError: If the format is unrecognized or invalid.
    """
    now = datetime.now()
    shortcut = shortcut.lower().strip()

    if shortcut.endswith("m"):
        minutes = int(shortcut[:-1])
        return now - timedelta(minutes=minutes), now
    elif shortcut.endswith("h"):
        hours = int(shortcut[:-1])
        return now - timedelta(hours=hours), now
    elif shortcut.endswith("d"):
        days = int(shortcut[:-1])
        return now - timedelta(days=days), now
    else:
        raise ValueError("Invalid time range shortcut. Use '15m', '2h', '1d', etc.")


def load_logs_from_files(file_paths: List[str]) -> List[str]:
    """
    Read all lines from the specified list of log file paths.

    Args:
        file_paths (List[str]): List of file paths to read from.

    Returns:
        List[str]: Combined list of all lines from all readable files.
        If a file fails to open, an error is printed and the file is skipped.
    """
    all_lines = []
    for path in file_paths:
        try:
            with open(path, "r") as f:
                all_lines.extend(f.readlines())
        except Exception as e:
            print(f"[ERROR] Failed to read {path}: {e}")
    return all_lines
