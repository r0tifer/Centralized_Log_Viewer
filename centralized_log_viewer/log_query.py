#!/usr/bin/env python3
"""
Log Query Utilities

Provides helper functions for filtering and transforming log lines using
regex patterns, time range expressions, and basic string matching.

Used by the log viewer to apply user-defined filters on the log buffer
before rendering to the output window.
"""

import sys
from pathlib import Path

import argparse
import glob
import sys
from datetime import datetime
from parser import load_logs_from_files, filter_log_lines, parse_timerange


def parse_datetime_range(range_str):
    """
    Parse an absolute datetime range string into two datetime objects.

    Expected format:
        'YYYY-MM-DD HH:MM to YYYY-MM-DD HH:MM'

    Args:
        range_str (str): A string representing a datetime range.

    Returns:
        tuple[datetime, datetime]: The start and end datetime objects.

    Raises:
        SystemExit: If the input format is invalid or parsing fails.
    """
    try:
        if "to" in range_str:
            start_str, end_str = [s.strip() for s in range_str.split("to")]
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end_str)
            return start_dt, end_dt
        else:
            raise ValueError("Invalid format. Use 'YYYY-MM-DD HH:MM to YYYY-MM-DD HH:MM'")
    except Exception as e:
        print(f"[ERROR] Failed to parse --range: {e}")
        sys.exit(1)


def main():
    """
    Command-line entry point for log filtering.

    Parses arguments for:
        --files : One or more log files or glob patterns.
        --level : Log level to match (e.g., INFO, ERROR).
        --regex : Regex pattern to apply to log messages.
        --last  : Relative time window (e.g., '15m', '1h', '2d').
        --range : Exact datetime window ('YYYY-MM-DD HH:MM to YYYY-MM-DD HH:MM').

    Loads, filters, and prints matching log lines to stdout.
    """
    parser = argparse.ArgumentParser(description="Filter and search Sheldon logs.")
    parser.add_argument("--files", nargs="+", help="Log file(s) or glob pattern(s) to search.", required=True)
    parser.add_argument("--level", help="Log level to filter by (INFO, ERROR, DEBUG, etc.)")
    parser.add_argument("--regex", help="Regex to search message content")
    parser.add_argument("--last", help="Relative time window like '15m', '2h', '1d'")
    parser.add_argument("--range", help="Exact time window: 'YYYY-MM-DD HH:MM to YYYY-MM-DD HH:MM'")

    args = parser.parse_args()

    file_list = []
    for f in args.files:
        file_list.extend(glob.glob(f))

    if not file_list:
        print("[ERROR] No matching files found.")
        sys.exit(1)

    start_time = end_time = None
    if args.last:
        try:
            start_time, end_time = parse_timerange(args.last)
        except Exception as e:
            print(f"[ERROR] Invalid --last value: {e}")
            sys.exit(1)
    elif args.range:
        start_time, end_time = parse_datetime_range(args.range)

    lines = load_logs_from_files(file_list)
    filtered = filter_log_lines(
        lines,
        level=args.level,
        regex=args.regex,
        start=start_time,
        end=end_time
    )

    for line in filtered:
        print(line, end="")


if __name__ == "__main__":
    main()
