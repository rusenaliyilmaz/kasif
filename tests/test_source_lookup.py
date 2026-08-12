from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from kasif.model import Dependency
from kasif.profiler import Profiler
from kasif.source_lookup import (
    GitRemoteRef,
    SourceResolution,
    SourceLookupError,
    candidate_refs,
    checkout_git_ref,
    enrich_dependencies,
    fetch_maven_pom,
    find_source,
    http_json,
    kasif_version,
    normalize_repository_url,
    resolve_pypi,
    resolve_git_ref,
    run_git,
    verify_manifest,
)


class SourceLookupTest(unittest.TestCase):
    def test_normalizes_exact_versions_for_source_lookup(self) -> None:
        self.assertEqual("3.0", kasif_version(dependency("pypi", "tzlocal", "==3.0")))
        self.assertEqual("18.2.0", kasif_version(dependency("npm", "react", "18.2.0")))
        self.assertEqual("2.17.2", kasif_version(dependency("maven", "com.example:demo", "2.17.2")))
        self.assertEqual("1.2.2", kasif_version(dependency("pub", "http", "1.2.2")))
        self.assertEqual("v1.10.0", kasif_version(dependency("go", "github.com/gin-gonic/gin", "v1.10.0")))

    def test_skips_non_exact_versions(self) -> None:
        self.assertIsNone(kasif_version(dependency("pypi", "requests", ">=2.0")))
        self.assertIsNone(kasif_version(dependency("npm", "react", "^18.2.0")))
        self.assertIsNone(kasif_version(dependency("pub", "http", "^1.2.2")))
        self.assertIsNone(kasif_version(dependency("maven", "com.example:demo", "${demo.version}")))

    def test_normalizes_repository_urls_and_subdirectories(self) -> None:
        self.assertEqual(
            ("https://github.com/regebro/tzlocal.git", None),
            normalize_repository_url("git+https://github.com/regebro/tzlocal.git"),
        )
        self.assertEqual(
            ("https://github.com/example/repo.git", "packages/demo"),
            normalize_repository_url("https://github.com/example/repo/tree/main/packages/demo"),
        )
        self.assertEqual(
            ("https://github.com/example/repo.git", None),
            normalize_repository_url("github:example/repo"),
        )
        self.assertEqual(
            ("https://github.com/spring-projects/spring-framework.git", None),
            normalize_repository_url("scm:git:git://github.com/spring-projects/spring-framework.git"),
        )
        self.assertEqual(
            ("https://github.com/spring-projects/spring-framework.git", None),
            normalize_repository_url("scm:git:ssh://git@github.com/spring-projects/spring-framework.git"),
        )
        self.assertEqual(
            ("https://github.com/spring-projects/spring-framework.git", None),
            normalize_repository_url("scm:git:git@github.com:spring-projects/spring-framework.git"),
        )
        self.assertEqual(
            ("https://git.code.sf.net/p/docutils/code", None),
            normalize_repository_url("https://sourceforge.net/p/docutils/code/"),
        )

    def test_find_source_builds_found_response(self) -> None:
        resolution = SourceResolution(
            ecosystem="pypi",
            package="tzlocal",
            version="3.0",
            repository_url="https://github.com/regebro/tzlocal.git",
            requested_ref="3.0",
            repository_subdirectory=None,
            source_archive_url="https://files.pythonhosted.org/tzlocal.tar.gz",
            source_archive_sha256="abc123",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "setup.cfg", """
                [metadata]
                name = tzlocal
                version = 3.0
            """)
            with patch("kasif.source_lookup.resolve_registry_source", return_value=resolution), \
                    patch("kasif.source_lookup.resolve_git_ref", return_value=("commit1", "3.0")), \
                    patch("kasif.source_lookup.checkout_git_ref", return_value=(root, "commit1")):
                response = find_source("pypi", "tzlocal", "3.0")

        self.assertEqual("FOUND", response["status"])
        self.assertEqual("https://github.com/regebro/tzlocal.git", response["repositoryUrl"])
        self.assertEqual("commit1", response["resolvedCommit"])
        self.assertEqual("setup.cfg", response["manifestPath"])
        self.assertEqual("git", response["sourceKind"])
        self.assertEqual([
            "--git-repo",
            "https://github.com/regebro/tzlocal.git",
            "--repo-ref",
            "commit1",
            "--package-coordinate",
            "pypi:tzlocal@3.0",
        ], response["ocakGitArguments"])
        self.assertEqual(response["ocakGitArguments"], response["ocakArguments"])

    def test_find_source_maps_source_lookup_errors_to_failed_response(self) -> None:
        with patch("kasif.source_lookup.resolve_registry_source", side_effect=SourceLookupError(
            "SOURCE_NOT_FOUND",
            "SOURCE_METADATA_MISSING",
            "metadata missing",
        )):
            response = find_source("npm", "missing", "1.0.0")

        self.assertEqual("SOURCE_NOT_FOUND", response["status"])
        self.assertEqual("npm:missing@1.0.0", response["packageCoordinate"])
        self.assertEqual("SOURCE_METADATA_MISSING", response["errorCode"])
        self.assertEqual([], response["ocakGitArguments"])
        self.assertEqual([], response["ocakArguments"])

    def test_find_source_shallow_check_skips_checkout_and_manifest_verification(self) -> None:
        resolution = SourceResolution(
            ecosystem="maven",
            package="org.springframework:spring-web",
            version="5.0.20.RELEASE",
            repository_url="scm:git:git://github.com/spring-projects/spring-framework.git",
            requested_ref="v5.0.20.RELEASE",
            repository_subdirectory=None,
        )
        with patch("kasif.source_lookup.resolve_registry_source", return_value=resolution), \
                patch("kasif.source_lookup.resolve_git_ref", return_value=("commit1", "v5.0.20.RELEASE")), \
                patch("kasif.source_lookup.checkout_git_ref") as checkout, \
                patch("kasif.source_lookup.verify_manifest") as verify:
            response = find_source("maven", "org.springframework:spring-web", "5.0.20.RELEASE", shallow_check=True)

        checkout.assert_not_called()
        verify.assert_not_called()
        self.assertEqual("FOUND", response["status"])
        self.assertEqual("commit1", response["resolvedCommit"])
        self.assertEqual("v5.0.20.RELEASE", response["matchedRef"])
        self.assertIsNone(response["manifestPath"])
        self.assertEqual("git", response["sourceKind"])
        self.assertIn("skipped checkout", response["message"])
        self.assertEqual([
            "--git-repo",
            "https://github.com/spring-projects/spring-framework.git",
            "--repo-ref",
            "commit1",
            "--package-coordinate",
            "maven:org.springframework:spring-web@5.0.20.RELEASE",
        ], response["ocakGitArguments"])

    def test_checkout_defaults_to_defter_cache_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"

            def fake_runner(command: list[str], **_: object) -> subprocess.CompletedProcess:
                if command[-2:] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(command, 0, stdout="commit1\n", stderr="")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with patch.dict("os.environ", {"DEFTER_CACHE_DIR": str(cache_dir)}, clear=False):
                checkout_root, resolved_commit = checkout_git_ref(
                    "https://github.com/example/demo.git",
                    "commit1",
                    checkout_dir=None,
                    timeout_seconds=1,
                    runner=fake_runner,
                )

        self.assertEqual("commit1", resolved_commit)
        self.assertIn("kasif-source-", checkout_root.name)
        self.assertEqual(cache_dir / "v1" / "sources" / "kasif-checkouts", checkout_root.parent)

    def test_fetch_maven_pom_falls_back_to_google_maven(self) -> None:
        first_error = SourceLookupError("SOURCE_NOT_FOUND", "PACKAGE_VERSION_NOT_FOUND", "missing")
        firebase_pom = """
            <project>
              <groupId>com.google.firebase</groupId>
              <artifactId>firebase-core</artifactId>
              <version>20.0.1</version>
            </project>
        """
        with patch("kasif.source_lookup.http_text", side_effect=[first_error, textwrap.dedent(firebase_pom)]):
            pom = fetch_maven_pom("com.google.firebase", "firebase-core", "20.0.1")

        self.assertIsNotNone(pom)
        self.assertEqual("firebase-core", pom.findtext("artifactId"))

    def test_enriches_dependency_without_java_bridge(self) -> None:
        with patch("kasif.source_lookup.find_source", return_value={"status": "FOUND", "repositoryUrl": "repo"}):
            enriched = enrich_dependencies([dependency("pypi", "tzlocal", "==3.0")])

        self.assertEqual("FOUND", enriched[0].source["status"])
        self.assertEqual("repo", enriched[0].source["repositoryUrl"])

    def test_enrich_skips_dependencies_without_exact_versions(self) -> None:
        with patch("kasif.source_lookup.find_source") as find_source_mock:
            enriched = enrich_dependencies([dependency("npm", "react", "^18.2.0")])

        find_source_mock.assert_not_called()
        self.assertEqual("SKIPPED", enriched[0].source["status"])
        self.assertEqual("VERSION_NOT_EXACT", enriched[0].source["errorCode"])

    def test_enrich_limits_source_resolution_attempts(self) -> None:
        with patch("kasif.source_lookup.find_source", return_value={"status": "FOUND", "repositoryUrl": "repo"}) as find_source_mock:
            enriched = enrich_dependencies(
                [
                    dependency("pypi", "first", "1.0.0"),
                    dependency("pypi", "second", "2.0.0"),
                ],
                max_source_resolutions=1,
            )

        self.assertEqual(1, find_source_mock.call_count)
        self.assertEqual("FOUND", enriched[0].source["status"])
        self.assertEqual("SKIPPED", enriched[1].source["status"])
        self.assertEqual("SOURCE_RESOLUTION_LIMIT_REACHED", enriched[1].source["errorCode"])

    def test_verifies_manifests_for_supported_ecosystems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "pyproject.toml", """
                [project]
                name = "demo-py"
                version = "1.0.0"
            """)
            self.write(root / "package.json", json.dumps({"name": "demo-npm", "version": "2.0.0"}))
            self.write(root / "pubspec.yaml", """
                name: demo_pub
                version: 3.0.0
            """)
            self.write(root / "go.mod", "module github.com/example/demo\n")
            self.write(root / "pom.xml", """
                <project>
                  <groupId>com.example</groupId>
                  <artifactId>demo</artifactId>
                  <version>4.0.0</version>
                </project>
            """)

            self.assertEqual(("pyproject.toml", None), verify_manifest(root, SourceResolution("pypi", "demo-py", "1.0.0", "repo", "v1.0.0", None)))
            self.assertEqual(("package.json", None), verify_manifest(root, SourceResolution("npm", "demo-npm", "2.0.0", "repo", "v2.0.0", None)))
            self.assertEqual(("pubspec.yaml", None), verify_manifest(root, SourceResolution("pub", "demo_pub", "3.0.0", "repo", "v3.0.0", None)))
            self.assertEqual(("go.mod", None), verify_manifest(root, SourceResolution("go", "github.com/example/demo", "v1.2.3", "repo", "v1.2.3", None)))
            self.assertEqual(("pom.xml", None), verify_manifest(root, SourceResolution("maven", "com.example:demo", "4.0.0", "repo", "v4.0.0", None)))

    def test_verifies_pypi_manifest_with_dynamic_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "pyproject.toml", """
                [project]
                name = "demo-py"
                dynamic = ["version"]
            """)

            self.assertEqual(
                ("pyproject.toml", None),
                verify_manifest(root, SourceResolution("pypi", "demo-py", "1.0.0", "repo", "v1.0.0", None)),
            )

    def test_verifies_maven_artifact_from_gradle_module_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "gradle.properties", "version=5.0.20.RELEASE")
            self.write(root / "build.gradle", """
                configure(allprojects) {
                    group = "org.springframework"
                    version = qualifyVersionIfNecessary(version)
                }
            """)
            self.write(root / "settings.gradle", """
                include "spring-web"
                rootProject.children.each { project ->
                    project.buildFileName = "${project.name}.gradle"
                }
            """)
            self.write(root / "spring-web" / "spring-web.gradle", """
                description = "Spring Web"
            """)

            self.assertEqual(
                ("spring-web/spring-web.gradle", "spring-web"),
                verify_manifest(root, SourceResolution(
                    "maven",
                    "org.springframework:spring-web",
                    "5.0.20.RELEASE",
                    "repo",
                    "v5.0.20.RELEASE",
                    None,
                )),
            )

    def test_resolve_git_ref_prefers_dereferenced_annotated_tag(self) -> None:
        resolution = SourceResolution("pub", "openai_dart", "5.0.0", "https://example.test/repo.git", "v5.0.0", None)
        with patch("kasif.source_lookup.git_ls_remote", return_value=[
            GitRemoteRef("tag-object", "refs/tags/v5.0.0"),
            GitRemoteRef("commit-object", "refs/tags/v5.0.0^{}"),
        ]):
            commit, matched_ref = resolve_git_ref(resolution, 300, subprocess.run)

        self.assertEqual("commit-object", commit)
        self.assertEqual("v5.0.0", matched_ref)

    def test_resolve_git_ref_uses_ecosystem_candidate_refs(self) -> None:
        resolution = SourceResolution("maven", "org.springframework:spring-web", "5.0.20.RELEASE", "https://example.test/repo.git", None, None)
        with patch("kasif.source_lookup.git_ls_remote", return_value=[
            GitRemoteRef("spring-commit", "refs/tags/spring-web-v5.0.20.RELEASE"),
        ]):
            commit, matched_ref = resolve_git_ref(resolution, 300, subprocess.run)

        self.assertEqual("spring-commit", commit)
        self.assertEqual("spring-web-v5.0.20.RELEASE", matched_ref)

    def test_candidate_refs_include_package_manager_conventions(self) -> None:
        npm_refs = candidate_refs("npm", "@types/node", "22.13.5")
        maven_refs = candidate_refs("maven", "org.springframework:spring-web", "5.0.20.RELEASE")

        self.assertIn("types-node@22.13.5", npm_refs)
        self.assertIn("types-node-v22.13.5", npm_refs)
        self.assertIn("org.springframework-spring-web-v5.0.20.RELEASE", maven_refs)

    def test_find_source_falls_back_to_verified_archive_when_git_ref_is_missing(self) -> None:
        body = tar_bytes("package/package.json", json.dumps({"name": "@types/node", "version": "22.13.5"}))
        digest = hashlib.sha256(body).hexdigest()
        resolution = SourceResolution(
            ecosystem="npm",
            package="@types/node",
            version="22.13.5",
            repository_url="https://github.com/DefinitelyTyped/DefinitelyTyped.git",
            requested_ref="v22.13.5",
            repository_subdirectory=None,
            source_archive_url="https://registry.npmjs.org/@types/node/-/node-22.13.5.tgz",
            source_archive_sha256=digest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            with patch("kasif.source_lookup.resolve_registry_source", return_value=resolution), \
                    patch("kasif.source_lookup.resolve_git_ref", side_effect=SourceLookupError(
                        "VERSION_REF_NOT_FOUND",
                        "VERSION_REF_NOT_FOUND",
                        "missing ref",
                    )), \
                    patch("kasif.source_lookup.http_bytes", return_value=body):
                response = find_source("npm", "@types/node", "22.13.5", checkout_dir=Path(tmp))

        self.assertEqual("FOUND", response["status"])
        self.assertEqual("archive", response["sourceKind"])
        self.assertEqual("package/package.json", response["manifestPath"])
        self.assertEqual("package", response["repositorySubdirectory"])
        self.assertEqual([], response["ocakGitArguments"])
        self.assertIn("--archive-file", response["ocakArguments"])
        self.assertIn("--archive-sha256", response["ocakArguments"])
        self.assertEqual(digest, response["sourceArchiveSha256"])

    def test_resolve_pypi_ignores_documentation_homepage_when_archive_exists(self) -> None:
        metadata = {
            "info": {"home_page": "https://execnet.readthedocs.io/en/latest/"},
            "urls": [{
                "packagetype": "sdist",
                "url": "https://files.pythonhosted.org/packages/execnet.tar.gz",
                "digests": {"sha256": "a" * 64},
            }],
        }

        with patch("kasif.source_lookup.http_json", return_value=metadata):
            resolution = resolve_pypi("execnet", "2.1.2")

        self.assertEqual("", resolution.repository_url)
        self.assertEqual("https://files.pythonhosted.org/packages/execnet.tar.gz", resolution.source_archive_url)

    def test_verify_manifest_finds_matching_npm_package_after_root_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "package.json", json.dumps({"name": "workspace-root", "version": "1.0.0"}))
            self.write(root / "packages" / "vsce" / "package.json", json.dumps({"name": "@vscode/vsce", "version": "3.7.1"}))

            manifest, subdir = verify_manifest(root, SourceResolution("npm", "@vscode/vsce", "3.7.1", "repo", "v3.7.1", None))

        self.assertEqual("packages/vsce/package.json", manifest)
        self.assertEqual("packages/vsce", subdir)

    def test_run_git_maps_timeout_to_source_timeout_error(self) -> None:
        def timeout_runner(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        with self.assertRaises(SourceLookupError) as context:
            run_git(["git", "ls-remote", "https://example.test/repo.git"], 1, timeout_runner)

        self.assertEqual("FAILED", context.exception.status)
        self.assertEqual("SOURCE_TIMEOUT", context.exception.error_code)

    def test_run_git_maps_oserror_to_git_unavailable(self) -> None:
        def missing_git(*_args, **_kwargs):
            raise FileNotFoundError("git")

        with self.assertRaises(SourceLookupError) as context:
            run_git(["git", "status"], 1, missing_git)

        self.assertEqual("FAILED", context.exception.status)
        self.assertEqual("GIT_UNAVAILABLE", context.exception.error_code)

    def test_run_git_records_profiler_metrics(self) -> None:
        profiler = Profiler()

        def runner(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                args=["git", "status"],
                returncode=0,
                stdout="hello",
                stderr="warning",
            )

        run_git(["git", "-C", "/tmp/example", "status"], 1, runner, profiler)
        payload = profiler.to_dict()

        self.assertEqual(len("hello"), payload["totals"]["gitStdoutBytes"])
        self.assertEqual(len("warning"), payload["totals"]["gitStderrBytes"])
        self.assertEqual("git -C <checkout> status", payload["events"][0]["detail"])
        self.assertEqual("git", payload["events"][0]["stage"])

    def test_checkout_cleans_up_failed_temporary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout_dir = Path(tmp)

            def runner(command: list[str], **_: object) -> subprocess.CompletedProcess:
                if "fetch" in command:
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="fetch failed")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with self.assertRaises(SourceLookupError):
                checkout_git_ref(
                    "https://github.com/example/demo.git",
                    "abcdef1",
                    checkout_dir=checkout_dir,
                    timeout_seconds=1,
                    runner=runner,
                )

            self.assertEqual([], list(checkout_dir.glob("kasif-source-*")))

    def test_invalid_json_manifest_returns_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("{not-json", encoding="utf-8")

            with self.assertRaises(SourceLookupError) as context:
                verify_manifest(root, SourceResolution("npm", "demo", "1.0.0", "repo", "v1.0.0", None))

        self.assertEqual("PACKAGE_MANIFEST_INVALID", context.exception.error_code)

    def test_http_json_maps_invalid_registry_payload(self) -> None:
        with patch("kasif.source_lookup.http_text", return_value="not-json"):
            with self.assertRaises(SourceLookupError) as context:
                http_json("https://example.test/package")

        self.assertEqual("SOURCE_METADATA_INVALID", context.exception.error_code)

    def test_subdirectory_manifest_is_returned_as_ocak_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "packages" / "demo" / "package.json", json.dumps({"name": "demo", "version": "1.0.0"}))

            manifest, subdir = verify_manifest(root, SourceResolution("npm", "demo", "1.0.0", "repo", "v1.0.0", "packages/demo"))

        self.assertEqual("packages/demo/package.json", manifest)
        self.assertEqual("packages/demo", subdir)

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def dependency(ecosystem: str, name: str, version: str) -> Dependency:
    return Dependency(
        ecosystem=ecosystem,
        name=name,
        version=version,
        requirement=version,
        scope="dependencies",
        manager="test",
        source_file="manifest",
    )


def tar_bytes(path: str, content: str) -> bytes:
    buffer = io.BytesIO()
    payload = content.encode("utf-8")
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(path)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
