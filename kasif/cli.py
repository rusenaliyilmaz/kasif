from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional

from .discovery import discover_dependencies
from .model import Dependency
from .profiler import Profiler
from .source_lookup import enrich_dependencies, find_source


def build_scan_parser(prog: str = "kasif") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Discover direct package dependencies in a project directory.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Project directory to scan. Defaults to the current directory.",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format.",
    )
    parser.add_argument(
        "--no-default-ignores",
        action="store_true",
        help="Do not skip common generated/vendor directories such as node_modules, target, and .git.",
    )
    parser.add_argument(
        "--resolve-sources",
        action="store_true",
        help="Resolve verified source repositories for exact-version dependencies.",
    )
    parser.add_argument(
        "--kasif-checkout-dir",
        type=Path,
        default=None,
        help="Checkout directory passed to Kasif source lookup.",
    )
    parser.add_argument(
        "--git-timeout-seconds",
        type=int,
        default=300,
        help="Timeout per Git command in Kasif source lookup.",
    )
    parser.add_argument(
        "--max-source-resolutions",
        type=int,
        default=None,
        help="Maximum exact-version dependencies to resolve with registry and Git lookups.",
    )
    parser.add_argument(
        "--enable-profiler",
        action="store_true",
        help="Print source lookup timing and traffic diagnostics to stderr.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print scan and source-resolution progress to stderr without changing stdout output.",
    )
    parser.add_argument(
        "--shallow-check",
        action="store_true",
        help="Resolve refs without cloning or verifying manifests during source lookup.",
    )
    return parser


def build_find_source_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kasif find-source",
        description="Resolve the verified source repository for one package coordinate.",
    )
    parser.add_argument("--ecosystem", required=True, choices=("maven", "npm", "pypi", "pub", "go", "git"))
    parser.add_argument("--package", required=True, dest="package_name")
    parser.add_argument("--version", required=True)
    parser.add_argument("--format", choices=("json", "table"), default="json")
    parser.add_argument(
        "--kasif-checkout-dir",
        type=Path,
        default=None,
        help="Directory where temporary Git checkouts are created.",
    )
    parser.add_argument(
        "--git-timeout-seconds",
        type=int,
        default=300,
        help="Timeout per Git command in source lookup.",
    )
    parser.add_argument(
        "--enable-profiler",
        action="store_true",
        help="Print source lookup timing and traffic diagnostics to stderr.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print source-resolution progress to stderr without changing stdout output.",
    )
    parser.add_argument(
        "--shallow-check",
        action="store_true",
        help="Resolve the package version ref without cloning or verifying manifests.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--help", "-h"):
        print_general_help()
        return 0
    if argv and argv[0] == "find-source":
        return find_source_main(argv[1:])
    if argv and argv[0] == "scan":
        argv = argv[1:]
        parser = build_scan_parser("kasif scan")
    else:
        parser = build_scan_parser()
    args = parser.parse_args(argv)
    root = Path(args.directory).expanduser().resolve()
    if not root.exists():
        parser.error(f"directory does not exist: {root}")
    if not root.is_dir():
        parser.error(f"path is not a directory: {root}")
    if args.git_timeout_seconds <= 0:
        parser.error("--git-timeout-seconds must be greater than zero")
    if args.max_source_resolutions is not None and args.max_source_resolutions < 0:
        parser.error("--max-source-resolutions must be zero or greater")

    profiler = Profiler() if args.enable_profiler else None
    progress = print_progress if args.progress else None
    dependencies = discover_dependencies(root, use_default_ignores=not args.no_default_ignores, progress=progress)
    if args.resolve_sources:
        dependencies = enrich_dependencies(
            dependencies,
            checkout_dir=args.kasif_checkout_dir,
            git_timeout_seconds=args.git_timeout_seconds,
            profiler=profiler,
            shallow_check=args.shallow_check,
            progress=progress,
            max_source_resolutions=args.max_source_resolutions,
        )
    if args.format == "json":
        print(json.dumps([dependency.to_dict() for dependency in dependencies], indent=2, sort_keys=True))
    else:
        print_table(dependencies)
    sys.stdout.flush()
    print_profiler(profiler)
    return 0


def print_general_help() -> None:
    print("""usage: kasif <command> [options]

Commands:
  scan         Discover dependencies in a project directory.
  find-source  Resolve the verified source repository for one package version.

Examples:
  kasif scan /path/to/project --resolve-sources --format json
  kasif find-source --ecosystem pypi --package tzlocal --version 3.0

For command-specific help:
  kasif scan --help
  kasif find-source --help
""")


def find_source_main(argv: List[str]) -> int:
    parser = build_find_source_parser()
    args = parser.parse_args(argv)
    if args.git_timeout_seconds <= 0:
        parser.error("--git-timeout-seconds must be greater than zero")
    profiler = Profiler() if args.enable_profiler else None
    progress = print_progress if args.progress else None
    response = find_source(
        args.ecosystem,
        args.package_name,
        args.version,
        checkout_dir=args.kasif_checkout_dir,
        git_timeout_seconds=args.git_timeout_seconds,
        profiler=profiler,
        shallow_check=args.shallow_check,
        progress=progress,
    )
    if args.format == "json":
        print(json.dumps(response, indent=2, sort_keys=True))
    else:
        print_source_response(response)
    sys.stdout.flush()
    print_profiler(profiler)
    return 0 if response["status"] == "FOUND" else 1


def print_profiler(profiler: object) -> None:
    if profiler is None:
        return
    if isinstance(profiler, Profiler):
        payload = profiler.to_dict()
    else:
        payload = profiler
    print(json.dumps({"profiler": payload}, indent=2, sort_keys=True), file=sys.stderr)


def print_progress(message: str) -> None:
    print(f"[kasif] {message}", file=sys.stderr, flush=True)


def print_source_response(response: dict) -> None:
    for key in (
        "status",
        "sourceKind",
        "packageCoordinate",
        "repositoryUrl",
        "resolvedCommit",
        "matchedRef",
        "repositorySubdirectory",
        "manifestPath",
        "sourceArchiveUrl",
        "sourceArchiveSha256",
        "sourceArchivePath",
        "errorCode",
        "message",
    ):
        print(f"{key}={response.get(key)}")
    print("ocakGitArguments=" + " ".join(response.get("ocakGitArguments") or []))
    print("ocakArguments=" + " ".join(response.get("ocakArguments") or []))


def print_table(dependencies: Iterable[Dependency]) -> None:
    dependency_list = list(dependencies)
    include_source = any(dependency.source is not None for dependency in dependency_list)
    headers = ["ecosystem", "name", "version", "requirement", "scope", "manager", "source_file"]
    if include_source:
        headers.extend(["source_status", "repository", "commit"])
    rows = []
    for dependency in dependency_list:
        row = [
            dependency.ecosystem,
            dependency.name,
            dependency.version or "",
            dependency.requirement or "",
            dependency.scope,
            dependency.manager,
            dependency.source_file,
        ]
        if include_source:
            source = dependency.source or {}
            row.extend([
                str(source.get("status") or ""),
                str(source.get("repositoryUrl") or ""),
                str(source.get("resolvedCommit") or ""),
            ])
        rows.append(row)
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def render(row: List[str]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))

    print(render(headers))
    print(render(["-" * width for width in widths]))
    for row in rows:
        print(render(row))
    if not rows:
        print("(no dependencies found)", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
