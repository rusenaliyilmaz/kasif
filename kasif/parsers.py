from __future__ import annotations

import configparser
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .model import Dependency, source_path


VERSION_OPERATORS = ("===", "==", "~=", "!=", "<=", ">=", "<", ">", "=")
GRADLE_COORDINATE = re.compile(
    r"""(?:^|\s)(?:api|implementation|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|annotationProcessor|kapt|classpath|compile)\s*(?:\(|\s)\s*['"]([^:'"]+):([^:'"]+):([^'"]+)['"]"""
)
GRADLE_MAP_COORDINATE = re.compile(
    r"""(?:^|\s)(?P<scope>api|implementation|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|annotationProcessor|kapt|classpath|compile)\s*(?:\(|\s)\s*group\s*:\s*['"](?P<group>[^'"]+)['"]\s*,\s*name\s*:\s*['"](?P<name>[^'"]+)['"]\s*,\s*version\s*:\s*['"](?P<version>[^'"]+)['"]"""
)
GRADLE_PLATFORM_COORDINATE = re.compile(
    r"""(?:^|\s)(?P<scope>api|implementation|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|annotationProcessor|kapt|classpath|compile)\s*(?:\(|\s)\s*(?:platform|enforcedPlatform)\(\s*['"](?P<group>[^:'"]+):(?P<name>[^:'"]+):(?P<version>[^'"]+)['"]"""
)
MAVEN_COORDINATE_TEXT = re.compile(r"^(?P<group>[^:\s]+):(?P<artifact>[^:\s]+):(?P<version>[^=\s]+)")


def parse_maven_pom(path: Path, root: Path) -> List[Dependency]:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return []
    project = tree.getroot()
    properties = maven_properties(project)
    managed_versions = maven_dependency_management(project, properties)
    dependencies: List[Dependency] = []
    dependencies_node = find_child(project, "dependencies")
    if dependencies_node is None:
        return dependencies
    for dependency in find_children(dependencies_node, "dependency"):
        group_id = child_text(dependency, "groupId")
        artifact_id = child_text(dependency, "artifactId")
        if not group_id or not artifact_id:
            continue
        version = child_text(dependency, "version")
        if version is None:
            version = managed_versions.get((group_id, artifact_id))
        version = resolve_maven_property(version, properties)
        scope = child_text(dependency, "scope") or "compile"
        dependencies.append(Dependency(
            ecosystem="maven",
            name=f"{group_id}:{artifact_id}",
            version=version,
            requirement=version,
            scope=scope,
            manager="maven",
            source_file=source_path(path, root),
        ))
    return dependencies


def maven_properties(project: ET.Element) -> Dict[str, str]:
    properties: Dict[str, str] = {}
    group_id = child_text(project, "groupId") or child_text(find_child(project, "parent"), "groupId")
    artifact_id = child_text(project, "artifactId")
    version = child_text(project, "version") or child_text(find_child(project, "parent"), "version")
    if group_id:
        properties["project.groupId"] = group_id
        properties["pom.groupId"] = group_id
    if artifact_id:
        properties["project.artifactId"] = artifact_id
        properties["pom.artifactId"] = artifact_id
    if version:
        properties["project.version"] = version
        properties["pom.version"] = version

    properties_node = find_child(project, "properties")
    if properties_node is not None:
        for child in list(properties_node):
            name = local_name(child.tag)
            if child.text and child.text.strip():
                properties[name] = child.text.strip()
    return properties


def maven_dependency_management(project: ET.Element, properties: Dict[str, str]) -> Dict[Tuple[str, str], str]:
    managed: Dict[Tuple[str, str], str] = {}
    dependency_management = find_child(project, "dependencyManagement")
    dependencies_node = find_child(dependency_management, "dependencies")
    if dependencies_node is None:
        return managed
    for dependency in find_children(dependencies_node, "dependency"):
        group_id = child_text(dependency, "groupId")
        artifact_id = child_text(dependency, "artifactId")
        version = resolve_maven_property(child_text(dependency, "version"), properties)
        if group_id and artifact_id and version:
            managed[(group_id, artifact_id)] = version
    return managed


def resolve_maven_property(value: Optional[str], properties: Dict[str, str]) -> Optional[str]:
    if value is None:
        return None
    result = value.strip()
    for _ in range(5):
        match = re.fullmatch(r"\$\{([^}]+)}", result)
        if match is None:
            break
        replacement = properties.get(match.group(1))
        if replacement is None:
            break
        result = replacement
    return result


def parse_gradle(path: Path, root: Path) -> List[Dependency]:
    dependencies: List[Dependency] = []
    text = read_text(path)
    properties = gradle_properties(path, root, text)
    for line in text.splitlines():
        stripped = strip_inline_comment(line).strip()
        if not stripped:
            continue
        platform_match = GRADLE_PLATFORM_COORDINATE.search(stripped)
        if platform_match is not None:
            version = resolve_gradle_property(platform_match.group("version"), properties)
            dependencies.append(Dependency(
                ecosystem="maven",
                name=f"{platform_match.group('group')}:{platform_match.group('name')}",
                version=version,
                requirement=platform_match.group("version"),
                scope=platform_match.group("scope"),
                manager="gradle",
                source_file=source_path(path, root),
            ))
            continue
        map_match = GRADLE_MAP_COORDINATE.search(stripped)
        if map_match is not None:
            version = resolve_gradle_property(map_match.group("version"), properties)
            dependencies.append(Dependency(
                ecosystem="maven",
                name=f"{map_match.group('group')}:{map_match.group('name')}",
                version=version,
                requirement=map_match.group("version"),
                scope=map_match.group("scope"),
                manager="gradle",
                source_file=source_path(path, root),
            ))
            continue
        coordinate_match = GRADLE_COORDINATE.search(stripped)
        if coordinate_match is not None:
            scope = stripped.split("(", 1)[0].strip().split()[0]
            version = resolve_gradle_property(coordinate_match.group(3), properties)
            dependencies.append(Dependency(
                ecosystem="maven",
                name=f"{coordinate_match.group(1)}:{coordinate_match.group(2)}",
                version=version,
                requirement=coordinate_match.group(3),
                scope=scope,
                manager="gradle",
                source_file=source_path(path, root),
            ))
    return dependencies


def parse_gradle_lockfile(path: Path, root: Path) -> List[Dependency]:
    dependencies: List[Dependency] = []
    for raw in read_lines(path):
        line = strip_inline_comment(raw).strip()
        if not line or line.startswith("#") or line.startswith("empty="):
            continue
        coordinate = line.split("=", 1)[0].strip()
        match = MAVEN_COORDINATE_TEXT.match(coordinate)
        if match is None:
            continue
        dependencies.append(Dependency(
            ecosystem="maven",
            name=f"{match.group('group')}:{match.group('artifact')}",
            version=match.group("version"),
            requirement=match.group("version"),
            scope="lock",
            manager="gradle-lockfile",
            source_file=source_path(path, root),
        ))
    return dependencies


def parse_gradle_version_catalog(path: Path, root: Path) -> List[Dependency]:
    lines = read_lines(path)
    versions: Dict[str, str] = {}
    dependencies: List[Dependency] = []
    current_section = ""
    for raw in lines:
        line = strip_inline_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line.strip("[]").strip()
            continue
        if current_section == "versions" and "=" in line:
            key, value = line.split("=", 1)
            versions[key.strip().strip("'\"")] = value.strip().strip(",").strip("'\"")
            continue
        if current_section != "libraries" or "=" not in line:
            continue
        alias, value = line.split("=", 1)
        module: Optional[str] = None
        requirement: Optional[str] = None
        if value.strip().startswith("{"):
            fields = parse_inline_table(value)
            module = string_value(fields.get("module"))
            if module is None:
                group = string_value(fields.get("group"))
                name = string_value(fields.get("name"))
                if group and name:
                    module = f"{group}:{name}"
            requirement = string_value(fields.get("version"))
            version_ref = string_value(fields.get("version.ref"))
            if requirement is None and version_ref:
                requirement = versions.get(version_ref)
        else:
            module = value.strip().strip("'\"")
        if module and ":" in module:
            dependencies.append(Dependency(
                ecosystem="maven",
                name=module,
                version=requirement,
                requirement=requirement,
                scope=alias.strip().strip("'\""),
                manager="gradle-version-catalog",
                source_file=source_path(path, root),
            ))
    return dependencies


def parse_npm_package(path: Path, root: Path) -> List[Dependency]:
    data = read_json(path)
    if not isinstance(data, dict):
        return []
    lock_versions = nearest_npm_lock_versions(path, root)
    dependencies: List[Dependency] = []
    for scope in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = data.get(scope)
        if not isinstance(section, dict):
            continue
        for name, requirement in sorted(section.items()):
            if not isinstance(requirement, str):
                continue
            version = lock_versions.get(name, requirement)
            dependencies.append(Dependency(
                ecosystem="npm",
                name=name,
                version=version,
                requirement=requirement,
                scope=scope,
                manager="npm",
                source_file=source_path(path, root),
            ))
    return dependencies


def parse_npm_lock(path: Path, root: Path) -> List[Dependency]:
    dependencies: List[Dependency] = []
    for name, version in sorted(npm_lock_versions(path).items()):
        dependencies.append(Dependency(
            ecosystem="npm",
            name=name,
            version=version,
            requirement=version,
            scope="lock",
            manager=path.name,
            source_file=source_path(path, root),
        ))
    return dependencies


def parse_pnpm_lock(path: Path, root: Path) -> List[Dependency]:
    dependencies: List[Dependency] = []
    for name, version in sorted(pnpm_lock_direct_versions(path).items()):
        dependencies.append(Dependency(
            ecosystem="npm",
            name=name,
            version=version,
            requirement=version,
            scope="lock",
            manager="pnpm",
            source_file=source_path(path, root),
        ))
    return dependencies


def parse_yarn_lock(path: Path, root: Path) -> List[Dependency]:
    dependencies: List[Dependency] = []
    for name, version in sorted(yarn_lock_versions(path).items()):
        dependencies.append(Dependency(
            ecosystem="npm",
            name=name,
            version=version,
            requirement=version,
            scope="lock",
            manager="yarn",
            source_file=source_path(path, root),
        ))
    return dependencies


def parse_bun_lock(path: Path, root: Path) -> List[Dependency]:
    dependencies: List[Dependency] = []
    for name, version in sorted(bun_lock_versions(path).items()):
        dependencies.append(Dependency(
            ecosystem="npm",
            name=name,
            version=version,
            requirement=version,
            scope="lock",
            manager="bun",
            source_file=source_path(path, root),
        ))
    return dependencies


def npm_lock_versions(path: Path) -> Dict[str, str]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {}
    versions: Dict[str, str] = {}
    packages = data.get("packages")
    if isinstance(packages, dict):
        for package_path, package_data in packages.items():
            if not isinstance(package_path, str) or not package_path.startswith("node_modules/"):
                continue
            if not isinstance(package_data, dict) or not isinstance(package_data.get("version"), str):
                continue
            name = package_path.removeprefix("node_modules/")
            versions[name] = package_data["version"]
    legacy = data.get("dependencies")
    if isinstance(legacy, dict):
        for name, package_data in legacy.items():
            if isinstance(package_data, dict) and isinstance(package_data.get("version"), str):
                versions.setdefault(name, package_data["version"])
    return versions


def pnpm_lock_direct_versions(path: Path) -> Dict[str, str]:
    lines = read_lines(path)
    versions: Dict[str, str] = {}
    in_direct_section = False
    current_name: Optional[str] = None
    for raw in lines:
        line = strip_yaml_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = indentation(line)
        text = line.strip()
        if indent == 0:
            in_direct_section = text in ("dependencies:", "devDependencies:", "optionalDependencies:")
            current_name = None
            continue
        if not in_direct_section:
            continue
        if indent == 2 and ":" in text:
            name, value = text.split(":", 1)
            current_name = name.strip().strip("'\"")
            value = value.strip().strip("'\"")
            if value and not value.startswith("{"):
                versions[current_name] = normalize_pnpm_version(value)
                current_name = None
            continue
        if indent >= 4 and current_name and text.startswith("version:"):
            versions[current_name] = normalize_pnpm_version(text.split(":", 1)[1].strip().strip("'\""))
            current_name = None
    return versions


def yarn_lock_versions(path: Path) -> Dict[str, str]:
    versions: Dict[str, str] = {}
    current_names: List[str] = []
    for raw in read_lines(path):
        line = raw.rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and line.endswith(":"):
            header = line[:-1].strip().strip("'\"")
            current_names = yarn_header_names(header)
            continue
        stripped = line.strip()
        if current_names and stripped.startswith("version "):
            version = stripped.split(" ", 1)[1].strip().strip("'\"")
            for name in current_names:
                versions.setdefault(name, version)
            current_names = []
    return versions


def bun_lock_versions(path: Path) -> Dict[str, str]:
    data = read_json(path)
    versions: Dict[str, str] = {}
    if not isinstance(data, dict):
        return versions
    packages = data.get("packages")
    if isinstance(packages, dict):
        for name, value in packages.items():
            version = None
            if isinstance(value, str):
                version = value
            elif isinstance(value, list) and value:
                version = value[0] if isinstance(value[0], str) else None
            elif isinstance(value, dict):
                version = string_value(value.get("version"))
            if isinstance(name, str) and version:
                versions[name] = normalize_pnpm_version(version)
    return versions


def nearest_npm_lock_versions(path: Path, root: Path) -> Dict[str, str]:
    versions: Dict[str, str] = {}
    for directory in ancestor_directories(path.parent, root):
        versions.update(npm_lock_versions(directory / "package-lock.json"))
        versions.update(pnpm_lock_direct_versions(directory / "pnpm-lock.yaml"))
        versions.update(yarn_lock_versions(directory / "yarn.lock"))
        versions.update(bun_lock_versions(directory / "bun.lock"))
        if versions:
            return versions
    return versions


def normalize_pnpm_version(value: str) -> str:
    return value.split("(", 1)[0].strip()


def yarn_header_names(header: str) -> List[str]:
    names: List[str] = []
    for descriptor in header.split(","):
        descriptor = descriptor.strip().strip("'\"")
        if descriptor.startswith("@"):
            parts = descriptor.split("@")
            if len(parts) >= 3:
                names.append("@" + parts[1])
            continue
        if "@" in descriptor:
            names.append(descriptor.split("@", 1)[0])
    return names


def parse_python_requirements(path: Path, root: Path) -> List[Dependency]:
    dependencies: List[Dependency] = []
    lock_versions = nearest_python_lock_versions(path, root)
    for requirement in iter_requirement_lines(path):
        parsed = parse_python_requirement(requirement)
        if parsed is None:
            continue
        name, version = parsed
        dependencies.append(python_dependency(name, version, "require", "requirements.txt", path, root, lock_versions))
    return dependencies


def parse_python_pyproject(path: Path, root: Path) -> List[Dependency]:
    lines = read_lines(path)
    dependencies: List[Dependency] = []
    lock_versions = nearest_python_lock_versions(path, root)
    current_section = ""
    index = 0
    while index < len(lines):
        line = strip_inline_comment(lines[index]).strip()
        if not line:
            index += 1
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line.strip("[]").strip()
            index += 1
            continue
        if current_section == "project" and line.startswith("dependencies"):
            values, index = parse_toml_string_array(lines, index)
            dependencies.extend(python_dependencies(values, "dependencies", "pyproject.toml", path, root, lock_versions))
            continue
        if current_section.startswith("project.optional-dependencies") and "=" in line:
            group = line.split("=", 1)[0].strip()
            values, index = parse_toml_string_array(lines, index)
            dependencies.extend(python_dependencies(values, f"optional:{group}", "pyproject.toml", path, root, lock_versions))
            continue
        if current_section == "tool.poetry.dependencies" and "=" in line:
            name, version = parse_poetry_dependency(line)
            if name and name.lower() != "python":
                dependencies.append(python_dependency(name, version, "dependencies", "poetry", path, root, lock_versions))
            index += 1
            continue
        if current_section == "tool.poetry.dev-dependencies" and "=" in line:
            name, version = parse_poetry_dependency(line)
            if name:
                dependencies.append(python_dependency(name, version, "dev-dependencies", "poetry", path, root, lock_versions))
            index += 1
            continue
        if current_section.startswith("tool.poetry.group.") and current_section.endswith(".dependencies") and "=" in line:
            name, version = parse_poetry_dependency(line)
            if name:
                group = current_section.removeprefix("tool.poetry.group.").removesuffix(".dependencies")
                dependencies.append(python_dependency(name, version, f"group:{group}", "poetry", path, root, lock_versions))
            index += 1
            continue
        index += 1
    return dependencies


def parse_python_setup_cfg(path: Path, root: Path) -> List[Dependency]:
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except (configparser.Error, OSError):
        return []
    dependencies: List[Dependency] = []
    lock_versions = nearest_python_lock_versions(path, root)
    if parser.has_option("options", "install_requires"):
        values = split_multiline_requirements(parser.get("options", "install_requires"))
        dependencies.extend(python_dependencies(values, "install_requires", "setup.cfg", path, root, lock_versions))
    if parser.has_section("options.extras_require"):
        for extra, value in parser.items("options.extras_require"):
            values = split_multiline_requirements(value)
            dependencies.extend(python_dependencies(values, f"extra:{extra}", "setup.cfg", path, root, lock_versions))
    return dependencies


def parse_python_setup_py(path: Path, root: Path) -> List[Dependency]:
    text = "\n".join(read_lines(path))
    dependencies: List[Dependency] = []
    lock_versions = nearest_python_lock_versions(path, root)
    for field, scope in (("install_requires", "install_requires"), ("setup_requires", "setup_requires"), ("tests_require", "tests_require")):
        values = parse_python_list_argument(text, field)
        dependencies.extend(python_dependencies(values, scope, "setup.py", path, root, lock_versions))
    extras_match = re.search(r"extras_require\s*=\s*\{(?P<body>.*?)\}", text, re.DOTALL)
    if extras_match is not None:
        for extra_match in re.finditer(r"""['"](?P<extra>[^'"]+)['"]\s*:\s*\[(?P<body>.*?)\]""", extras_match.group("body"), re.DOTALL):
            values = re.findall(r"""['"]([^'"]+)['"]""", extra_match.group("body"))
            dependencies.extend(python_dependencies(values, f"extra:{extra_match.group('extra')}", "setup.py", path, root, lock_versions))
    return dependencies


def parse_pipfile(path: Path, root: Path) -> List[Dependency]:
    lines = read_lines(path)
    dependencies: List[Dependency] = []
    lock_versions = nearest_python_lock_versions(path, root)
    current_section = ""
    for raw in lines:
        line = strip_inline_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line.strip("[]").strip()
            continue
        if current_section not in ("packages", "dev-packages") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        version = parse_pipfile_value(value.strip())
        dependencies.append(python_dependency(name.strip().strip("'\""), version, current_section, "pipenv", path, root, lock_versions))
    return dependencies


def parse_pipfile_lock(path: Path, root: Path) -> List[Dependency]:
    data = read_json(path)
    if not isinstance(data, dict):
        return []
    dependencies: List[Dependency] = []
    for section, scope in (("default", "default"), ("develop", "develop")):
        packages = data.get(section)
        if not isinstance(packages, dict):
            continue
        for name, value in packages.items():
            version = None
            if isinstance(value, dict):
                version = string_value(value.get("version"))
            elif isinstance(value, str):
                version = value
            dependencies.append(python_dependency(str(name), version, scope, "pipenv-lock", path, root))
    return dependencies


def parse_poetry_lock(path: Path, root: Path) -> List[Dependency]:
    dependencies: List[Dependency] = []
    current_name: Optional[str] = None
    current_version: Optional[str] = None
    current_scope = "lock"
    for raw in read_lines(path):
        line = strip_inline_comment(raw).strip()
        if line == "[[package]]":
            if current_name:
                dependencies.append(python_dependency(current_name, current_version, current_scope, "poetry-lock", path, root))
            current_name = None
            current_version = None
            current_scope = "lock"
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key == "name":
            current_name = value
        elif key == "version":
            current_version = value
        elif key == "category":
            current_scope = value
        elif key == "groups":
            groups = re.findall(r"""['"]([^'"]+)['"]""", value)
            if groups:
                current_scope = ",".join(groups)
    if current_name:
        dependencies.append(python_dependency(current_name, current_version, current_scope, "poetry-lock", path, root))
    return dependencies


def parse_pubspec(path: Path, root: Path) -> List[Dependency]:
    dependencies = pubspec_dependencies(path, root)
    lock_versions = pubspec_lock_versions(path.with_name("pubspec.lock"))
    result: List[Dependency] = []
    for dependency in dependencies:
        version = lock_versions.get(dependency.name, dependency.version)
        result.append(Dependency(
            ecosystem=dependency.ecosystem,
            name=dependency.name,
            version=version,
            requirement=dependency.requirement,
            scope=dependency.scope,
            manager=dependency.manager,
            source_file=dependency.source_file,
        ))
    return result


def pubspec_dependencies(path: Path, root: Path) -> List[Dependency]:
    lines = read_lines(path)
    dependencies: List[Dependency] = []
    current_scope: Optional[str] = None
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = strip_yaml_comment(raw).rstrip()
        if not stripped.strip():
            index += 1
            continue
        indent = indentation(raw)
        text = stripped.strip()
        if indent == 0 and text in ("dependencies:", "dev_dependencies:", "dependency_overrides:"):
            current_scope = text[:-1]
            index += 1
            continue
        if current_scope and indent == 2 and ":" in text:
            name, value = text.split(":", 1)
            name = name.strip()
            value = value.strip().strip("'\"") or None
            if value is None:
                value = nested_yaml_value(lines, index, "version") or nested_yaml_value(lines, index, "sdk")
                if value is None and nested_yaml_value(lines, index, "git") is not None:
                    value = "git"
                if value is None and nested_yaml_value(lines, index, "path") is not None:
                    value = "path"
            dependencies.append(Dependency(
                ecosystem="pub",
                name=name,
                version=value,
                requirement=value,
                scope=current_scope,
                manager="pub",
                source_file=source_path(path, root),
            ))
        if indent == 0 and not text.endswith(":"):
            current_scope = None
        index += 1
    return dependencies


def pubspec_lock_versions(path: Path) -> Dict[str, str]:
    lines = read_lines(path)
    versions: Dict[str, str] = {}
    in_packages = False
    current_package: Optional[str] = None
    current_dependency: Optional[str] = None
    current_version: Optional[str] = None
    for raw in lines:
        stripped = strip_yaml_comment(raw).rstrip()
        if not stripped.strip():
            continue
        indent = indentation(raw)
        text = stripped.strip()
        if indent == 0:
            in_packages = text == "packages:"
            current_package = None
            continue
        if not in_packages:
            continue
        if indent == 2 and text.endswith(":"):
            if current_package and current_version and current_dependency and current_dependency.startswith("direct"):
                versions[current_package] = current_version
            current_package = text[:-1]
            current_dependency = None
            current_version = None
            continue
        if indent == 4 and current_package and ":" in text:
            key, value = text.split(":", 1)
            value = value.strip().strip("'\"")
            if key == "dependency":
                current_dependency = value
            elif key == "version":
                current_version = value
    if current_package and current_version and current_dependency and current_dependency.startswith("direct"):
        versions[current_package] = current_version
    return versions


def parse_go_mod(path: Path, root: Path) -> List[Dependency]:
    dependencies: List[Dependency] = []
    in_require_block = False
    for raw in read_lines(path):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line == "require (":
            in_require_block = True
            continue
        if in_require_block and line == ")":
            in_require_block = False
            continue
        if line.startswith("require "):
            requirement = line.removeprefix("require ").strip()
        elif in_require_block:
            requirement = line
        else:
            continue
        requirement = requirement.split("//", 1)[0].strip()
        parts = requirement.split()
        if len(parts) < 2:
            continue
        dependencies.append(Dependency(
            ecosystem="go",
            name=parts[0],
            version=parts[1],
            requirement=parts[1],
            scope="require",
            manager="go",
            source_file=source_path(path, root),
        ))
    return dependencies


def parse_vcpkg_json(path: Path, root: Path) -> List[Dependency]:
    data = read_json(path)
    if not isinstance(data, dict):
        return []
    dependencies: List[Dependency] = []
    for value in list_values(data.get("dependencies")):
        name: Optional[str] = None
        version: Optional[str] = None
        features: List[str] = []
        if isinstance(value, str):
            name = value
        elif isinstance(value, dict):
            name = string_value(value.get("name"))
            version = (
                string_value(value.get("version>="))
                or string_value(value.get("version"))
            )
            raw_features = value.get("features")
            if isinstance(raw_features, list):
                features = [str(feature) for feature in raw_features]
        if not name:
            continue
        dependencies.append(Dependency(
            ecosystem="vcpkg",
            name=name,
            version=version,
            requirement=version,
            scope="dependencies" + (":" + ",".join(features) if features else ""),
            manager="vcpkg",
            source_file=source_path(path, root),
        ))
    return dependencies


def parse_conanfile_txt(path: Path, root: Path) -> List[Dependency]:
    dependencies: List[Dependency] = []
    current_section = ""
    for raw in read_lines(path):
        line = strip_inline_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line.strip("[]").strip()
            continue
        if current_section != "requires":
            continue
        parsed = parse_conan_reference(line)
        if parsed is None:
            continue
        name, version = parsed
        dependencies.append(Dependency(
            ecosystem="conan",
            name=name,
            version=version,
            requirement=version,
            scope="requires",
            manager="conan",
            source_file=source_path(path, root),
        ))
    return dependencies


def parse_conanfile_py(path: Path, root: Path) -> List[Dependency]:
    text = "\n".join(read_lines(path))
    dependencies: List[Dependency] = []
    references: List[str] = []
    for match in re.finditer(r"""(?:requires|tool_requires|test_requires)\s*=\s*(?P<value>.+)""", text):
        references.extend(re.findall(r"""['"]([^'"]+/[^'"]+)['"]""", match.group("value")))
    for match in re.finditer(r"""self\.(?:requires|tool_requires|test_requires)\(\s*['"]([^'"]+/[^'"]+)['"]""", text):
        references.append(match.group(1))
    for reference in references:
        parsed = parse_conan_reference(reference)
        if parsed is None:
            continue
        name, version = parsed
        dependencies.append(Dependency(
            ecosystem="conan",
            name=name,
            version=version,
            requirement=version,
            scope="requires",
            manager="conan",
            source_file=source_path(path, root),
        ))
    return dependencies


def parse_cmake_lists(path: Path, root: Path) -> List[Dependency]:
    text = "\n".join(read_lines(path))
    dependencies: List[Dependency] = []
    for match in re.finditer(r"find_package\s*\(\s*([A-Za-z0-9_.+-]+)(?P<body>[^)]*)\)", text, re.IGNORECASE):
        name = match.group(1)
        body = match.group("body")
        version_match = re.search(r"\b(\d+(?:\.\d+)*(?:[-+][A-Za-z0-9_.-]+)?)\b", body)
        version = version_match.group(1) if version_match else None
        dependencies.append(Dependency(
            ecosystem="cmake",
            name=name,
            version=version,
            requirement=version,
            scope="find_package",
            manager="cmake",
            source_file=source_path(path, root),
        ))
    for match in re.finditer(r"FetchContent_Declare\s*\(\s*([A-Za-z0-9_.+-]+)(?P<body>.*?)\)", text, re.IGNORECASE | re.DOTALL):
        name = match.group(1)
        body = match.group("body")
        tag_match = re.search(r"GIT_TAG\s+([^\s)]+)", body, re.IGNORECASE)
        version = tag_match.group(1).strip("'\"") if tag_match else None
        dependencies.append(Dependency(
            ecosystem="cmake",
            name=name,
            version=version,
            requirement=version,
            scope="fetchcontent",
            manager="cmake",
            source_file=source_path(path, root),
        ))
    return dependencies


def read_json(path: Path) -> object:
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError:
        return None


def read_lines(path: Path) -> List[str]:
    return read_text(path).splitlines()


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(errors="ignore")
        except OSError:
            return ""
    except OSError:
        return ""


def find_child(element: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    if element is None:
        return None
    for child in list(element):
        if local_name(child.tag) == name:
            return child
    return None


def find_children(element: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in list(element):
        if local_name(child.tag) == name:
            yield child


def child_text(element: Optional[ET.Element], name: str) -> Optional[str]:
    child = find_child(element, name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def strip_inline_comment(line: str) -> str:
    in_quote: Optional[str] = None
    for index, character in enumerate(line):
        if character in ("'", '"'):
            if in_quote == character:
                in_quote = None
            elif in_quote is None:
                in_quote = character
        if character == "#" and in_quote is None:
            return line[:index]
    return line.split("//", 1)[0] if "//" in line and not line.lstrip().startswith("http") else line


def strip_yaml_comment(line: str) -> str:
    in_quote: Optional[str] = None
    for index, character in enumerate(line):
        if character in ("'", '"'):
            if in_quote == character:
                in_quote = None
            elif in_quote is None:
                in_quote = character
        if character == "#" and in_quote is None:
            return line[:index]
    return line


def indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def nested_yaml_value(lines: List[str], start_index: int, key: str) -> Optional[str]:
    for raw in lines[start_index + 1:]:
        indent = indentation(raw)
        text = strip_yaml_comment(raw).strip()
        if not text:
            continue
        if indent <= 2:
            return None
        if indent >= 4 and text.startswith(key + ":"):
            value = text.split(":", 1)[1].strip().strip("'\"")
            return value or key
    return None


def iter_requirement_lines(path: Path) -> Iterable[str]:
    pending = ""
    for raw in read_lines(path):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = strip_inline_comment(line).strip()
        if pending:
            line = (pending + line).strip()
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1].rstrip() + " "
            continue
        if not line or line.startswith(("-r ", "--requirement", "-c ", "--constraint", "--index-url", "--extra-index-url")):
            continue
        yield line
    if pending.strip():
        yield pending.strip()


def parse_python_requirement(requirement: str) -> Optional[Tuple[str, Optional[str]]]:
    requirement = requirement.split(";", 1)[0].strip()
    if not requirement or requirement.startswith(("-", "http://", "https://", "git+")):
        egg_match = re.search(r"#egg=([^&]+)", requirement)
        if egg_match is None:
            return None
        return egg_match.group(1), requirement
    requirement = re.sub(r"\[[^]]+]", "", requirement)
    for operator in VERSION_OPERATORS:
        if operator in requirement:
            name, version = requirement.split(operator, 1)
            return name.strip(), (operator + version.strip())
    name = re.split(r"\s+", requirement, 1)[0].strip()
    if not name:
        return None
    return name, None


def parse_python_list_argument(text: str, field: str) -> List[str]:
    match = re.search(rf"{re.escape(field)}\s*=\s*\[(?P<body>.*?)\]", text, re.DOTALL)
    if match is None:
        return []
    return re.findall(r"""['"]([^'"]+)['"]""", match.group("body"))


def parse_pipfile_value(value: str) -> Optional[str]:
    value = value.strip().strip(",")
    if value.startswith("{"):
        fields = parse_inline_table(value)
        version = fields.get("version")
        if isinstance(version, str):
            return version
        return None
    value = value.strip("'\"")
    return None if value == "*" else value


def parse_conan_reference(reference: str) -> Optional[Tuple[str, Optional[str]]]:
    name_version = reference.split("@", 1)[0].strip()
    if "/" not in name_version:
        return None
    name, version = name_version.split("/", 1)
    name = name.strip()
    version = version.strip() or None
    if not name:
        return None
    return name, version


def parse_inline_table(value: str) -> Dict[str, Any]:
    text = value.strip().strip("{}").strip()
    result: Dict[str, Any] = {}
    for match in re.finditer(r"""(?P<key>[A-Za-z0-9_.-]+)\s*=\s*(?P<value>'[^']*'|"[^"]*"|[^,}]+)""", text):
        raw = match.group("value").strip().strip(",")
        if raw.startswith(("'", '"')) and raw.endswith(("'", '"')):
            result[match.group("key")] = raw[1:-1]
        else:
            result[match.group("key")] = raw
    return result


def string_value(value: object) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def list_values(value: object) -> List[object]:
    return value if isinstance(value, list) else []


def normalize_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_toml_string_array(lines: List[str], index: int) -> Tuple[List[str], int]:
    line = strip_inline_comment(lines[index]).strip()
    if "[" not in line:
        return [], index + 1
    collected = line
    index += 1
    while not toml_array_is_closed(collected) and index < len(lines):
        collected += "\n" + strip_inline_comment(lines[index]).strip()
        index += 1
    values = parse_toml_quoted_strings(collected)
    return values, index


def toml_array_is_closed(value: str) -> bool:
    in_quote: Optional[str] = None
    escaped = False
    depth = 0
    for char in value:
        if in_quote:
            if escaped:
                escaped = False
            elif char == "\\" and in_quote == '"':
                escaped = True
            elif char == in_quote:
                in_quote = None
            continue
        if char in ("'", '"'):
            in_quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return True
    return False


def parse_toml_quoted_strings(value: str) -> List[str]:
    values: List[str] = []
    in_quote: Optional[str] = None
    escaped = False
    current: List[str] = []
    for char in value:
        if in_quote:
            if escaped:
                current.append(char)
                escaped = False
            elif char == "\\" and in_quote == '"':
                escaped = True
            elif char == in_quote:
                values.append("".join(current))
                current = []
                in_quote = None
            else:
                current.append(char)
            continue
        if char in ("'", '"'):
            in_quote = char
    return values


def parse_poetry_dependency(line: str) -> Tuple[Optional[str], Optional[str]]:
    if "=" not in line:
        return None, None
    name, value = line.split("=", 1)
    name = name.strip().strip("'\"")
    value = value.strip().strip(",")
    if value.startswith("{"):
        version_match = re.search(r"""version\s*=\s*['"]([^'"]+)['"]""", value)
        return name, version_match.group(1) if version_match else value
    return name, value.strip("'\"")


def python_dependencies(
    values: Iterable[str],
    scope: str,
    manager: str,
    path: Path,
    root: Path,
    lock_versions: Optional[Dict[str, str]] = None,
) -> List[Dependency]:
    dependencies: List[Dependency] = []
    for value in values:
        parsed = parse_python_requirement(value)
        if parsed is None:
            continue
        name, version = parsed
        dependencies.append(python_dependency(name, version, scope, manager, path, root, lock_versions))
    return dependencies


def python_dependency(
    name: str,
    version: Optional[str],
    scope: str,
    manager: str,
    path: Path,
    root: Path,
    lock_versions: Optional[Dict[str, str]] = None,
) -> Dependency:
    normalized_name = normalize_pypi_name(name)
    resolved_version = (lock_versions or {}).get(normalized_name, version)
    return Dependency(
        ecosystem="pypi",
        name=normalized_name,
        version=resolved_version,
        requirement=version,
        scope=scope,
        manager=manager,
        source_file=source_path(path, root),
    )


def split_multiline_requirements(value: str) -> List[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def nearest_python_lock_versions(path: Path, root: Path) -> Dict[str, str]:
    versions: Dict[str, str] = {}
    for directory in ancestor_directories(path.parent, root):
        versions.update(toml_package_versions(directory / "uv.lock"))
        versions.update(toml_package_versions(directory / "poetry.lock"))
        versions.update(toml_package_versions(directory / "pylock.toml"))
        versions.update(pipfile_lock_versions(directory / "Pipfile.lock"))
        if versions:
            return versions
    return versions


def toml_package_versions(path: Path) -> Dict[str, str]:
    versions: Dict[str, str] = {}
    current_name: Optional[str] = None
    current_version: Optional[str] = None
    for raw in read_lines(path):
        line = strip_inline_comment(raw).strip()
        if line in {"[[package]]", "[[packages]]"}:
            if current_name and current_version:
                versions[normalize_pypi_name(current_name)] = normalize_exact_python_lock_version(current_version)
            current_name = None
            current_version = None
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip(",").strip("'\"")
        if key == "name":
            current_name = value
        elif key == "version":
            current_version = value
    if current_name and current_version:
        versions[normalize_pypi_name(current_name)] = normalize_exact_python_lock_version(current_version)
    return versions


def pipfile_lock_versions(path: Path) -> Dict[str, str]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {}
    versions: Dict[str, str] = {}
    for section in ("default", "develop"):
        packages = data.get(section)
        if not isinstance(packages, dict):
            continue
        for name, value in packages.items():
            version = None
            if isinstance(value, dict):
                version = string_value(value.get("version"))
            elif isinstance(value, str):
                version = value
            if version:
                versions[normalize_pypi_name(str(name))] = normalize_exact_python_lock_version(version)
    return versions


def normalize_exact_python_lock_version(version: str) -> str:
    value = version.strip()
    if value.startswith("==="):
        return value[3:].strip()
    if value.startswith("=="):
        return value[2:].strip()
    return value


def ancestor_directories(start: Path, root: Path) -> Iterable[Path]:
    try:
        current = start.resolve()
        stop = root.resolve()
    except OSError:
        return []
    directories: List[Path] = []
    while True:
        directories.append(current)
        if current == stop or not is_relative_path(current, stop):
            break
        if current.parent == current:
            break
        current = current.parent
    return directories


def is_relative_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def gradle_properties(path: Path, root: Path, text: str) -> Dict[str, str]:
    properties: Dict[str, str] = {}
    for directory in reversed(list(ancestor_directories(path.parent, root))):
        for raw in read_lines(directory / "gradle.properties"):
            line = strip_inline_comment(raw).strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            properties[key.strip()] = value.strip()
    for match in re.finditer(r"""(?m)^\s*(?:def\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*['"](?P<value>[^'"]+)['"]""", text):
        properties[match.group("key")] = match.group("value")
    for match in re.finditer(r"""(?m)^\s*(?:ext|extra)\.(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*['"](?P<value>[^'"]+)['"]""", text):
        properties[match.group("key")] = match.group("value")
    for match in re.finditer(r"""(?m)^\s*(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)\s*:\s*['"](?P<value>[^'"]+)['"]""", text):
        properties[match.group("key")] = match.group("value")
    for match in re.finditer(r"""extra\[['"](?P<key>[^'"]+)['"]]\s*=\s*['"](?P<value>[^'"]+)['"]""", text):
        properties[match.group("key")] = match.group("value")
    return properties


def resolve_gradle_property(value: str, properties: Dict[str, str]) -> str:
    result = value.strip()
    for _ in range(5):
        changed = False
        for match in list(re.finditer(r"\$\{([^}]+)}", result)):
            replacement = properties.get(match.group(1))
            if replacement is None:
                continue
            result = result.replace(match.group(0), replacement)
            changed = True
        if not changed:
            break
    return result
