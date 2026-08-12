from __future__ import annotations

import os
import sys
from pathlib import Path


def defter_cache_dir() -> Path:
    override = os.environ.get("DEFTER_CACHE_DIR") or os.environ.get("DOCTEXT_CACHE_DIR")
    if override:
        return Path(override).expanduser()

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "dev.yordam.defter"

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Defter" / "Cache"
        return Path.home() / "AppData" / "Local" / "Defter" / "Cache"

    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / "defter"
    return Path.home() / ".cache" / "defter"


def defter_cache_path(*parts: str) -> Path:
    return defter_cache_dir().joinpath(*parts)


def doctext_cache_dir() -> Path:
    return defter_cache_dir()


def doctext_cache_path(*parts: str) -> Path:
    return defter_cache_path(*parts)
