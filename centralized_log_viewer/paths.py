#!/usr/bin/env python3
"""
Path and resource resolution helpers for CLV.

This centralizes logic for discovering:
- User config file (XDG config home preferred)
- Static resource directory (next to the frozen binary or in a system path)
- Individual resource files like the CSS

Precedence rules:
1) Config: $XDG_CONFIG_HOME/clv/settings.conf, else ~/.config/clv/settings.conf
2) Static dir (assets): directory of the running executable (when frozen), else
   '/usr/local/bin' if it exists, else the package directory for development.
3) Direct file fallback: if a specific file is not found in the static dir,
   attempt '/usr/local/bin/<name>' and finally package directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def _is_frozen() -> bool:
    """Return True if running from a PyInstaller-frozen bundle."""
    return getattr(sys, "frozen", False) is True


def get_xdg_config_home() -> Path:
    """Resolve the XDG config home directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser()
    return Path.home() / ".config"


def get_config_file() -> Optional[Path]:
    """
    Locate the settings.conf file using the following precedence:
    - $XDG_CONFIG_HOME/clv/settings.conf (or ~/.config/clv/settings.conf)
    - Next to the executable when frozen
    - /usr/local/bin/settings.conf
    - Project root settings.conf (development fallback)
    Returns the first existing path, or None if not found.
    """
    # XDG config
    xdg_conf = get_xdg_config_home() / "clv" / "settings.conf"
    if xdg_conf.exists():
        return xdg_conf

    # Frozen bundle/executable directory
    if _is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        bundle_conf = exe_dir / "settings.conf"
        if bundle_conf.exists():
            return bundle_conf

    # Static system location
    usr_local_conf = Path("/usr/local/bin/settings.conf")
    if usr_local_conf.exists():
        return usr_local_conf

    # Development fallback: repo root settings.conf (two parents up from this file)
    dev_conf = Path(__file__).resolve().parents[1] / "settings.conf"
    if dev_conf.exists():
        return dev_conf

    return None


def get_static_dir() -> Path:
    """
    Determine where static resources (like CSS) should be read from at runtime.
    Order:
      - Frozen bundle/executable directory
      - /usr/local/bin if it exists
      - Package directory (development)
    """
    if _is_frozen():
        return Path(sys.executable).resolve().parent

    usr_local = Path("/usr/local/bin")
    if usr_local.exists():
        return usr_local

    # Package directory for development
    return Path(__file__).resolve().parent


def get_resource_path(name: str) -> Optional[Path]:
    """
    Resolve a resource file by name using get_static_dir(),
    then try '/usr/local/bin/<name>' and finally the package directory.
    Returns a Path if found, otherwise None.
    """
    static_dir = get_static_dir()
    candidate = static_dir / name
    if candidate.exists():
        return candidate

    usr_local = Path("/usr/local/bin") / name
    if usr_local.exists():
        return usr_local

    pkg_dir = Path(__file__).resolve().parent / name
    if pkg_dir.exists():
        return pkg_dir

    return None

