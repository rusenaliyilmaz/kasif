from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def kasif_cache_dir() -> Path:
    override = os.environ.get("KASIF_CACHE_DIR") or os.environ.get("DEFTER_CACHE_DIR") or os.environ.get("DOCTEXT_CACHE_DIR")
    if override:
        return Path(override).expanduser()

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "dev.kasif"

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Kasif" / "Cache"
        return Path.home() / "AppData" / "Local" / "Kasif" / "Cache"

    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / "kasif"
    return Path.home() / ".cache" / "kasif"


def kasif_cache_path(*parts: str) -> Path:
    return kasif_cache_dir().joinpath(*parts)


def cache_path_from(cache_dir: Optional[Path], *parts: str) -> Path:
    parent = Path(cache_dir).expanduser() if cache_dir is not None else kasif_cache_dir()
    return parent.joinpath(*parts)


def defter_cache_dir() -> Path:
    return kasif_cache_dir()


def defter_cache_path(*parts: str) -> Path:
    return kasif_cache_path(*parts)


def doctext_cache_dir() -> Path:
    return defter_cache_dir()


def doctext_cache_path(*parts: str) -> Path:
    return defter_cache_path(*parts)
