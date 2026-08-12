from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .model import Dependency
from .parsers import (
    parse_bun_lock,
    parse_cmake_lists,
    parse_conanfile_py,
    parse_conanfile_txt,
    parse_go_mod,
    parse_gradle,
    parse_gradle_lockfile,
    parse_gradle_version_catalog,
    parse_maven_pom,
    parse_npm_lock,
    parse_npm_package,
    parse_pipfile,
    parse_pipfile_lock,
    parse_pnpm_lock,
    parse_poetry_lock,
    parse_pubspec,
    parse_python_setup_py,
    parse_python_requirements,
    parse_python_setup_cfg,
    parse_python_pyproject,
    parse_vcpkg_json,
    parse_yarn_lock,
)


DEFAULT_IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "target",
    "build",
    "dist",
    ".dart_tool",
}


Parser = Callable[[Path, Path], List[Dependency]]
Progress = Callable[[str], None]


MANIFEST_PARSERS: Dict[str, Parser] = {
    "pom.xml": parse_maven_pom,
    "package.json": parse_npm_package,
    "package-lock.json": parse_npm_lock,
    "pnpm-lock.yaml": parse_pnpm_lock,
    "yarn.lock": parse_yarn_lock,
    "bun.lock": parse_bun_lock,
    "pubspec.yaml": parse_pubspec,
    "pubspec.yml": parse_pubspec,
    "go.mod": parse_go_mod,
    "pyproject.toml": parse_python_pyproject,
    "setup.cfg": parse_python_setup_cfg,
    "setup.py": parse_python_setup_py,
    "Pipfile": parse_pipfile,
    "Pipfile.lock": parse_pipfile_lock,
    "poetry.lock": parse_poetry_lock,
    "build.gradle": parse_gradle,
    "build.gradle.kts": parse_gradle,
    "gradle.lockfile": parse_gradle_lockfile,
    "libs.versions.toml": parse_gradle_version_catalog,
    "vcpkg.json": parse_vcpkg_json,
    "conanfile.txt": parse_conanfile_txt,
    "conanfile.py": parse_conanfile_py,
    "CMakeLists.txt": parse_cmake_lists,
}


def discover_dependencies(
    root: Path,
    use_default_ignores: bool = True,
    progress: Optional[Progress] = None,
) -> List[Dependency]:
    dependencies: List[Dependency] = []
    root = root.resolve()
    file_count = 0
    manifest_count = 0
    last_progress = time.monotonic()
    report_progress(progress, f"scanning project files under {root}")
    for path in iter_project_files(root, use_default_ignores):
        file_count += 1
        now = time.monotonic()
        if now - last_progress >= 5:
            report_progress(
                progress,
                f"scanning files... files_seen={file_count} manifests_seen={manifest_count} current={safe_relative(path, root)}",
            )
            last_progress = now
        if should_skip_manifest(path):
            continue
        parser = parser_for(path)
        if parser is None:
            continue
        manifest_count += 1
        report_progress(progress, f"parsing manifest {safe_relative(path, root)}")
        parsed = parse_manifest(parser, path, root, progress)
        dependencies.extend(parsed)
        report_progress(progress, f"parsed {safe_relative(path, root)} dependencies={len(parsed)}")
    deduped = sorted(deduplicate(dependencies), key=dependency_sort_key)
    report_progress(
        progress,
        f"dependency discovery complete files_seen={file_count} manifests_seen={manifest_count} dependencies={len(deduped)}",
    )
    return deduped


def iter_project_files(root: Path, use_default_ignores: bool) -> Iterable[Path]:
    ignored = DEFAULT_IGNORED_DIRECTORIES if use_default_ignores else set()
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if safe_is_dir(entry):
                if entry.name not in ignored:
                    stack.append(entry)
                continue
            if safe_is_file(entry):
                yield entry


def parse_manifest(parser: Parser, path: Path, root: Path, progress: Optional[Progress]) -> List[Dependency]:
    try:
        return parser(path, root)
    except Exception as error:
        # Manifest parsers consume user-owned project files; a malformed or
        # concurrently modified file should not abort discovery for the repo.
        report_progress(
            progress,
            f"skipped manifest {safe_relative(path, root)} error={error.__class__.__name__}",
        )
        return []


def safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir() and not path.is_symlink()
    except OSError:
        return False


def safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def parser_for(path: Path) -> Optional[Parser]:
    if path.name in MANIFEST_PARSERS:
        return MANIFEST_PARSERS[path.name]
    if path.name.startswith("requirements") and path.suffix == ".txt":
        return parse_python_requirements
    return None


def should_skip_manifest(path: Path) -> bool:
    primary_by_lock = {
        "package-lock.json": "package.json",
        "pnpm-lock.yaml": "package.json",
        "yarn.lock": "package.json",
        "bun.lock": "package.json",
        "Pipfile.lock": "Pipfile",
        "poetry.lock": "pyproject.toml",
    }
    primary = primary_by_lock.get(path.name)
    return primary is not None and path.with_name(primary).exists()


def deduplicate(dependencies: Iterable[Dependency]) -> List[Dependency]:
    seen: Set[Dependency] = set()
    result: List[Dependency] = []
    for dependency in dependencies:
        if dependency in seen:
            continue
        seen.add(dependency)
        result.append(dependency)
    return result


def dependency_sort_key(dependency: Dependency) -> Tuple[str, str, str, str]:
    return (
        dependency.ecosystem,
        dependency.name.lower(),
        dependency.scope,
        dependency.source_file,
    )


def report_progress(progress: Optional[Progress], message: str) -> None:
    if progress is not None:
        progress(message)


def safe_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
