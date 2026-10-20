"""Release artifacts must preserve the qualified code and installation contract."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[1] / "scripts/build_release.py"
_module_spec = importlib.util.spec_from_file_location("manta_build_release", _path)
release = importlib.util.module_from_spec(_module_spec)
sys.modules[_module_spec.name] = release
_module_spec.loader.exec_module(release)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    files = {
        "pyproject.toml": '''[project]
name = "manta-code"
version = "0.3.0"
requires-python = ">=3.12,<4"
dependencies = ["typer>=0.27.3"]
[project.optional-dependencies]
agent = ["openai>=3.31.0"]
''',
        "src/manta_code/__init__.py": '__version__ = "0.3.0"\n',
        "src/manta_code/main.py": 'def main_entry():\n    print("0.3.0")\n',
        "docs/releases/v0.3.0.md": "# Manta v0.3.0\n\nRelease notes.\n",
        "requirements-dev.lock": "typer==0.27.3\nopenai==3.31.0\n",
        "docs/16-installation-and-upgrades.md": "# Install Manta\n",
        "evals/__main__.py": 'print("evaluation harness")\n',
        "conftest.py": "# Offline test configuration\n",
        "scripts/build_release.py": "# Release tooling\n",
    }
    files.update({name: f"# {name}\n" for name in (
        "README.md", "LICENSE", "MANIFEST.in", "CONTRIBUTING.md", "CHANGELOG.md",
        "SECURITY.md", "VISION.md", "CLOSE_GAPS.md",
    )})
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    return release.source_spec(root, "v0.3.0")


def metadata(source):
    fields = [
        "Metadata-Version: 2.4", "Name: manta-code", f"Version: {source.version}",
        "Requires-Python: <4,>=3.12", "Provides-Extra: agent",
        *(f"Requires-Dist: {value}" for value in source.dependencies),
    ]
    return "\n".join(fields) + "\n\n"


def wheel(tmp_path, source, changes=None, *, corrupt_record=False, omit=None):
    files = {name: (source.root / "src" / name).read_bytes() for name in source.modules}
    prefix = "manta_code-0.3.0.dist-info"
    files[f"{prefix}/METADATA"] = metadata(source).encode()
    files[f"{prefix}/entry_points.txt"] = b"[console_scripts]\nmanta=manta_code.main:main_entry\n"
    files.update(changes or {})
    if omit:
        del files[omit]
    record = "".join(
        f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')},{len(data)}\n"
        for name, data in files.items()
    )
    record += f"{prefix}/RECORD,,\n"
    if corrupt_record:
        record = record.replace("sha256=", "sha256=invalid", 1)
    files[f"{prefix}/RECORD"] = record.encode()
    path = tmp_path / "manta_code-0.3.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def sdist(tmp_path, source, *, omit=None, changes=None):
    files = {
        path.relative_to(source.root).as_posix(): path.read_bytes()
        for path in source.root.rglob("*") if path.is_file()
    }
    files["PKG-INFO"] = metadata(source).encode()
    files.update(changes or {})
    path = tmp_path / "manta_code-0.3.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            if name == omit:
                continue
            info = tarfile.TarInfo(f"manta_code-0.3.0/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def test_release_requires_matching_tag_and_runtime_version(source):
    with pytest.raises(release.ReleaseError, match="Tag"):
        release.source_spec(source.root, "v0.2.0")
    (source.root / "src/manta_code/__init__.py").write_text('__version__ = "0.2.0"\n')
    with pytest.raises(release.ReleaseError, match="versions differ"):
        release.source_spec(source.root, "v0.3.0")


def test_release_requires_versioned_notes(source):
    source.notes.write_text("")
    with pytest.raises(release.ReleaseError, match="release notes"):
        release.source_spec(source.root, "v0.3.0")


def test_valid_artifacts_preserve_runtime_and_source_contract(tmp_path, source):
    release.validate_wheel(wheel(tmp_path, source), source)
    extracted = release.validate_sdist(sdist(tmp_path, source), source, tmp_path / "unpacked")
    assert (extracted / "requirements-dev.lock").read_bytes() == (
        source.root / "requirements-dev.lock"
    ).read_bytes()
    assert (extracted / "docs/16-installation-and-upgrades.md").is_file()
    assert (extracted / "conftest.py").is_file()


@pytest.mark.parametrize(("before", "after", "error"), [
    ("Version: 0.3.0", "Version: 0.2.0", "Version"),
    ("Requires-Python: <4,>=3.12", "Requires-Python: >=3.11", "Python requirement"),
    ("Requires-Dist: typer>=0.27.3", "Requires-Dist: typer>=0.20", "dependencies"),
])
def test_wheel_rejects_changed_package_metadata(tmp_path, source, before, after, error):
    path = wheel(tmp_path, source, {
        "manta_code-0.3.0.dist-info/METADATA": metadata(source).replace(before, after).encode(),
    })
    with pytest.raises(release.ReleaseError, match=error):
        release.validate_wheel(path, source)


def test_wheel_rejects_unqualified_runtime_code(tmp_path, source):
    path = wheel(tmp_path, source, {"manta_code/main.py": b"# stale or changed code\n"})
    with pytest.raises(release.ReleaseError, match="differs from qualified source"):
        release.validate_wheel(path, source)


def test_wheel_requires_every_runtime_module(tmp_path, source):
    with pytest.raises(release.ReleaseError, match="missing runtime modules"):
        release.validate_wheel(wheel(tmp_path, source, omit="manta_code/main.py"), source)


def test_wheel_requires_the_public_console_entry_point(tmp_path, source):
    path = wheel(tmp_path, source, {
        "manta_code-0.3.0.dist-info/entry_points.txt": b"[console_scripts]\nmanta=other:main\n",
    })
    with pytest.raises(release.ReleaseError, match="console entry point"):
        release.validate_wheel(path, source)


def test_wheel_rejects_invalid_integrity_record(tmp_path, source):
    with pytest.raises(release.ReleaseError, match="integrity failure"):
        release.validate_wheel(wheel(tmp_path, source, corrupt_record=True), source)


@pytest.mark.parametrize("missing", [
    "requirements-dev.lock", "docs/16-installation-and-upgrades.md", "conftest.py",
    "evals/__main__.py", "src/manta_code/main.py",
])
def test_sdist_requires_installable_and_testable_sources(tmp_path, source, missing):
    with pytest.raises(release.ReleaseError, match="missing files"):
        release.validate_sdist(sdist(tmp_path, source, omit=missing), source, tmp_path / "unpack")


def test_sdist_rejects_altered_qualified_files(tmp_path, source):
    path = sdist(tmp_path, source, changes={"requirements-dev.lock": b"unqualified==1\n"})
    with pytest.raises(release.ReleaseError, match="differs from qualified source"):
        release.validate_sdist(path, source, tmp_path / "unpack")


def test_sdist_rejects_traversal_before_extraction(tmp_path, source):
    path = sdist(tmp_path, source, changes={"../../escaped": b"unsafe\n"})
    with pytest.raises(release.ReleaseError, match="unsafe"):
        release.validate_sdist(path, source, tmp_path / "unpack")
    assert not (tmp_path / "escaped").exists()


def test_checksums_cover_every_release_asset_but_not_manifest(tmp_path):
    files = {
        "manta_code-0.3.0-py3-none-any.whl": b"wheel",
        "manta_code-0.3.0.tar.gz": b"source",
        "requirements-dev.lock": b"dependencies",
        "release-notes.md": b"release notes",
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    release.write_checksums(tmp_path)
    entries = {}
    for line in (tmp_path / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        entries[name] = digest
    assert entries == {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def test_build_refuses_stale_assets_before_invoking_backend(monkeypatch, tmp_path, source):
    output = tmp_path / "dist"
    output.mkdir()
    (output / "old.whl").write_bytes(b"old")

    def forbidden(*args):
        pytest.fail("build backend must not run with stale output assets")

    monkeypatch.setattr(release, "_build", forbidden)
    with pytest.raises(release.ReleaseError, match="must be empty"):
        release.build_release(source.root, output, "v0.3.0")
