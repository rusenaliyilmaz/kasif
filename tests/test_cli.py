from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kasif.cli import main


class CliTest(unittest.TestCase):
    def test_find_source_subcommand_prints_json(self) -> None:
        with patch("kasif.cli.find_source", return_value={
            "status": "FOUND",
            "packageCoordinate": "pypi:tzlocal@3.0",
            "repositoryUrl": "https://github.com/regebro/tzlocal.git",
            "resolvedCommit": "abc123",
        }) as find_source:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main([
                    "find-source",
                    "--ecosystem", "pypi",
                    "--package", "tzlocal",
                    "--version", "3.0",
                    "--git-timeout-seconds", "17",
                ])

        self.assertEqual(0, exit_code)
        find_source.assert_called_once_with(
            "pypi",
            "tzlocal",
            "3.0",
            checkout_dir=None,
            cache_dir=None,
            git_timeout_seconds=17,
            http_timeout_seconds=None,
            http_retries=0,
            maven_repositories=None,
            offline=False,
            profiler=None,
            shallow_check=False,
            progress=None,
        )
        self.assertEqual("FOUND", json.loads(output.getvalue())["status"])

    def test_find_source_subcommand_forwards_shallow_check(self) -> None:
        with patch("kasif.cli.find_source", return_value={"status": "FOUND"}) as find_source:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main([
                    "find-source",
                    "--ecosystem", "maven",
                    "--package", "org.springframework:spring-web",
                    "--version", "5.0.20.RELEASE",
                    "--shallow-check",
                ])

        self.assertEqual(0, exit_code)
        self.assertTrue(find_source.call_args.kwargs["shallow_check"])

    def test_find_source_rejects_non_positive_git_timeout(self) -> None:
        diagnostics = io.StringIO()
        with redirect_stderr(diagnostics):
            with self.assertRaises(SystemExit) as context:
                main([
                    "find-source",
                    "--ecosystem", "pypi",
                    "--package", "tzlocal",
                    "--version", "3.0",
                    "--git-timeout-seconds", "0",
                ])

        self.assertEqual(2, context.exception.code)
        self.assertIn("--git-timeout-seconds must be greater than zero", diagnostics.getvalue())

    def test_find_source_profiler_prints_to_stderr(self) -> None:
        with patch("kasif.cli.find_source", return_value={"status": "FOUND"}) as find_source:
            output = io.StringIO()
            diagnostics = io.StringIO()
            with redirect_stdout(output), redirect_stderr(diagnostics):
                exit_code = main([
                    "find-source",
                    "--ecosystem", "pypi",
                    "--package", "tzlocal",
                    "--version", "3.0",
                    "--enable-profiler",
                ])

        self.assertEqual(0, exit_code)
        self.assertEqual("FOUND", json.loads(output.getvalue())["status"])
        profiler = find_source.call_args.kwargs["profiler"]
        self.assertIsNotNone(profiler)
        diagnostics_payload = json.loads(diagnostics.getvalue())
        self.assertIn("profiler", diagnostics_payload)
        self.assertIn("totalDurationMs", diagnostics_payload["profiler"])

    def test_find_source_progress_prints_to_stderr(self) -> None:
        def fake_find_source(*_: object, **kwargs: object) -> dict:
            progress = kwargs["progress"]
            progress("lookup started")
            return {"status": "FOUND"}

        with patch("kasif.cli.find_source", side_effect=fake_find_source):
            output = io.StringIO()
            diagnostics = io.StringIO()
            with redirect_stdout(output), redirect_stderr(diagnostics):
                exit_code = main([
                    "find-source",
                    "--ecosystem", "pypi",
                    "--package", "tzlocal",
                    "--version", "3.0",
                    "--progress",
                ])

        self.assertEqual(0, exit_code)
        self.assertEqual("FOUND", json.loads(output.getvalue())["status"])
        self.assertIn("[kasif] lookup started", diagnostics.getvalue())

    def test_find_source_subcommand_returns_failure_exit_code(self) -> None:
        with patch("kasif.cli.find_source", return_value={
            "status": "SOURCE_NOT_FOUND",
            "errorCode": "SOURCE_METADATA_MISSING",
        }):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["find-source", "--ecosystem", "npm", "--package", "missing", "--version", "1.0.0"])

        self.assertEqual(1, exit_code)
        self.assertEqual("SOURCE_NOT_FOUND", json.loads(output.getvalue())["status"])

    def test_scan_subcommand_discovers_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("tzlocal==3.0\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["scan", str(root), "--format", "json"])

        self.assertEqual(0, exit_code)
        dependencies = json.loads(output.getvalue())
        self.assertEqual("tzlocal", dependencies[0]["name"])

    def test_scan_subcommand_forwards_source_resolution_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout_dir = root / "checkouts"
            (root / "requirements.txt").write_text("tzlocal==3.0\n", encoding="utf-8")
            with patch("kasif.cli.enrich_dependencies", side_effect=lambda dependencies, **_: list(dependencies)) as enrich:
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = main([
                        "scan",
                        str(root),
                        "--resolve-sources",
                        "--kasif-checkout-dir", str(checkout_dir),
                        "--git-timeout-seconds", "19",
                        "--shallow-check",
                        "--format", "json",
                    ])

        self.assertEqual(0, exit_code)
        enrich.assert_called_once()
        self.assertEqual(checkout_dir, enrich.call_args.kwargs["checkout_dir"])
        self.assertIsNone(enrich.call_args.kwargs["cache_dir"])
        self.assertEqual(19, enrich.call_args.kwargs["git_timeout_seconds"])
        self.assertIsNone(enrich.call_args.kwargs["http_timeout_seconds"])
        self.assertEqual(0, enrich.call_args.kwargs["http_retries"])
        self.assertIsNone(enrich.call_args.kwargs["maven_repositories"])
        self.assertFalse(enrich.call_args.kwargs["offline"])
        self.assertTrue(enrich.call_args.kwargs["shallow_check"])
        self.assertIsNone(enrich.call_args.kwargs["progress"])
        self.assertIsNone(enrich.call_args.kwargs["max_source_resolutions"])
        self.assertEqual("tzlocal", json.loads(output.getvalue())[0]["name"])

    def test_scan_subcommand_forwards_source_resolution_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("tzlocal==3.0\n", encoding="utf-8")
            with patch("kasif.cli.enrich_dependencies", side_effect=lambda dependencies, **_: list(dependencies)) as enrich:
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = main([
                        "scan",
                        str(root),
                        "--resolve-sources",
                        "--max-source-resolutions",
                        "50",
                        "--format",
                        "json",
                    ])

        self.assertEqual(0, exit_code)
        self.assertEqual(50, enrich.call_args.kwargs["max_source_resolutions"])

    def test_scan_progress_prints_to_stderr_without_polluting_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("tzlocal==3.0\n", encoding="utf-8")
            output = io.StringIO()
            diagnostics = io.StringIO()
            with redirect_stdout(output), redirect_stderr(diagnostics):
                exit_code = main(["scan", str(root), "--format", "json", "--progress"])

        self.assertEqual(0, exit_code)
        self.assertEqual("tzlocal", json.loads(output.getvalue())[0]["name"])
        self.assertIn("[kasif] scanning project files", diagnostics.getvalue())

    def test_scan_filters_ecosystems_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("tzlocal==3.0\n", encoding="utf-8")
            (root / "frontend").mkdir()
            (root / "frontend" / "package.json").write_text(
                json.dumps({"dependencies": {"react": "18.2.0"}}),
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main([
                    "scan",
                    str(root),
                    "--format",
                    "json",
                    "--ecosystem",
                    "npm",
                    "--include-path",
                    "frontend/**",
                ])

        dependencies = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual(["react"], [dependency["name"] for dependency in dependencies])

    def test_find_source_forwards_network_and_cache_options(self) -> None:
        with patch("kasif.cli.find_source", return_value={"status": "FOUND"}) as find_source:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main([
                    "find-source",
                    "--ecosystem", "maven",
                    "--package", "com.example:demo",
                    "--version", "1.0.0",
                    "--cache-dir", "/tmp/kasif-cache",
                    "--http-timeout-seconds", "9",
                    "--retries", "2",
                    "--maven-repository", "https://repo.example.test/maven2",
                    "--offline",
                ])

        self.assertEqual(0, exit_code)
        self.assertEqual(Path("/tmp/kasif-cache"), find_source.call_args.kwargs["cache_dir"])
        self.assertEqual(9, find_source.call_args.kwargs["http_timeout_seconds"])
        self.assertEqual(2, find_source.call_args.kwargs["http_retries"])
        self.assertEqual(["https://repo.example.test/maven2"], find_source.call_args.kwargs["maven_repositories"])
        self.assertTrue(find_source.call_args.kwargs["offline"])

    def test_scan_writes_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_path = root / "dependencies.json"
            (root / "requirements.txt").write_text("tzlocal==3.0\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["scan", str(root), "--format", "json", "--output", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", output.getvalue())
            self.assertEqual("tzlocal", json.loads(output_path.read_text(encoding="utf-8"))[0]["name"])


if __name__ == "__main__":
    unittest.main()
