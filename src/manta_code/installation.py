"""Release-aware recovery instructions for installations missing the runtime."""

from __future__ import annotations

from . import __version__


def runtime_install_command() -> str:
    """Return an install/upgrade command usable outside a source checkout."""
    tag = f"v{__version__}"
    repository = "https://github.com/althrussell/manta-code.git"
    constraints = (
        "https://raw.githubusercontent.com/althrussell/manta-code/"
        f"{tag}/requirements-dev.lock"
    )
    return (
        "uv tool install --python 3.12 --force --upgrade "
        f'--constraints "{constraints}" "manta-code[agent] @ git+{repository}@{tag}"'
    )
