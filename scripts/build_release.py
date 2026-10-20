#!/usr/bin/env python3
"""Build and verify GitHub release assets using the installed, locked build backend.

Run from a qualified development environment:
    python scripts/build_release.py --output-dir dist --tag v0.3.0

No dependencies are downloaded. The wheel is built from the source archive and
its CLI is exercised outside the checkout before any assets reach output-dir.
"""
from __future__ import annotations

import argparse
import ast
import base64
import configparser
import csv
import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name


class ReleaseError(ValueError):
    """A source tree or artifact does not meet the release contract."""


@dataclass(frozen=True)
class ReleaseSpec:
    root: Path
    version: str
    python: str
    dependencies: tuple[str, ...]
    extras: tuple[str, ...]
    modules: frozenset[str]
    notes: Path


def source_spec(root: Path, tag: str) -> ReleaseSpec:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    version = project["version"]
    if tag != f"v{version}" or not all(part.isdecimal() for part in version.split(".")):
        raise ReleaseError(f"Tag {tag!r} must match project version v{version}")
    if len(version.split(".")) != 3:
        raise ReleaseError("Release versions must have three numeric components")

    package_version = None
    for statement in ast.parse((root / "src/manta_code/__init__.py").read_text()).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            package_version = ast.literal_eval(statement.value)
    if package_version != version:
        raise ReleaseError("Project and manta_code.__version__ versions differ")

    notes = root / "docs/releases" / f"v{version}.md"
    if not notes.is_file() or not notes.read_text().startswith(f"# Manta v{version}\n"):
        raise ReleaseError(f"Missing or mismatched release notes: {notes}")
    if not (root / "requirements-dev.lock").is_file():
        raise ReleaseError("Missing qualified dependency lock: requirements-dev.lock")

    dependencies = list(project["dependencies"])
    for extra, requirements in project.get("optional-dependencies", {}).items():
        for value in requirements:
            requirement = Requirement(value)
            marker = f"({requirement.marker}) and " if requirement.marker else ""
            requirement.marker = Marker(f'{marker}extra == "{extra}"')
            dependencies.append(str(requirement))
    modules = frozenset(
        str(path.relative_to(root / "src").as_posix())
        for path in (root / "src/manta_code").rglob("*.py")
    )
    return ReleaseSpec(
        root, version, project["requires-python"], tuple(dependencies),
        tuple(project.get("optional-dependencies", {})), modules, notes,
    )


def _requirement_key(value: str) -> tuple:
    requirement = Requirement(value)
    return (
        canonicalize_name(requirement.name), tuple(sorted(requirement.extras)),
        str(requirement.specifier), requirement.url, str(requirement.marker),
    )


def _validate_metadata(contents: str, spec: ReleaseSpec) -> None:
    metadata = Parser().parsestr(contents)
    expected = {"Name": "manta-code", "Version": spec.version}
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise ReleaseError(f"Artifact {key} is {metadata.get(key)!r}; expected {value!r}")
    if SpecifierSet(metadata.get("Requires-Python", "")) != SpecifierSet(spec.python):
        raise ReleaseError("Artifact Python requirement differs from pyproject.toml")
    actual = {_requirement_key(value) for value in metadata.get_all("Requires-Dist", [])}
    required = {_requirement_key(value) for value in spec.dependencies}
    if actual != required:
        raise ReleaseError("Artifact dependencies differ from pyproject.toml")
    if set(metadata.get_all("Provides-Extra", [])) != set(spec.extras):
        raise ReleaseError("Artifact extras differ from pyproject.toml")


def validate_wheel(path: Path, spec: ReleaseSpec) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(
            PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
            for name in names
        ):
            raise ReleaseError("Wheel has duplicate or unsafe member paths")
        missing = spec.modules - set(names)
        if missing:
            raise ReleaseError(f"Wheel is missing runtime modules: {sorted(missing)}")
        for name in spec.modules:
            if archive.read(name) != (spec.root / "src" / name).read_bytes():
                raise ReleaseError(f"Wheel runtime module differs from qualified source: {name}")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ReleaseError("Wheel must contain one distribution metadata directory")
        metadata_name = metadata_names[0]
        _validate_metadata(archive.read(metadata_name).decode(), spec)
        record_name = metadata_name.replace("METADATA", "RECORD")
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode())))
        if any(len(row) != 3 for row in rows) or len(rows) != len({row[0] for row in rows}):
            raise ReleaseError("Wheel RECORD contains invalid or duplicate entries")
        if {row[0] for row in rows} != {name for name in names if not name.endswith("/")}:
            raise ReleaseError("Wheel RECORD does not cover every packaged file")
        for name, checksum, size in rows:
            if name == record_name:
                if checksum or size:
                    raise ReleaseError("Wheel RECORD must leave its own hash and size empty")
                continue
            data = archive.read(name)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            if checksum != f"sha256={digest.decode()}" or size != str(len(data)):
                raise ReleaseError(f"Wheel RECORD integrity failure: {name}")
        entries = configparser.ConfigParser(interpolation=None)
        entries.read_string(archive.read(metadata_name.replace("METADATA", "entry_points.txt"))
                            .decode())
        if entries.get("console_scripts", "manta", fallback=None) != "manta_code.main:main_entry":
            raise ReleaseError("Wheel does not expose the manta console entry point")


def validate_sdist(path: Path, spec: ReleaseSpec, destination: Path) -> Path:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [PurePosixPath(member.name) for member in members]
        if len(names) != len(set(names)) or any(
            name.is_absolute() or ".." in name.parts
            or member.issym() or member.islnk() or not (member.isfile() or member.isdir())
            for name, member in zip(names, members)
        ):
            raise ReleaseError("Source archive has duplicate, unsafe, or linked members")
        roots = {name.parts[0] for name in names if name.parts}
        if len(roots) != 1:
            raise ReleaseError("Source archive must have one top-level directory")
        prefix = next(iter(roots))
        files = {str(name.relative_to(prefix)) for name in names}
        required = {
            "pyproject.toml", "README.md", "LICENSE", "MANIFEST.in", "PKG-INFO",
            "requirements-dev.lock", "scripts/build_release.py",
            "CONTRIBUTING.md", "CHANGELOG.md", "SECURITY.md", "VISION.md", "CLOSE_GAPS.md",
            "conftest.py", "docs/16-installation-and-upgrades.md", "evals/__main__.py",
            f"docs/releases/v{spec.version}.md",
            *(f"src/{name}" for name in spec.modules),
        }
        for pattern in (
            "docs/**/*.md", "docs/qualification/*.json", "configs/**/*.toml", "scripts/**/*.py", "scripts/**/*.sh",
            "examples/**/*.yml", "evals/**/*.py", "evals/**/*.md", "tests/**/*.py",
            ".github/workflows/*.yml",
        ):
            required.update(str(path.relative_to(spec.root).as_posix())
                            for path in spec.root.glob(pattern) if path.is_file())
        if required - files:
            raise ReleaseError(f"Source archive is missing files: {sorted(required - files)}")
        for name in required - {"PKG-INFO"}:
            packaged = archive.extractfile(f"{prefix}/{name}")
            if packaged is None or packaged.read() != (spec.root / name).read_bytes():
                raise ReleaseError(f"Source archive file differs from qualified source: {name}")
        metadata = archive.extractfile(f"{prefix}/PKG-INFO")
        if metadata is None:
            raise ReleaseError("Source archive has no package metadata")
        _validate_metadata(metadata.read().decode(), spec)
        lock = archive.extractfile(f"{prefix}/requirements-dev.lock")
        if lock is None or lock.read() != (spec.root / "requirements-dev.lock").read_bytes():
            raise ReleaseError("Source archive dependency lock differs from qualified source")
        archive.extractall(destination, filter="data")
    extracted = destination / prefix
    source_spec(extracted, f"v{spec.version}")
    return extracted


def _build(root: Path, output: Path, operation: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; from setuptools import build_meta; "
         "getattr(build_meta, sys.argv[1])(sys.argv[2])", operation, str(output)],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode:
        raise ReleaseError(f"{operation} failed:\n{result.stdout}\n{result.stderr}")


def smoke_wheel(path: Path, spec: ReleaseSpec, directory: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import manta_code; "
         "assert manta_code.__file__.startswith(sys.argv[1] + '/'); "
         "from manta_code.main import main_entry; "
         "sys.argv = ['manta', '--version']; main_entry()", str(path)],
        cwd=directory, capture_output=True, text=True,
    )
    if result.returncode or result.stdout.strip() != spec.version:
        raise ReleaseError(f"Wheel CLI smoke failed:\n{result.stdout}\n{result.stderr}")


def write_checksums(directory: Path) -> None:
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(directory.iterdir()) if path.name != "SHA256SUMS" and path.is_file()
    ]
    (directory / "SHA256SUMS").write_text("".join(lines))


def build_release(root: Path, output: Path, tag: str) -> list[Path]:
    root, output = root.resolve(), output.resolve()
    spec = source_spec(root, tag)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ReleaseError(f"Output directory must be empty: {output}")
    with tempfile.TemporaryDirectory(prefix="manta-release-") as temporary:
        temporary_path = Path(temporary)
        assets = temporary_path / "assets"
        assets.mkdir()
        _build(root, assets, "build_sdist")
        archives = list(assets.glob("*.tar.gz"))
        if len(archives) != 1:
            raise ReleaseError("Build must produce one source archive")
        extracted = validate_sdist(archives[0], spec, temporary_path / "source")
        _build(extracted, assets, "build_wheel")
        wheels = list(assets.glob("*.whl"))
        if len(wheels) != 1:
            raise ReleaseError("Build must produce one wheel")
        validate_wheel(wheels[0], spec)
        smoke_wheel(wheels[0], spec, temporary_path)
        shutil.copyfile(root / "requirements-dev.lock", assets / "requirements-dev.lock")
        shutil.copyfile(spec.notes, assets / "release-notes.md")
        write_checksums(assets)
        output.mkdir(parents=True, exist_ok=True)
        for path in sorted(assets.iterdir()):
            shutil.copyfile(path, output / path.name)
    return sorted(output.iterdir())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--tag", required=True, help="Release tag, such as v0.3.0")
    args = parser.parse_args()
    try:
        files = build_release(Path(__file__).resolve().parents[1], args.output_dir, args.tag)
    except (ReleaseError, OSError, KeyError, zipfile.BadZipFile, tarfile.TarError) as error:
        parser.exit(1, f"Release validation failed: {error}\n")
    for path in files:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
