from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from .model import Dependency
from .parsers import normalize_pypi_name, read_text
from .paths import defter_cache_path
from .profiler import Profiler, directory_size, git_object_store_size


Runner = Callable[..., subprocess.CompletedProcess]
Progress = Callable[[str], None]
COMMON_GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}
DOCUMENTATION_HOST_PARTS = ("readthedocs.io", "readthedocs.org", "sourceforge.io")
COMMIT_SHA = re.compile(r"^[a-fA-F0-9]{7,40}$")
MAVEN_REPOSITORIES = (
    "https://repo1.maven.org/maven2/",
    "https://dl.google.com/dl/android/maven2/",
)


@dataclass(frozen=True)
class SourceResolution:
    ecosystem: str
    package: str
    version: str
    repository_url: str
    requested_ref: Optional[str]
    repository_subdirectory: Optional[str]
    source_archive_url: Optional[str] = None
    source_archive_sha256: Optional[str] = None


@dataclass(frozen=True)
class GitRemoteRef:
    commit: str
    ref: str

    @property
    def short_name(self) -> str:
        if self.ref.startswith("refs/tags/"):
            return self.ref[len("refs/tags/"):].removesuffix("^{}")
        if self.ref.startswith("refs/heads/"):
            return self.ref[len("refs/heads/"):]
        return self.ref

    @property
    def is_dereferenced_tag(self) -> bool:
        return self.ref.startswith("refs/tags/") and self.ref.endswith("^{}")

    @property
    def is_tag(self) -> bool:
        return self.ref.startswith("refs/tags/")


def enrich_dependencies(
    dependencies: Iterable[Dependency],
    checkout_dir: Optional[Path] = None,
    git_timeout_seconds: int = 300,
    runner: Runner = subprocess.run,
    profiler: Optional[Profiler] = None,
    shallow_check: bool = False,
    progress: Optional[Progress] = None,
    max_source_resolutions: Optional[int] = None,
) -> List[Dependency]:
    enriched: List[Dependency] = []
    dependency_list = list(dependencies)
    total = len(dependency_list)
    attempted = 0
    report_progress(
        progress,
        f"source resolution starting dependencies={total} max_source_resolutions={max_source_resolutions or 'unlimited'}",
    )
    for index, dependency in enumerate(dependency_list, start=1):
        coordinate = dependency_coordinate(dependency)
        report_progress(progress, f"source {index}/{total} start {coordinate}")
        if max_source_resolutions is not None and attempted >= max_source_resolutions and kasif_version(dependency) is not None:
            source = skipped(
                "SOURCE_RESOLUTION_LIMIT_REACHED",
                f"Skipped because --max-source-resolutions={max_source_resolutions} was reached.",
            )
            report_progress(progress, f"source {index}/{total} SKIPPED {coordinate} error=SOURCE_RESOLUTION_LIMIT_REACHED")
            enriched.append(replace(dependency, source=source))
            continue
        if kasif_version(dependency) is not None:
            attempted += 1
        source = resolve_dependency_source(
            dependency,
            checkout_dir=checkout_dir,
            git_timeout_seconds=git_timeout_seconds,
            runner=runner,
            profiler=profiler,
            shallow_check=shallow_check,
            progress=progress,
        )
        report_progress(
            progress,
            f"source {index}/{total} {source.get('status')} {coordinate} error={source.get('errorCode')}",
        )
        enriched.append(replace(dependency, source=source))
    report_progress(progress, f"source resolution complete dependencies={total} attempted={attempted}")
    return enriched


def resolve_dependency_source(
    dependency: Dependency,
    checkout_dir: Optional[Path] = None,
    git_timeout_seconds: int = 300,
    runner: Runner = subprocess.run,
    profiler: Optional[Profiler] = None,
    shallow_check: bool = False,
    progress: Optional[Progress] = None,
) -> Dict[str, Any]:
    version = kasif_version(dependency)
    if version is None:
        report_progress(progress, f"skip non-exact dependency {dependency_coordinate(dependency)}")
        return skipped("VERSION_NOT_EXACT", "Dependency does not have an exact version suitable for source lookup.")
    return find_source(
        dependency.ecosystem,
        dependency.name,
        version,
        checkout_dir=checkout_dir,
        git_timeout_seconds=git_timeout_seconds,
        runner=runner,
        profiler=profiler,
        shallow_check=shallow_check,
        progress=progress,
    )


def find_source(
    ecosystem: str,
    package: str,
    version: str,
    checkout_dir: Optional[Path] = None,
    git_timeout_seconds: int = 300,
    runner: Runner = subprocess.run,
    profiler: Optional[Profiler] = None,
    shallow_check: bool = False,
    progress: Optional[Progress] = None,
) -> Dict[str, Any]:
    ecosystem = ecosystem.strip().lower()
    package = package.strip()
    version = version.strip()
    package_coordinate = f"{ecosystem}:{package}@{version}"
    try:
        report_progress(progress, f"{package_coordinate} registry lookup")
        with profile_stage(profiler, "registry", f"{ecosystem}:{package}@{version}"):
            resolution = resolve_registry_source(ecosystem, package, version, profiler)
        report_progress(progress, f"{package_coordinate} registry source={resolution.repository_url}")
        if not resolution.repository_url:
            report_progress(progress, f"{package_coordinate} registry source unavailable; trying archive")
            return archive_found_response_for_resolution(resolution, package_coordinate, checkout_dir, profiler, progress)
        report_progress(progress, f"{package_coordinate} normalizing repository")
        try:
            with profile_stage(profiler, "normalize_repository", resolution.repository_url):
                normalized_repository, normalized_subdirectory = normalize_repository_url(resolution.repository_url)
        except SourceLookupError:
            if not can_try_archive(resolution):
                raise
            report_progress(progress, f"{package_coordinate} repository metadata not cloneable; trying archive")
            return archive_found_response_for_resolution(resolution, package_coordinate, checkout_dir, profiler, progress)
        resolution = SourceResolution(
            ecosystem=resolution.ecosystem,
            package=resolution.package,
            version=resolution.version,
            repository_url=normalized_repository,
            requested_ref=resolution.requested_ref,
            repository_subdirectory=first_non_blank(resolution.repository_subdirectory, normalized_subdirectory),
            source_archive_url=resolution.source_archive_url,
            source_archive_sha256=resolution.source_archive_sha256,
        )
        report_progress(progress, f"{package_coordinate} resolving git ref repo={resolution.repository_url}")
        try:
            with profile_stage(profiler, "resolve_git_ref", resolution.repository_url):
                matched_commit, matched_ref = resolve_git_ref(resolution, git_timeout_seconds, runner, profiler)
        except SourceLookupError as error:
            if error.error_code != "VERSION_REF_NOT_FOUND" or not can_try_archive(resolution):
                raise
            report_progress(progress, f"{package_coordinate} git ref unavailable; trying archive")
            return archive_found_response_for_resolution(resolution, package_coordinate, checkout_dir, profiler, progress)
        report_progress(progress, f"{package_coordinate} git ref matched={matched_ref} commit={matched_commit[:12]}")
        if shallow_check:
            report_progress(progress, f"{package_coordinate} shallow check complete; checkout skipped")
            return shallow_found_response(resolution, package_coordinate, matched_commit, matched_ref)
        report_progress(progress, f"{package_coordinate} checkout starting ref={matched_ref}")
        with profile_stage(profiler, "checkout", f"{resolution.repository_url}@{matched_ref}"):
            checkout_root, resolved_commit = checkout_git_ref(
                resolution.repository_url,
                matched_ref,
                checkout_dir,
                git_timeout_seconds,
                runner,
                profiler,
            )
        report_progress(progress, f"{package_coordinate} checkout complete path={checkout_root}")
        report_progress(progress, f"{package_coordinate} verifying manifest")
        with profile_stage(profiler, "verify_manifest", resolution.package):
            manifest_path, verified_subdirectory = verify_manifest(checkout_root, resolution)
        report_progress(progress, f"{package_coordinate} verified manifest={manifest_path or '<none>'}")
        return found_response(resolution, package_coordinate, resolved_commit, matched_ref, verified_subdirectory, manifest_path)
    except SourceLookupError as error:
        report_progress(progress, f"{package_coordinate} failed {error.error_code}: {error.message}")
        return failed_response(error.status, package_coordinate, error.error_code, error.message)
    except Exception as error:  # defensive CLI boundary
        report_progress(progress, f"{package_coordinate} failed SOURCE_LOOKUP_FAILED: {error or error.__class__.__name__}")
        return failed_response("FAILED", package_coordinate, "SOURCE_LOOKUP_FAILED", str(error) or error.__class__.__name__)


@contextmanager
def profile_stage(profiler: Optional[Profiler], stage: str, detail: str, **metadata: Any) -> Iterator[None]:
    if profiler is None:
        yield
        return
    with profiler.measure(stage, detail, **metadata):
        yield


def resolve_registry_source(ecosystem: str, package: str, version: str, profiler: Optional[Profiler] = None) -> SourceResolution:
    if ecosystem == "pypi":
        return resolve_pypi(package, version, profiler)
    if ecosystem == "npm":
        return resolve_npm(package, version, profiler)
    if ecosystem == "pub":
        return resolve_pub(package, version, profiler)
    if ecosystem == "maven":
        return resolve_maven(package, version, profiler)
    if ecosystem == "go":
        return resolve_go(package, version, profiler)
    if ecosystem == "git":
        return SourceResolution("git", package, version, package, version, None)
    raise SourceLookupError("FAILED", "UNSUPPORTED_ECOSYSTEM", f"Unsupported ecosystem: {ecosystem}")


def resolve_pypi(package: str, version: str, profiler: Optional[Profiler] = None) -> SourceResolution:
    metadata = http_json(f"https://pypi.org/pypi/{urllib.parse.quote(package)}/{urllib.parse.quote(version)}/json", profiler)
    info = metadata.get("info") or {}
    project_urls = info.get("project_urls") or {}
    repository_url = None
    for label in ("source", "source code", "repository", "code", "github", "homepage"):
        for key, value in project_urls.items():
            if key.lower() == label and (label != "homepage" or looks_like_repository(str(value))):
                repository_url = str(value)
                break
        if repository_url:
            break
    if repository_url is None and looks_like_repository(str(info.get("home_page") or "")):
        repository_url = str(info.get("home_page"))
    archive_url = None
    archive_sha = None
    for file_info in metadata.get("urls") or []:
        if file_info.get("packagetype") == "sdist":
            archive_url = file_info.get("url")
            archive_sha = (file_info.get("digests") or {}).get("sha256")
            break
    if repository_url is None and archive_url is None:
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "PyPI metadata did not contain a source repository URL.")
    return SourceResolution("pypi", normalize_pypi_name(package), version, repository_url or "", "v" + version, None, archive_url, archive_sha)


def resolve_npm(package: str, version: str, profiler: Optional[Profiler] = None) -> SourceResolution:
    encoded_name = urllib.parse.quote(package, safe="")
    metadata = http_json(f"https://registry.npmjs.org/{encoded_name}/{urllib.parse.quote(version, safe='')}", profiler)
    repository = metadata.get("repository")
    repository_url = None
    repository_directory = None
    if isinstance(repository, str):
        repository_url = repository
    elif isinstance(repository, dict):
        repository_url = repository.get("url")
        repository_directory = repository.get("directory")
    if repository_url is None:
        for value in (metadata.get("homepage"), (metadata.get("bugs") or {}).get("url")):
            if looks_like_repository(str(value or "")):
                repository_url = str(value)
                break
    dist = metadata.get("dist") or {}
    archive_url = dist.get("tarball")
    if repository_url is None and archive_url is None:
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "npm metadata did not contain a source repository URL.")
    return SourceResolution("npm", package.lower(), version, repository_url or "", "v" + version, repository_directory, archive_url, None)


def resolve_pub(package: str, version: str, profiler: Optional[Profiler] = None) -> SourceResolution:
    metadata = http_json(f"https://pub.dev/api/packages/{urllib.parse.quote(package)}/versions/{urllib.parse.quote(version)}", profiler)
    pubspec = metadata.get("pubspec") or {}
    repository_url = pubspec.get("repository")
    if repository_url is None:
        for value in (pubspec.get("homepage"), pubspec.get("issue_tracker")):
            if looks_like_repository(str(value or "")):
                repository_url = str(value)
                break
    if repository_url is None and metadata.get("archive_url") is None:
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "Pub metadata did not contain a source repository URL.")
    return SourceResolution(
        "pub",
        package.lower(),
        version,
        str(repository_url or ""),
        "v" + version,
        None,
        metadata.get("archive_url"),
        metadata.get("archive_sha256"),
    )


def resolve_maven(package: str, version: str, profiler: Optional[Profiler] = None) -> SourceResolution:
    if ":" not in package:
        raise SourceLookupError("FAILED", "INVALID_COORDINATE", "Maven package must use groupId:artifactId.")
    group_id, artifact_id = package.split(":", 1)
    pom = fetch_maven_pom(group_id, artifact_id, version, profiler)
    if pom is None:
        raise SourceLookupError("SOURCE_NOT_FOUND", "PACKAGE_VERSION_NOT_FOUND", f"Maven POM was not found for {package}@{version}.")
    repository_url, tag = maven_repository_from_pom(pom, depth=0, profiler=profiler)
    if repository_url is None:
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "Maven POM metadata did not contain SCM repository information.")
    return SourceResolution("maven", package, version, repository_url, tag, None)


def resolve_go(package: str, version: str, profiler: Optional[Profiler] = None) -> SourceResolution:
    parts = package.split("/")
    if (package.startswith("github.com/") or package.startswith("gitlab.com/")) and len(parts) >= 3:
        repository = f"https://{parts[0]}/{parts[1]}/{parts[2]}.git"
        subdirectory = None if len(parts) <= 3 or (len(parts) == 4 and re.fullmatch(r"v[2-9][0-9]*", parts[3])) else "/".join(parts[3:])
        return SourceResolution("go", package, version, repository, version, subdirectory)
    html = http_text("https://" + package + "?go-get=1", profiler)
    match = re.search(r'<meta\s+name=["\']go-import["\']\s+content=["\']([^"\']+)["\']', html)
    if not match:
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "Go vanity module metadata did not contain go-import information.")
    fields = match.group(1).split()
    if len(fields) < 3 or fields[1] != "git":
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "Only git-backed Go module metadata is supported.")
    prefix, repository_url = fields[0], fields[2]
    if package != prefix and not package.startswith(prefix + "/"):
        raise SourceLookupError("FAILED", "PACKAGE_IDENTITY_MISMATCH", "Go import prefix did not match the requested module path.")
    subdirectory = None if package == prefix else package[len(prefix) + 1:]
    return SourceResolution("go", package, version, repository_url, version, subdirectory)


def resolve_git_ref(
    resolution: SourceResolution,
    timeout_seconds: int,
    runner: Runner,
    profiler: Optional[Profiler] = None,
) -> Tuple[str, str]:
    if looks_like_commit(resolution.requested_ref):
        return resolution.requested_ref, resolution.requested_ref
    remote_refs = git_ls_remote(resolution.repository_url, timeout_seconds, runner, profiler)
    refs_by_short_name: Dict[str, GitRemoteRef] = {}
    for remote_ref in remote_refs:
        existing = refs_by_short_name.get(remote_ref.short_name)
        if existing is None or remote_ref.is_dereferenced_tag or (remote_ref.is_tag and not existing.is_tag):
            refs_by_short_name[remote_ref.short_name] = remote_ref
    candidates: List[str] = []
    if resolution.requested_ref and resolution.requested_ref != "HEAD":
        candidates.append(strip_ref_prefix(resolution.requested_ref))
    candidates.extend(candidate_refs(resolution.ecosystem, resolution.package, resolution.version))
    for candidate in unique(candidates):
        match = refs_by_short_name.get(strip_ref_prefix(candidate))
        if match is not None:
            return match.commit, match.short_name
    raise SourceLookupError("VERSION_REF_NOT_FOUND", "VERSION_REF_NOT_FOUND", f"No repository ref could be verified for version {resolution.version}.")


def candidate_refs(ecosystem: str, package: str, version: str) -> List[str]:
    leaf = package.rsplit("/", 1)[-1]
    normalized_package = re.sub(r"[^0-9A-Za-z._-]+", "-", package).strip("-")
    if ecosystem == "maven" and ":" in package:
        leaf = package.split(":", 1)[1]
        normalized_package = package.replace(":", "-")
    if ecosystem == "npm" and package.startswith("@"):
        leaf = package.split("/", 1)[1]
        normalized_package = package.removeprefix("@").replace("/", "-")
    return [
        "v" + version,
        version,
        package + "@" + version,
        normalized_package + "@" + version,
        normalized_package + "-v" + version,
        normalized_package + "-" + version,
        "release-" + version,
        leaf + "-v" + version,
        leaf + "-" + version,
        "artifact-v" + version,
        "artifact-" + version,
    ]


def checkout_git_ref(
    repository_url: str,
    matched_ref: str,
    checkout_dir: Optional[Path],
    timeout_seconds: int,
    runner: Runner,
    profiler: Optional[Profiler] = None,
) -> Tuple[Path, str]:
    parent = Path(checkout_dir) if checkout_dir is not None else defter_cache_path("v1", "sources", "kasif-checkouts")
    try:
        parent.mkdir(parents=True, exist_ok=True)
        checkout_root = Path(tempfile.mkdtemp(prefix="kasif-source-", dir=str(parent)))
    except OSError as error:
        raise SourceLookupError("FAILED", "CACHE_UNAVAILABLE", f"Could not create checkout directory: {error}")

    success = False
    try:
        if looks_like_commit(matched_ref):
            run_git(["git", "-C", str(checkout_root), "init", "--quiet"], timeout_seconds, runner, profiler)
            run_git(["git", "-C", str(checkout_root), "remote", "add", "origin", repository_url], timeout_seconds, runner, profiler)
            run_git(["git", "-C", str(checkout_root), "fetch", "--depth", "1", "origin", matched_ref], timeout_seconds, runner, profiler)
            run_git(["git", "-C", str(checkout_root), "checkout", "--quiet", "--detach", "FETCH_HEAD"], timeout_seconds, runner, profiler)
        else:
            shutil.rmtree(checkout_root)
            run_git([
                "git",
                "-c",
                "filter.lfs.required=false",
                "-c",
                "submodule.recurse=false",
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--branch",
                matched_ref,
                "--no-checkout",
                repository_url,
                str(checkout_root),
            ], timeout_seconds, runner, profiler)
            run_git(["git", "-C", str(checkout_root), "checkout", "--quiet", "--detach", matched_ref], timeout_seconds, runner, profiler)
        if profiler is not None:
            profiler.set_total("gitObjectBytes", git_object_store_size(checkout_root))
            profiler.set_total("checkoutWorktreeBytes", directory_size(checkout_root, exclude_git=True))
        rev_parse = run_git(["git", "-C", str(checkout_root), "rev-parse", "HEAD"], timeout_seconds, runner, profiler)
        success = True
        return checkout_root, rev_parse.stdout.strip()
    finally:
        if not success:
            shutil.rmtree(checkout_root, ignore_errors=True)


def archive_found_response_for_resolution(
    resolution: SourceResolution,
    package_coordinate: str,
    checkout_dir: Optional[Path],
    profiler: Optional[Profiler],
    progress: Optional[Progress],
) -> Dict[str, Any]:
    if not can_try_archive(resolution):
        raise SourceLookupError("VERSION_REF_NOT_FOUND", "VERSION_REF_NOT_FOUND", f"No repository ref could be verified for version {resolution.version}.")
    report_progress(progress, f"{package_coordinate} archive download starting url={resolution.source_archive_url}")
    with profile_stage(profiler, "archive_download", resolution.source_archive_url or ""):
        archive_path, archive_sha256 = download_archive(resolution, package_coordinate, checkout_dir, profiler)
    report_progress(progress, f"{package_coordinate} archive cached path={archive_path}")
    with tempfile.TemporaryDirectory(prefix="kasif-archive-") as tmp:
        extract_root = Path(tmp) / "src"
        extract_root.mkdir()
        with profile_stage(profiler, "archive_extract", str(archive_path)):
            extract_archive(archive_path, extract_root)
        with profile_stage(profiler, "verify_manifest", resolution.package):
            manifest_path, verified_subdirectory = verify_manifest(extract_root, resolution)
    return archive_found_response(resolution, package_coordinate, archive_path, archive_sha256, verified_subdirectory, manifest_path)


def can_try_archive(resolution: SourceResolution) -> bool:
    return bool(resolution.source_archive_url)


def download_archive(
    resolution: SourceResolution,
    package_coordinate: str,
    checkout_dir: Optional[Path],
    profiler: Optional[Profiler] = None,
) -> Tuple[Path, str]:
    parent = archive_cache_dir(checkout_dir)
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SourceLookupError("FAILED", "CACHE_UNAVAILABLE", f"Could not create archive cache directory: {error}")
    suffix = archive_suffix(resolution.source_archive_url or "")
    archive_path = parent / (safe_cache_name(package_coordinate) + suffix)
    body = http_bytes(resolution.source_archive_url or "", profiler)
    actual_sha256 = hashlib.sha256(body).hexdigest()
    expected_sha256 = resolution.source_archive_sha256
    if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
        raise SourceLookupError("FAILED", "SOURCE_ARCHIVE_HASH_MISMATCH", f"Source archive SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}.")
    try:
        archive_path.write_bytes(body)
    except OSError as error:
        raise SourceLookupError("FAILED", "CACHE_UNAVAILABLE", f"Could not write source archive: {error}")
    return archive_path, actual_sha256


def archive_cache_dir(checkout_dir: Optional[Path]) -> Path:
    if checkout_dir is not None:
        return Path(checkout_dir) / "archives"
    return defter_cache_path("v1", "sources", "kasif-archives")


def archive_suffix(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    for suffix in (".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tar.xz"):
        if path.endswith(suffix):
            return suffix
    return ".tar.gz"


def safe_cache_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-") or "source"


def extract_archive(archive_path: Path, extract_root: Path) -> None:
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            members = archive.getmembers()
            for member in members:
                validate_archive_member(member, extract_root)
            archive.extractall(extract_root)
    except (tarfile.TarError, OSError) as error:
        raise SourceLookupError("FAILED", "SOURCE_ARCHIVE_INVALID", f"Source archive could not be extracted: {error}")


def validate_archive_member(member: tarfile.TarInfo, extract_root: Path) -> None:
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        raise SourceLookupError("FAILED", "SOURCE_ARCHIVE_INVALID", f"Source archive member '{member.name}' has unsupported file type.")
    target = (extract_root / member.name).resolve()
    root = extract_root.resolve()
    if not is_relative_to(target, root):
        raise SourceLookupError("FAILED", "SOURCE_ARCHIVE_INVALID", f"Source archive member '{member.name}' escapes extraction root.")
    if member.issym() or member.islnk():
        link_target = (target.parent / member.linkname).resolve()
        if not is_relative_to(link_target, root):
            raise SourceLookupError("FAILED", "SOURCE_ARCHIVE_INVALID", f"Source archive link '{member.name}' escapes extraction root.")


def verify_manifest(checkout_root: Path, resolution: SourceResolution) -> Tuple[str, Optional[str]]:
    if resolution.ecosystem == "pypi":
        manifest_path = verify_pypi_manifest(checkout_root, resolution)
    elif resolution.ecosystem == "npm":
        manifest_path = verify_json_manifest(checkout_root, resolution, "package.json", "name")
    elif resolution.ecosystem == "pub":
        manifest_path = verify_yaml_manifest(checkout_root, resolution, "pubspec.yaml")
    elif resolution.ecosystem == "maven":
        manifest_path = verify_maven_manifest(checkout_root, resolution)
    elif resolution.ecosystem == "go":
        manifest_path = verify_go_manifest(checkout_root, resolution)
    elif resolution.ecosystem == "git":
        manifest_path = ""
    else:
        raise SourceLookupError("FAILED", "PACKAGE_MANIFEST_NOT_FOUND", f"No manifest verifier exists for {resolution.ecosystem}.")
    verified_subdirectory = resolution.repository_subdirectory
    if not verified_subdirectory and manifest_path and "/" in manifest_path:
        verified_subdirectory = manifest_path.rsplit("/", 1)[0]
    return manifest_path, verified_subdirectory


def verify_pypi_manifest(checkout_root: Path, resolution: SourceResolution) -> str:
    first_mismatch = None
    for manifest_name in ("pyproject.toml", "setup.cfg", "PKG-INFO", "setup.py"):
        for manifest in find_manifests(checkout_root, resolution.repository_subdirectory, manifest_name):
            name, version = parse_python_manifest(manifest)
            if name is None and version is None:
                continue
            relative = relative_path(checkout_root, manifest)
            if normalize_pypi_name(name or "") == resolution.package and (version is None or version == resolution.version):
                return relative
            first_mismatch = first_mismatch or relative
    if first_mismatch:
        raise SourceLookupError("FAILED", "PACKAGE_IDENTITY_MISMATCH", "Package manifest did not match the requested PyPI coordinate.")
    raise SourceLookupError("FAILED", "PACKAGE_MANIFEST_NOT_FOUND", "PyPI metadata file was not found.")


def verify_json_manifest(checkout_root: Path, resolution: SourceResolution, file_name: str, name_field: str) -> str:
    first_mismatch = None
    first_invalid = None
    for manifest in find_manifests(checkout_root, resolution.repository_subdirectory, file_name):
        relative = relative_path(checkout_root, manifest)
        try:
            data = json.loads(read_text(manifest))
        except json.JSONDecodeError:
            first_invalid = first_invalid or relative
            continue
        if data.get(name_field) == resolution.package and data.get("version") == resolution.version:
            return relative
        first_mismatch = first_mismatch or relative
    if first_mismatch:
        raise SourceLookupError("FAILED", "PACKAGE_IDENTITY_MISMATCH", f"{file_name} did not match the requested package coordinate.")
    if first_invalid:
        raise SourceLookupError("FAILED", "PACKAGE_MANIFEST_INVALID", f"{file_name} could not be parsed.")
    raise SourceLookupError("FAILED", "PACKAGE_MANIFEST_NOT_FOUND", f"{file_name} was not found.")


def verify_yaml_manifest(checkout_root: Path, resolution: SourceResolution, file_name: str) -> str:
    first_mismatch = None
    for manifest in find_manifests(checkout_root, resolution.repository_subdirectory, file_name):
        values = parse_top_level_yaml_scalars(read_text(manifest))
        relative = relative_path(checkout_root, manifest)
        if values.get("name") == resolution.package and values.get("version") == resolution.version:
            return relative
        first_mismatch = first_mismatch or relative
    if first_mismatch:
        raise SourceLookupError("FAILED", "PACKAGE_IDENTITY_MISMATCH", f"{file_name} did not match the requested package coordinate.")
    raise SourceLookupError("FAILED", "PACKAGE_MANIFEST_NOT_FOUND", f"{file_name} was not found.")


def verify_maven_manifest(checkout_root: Path, resolution: SourceResolution) -> str:
    group_id, artifact_id = resolution.package.split(":", 1)
    first_mismatch = None
    first_invalid = None
    for manifest in find_manifests(checkout_root, resolution.repository_subdirectory, "pom.xml"):
        relative = relative_path(checkout_root, manifest)
        try:
            root = ET.parse(manifest).getroot()
        except (ET.ParseError, OSError):
            first_invalid = first_invalid or relative
            continue
        pom_group = xml_child_text(root, "groupId") or xml_child_text(xml_child(root, "parent"), "groupId")
        pom_artifact = xml_child_text(root, "artifactId")
        pom_version = xml_child_text(root, "version") or xml_child_text(xml_child(root, "parent"), "version")
        if pom_group == group_id and pom_artifact == artifact_id and pom_version == resolution.version:
            return relative
        first_mismatch = first_mismatch or relative
    gradle_manifest, gradle_mismatch = verify_maven_gradle_manifest(checkout_root, resolution, group_id, artifact_id)
    if gradle_manifest:
        return gradle_manifest
    first_mismatch = first_mismatch or gradle_mismatch
    if first_mismatch:
        raise SourceLookupError("FAILED", "PACKAGE_IDENTITY_MISMATCH", "Maven package manifest did not match the requested coordinate.")
    if first_invalid:
        raise SourceLookupError("FAILED", "PACKAGE_MANIFEST_INVALID", "Maven package manifest could not be parsed.")
    raise SourceLookupError("FAILED", "PACKAGE_MANIFEST_NOT_FOUND", "No Maven package manifest was found.")


def verify_maven_gradle_manifest(
    checkout_root: Path,
    resolution: SourceResolution,
    group_id: str,
    artifact_id: str,
) -> Tuple[Optional[str], Optional[str]]:
    first_mismatch = None
    for manifest in find_gradle_module_manifests(checkout_root, resolution.repository_subdirectory, artifact_id):
        relative = relative_path(checkout_root, manifest)
        module_root = manifest.parent
        group = gradle_project_group(module_root, checkout_root)
        version = gradle_project_version(module_root, checkout_root)
        group_matches = group == group_id or group is None
        artifact_matches = module_root.name == artifact_id or manifest.stem == artifact_id
        if group_matches and artifact_matches and version == resolution.version:
            return relative, None
        first_mismatch = first_mismatch or relative
    return None, first_mismatch


def find_gradle_module_manifests(checkout_root: Path, repository_subdirectory: Optional[str], artifact_id: str) -> List[Path]:
    package_root = checkout_root / repository_subdirectory if repository_subdirectory else checkout_root
    package_root = package_root.resolve()
    if not package_root.is_dir() or not is_relative_to(package_root, checkout_root.resolve()):
        return []
    candidates = [
        package_root / artifact_id / f"{artifact_id}.gradle",
        package_root / artifact_id / f"{artifact_id}.gradle.kts",
        package_root / artifact_id / "build.gradle",
        package_root / artifact_id / "build.gradle.kts",
    ]
    if repository_subdirectory:
        candidates.extend([
            package_root / f"{artifact_id}.gradle",
            package_root / f"{artifact_id}.gradle.kts",
            package_root / "build.gradle",
            package_root / "build.gradle.kts",
        ])
    results = [candidate for candidate in candidates if candidate.is_file()]
    if results:
        return sorted(set(results), key=lambda path: (len(path.parts), path.as_posix()))
    for root, dirs, files in os.walk(package_root):
        depth = len(Path(root).relative_to(package_root).parts)
        if depth >= 5:
            dirs[:] = []
        current = Path(root)
        for file_name in files:
            if file_name not in {f"{artifact_id}.gradle", f"{artifact_id}.gradle.kts", "build.gradle", "build.gradle.kts"}:
                continue
            if current.name == artifact_id or file_name.startswith(artifact_id + "."):
                results.append(current / file_name)
    return sorted(set(results), key=lambda path: (len(path.parts), path.as_posix()))


def gradle_project_group(module_root: Path, checkout_root: Path) -> Optional[str]:
    for path in gradle_metadata_files(module_root, checkout_root):
        value = gradle_literal_assignment(path, "group")
        if value:
            return value
    return None


def gradle_project_version(module_root: Path, checkout_root: Path) -> Optional[str]:
    for path in gradle_metadata_files(module_root, checkout_root):
        value = gradle_properties_value(path, "version") if path.name == "gradle.properties" else gradle_literal_assignment(path, "version")
        if value:
            return value
    return None


def gradle_metadata_files(module_root: Path, checkout_root: Path) -> List[Path]:
    candidates = [
        module_root / "gradle.properties",
        module_root / "build.gradle",
        module_root / "build.gradle.kts",
        checkout_root / "gradle.properties",
        checkout_root / "build.gradle",
        checkout_root / "build.gradle.kts",
    ]
    result: List[Path] = []
    for candidate in candidates:
        if candidate.is_file() and candidate not in result:
            result.append(candidate)
    return result


def gradle_properties_value(path: Path, key: str) -> Optional[str]:
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip()
    return None


def gradle_literal_assignment(path: Path, key: str) -> Optional[str]:
    text = read_text(path)
    match = re.search(rf"""(?m)^\s*{re.escape(key)}\s*=\s*['"]([^'"]+)['"]""", text)
    if match:
        return match.group(1)
    match = re.search(rf"""(?m)^\s*{re.escape(key)}\s+['"]([^'"]+)['"]""", text)
    if match:
        return match.group(1)
    return None


def verify_go_manifest(checkout_root: Path, resolution: SourceResolution) -> str:
    first_mismatch = None
    for manifest in find_manifests(checkout_root, resolution.repository_subdirectory, "go.mod"):
        module_path = parse_go_module_path(read_text(manifest))
        relative = relative_path(checkout_root, manifest)
        if module_path == resolution.package:
            return relative
        first_mismatch = first_mismatch or relative
    if first_mismatch:
        raise SourceLookupError("FAILED", "PACKAGE_IDENTITY_MISMATCH", "go.mod did not match the requested Go module.")
    raise SourceLookupError("FAILED", "PACKAGE_MANIFEST_NOT_FOUND", "go.mod was not found.")


def parse_python_manifest(path: Path) -> Tuple[Optional[str], Optional[str]]:
    content = read_text(path)
    if path.name == "pyproject.toml":
        values = parse_toml_section_scalars(content, "project")
        return values.get("name"), values.get("version")
    if path.name == "setup.cfg":
        parser = configparser.ConfigParser()
        try:
            parser.read_string(content)
        except configparser.Error:
            return None, None
        if parser.has_section("metadata"):
            return parser.get("metadata", "name", fallback=None), parser.get("metadata", "version", fallback=None)
        return None, None
    if path.name == "PKG-INFO":
        values = parse_rfc822_headers(content)
        return values.get("name"), values.get("version")
    name = None
    version = None
    for match in re.finditer(r"""(?m)^\s*(name|version)\s*=\s*['"]([^'"]+)['"]\s*,?""", content):
        if match.group(1) == "name":
            name = match.group(2)
        elif match.group(1) == "version":
            version = match.group(2)
    return name, version


def find_manifests(checkout_root: Path, repository_subdirectory: Optional[str], file_name: str) -> List[Path]:
    package_root = checkout_root / repository_subdirectory if repository_subdirectory else checkout_root
    package_root = package_root.resolve()
    if not package_root.is_dir() or not is_relative_to(package_root, checkout_root.resolve()):
        return []
    exact = package_root / file_name
    if repository_subdirectory:
        return [exact] if exact.is_file() else []
    results: List[Path] = []
    for root, dirs, files in os.walk(package_root):
        depth = len(Path(root).relative_to(package_root).parts)
        if depth >= 5:
            dirs[:] = []
        if file_name in files:
            results.append(Path(root) / file_name)
    return sorted(results, key=lambda path: len(path.parts))


def found_response(
    resolution: SourceResolution,
    package_coordinate: str,
    resolved_commit: str,
    matched_ref: str,
    verified_subdirectory: Optional[str],
    manifest_path: str,
) -> Dict[str, Any]:
    return {
        "status": "FOUND",
        "sourceKind": "git",
        "packageCoordinate": package_coordinate,
        "repositoryUrl": resolution.repository_url,
        "resolvedCommit": resolved_commit,
        "matchedRef": matched_ref,
        "repositorySubdirectory": verified_subdirectory,
        "manifestPath": manifest_path,
        "sourceArchiveUrl": resolution.source_archive_url,
        "sourceArchiveSha256": resolution.source_archive_sha256,
        "sourceArchivePath": None,
        "ocakGitArguments": ocak_arguments(resolution, resolved_commit, verified_subdirectory),
        "ocakArguments": ocak_arguments(resolution, resolved_commit, verified_subdirectory),
        "errorCode": None,
        "message": None,
        "resolvedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def shallow_found_response(
    resolution: SourceResolution,
    package_coordinate: str,
    resolved_commit: str,
    matched_ref: str,
) -> Dict[str, Any]:
    return {
        "status": "FOUND",
        "sourceKind": "git",
        "packageCoordinate": package_coordinate,
        "repositoryUrl": resolution.repository_url,
        "resolvedCommit": resolved_commit,
        "matchedRef": matched_ref,
        "repositorySubdirectory": resolution.repository_subdirectory,
        "manifestPath": None,
        "sourceArchiveUrl": resolution.source_archive_url,
        "sourceArchiveSha256": resolution.source_archive_sha256,
        "sourceArchivePath": None,
        "ocakGitArguments": ocak_arguments(resolution, resolved_commit, resolution.repository_subdirectory),
        "ocakArguments": ocak_arguments(resolution, resolved_commit, resolution.repository_subdirectory),
        "errorCode": None,
        "message": "Shallow check resolved the version ref but skipped checkout and manifest verification.",
        "resolvedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def archive_found_response(
    resolution: SourceResolution,
    package_coordinate: str,
    archive_path: Path,
    archive_sha256: str,
    verified_subdirectory: Optional[str],
    manifest_path: str,
) -> Dict[str, Any]:
    return {
        "status": "FOUND",
        "sourceKind": "archive",
        "packageCoordinate": package_coordinate,
        "repositoryUrl": resolution.repository_url or None,
        "resolvedCommit": None,
        "matchedRef": None,
        "repositorySubdirectory": verified_subdirectory,
        "manifestPath": manifest_path,
        "sourceArchiveUrl": resolution.source_archive_url,
        "sourceArchiveSha256": archive_sha256,
        "sourceArchivePath": str(archive_path),
        "ocakGitArguments": [],
        "ocakArguments": archive_ocak_arguments(resolution, archive_path, archive_sha256, verified_subdirectory),
        "errorCode": None,
        "message": "Resolved from exact registry source archive after Git ref verification was unavailable.",
        "resolvedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def ocak_arguments(resolution: SourceResolution, resolved_commit: str, repository_subdirectory: Optional[str]) -> List[str]:
    package_coordinate = f"{resolution.ecosystem}:{resolution.package}@{resolution.version}"
    args = [
        "--git-repo",
        resolution.repository_url,
        "--repo-ref",
        resolved_commit,
        "--package-coordinate",
        package_coordinate,
    ]
    if repository_subdirectory:
        args.extend(["--repo-subdir", repository_subdirectory])
    return args


def archive_ocak_arguments(
    resolution: SourceResolution,
    archive_path: Path,
    archive_sha256: str,
    repository_subdirectory: Optional[str],
) -> List[str]:
    package_coordinate = f"{resolution.ecosystem}:{resolution.package}@{resolution.version}"
    args = [
        "--archive-file",
        str(archive_path),
        "--archive-sha256",
        archive_sha256,
        "--package-coordinate",
        package_coordinate,
    ]
    if repository_subdirectory:
        args.extend(["--repo-subdir", repository_subdirectory])
    return args


def failed_response(status: str, package_coordinate: Optional[str], error_code: str, message: str) -> Dict[str, Any]:
    return {
        "status": status,
        "sourceKind": None,
        "packageCoordinate": package_coordinate,
        "repositoryUrl": None,
        "resolvedCommit": None,
        "matchedRef": None,
        "repositorySubdirectory": None,
        "manifestPath": None,
        "sourceArchiveUrl": None,
        "sourceArchiveSha256": None,
        "sourceArchivePath": None,
        "ocakGitArguments": [],
        "ocakArguments": [],
        "errorCode": error_code,
        "message": message,
        "resolvedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def skipped(error_code: str, message: str) -> Dict[str, Any]:
    response = failed_response("SKIPPED", None, error_code, message)
    response["resolvedAt"] = None
    return response


def http_json(url: str, profiler: Optional[Profiler] = None) -> Dict[str, Any]:
    try:
        metadata = json.loads(http_text(url, profiler))
    except json.JSONDecodeError as error:
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_INVALID", f"Registry metadata was not valid JSON: {error.msg}")
    if not isinstance(metadata, dict):
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_INVALID", "Registry metadata JSON was not an object.")
    return metadata


def http_text(url: str, profiler: Optional[Profiler] = None) -> str:
    request = urllib.request.Request(url, headers={"Accept": "application/json, text/html;q=0.8, */*;q=0.5"})
    try:
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
        if profiler is not None:
            profiler.add_total("httpBytesReceived", len(body))
            profiler.record("http", url, started, responseBytes=len(body))
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_INVALID", f"Registry metadata was not UTF-8: {error}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise SourceLookupError("SOURCE_NOT_FOUND", "PACKAGE_VERSION_NOT_FOUND", f"Registry metadata was not found: {url}")
        raise SourceLookupError("SOURCE_NOT_FOUND", "PACKAGE_NOT_FOUND", f"Registry metadata request failed with HTTP {error.code}.")
    except OSError as error:
        raise SourceLookupError("SOURCE_NOT_FOUND", "PACKAGE_NOT_FOUND", f"Registry metadata request failed: {error}")


def http_bytes(url: str, profiler: Optional[Profiler] = None) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream, */*;q=0.5"})
    try:
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        if profiler is not None:
            profiler.add_total("httpBytesReceived", len(body))
            profiler.record("http", url, started, responseBytes=len(body))
        return body
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_ARCHIVE_NOT_FOUND", f"Source archive was not found: {url}")
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_ARCHIVE_UNAVAILABLE", f"Source archive request failed with HTTP {error.code}.")
    except OSError as error:
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_ARCHIVE_UNAVAILABLE", f"Source archive request failed: {error}")


def git_ls_remote(
    repository_url: str,
    timeout_seconds: int,
    runner: Runner,
    profiler: Optional[Profiler] = None,
) -> List[GitRemoteRef]:
    result = run_git(["git", "ls-remote", "--heads", "--tags", repository_url], timeout_seconds, runner, profiler)
    refs: List[GitRemoteRef] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            refs.append(GitRemoteRef(parts[0], parts[1]))
    return refs


def run_git(
    command: List[str],
    timeout_seconds: int,
    runner: Runner,
    profiler: Optional[Profiler] = None,
) -> subprocess.CompletedProcess:
    started = time.perf_counter()
    try:
        result = runner(
            command,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"},
        )
    except subprocess.TimeoutExpired:
        if profiler is not None:
            profiler.record("git", git_command_label(command), started, status="error", errorType="TimeoutExpired")
        raise SourceLookupError("FAILED", "SOURCE_TIMEOUT", "Git command timed out.")
    except OSError as error:
        if profiler is not None:
            profiler.record("git", git_command_label(command), started, status="error", errorType=error.__class__.__name__)
        raise SourceLookupError("FAILED", "GIT_UNAVAILABLE", f"Git command could not be started: {error}")
    stdout_bytes = len((result.stdout or "").encode("utf-8"))
    stderr_bytes = len((result.stderr or "").encode("utf-8"))
    if profiler is not None:
        profiler.add_total("gitStdoutBytes", stdout_bytes)
        profiler.add_total("gitStderrBytes", stderr_bytes)
        profiler.record(
            "git",
            git_command_label(command),
            started,
            returnCode=result.returncode,
            stdoutBytes=stdout_bytes,
            stderrBytes=stderr_bytes,
        )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Git command failed.").strip()
        raise SourceLookupError("FAILED", "REPOSITORY_UNAVAILABLE", message)
    return result


def git_command_label(command: List[str]) -> str:
    parts: List[str] = []
    skip_next = False
    for part in command:
        if skip_next:
            skip_next = False
            continue
        if part == "-C":
            parts.extend(["-C", "<checkout>"])
            skip_next = True
            continue
        if part.startswith("/tmp/") or part.startswith("/var/") or part.startswith("/private/"):
            parts.append("<path>")
            continue
        parts.append(part)
    return " ".join(parts)


def fetch_maven_pom(
    group_id: str,
    artifact_id: str,
    version: str,
    profiler: Optional[Profiler] = None,
) -> Optional[ET.Element]:
    path = f"{group_id.replace('.', '/')}/{artifact_id}/{version}/{artifact_id}-{version}.pom"
    for repository in MAVEN_REPOSITORIES:
        try:
            return ET.fromstring(http_text(repository + path, profiler))
        except ET.ParseError as error:
            raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_INVALID", f"Maven POM metadata was not valid XML: {error}")
        except SourceLookupError as error:
            if error.error_code == "PACKAGE_VERSION_NOT_FOUND":
                continue
            raise
    return None


def maven_repository_from_pom(
    root: ET.Element,
    depth: int,
    profiler: Optional[Profiler] = None,
) -> Tuple[Optional[str], Optional[str]]:
    scm = xml_child(root, "scm")
    if scm is not None:
        repository = first_non_blank(xml_child_text(scm, "connection"), xml_child_text(scm, "developerConnection"), xml_child_text(scm, "url"))
        tag = xml_child_text(scm, "tag")
        if repository:
            return repository, tag
    project_url = xml_child_text(root, "url")
    if project_url and looks_like_repository(project_url):
        return project_url, None
    parent = xml_child(root, "parent")
    if parent is not None and depth < 5:
        parent_group = xml_child_text(parent, "groupId")
        parent_artifact = xml_child_text(parent, "artifactId")
        parent_version = xml_child_text(parent, "version")
        if parent_group and parent_artifact and parent_version:
            parent_pom = fetch_maven_pom(parent_group, parent_artifact, parent_version, profiler)
            if parent_pom is not None:
                return maven_repository_from_pom(parent_pom, depth + 1, profiler)
    return None, None


def normalize_repository_url(raw_url: str) -> Tuple[str, Optional[str]]:
    value = raw_url.strip()
    github_shorthand = re.fullmatch(r"(?:github:)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:\.git)?", value)
    if github_shorthand:
        value = "https://github.com/" + github_shorthand.group(1) + ".git"
    while value.startswith("scm:"):
        value = value[len("scm:"):]
    if value.startswith("git:") and not value.startswith("git://"):
        value = value[len("git:"):]
    if value.startswith("git+"):
        value = value[len("git+"):]
    if value.startswith("git://"):
        value = "https://" + value[len("git://"):]
    ssh_url_match = re.fullmatch(r"ssh://git@([^/]+)/(.+)", value)
    if ssh_url_match:
        value = "https://" + ssh_url_match.group(1) + "/" + ssh_url_match.group(2)
    ssh_match = re.fullmatch(r"git@([^:]+):(.+)", value)
    if ssh_match:
        value = "https://" + ssh_match.group(1) + "/" + ssh_match.group(2)

    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError as error:
        raise SourceLookupError("FAILED", "REPOSITORY_BLOCKED", f"Repository URL is invalid: {error}")
    if parsed.scheme != "https" or not parsed.netloc:
        raise SourceLookupError("FAILED", "REPOSITORY_BLOCKED", "Only HTTPS Git repository URLs are allowed.")
    host = parsed.hostname or ""
    path = parsed.path
    if host.lower() == "sourceforge.net":
        sourceforge_repository = normalize_sourceforge_repository(path)
        if sourceforge_repository:
            return sourceforge_repository, None
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "SourceForge URL did not point to a Git repository.")
    if host.lower() not in COMMON_GIT_HOSTS:
        if is_documentation_url(parsed):
            raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "Repository metadata pointed to a documentation site.")
        if host.lower() == "git.code.sf.net" and path.startswith("/p/"):
            return strip_query_fragment(value), None
        if not path.endswith(".git"):
            raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "Repository metadata did not point to a cloneable Git URL.")
        return strip_query_fragment(value), None
    repository_path, subdirectory = split_hosted_repository_path(host.lower(), path)
    repository = f"https://{host}/{repository_path.removesuffix('.git')}.git"
    return repository, normalize_subdirectory(subdirectory)


def looks_like_repository(raw_url: str) -> bool:
    if not raw_url or raw_url == "None":
        return False
    try:
        normalize_repository_url(raw_url)
        return True
    except SourceLookupError:
        return False


def is_documentation_url(parsed: urllib.parse.ParseResult) -> bool:
    host = (parsed.hostname or "").lower()
    if any(host == part or host.endswith("." + part) for part in DOCUMENTATION_HOST_PARTS):
        return True
    return any(part in parsed.path.lower().split("/") for part in {"docs", "documentation"})


def normalize_sourceforge_repository(path: str) -> Optional[str]:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "p":
        return f"https://git.code.sf.net/p/{parts[1]}/{parts[2]}"
    return None


def split_hosted_repository_path(host: str, path: str) -> Tuple[str, Optional[str]]:
    parts = [part for part in path.split("/") if part]
    if host == "gitlab.com":
        if "-" in parts:
            dash = parts.index("-")
            if dash < 2 or len(parts) < dash + 3 or parts[dash + 1] not in {"tree", "blob"}:
                raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "GitLab URL did not point to a repository root or tree.")
            return "/".join(parts[:dash]), "/".join(parts[dash + 3:])
        if len(parts) >= 2:
            return "/".join(parts), None
    if len(parts) < 2:
        raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "Hosted repository URL did not include owner and repository.")
    repository_path = "/".join(parts[:2])
    if len(parts) == 2:
        return repository_path, None
    if len(parts) >= 5 and parts[2] in {"tree", "blob"}:
        return repository_path, "/".join(parts[4:])
    raise SourceLookupError("SOURCE_NOT_FOUND", "SOURCE_METADATA_MISSING", "Hosted repository URL points to a page, not a repository root.")


def normalize_subdirectory(subdirectory: Optional[str]) -> Optional[str]:
    if not subdirectory:
        return None
    normalized = subdirectory.replace("\\", "/").strip("/")
    if not normalized or normalized.startswith("/") or normalized.endswith("/") or ".." in normalized.split("/"):
        raise SourceLookupError("FAILED", "REPOSITORY_BLOCKED", "Repository subdirectory is unsafe.")
    return normalized


def strip_query_fragment(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def parse_toml_section_scalars(content: str, section_name: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == f"[{section_name}]"
            continue
        if not in_section or not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = strip_quotes_and_comment(value.strip())
    return values


def parse_rfc822_headers(content: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in content.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip().lower()] = value.strip()
    return values


def parse_top_level_yaml_scalars(content: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in content.splitlines():
        if not line or line.startswith((" ", "#")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = strip_quotes_and_comment(value.strip())
        if value:
            values[key.strip()] = value
    return values


def parse_go_module_path(content: str) -> Optional[str]:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("module "):
            return stripped[len("module "):].strip()
    return None


def xml_child(element: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    if element is None:
        return None
    for child in list(element):
        if child.tag.rsplit("}", 1)[-1] == name:
            return child
    return None


def xml_child_text(element: Optional[ET.Element], name: str) -> Optional[str]:
    child = xml_child(element, name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def strip_quotes_and_comment(value: str) -> str:
    value = value.split("#", 1)[0].strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return value.strip()


def relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def strip_ref_prefix(ref: str) -> str:
    return ref.removeprefix("refs/tags/").removeprefix("refs/heads/").removesuffix("^{}")


def looks_like_commit(value: Optional[str]) -> bool:
    return bool(value and COMMIT_SHA.fullmatch(value.strip()))


def unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def first_non_blank(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value and value.strip():
            return value.strip()
    return None


def report_progress(progress: Optional[Progress], message: str) -> None:
    if progress is not None:
        progress(message)


def dependency_coordinate(dependency: Dependency) -> str:
    version = dependency.version or dependency.requirement or "unknown"
    return f"{dependency.ecosystem}:{dependency.name}@{version}"


def kasif_version(dependency: Dependency) -> Optional[str]:
    version = dependency.version or dependency.requirement
    if version is None:
        return None
    value = version.strip()
    if dependency.ecosystem == "pypi":
        if value.startswith("==="):
            return value[3:].strip()
        if value.startswith("=="):
            return value[2:].strip()
        return value if is_plain_version(value) else None
    if dependency.ecosystem == "npm":
        return value if is_plain_version(value) else None
    if dependency.ecosystem == "maven":
        return value if is_maven_version(value) else None
    if dependency.ecosystem == "pub":
        return value if is_plain_version(value) else None
    if dependency.ecosystem == "go":
        return value if value.startswith("v") else None
    return None


def is_plain_version(value: str) -> bool:
    if not value or value in {"*", "latest"}:
        return False
    if value.startswith(("^", "~", ">", "<", "=", "!", "file:", "git+", "http:", "https:", "workspace:", "link:", "path")):
        return False
    if any(character.isspace() for character in value):
        return False
    return re.match(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$", value) is not None


def is_maven_version(value: str) -> bool:
    if not value or value.startswith("${"):
        return False
    if any(character in value for character in "[](),"):
        return False
    if any(character.isspace() for character in value):
        return False
    return True


class SourceLookupError(Exception):
    def __init__(self, status: str, error_code: str, message: str):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.message = message
