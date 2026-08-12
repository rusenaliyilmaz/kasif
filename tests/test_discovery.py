from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from kasif.discovery import discover_dependencies, parse_manifest


class DependencyDiscoveryTest(unittest.TestCase):
    def test_discovers_supported_ecosystems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "pom.xml", """
                <project xmlns="http://maven.apache.org/POM/4.0.0">
                  <properties>
                    <junit.version>5.10.3</junit.version>
                  </properties>
                  <dependencies>
                    <dependency>
                      <groupId>org.junit.jupiter</groupId>
                      <artifactId>junit-jupiter</artifactId>
                      <version>${junit.version}</version>
                      <scope>test</scope>
                    </dependency>
                  </dependencies>
                </project>
            """)
            self.write_json(root / "package.json", {
                "dependencies": {"react": "^18.2.0"},
                "devDependencies": {"typescript": "^5.5.0"},
            })
            self.write_json(root / "package-lock.json", {
                "packages": {
                    "": {"dependencies": {"react": "^18.2.0"}},
                    "node_modules/react": {"version": "18.2.0"},
                    "node_modules/typescript": {"version": "5.5.4"},
                }
            })
            self.write(root / "requirements.txt", """
                requests==2.32.3
                rich>=13.7
            """)
            self.write(root / "pubspec.yaml", """
                dependencies:
                  http: ^1.2.2
                dev_dependencies:
                  test: ^1.25.0
            """)
            self.write(root / "pubspec.lock", """
                packages:
                  http:
                    dependency: "direct main"
                    version: "1.2.2"
                  test:
                    dependency: "direct dev"
                    version: "1.25.8"
            """)
            self.write(root / "go.mod", """
                module example.com/demo

                require (
                    github.com/gin-gonic/gin v1.10.0
                )
            """)
            self.write(root / "build.gradle", """
                dependencies {
                    implementation 'com.google.guava:guava:33.2.1-jre'
                }
            """)

            dependencies = discover_dependencies(root)
            found = {(dependency.ecosystem, dependency.name): dependency for dependency in dependencies}

            self.assertEqual("5.10.3", found[("maven", "org.junit.jupiter:junit-jupiter")].version)
            self.assertEqual("18.2.0", found[("npm", "react")].version)
            self.assertEqual("^18.2.0", found[("npm", "react")].requirement)
            self.assertEqual("==2.32.3", found[("pypi", "requests")].version)
            self.assertEqual("1.2.2", found[("pub", "http")].version)
            self.assertEqual("v1.10.0", found[("go", "github.com/gin-gonic/gin")].version)
            self.assertEqual("33.2.1-jre", found[("maven", "com.google.guava:guava")].version)

    def test_discovers_pyproject_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "pyproject.toml", """
                [project]
                dependencies = [
                  "FastAPI>=0.111",
                  "uvicorn[standard]==0.30.1",
                ]

                [project.optional-dependencies]
                test = ["pytest>=8"]

                [tool.poetry.group.docs.dependencies]
                mkdocs = "^1.6.0"
            """)

            dependencies = discover_dependencies(root)
            found = {(dependency.name, dependency.scope): dependency for dependency in dependencies}

            self.assertEqual(">=0.111", found[("fastapi", "dependencies")].version)
            self.assertEqual("==0.30.1", found[("uvicorn", "dependencies")].version)
            self.assertEqual(">=8", found[("pytest", "optional:test")].version)
            self.assertEqual("^1.6.0", found[("mkdocs", "group:docs")].version)

    def test_discovers_extended_ocak_language_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "libs.versions.toml", """
                [versions]
                junit = "5.10.3"

                [libraries]
                junit-jupiter = { module = "org.junit.jupiter:junit-jupiter", version.ref = "junit" }
            """)
            self.write(root / "gradle.lockfile", """
                com.google.guava:guava:33.2.1-jre=compileClasspath
            """)
            self.write(root / "Pipfile", """
                [packages]
                requests = "==2.32.3"
            """)
            self.write_json(root / "piplocked" / "Pipfile.lock", {
                "default": {"rich": {"version": "==13.7.1"}}
            })
            self.write(root / "poetry.lock", """
                [[package]]
                name = "fastapi"
                version = "0.111.0"
                groups = ["main"]
            """)
            self.write(root / "frontend" / "yarn.lock", """
                react@^18.2.0:
                  version "18.2.0"
            """)
            self.write(root / "frontend" / "pnpm-lock.yaml", """
                dependencies:
                  "@vitejs/plugin-react":
                    version: 4.3.1(vite@5.4.0)
            """)
            self.write_json(root / "frontend" / "bun.lock", {
                "packages": {"zod": ["3.23.8"]}
            })
            self.write_json(root / "native" / "vcpkg.json", {
                "dependencies": [
                    "fmt",
                    {"name": "openssl", "version>=": "3.2.0", "features": ["tools"]},
                ]
            })
            self.write(root / "native" / "conanfile.txt", """
                [requires]
                zlib/1.3.1
            """)
            self.write(root / "native" / "conanfile.py", """
                from conan import ConanFile

                class Demo(ConanFile):
                    requires = "fmt/10.2.1"
            """)
            self.write(root / "native" / "CMakeLists.txt", """
                find_package(OpenSSL 3.2 REQUIRED)
                FetchContent_Declare(googletest GIT_REPOSITORY https://github.com/google/googletest.git GIT_TAG v1.14.0)
            """)

            dependencies = discover_dependencies(root)
            found = {(dependency.ecosystem, dependency.name): dependency for dependency in dependencies}

            self.assertEqual("5.10.3", found[("maven", "org.junit.jupiter:junit-jupiter")].version)
            self.assertEqual("33.2.1-jre", found[("maven", "com.google.guava:guava")].version)
            self.assertEqual("==2.32.3", found[("pypi", "requests")].version)
            self.assertEqual("==13.7.1", found[("pypi", "rich")].version)
            self.assertEqual("0.111.0", found[("pypi", "fastapi")].version)
            self.assertEqual("18.2.0", found[("npm", "react")].version)
            self.assertEqual("4.3.1", found[("npm", "@vitejs/plugin-react")].version)
            self.assertEqual("3.23.8", found[("npm", "zod")].version)
            self.assertIsNone(found[("vcpkg", "fmt")].version)
            self.assertEqual("3.2.0", found[("vcpkg", "openssl")].version)
            self.assertEqual("1.3.1", found[("conan", "zlib")].version)
            self.assertEqual("10.2.1", found[("conan", "fmt")].version)
            self.assertEqual("3.2", found[("cmake", "OpenSSL")].version)
            self.assertEqual("v1.14.0", found[("cmake", "googletest")].version)

    def test_real_project_snapshots_with_fewer_than_30_dependencies(self) -> None:
        fixtures = {
            "flask": {
                "files": {
                    "pyproject.toml": """
                        [project]
                        name = "Flask"
                        dependencies = [
                          "blinker>=1.9.0",
                          "click>=8.1.3",
                          "itsdangerous>=2.2.0",
                          "jinja2>=3.1.2",
                          "markupsafe>=2.1.1",
                          "werkzeug>=3.1.0",
                        ]

                        [project.optional-dependencies]
                        async = ["asgiref>=3.2"]
                        dotenv = ["python-dotenv"]
                    """,
                },
                "expected_count": 8,
                "expected": {
                    ("pypi", "werkzeug", ">=3.1.0"),
                    ("pypi", "python-dotenv", None),
                },
            },
            "werkzeug": {
                "files": {
                    "pyproject.toml": """
                        [project]
                        name = "Werkzeug"
                        dependencies = [
                          "markupsafe>=3.0.3",
                        ]

                        [project.optional-dependencies]
                        watchdog = ["watchdog>=6"]
                    """,
                },
                "expected_count": 2,
                "expected": {
                    ("pypi", "markupsafe", ">=3.0.3"),
                    ("pypi", "watchdog", ">=6"),
                },
            },
            "black": {
                "files": {
                    "pyproject.toml": """
                        [project]
                        name = "black"
                        dependencies = [
                          "click>=8.0.0",
                          "mypy-extensions>=0.4.3",
                          "packaging>=22.0",
                          "pathspec>=1.0.0",
                          "platformdirs>=2",
                          "pytokens~=0.4.0",
                          "tomli>=1.1.0; python_version<'3.11'",
                          "typing-extensions>=4.0.1; python_version<'3.11'",
                        ]

                        [project.optional-dependencies]
                        colorama = ["colorama>=0.4.3"]
                        uvloop = [
                          "uvloop>=0.15.2; sys_platform != 'win32'",
                          "winloop>=0.5.0; sys_platform == 'win32'",
                        ]
                        d = ["aiohttp>=3.10"]
                        jupyter = ["ipython>=7.8.0", "tokenize-rt>=3.2.0"]
                    """,
                },
                "expected_count": 14,
                "expected": {
                    ("pypi", "pytokens", "~=0.4.0"),
                    ("pypi", "tokenize-rt", ">=3.2.0"),
                },
            },
            "rich": {
                "files": {
                    "pyproject.toml": """
                        [tool.poetry]
                        name = "rich"
                        version = "15.0.0"

                        [tool.poetry.dependencies]
                        python = ">=3.9.0"
                        pygments = "^2.13.0"
                        ipywidgets = { version = ">=7.5.1,<9", optional = true }
                        markdown-it-py = ">=2.2.0"

                        [tool.poetry.dev-dependencies]
                        pytest = "^7.0.0"
                        black = "^22.6"
                        mypy = "^1.11"
                        pytest-cov = "^3.0.0"
                        attrs = "^21.4.0"
                        pre-commit = "^2.17.0"
                        typing-extensions = ">=4.0.0, <5.0"
                    """,
                },
                "expected_count": 10,
                "expected": {
                    ("pypi", "markdown-it-py", ">=2.2.0"),
                    ("pypi", "typing-extensions", ">=4.0.0, <5.0"),
                },
            },
            "cobra": {
                "files": {
                    "go.mod": """
                        module github.com/spf13/cobra

                        go 1.15

                        require (
                          github.com/cpuguy83/go-md2man/v2 v2.0.6
                          github.com/inconshreveable/mousetrap v1.1.0
                          github.com/spf13/pflag v1.0.9
                          go.yaml.in/yaml/v3 v3.0.4
                        )
                    """,
                },
                "expected_count": 4,
                "expected": {
                    ("go", "github.com/spf13/pflag", "v1.0.9"),
                    ("go", "go.yaml.in/yaml/v3", "v3.0.4"),
                },
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for project, fixture in fixtures.items():
                project_root = root / project
                for relative_path, content in fixture["files"].items():
                    self.write(project_root / relative_path, content)

                with self.subTest(project=project):
                    dependencies = discover_dependencies(project_root)
                    found = {
                        (dependency.ecosystem, dependency.name, dependency.version)
                        for dependency in dependencies
                    }

                    self.assertLess(len(dependencies), 30)
                    self.assertEqual(fixture["expected_count"], len(dependencies))
                    self.assertTrue(fixture["expected"].issubset(found))

    def test_ignores_generated_directories_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_json(root / "package.json", {"dependencies": {"left-pad": "1.3.0"}})
            self.write_json(root / "node_modules" / "vendored" / "package.json", {
                "dependencies": {"should-not-see": "1.0.0"}
            })

            dependencies = discover_dependencies(root)
            names = {dependency.name for dependency in dependencies}

            self.assertIn("left-pad", names)
            self.assertNotIn("should-not-see", names)

    def test_lockfile_refines_primary_manifest_without_duplicate_lock_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_json(root / "package.json", {"dependencies": {"react": "^18.2.0"}})
            self.write_json(root / "package-lock.json", {
                "packages": {
                    "node_modules/react": {"version": "18.2.0"},
                }
            })

            dependencies = discover_dependencies(root)

            self.assertEqual(1, len(dependencies))
            self.assertEqual("react", dependencies[0].name)
            self.assertEqual("18.2.0", dependencies[0].version)
            self.assertEqual("^18.2.0", dependencies[0].requirement)
            self.assertEqual("npm", dependencies[0].manager)

    def test_ancestor_lockfile_refines_nested_npm_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_json(root / "package.json", {"private": True})
            self.write_json(root / "package-lock.json", {
                "packages": {
                    "node_modules/@actions/core": {"version": "1.11.1"},
                }
            })
            self.write_json(root / ".github" / "actions" / "github-release" / "package.json", {
                "dependencies": {"@actions/core": "^1.6"},
            })

            dependencies = discover_dependencies(root)

            self.assertEqual(1, len(dependencies))
            self.assertEqual("@actions/core", dependencies[0].name)
            self.assertEqual("1.11.1", dependencies[0].version)
            self.assertEqual("^1.6", dependencies[0].requirement)

    def test_python_requirement_continuations_drop_trailing_backslash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "requirements.txt", """
                ruff==0.4.9 \\
            """)

            dependencies = discover_dependencies(root)
            found = {dependency.name: dependency for dependency in dependencies}

            self.assertEqual("==0.4.9", found["ruff"].version)

    def test_python_lock_overlay_preserves_requirement_and_sets_exact_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "pyproject.toml", """
                [project]
                dependencies = ["FastAPI>=0.111", "uvicorn>=0.12.0"]
            """)
            self.write(root / "uv.lock", """
                [[package]]
                name = "fastapi"
                version = "0.116.1"

                [[package]]
                name = "uvicorn"
                version = "0.35.0"
            """)

            dependencies = discover_dependencies(root)
            found = {dependency.name: dependency for dependency in dependencies}

            self.assertEqual("0.116.1", found["fastapi"].version)
            self.assertEqual(">=0.111", found["fastapi"].requirement)
            self.assertEqual("0.35.0", found["uvicorn"].version)
            self.assertEqual(">=0.12.0", found["uvicorn"].requirement)

    def test_gradle_property_placeholders_resolve_to_exact_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "gradle.properties", """
                javaFormatVersion=0.0.47
                assertjVersion=3.27.3
            """)
            self.write(root / "buildSrc" / "build.gradle", """
                ext.kotlinVersion = "2.2.0"
                extra["junitVersion"] = "5.13.4"
                dependencies {
                    classpath "io.spring.javaformat:spring-javaformat-gradle-plugin:${javaFormatVersion}"
                    implementation "org.assertj:assertj-core:${assertjVersion}"
                    implementation "org.jetbrains.kotlin:kotlin-gradle-plugin:${kotlinVersion}"
                    implementation "org.junit:junit-bom:${junitVersion}"
                }
            """)

            dependencies = discover_dependencies(root)
            found = {dependency.name: dependency for dependency in dependencies}

            self.assertEqual("0.0.47", found["io.spring.javaformat:spring-javaformat-gradle-plugin"].version)
            self.assertEqual("3.27.3", found["org.assertj:assertj-core"].version)
            self.assertEqual("2.2.0", found["org.jetbrains.kotlin:kotlin-gradle-plugin"].version)
            self.assertEqual("5.13.4", found["org.junit:junit-bom"].version)

    def test_standalone_lockfile_is_discovered_without_primary_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_json(root / "package-lock.json", {
                "packages": {
                    "node_modules/react": {"version": "18.2.0"},
                }
            })

            dependencies = discover_dependencies(root)

            self.assertEqual(1, len(dependencies))
            self.assertEqual("react", dependencies[0].name)
            self.assertEqual("18.2.0", dependencies[0].version)
            self.assertEqual("lock", dependencies[0].scope)
            self.assertEqual("package-lock.json", dependencies[0].manager)

    def test_malformed_manifest_does_not_abort_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "pom.xml", "<project><dependencies>")
            self.write(root / "requirements.txt", "tzlocal==3.0")

            dependencies = discover_dependencies(root)

            self.assertEqual(["tzlocal"], [dependency.name for dependency in dependencies])

    def test_strict_malformed_manifest_aborts_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root / "package.json", "{not-json")

            with self.assertRaises(Exception):
                discover_dependencies(root, strict=True)

    def test_parser_exceptions_are_contained_per_manifest(self) -> None:
        progress_messages = []

        def broken_parser(_path: Path, _root: Path):
            raise RuntimeError("boom")

        parsed = parse_manifest(
            broken_parser,
            Path("/workspace/package.json"),
            Path("/workspace"),
            progress_messages.append,
        )

        self.assertEqual([], parsed)
        self.assertIn("skipped manifest package.json error=RuntimeError", progress_messages)

    def test_read_errors_do_not_abort_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unreadable = root / "package.json"
            self.write_json(unreadable, {"dependencies": {"react": "18.2.0"}})
            self.write(root / "requirements.txt", "tzlocal==3.0")

            with patch("kasif.parsers.Path.read_text", side_effect=OSError("permission denied")):
                dependencies = discover_dependencies(root)

            self.assertEqual([], dependencies)

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")

    def write_json(self, path: Path, content: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
