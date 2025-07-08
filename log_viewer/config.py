#!/usr/bin/env python3
"""
Configuration loader for the Textual Log Viewer.

Reads user-defined settings from `settings.conf` and returns a dictionary of values
used throughout the application, including log directories, buffer sizes, refresh rate,
and UI layout defaults.
"""
import configparser
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "settings.conf"

def load_config():
    """
    Load and parse configuration values from `settings.conf`.

    Returns:
        dict: A dictionary with the following keys:
            - log_dirs (list[Path]): List of directories to recursively scan for .log files.
            - max_buffer_lines (int): Max number of log lines to keep in memory per file.
            - default_show_lines (int): Number of lines to show when a file is first opened.
            - refresh_hz (int): Log update polling frequency in Hz.
            - default_tree_width (int): Initial width of the tree sidebar in columns.
            - min_show_lines (int): Minimum number of lines allowed in the view window.
            - show_step (int): Number of lines added/removed per zoom step.
    """

    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    viewer = config["log_viewer"]

    # parse and normalize all roots
    raw_dirs = viewer.get("log_dirs", "logs").split(",")
    log_dirs = []
    for p in raw_dirs:
        p = p.strip()
        if not p:
            continue
        path = Path(p).expanduser()
        if not path.is_absolute():
            raise ValueError(
                f"\n❌ Config Error in [log_viewer] → log_dirs:\n"
                f"   ➤ '{p}' is not an absolute path.\n"
                f"   Hint: It must start with a '/' (e.g. '/var/log', not 'var/log').\n"
                f"   Full setting: log_dirs = {viewer.get('log_dirs')}\n"
            )
        try:
            path = path.resolve()
        except FileNotFoundError:
            pass
        log_dirs.append(path)

    return {
        "log_dirs":          log_dirs,
        "max_buffer_lines":  viewer.getint("max_buffer_lines", 500),
        "default_show_lines":viewer.getint("default_show_lines", 40),
        "refresh_hz":        viewer.getint("refresh_hz", 4),
        "default_tree_width":viewer.getint("default_tree_width", 30),
        "min_show_lines":    viewer.getint("min_show_lines", 10),
        "show_step":         viewer.getint("show_step", 10),
    }
