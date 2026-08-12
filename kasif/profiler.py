from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


class Profiler:
    def __init__(self) -> None:
        self.started_at = time.perf_counter()
        self.events: List[Dict[str, Any]] = []
        self.totals: Dict[str, int] = {
            "httpBytesReceived": 0,
            "gitStdoutBytes": 0,
            "gitStderrBytes": 0,
            "gitObjectBytes": 0,
            "checkoutWorktreeBytes": 0,
        }

    @contextmanager
    def measure(self, stage: str, detail: str, **metadata: Any) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        except Exception as error:
            self.record(stage, detail, started, status="error", errorType=error.__class__.__name__, **metadata)
            raise
        else:
            self.record(stage, detail, started, **metadata)

    def record(self, stage: str, detail: str, started: float, status: str = "ok", **metadata: Any) -> None:
        event = {
            "stage": stage,
            "detail": detail,
            "status": status,
            "durationMs": round((time.perf_counter() - started) * 1000, 3),
        }
        event.update({key: value for key, value in metadata.items() if value is not None})
        self.events.append(event)

    def add_total(self, key: str, value: int) -> None:
        self.totals[key] = self.totals.get(key, 0) + max(value, 0)

    def set_total(self, key: str, value: int) -> None:
        self.totals[key] = max(value, 0)

    def to_dict(self) -> Dict[str, Any]:
        estimated_network = self.totals["httpBytesReceived"] + self.totals["gitObjectBytes"]
        return {
            "totalDurationMs": round((time.perf_counter() - self.started_at) * 1000, 3),
            "totals": {
                **self.totals,
                "estimatedNetworkBytesReceived": estimated_network,
            },
            "events": self.events,
            "notes": [
                "HTTP response bytes are exact for registry metadata requests.",
                "Git network bytes are estimated from the local .git object store after checkout.",
                "checkoutWorktreeBytes measures checked-out files and excludes .git.",
            ],
        }


def directory_size(path: Path, exclude_git: bool = False) -> int:
    total = 0
    if not path.exists():
        return total
    for root, dirs, files in os.walk(path):
        if exclude_git and ".git" in dirs:
            dirs.remove(".git")
        for file_name in files:
            file_path = Path(root) / file_name
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def git_object_store_size(checkout_root: Path) -> int:
    git_dir = checkout_root / ".git"
    if not git_dir.is_dir():
        return 0
    return directory_size(git_dir / "objects") + directory_size(git_dir / "packed-refs")


def output_profiler(profile: Optional[Profiler]) -> Optional[Dict[str, Any]]:
    return profile.to_dict() if profile is not None else None
