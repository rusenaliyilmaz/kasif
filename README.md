# Kaşif

Kaşif is a standalone CLI for dependency discovery and package source lookup.
It scans a project directory for direct dependencies, then can resolve exact
package versions to verified source repositories, immutable commits, or source
archives when a registry exposes them.

Kaşif is deliberately focused on discovery and provenance. It does not clone
entire dependency graphs, generate documentation, index repositories, or manage
background jobs.

## Installation

For local development, install the package in editable mode:

```bash
python3 -m pip install -e .
```

You can also run the package directly from the repository:

```bash
python3 -m kasif --help
```

## Quick Start

Scan a project and print a table of direct dependencies:

```bash
kasif scan /path/to/project
```

Return JSON instead:

```bash
kasif scan /path/to/project --format json
```

Scan dependencies and resolve source metadata for dependencies with exact
versions:

```bash
kasif scan /path/to/project --resolve-sources --format json
```

Resolve one package version:

```bash
kasif find-source --ecosystem pypi --package tzlocal --version 3.0
```

If the console script is not installed, use the module form:

```bash
python3 -m kasif scan /path/to/project --format json

```

## Commands

### `kasif scan`

Discovers direct dependencies from supported project manifests and lockfiles.

Useful options:

- `--format table|json`: choose human-readable table output or structured JSON.
- `--ecosystem NAME`: only include dependencies from an ecosystem.
- `--exclude-ecosystem NAME`: exclude dependencies from an ecosystem.
- `--manifest PATH`: parse only a selected manifest. May be repeated.
- `--include-path GLOB`: only include matching manifest paths.
- `--exclude-path GLOB`: exclude matching manifest paths.
- `--strict`: fail on malformed JSON manifests and Maven POMs.
- `--resolve-sources`: enrich exact-version dependencies with source metadata.
- `--max-source-resolutions N`: cap registry and Git lookups during a scan.
- `--shallow-check`: resolve Git refs without cloning or verifying manifests.
- `--http-timeout-seconds N`: set the timeout for HTTP registry/archive requests.
- `--retries N`: retry transient HTTP registry/archive failures.
- `--cache-dir PATH`: choose the Kaşif cache root for default checkouts and archives.
- `--offline` or `--cache-only`: disable network source lookups.
- `--maven-repository URL`: query a custom Maven repository. May be repeated.
- `--output FILE`: write stdout output to a file.
- `--fail-on-unresolved`: return non-zero when source resolution leaves any
  dependency unresolved.
- `--progress`: print progress diagnostics to stderr.
- `--no-default-ignores`: include normally skipped directories such as
  `node_modules`, `target`, `.git`, and virtual environments.

### `kasif find-source`

Resolves source metadata for a single package coordinate.

```bash
kasif find-source \
  --ecosystem npm \
  --package openai \
  --version 5.0.0 \
  --format json
```

Supported ecosystems:

- `maven`
- `npm`
- `pypi`
- `pub`
- `go`
- `git`

`scan` also discovers `vcpkg`, `conan`, and `cmake` manifests. These are
discovery-only ecosystems today; when `--resolve-sources` is enabled, their
source result is `SKIPPED` with `errorCode` set to `UNSUPPORTED_ECOSYSTEM`.

Coordinate shape:

```text
<ecosystem>:<package>@<version>
```

Examples:

```text
npm:openai@5.0.0
pypi:tzlocal@3.0
pub:openai_dart@5.0.0
maven:org.springframework:spring-web@5.0.20.RELEASE
go:github.com/gin-gonic/gin@v1.10.0
git:https://github.com/example/project.git@0123456789abcdef0123456789abcdef01234567
```

## Dependency Scanning

Kaşif scans common package manifests and lockfiles, including:

- Java/JVM: `pom.xml`, `build.gradle`, `build.gradle.kts`,
  `gradle.lockfile`, `libs.versions.toml`
- JavaScript/TypeScript: `package.json`, `package-lock.json`,
  `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`
- Python/PyPI: `requirements*.txt`, `pyproject.toml`, `setup.cfg`,
  `setup.py`, `Pipfile`, `Pipfile.lock`, `poetry.lock`
- Dart/pub.dev: `pubspec.yaml`, `pubspec.lock`
- Go modules: `go.mod`
- C/C++: `vcpkg.json`, `conanfile.txt`, `conanfile.py`,
  `CMakeLists.txt`

The default output contains direct project dependencies. When a lockfile
contains an exact version for a direct dependency, Kaşif reports that exact
version and keeps the original requested constraint in the `requirement` field.
If a lockfile appears without its primary manifest, Kaşif reports locked
packages with `scope` set to `lock`.

## Source Lookup

Source lookup uses registry metadata and Git refs to connect a package version
to source code. Successful responses can include:

- package coordinate
- repository URL
- resolved immutable commit
- matched tag, branch, or ref
- repository subdirectory, when the package lives in a monorepo
- manifest path
- registry archive URL and SHA-256, when the ecosystem exposes one
- local source archive path, when archive fallback is used
- `kasifGitArguments` for source consumers that require Git coordinates
- `kasifArguments` for source consumers that can accept Git coordinates or a
  verified source archive

JSON responses also include deprecated `ocakGitArguments` and `ocakArguments`
aliases for compatibility. New integrations should read the `kasif*` fields.

For Maven coordinates, Kaşif checks Maven Central and Google's Android Maven
repository. Some artifacts publish valid POMs without source-control metadata
or versioned Git refs; in those cases Kaşif reports missing source metadata
instead of guessing.

## Shallow Checks

Use `--shallow-check` when you only need to resolve a package version to an
immutable commit and want to avoid cloning a large repository:

```bash
kasif find-source \
  --ecosystem maven \
  --package org.springframework:spring-web \
  --version 5.0.20.RELEASE \
  --shallow-check \
  --format json
```

Shallow checks still read registry metadata and run `git ls-remote` to verify
the version ref. They skip checkout and package manifest verification, so
`manifestPath` may be `null` and `repositorySubdirectory` is returned only when
registry metadata already provides it.

## Cache And Checkouts

Kaşif creates temporary source checkouts while verifying package manifests. To
choose an explicit checkout location, pass:

```bash
kasif find-source \
  --ecosystem pypi \
  --package tzlocal \
  --version 3.0 \
  --kasif-checkout-dir /tmp/kasif-checkouts
```

The same option is available on `kasif scan --resolve-sources`.

To choose the cache root for default checkouts and source archives, pass
`--cache-dir` or set `KASIF_CACHE_DIR`. `DEFTER_CACHE_DIR` and
`DOCTEXT_CACHE_DIR` remain deprecated compatibility aliases.

## Profiling

Use `--enable-profiler` to print timing and traffic diagnostics to stderr
without changing stdout:

```bash
kasif find-source \
  --ecosystem maven \
  --package org.springframework:spring-web \
  --version 5.0.20.RELEASE \
  --enable-profiler \
  --format json \
  2> profiler.json
```

The profiler reports:

- total command duration
- per-stage timings for registry lookup, ref resolution, checkout, and
  manifest verification
- per-Git-command timings
- HTTP metadata bytes received
- Git stdout/stderr bytes
- estimated Git transfer size based on the local `.git` object store
- checked-out worktree size, excluding `.git`

Git network traffic is an estimate because Git does not expose exact packfile
download bytes through `subprocess.run`; the local object store size is usually
the most useful close proxy.

## Development

Run the test suite:

```bash
python3 -m unittest discover -s tests
```

## AI Usage Disclosure
This tool was created partly by the use of AI. As any software does, it may contain bugs, errors, etc. 