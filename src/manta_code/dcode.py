"""Launch ``deepagents-code``'s interactive TUI, preconfigured for Databricks.

Manta adopts `deepagents-code <https://pypi.org/project/deepagents-code/>`_ as
its interactive coding-agent surface (the thing you get when you type ``manta``).
Rather than forking its model layer, we drive it through its public extension
points:

- **Model** — Databricks is registered as a ``deepagents-code`` *provider* in
  ``~/.deepagents/config.toml`` via a ``class_path`` that points at
  :class:`databricks_langchain.ChatDatabricks`. ``deepagents-code``'s
  ``create_model("databricks:<endpoint>")`` then instantiates it directly, with
  no fork of its model registry.
- **Auth / profile** — the active Databricks profile is selected by exporting
  ``DATABRICKS_CONFIG_PROFILE``; the Databricks SDK (and therefore
  ``ChatDatabricks``) reads it for unified auth from ``~/.databrickscfg``. This
  is how ``-p/--profile`` flows through to the model.

The functions here are deliberately small and pure (config merge, env build,
argv build) so they can be unit-tested without launching a subprocess; only
:func:`launch` has side effects.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

from .auth import resolve_profile

#: Provider key registered in ``deepagents-code``'s ``config.toml``.
DATABRICKS_PROVIDER = "databricks"

#: ``module:ClassName`` that ``deepagents-code`` instantiates for the provider.
DATABRICKS_CLASS_PATH = "databricks_langchain:ChatDatabricks"

#: Location of ``deepagents-code``'s user config (hard-coded upstream).
DEEPAGENTS_CONFIG_DIR = Path.home() / ".deepagents"
DEEPAGENTS_CONFIG_PATH = DEEPAGENTS_CONFIG_DIR / "config.toml"

#: Light-touch branding: overrides the startup splash subheader. Deeper
#: rebranding would require forking the TUI, which is out of scope for now.
SPLASH_SUBHEADER_ENV = "DEEPAGENTS_CODE_DANGEROUSLY_OVERRIDE_STARTUP_SUBHEADER"
SPLASH_SUBHEADER = "Manta - Databricks coding agent"


class LauncherError(RuntimeError):
    """Raised when the deepagents-code launch cannot be prepared."""


def _dedupe(values: Sequence[str]) -> list[str]:
    """Return ``values`` with falsy entries dropped and order-preserving dedupe."""
    return list(dict.fromkeys(v for v in values if v))


def merge_databricks_provider(
    existing: dict,
    endpoints: Sequence[str],
    *,
    params: Mapping[str, object] | None = None,
) -> dict:
    """Merge Manta's Databricks provider into an existing config dict.

    Returns a new dict (the input is not mutated). All unrelated user settings
    are preserved. Manta owns ``class_path`` and unions ``models`` with whatever
    the user already had; user ``params`` are preserved and shallow-overridden
    by ``params`` when provided.
    """
    result = copy.deepcopy(existing) if existing else {}
    models_tbl = result.setdefault("models", {})
    if not isinstance(models_tbl, dict):
        raise LauncherError(
            "Existing [models] section in ~/.deepagents/config.toml is not a table; "
            "refusing to overwrite. Please fix or remove it."
        )
    providers = models_tbl.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise LauncherError(
            "Existing [models.providers] in ~/.deepagents/config.toml is not a table; "
            "refusing to overwrite. Please fix or remove it."
        )
    previous = providers.get(DATABRICKS_PROVIDER, {})
    if not isinstance(previous, dict):
        previous = {}
    merged_models = _dedupe([*previous.get("models", []), *endpoints])
    provider: dict[str, object] = {
        **previous,
        "class_path": DATABRICKS_CLASS_PATH,
        "models": merged_models,
    }
    if params:
        provider["params"] = {**previous.get("params", {}), **dict(params)}
    providers[DATABRICKS_PROVIDER] = provider
    return result


def _read_toml(path: Path) -> dict:
    """Read a TOML file, returning ``{}`` when absent. Raises on malformed TOML."""
    if not path.is_file():
        return {}
    import tomllib

    with path.open("rb") as handle:
        try:
            return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise LauncherError(
                f"~/.deepagents/config.toml is not valid TOML ({exc}); "
                "refusing to overwrite. Please fix or remove it."
            ) from exc


def _write_toml(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` as TOML using tomli-w."""
    try:
        import tomli_w
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise LauncherError(
            "tomli-w is not installed. Install with: pip install -e '.[agent]'"
        ) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        tomli_w.dump(data, handle)


def ensure_dcode_config(
    endpoints: Sequence[str],
    *,
    config_path: Path | None = None,
    params: Mapping[str, object] | None = None,
) -> Path:
    """Idempotently ensure the Databricks provider exists in deepagents-code config.

    Reads the user's existing ``~/.deepagents/config.toml`` (if any), merges in
    Manta's Databricks provider, and writes it back. Returns the config path.
    """
    path = config_path or DEEPAGENTS_CONFIG_PATH
    merged = merge_databricks_provider(_read_toml(path), endpoints, params=params)
    _write_toml(path, merged)
    return path


def build_launch_env(
    profile: str | None,
    *,
    base_env: Mapping[str, str] | None = None,
    brand: bool = True,
) -> dict[str, str]:
    """Build the environment for the deepagents-code subprocess.

    Sets ``DATABRICKS_CONFIG_PROFILE`` to the resolved profile so the Databricks
    SDK authenticates against the right ``~/.databrickscfg`` entry. Existing
    environment values are preserved; an explicit profile always wins.
    """
    env = dict(os.environ if base_env is None else base_env)
    resolved = resolve_profile(profile)
    if resolved:
        env["DATABRICKS_CONFIG_PROFILE"] = resolved
    if brand:
        env.setdefault(SPLASH_SUBHEADER_ENV, SPLASH_SUBHEADER)
    return env


def _has_model_flag(args: Sequence[str]) -> bool:
    """Return ``True`` if the passthrough args already specify a model (-M/--model)."""
    for arg in args:
        if arg in ("-M", "--model"):
            return True
        if arg.startswith("-M") or arg.startswith("--model="):
            return True
    return False


def build_dcode_argv(
    default_endpoint: str | None,
    passthrough: Sequence[str],
    *,
    python: str | None = None,
) -> list[str]:
    """Build the argv to launch deepagents-code via ``python -m deepagents_code``.

    Injects ``-M databricks:<endpoint>`` only when the user did not pass their
    own ``-M/--model``. Extra args are forwarded verbatim.
    """
    argv = [python or sys.executable, "-m", "deepagents_code"]
    extras = list(passthrough)
    if default_endpoint and not _has_model_flag(extras):
        argv += ["-M", f"{DATABRICKS_PROVIDER}:{default_endpoint}"]
    argv += extras
    return argv


def launch(
    *,
    profile: str | None,
    default_endpoint: str | None,
    endpoints: Sequence[str],
    passthrough: Sequence[str] = (),
    config_path: Path | None = None,
    params: Mapping[str, object] | None = None,
    exec_replace: bool = True,
) -> int:
    """Provision config + env and launch the deepagents-code TUI.

    When ``exec_replace`` is true (the default) the current process is replaced
    via :func:`os.execvpe` so the TUI owns the terminal cleanly and this call
    never returns. When false (used by tests), the launcher runs the subprocess
    and returns its exit code.
    """
    ensure_dcode_config(endpoints, config_path=config_path, params=params)
    env = build_launch_env(profile)
    argv = build_dcode_argv(default_endpoint, passthrough)
    if exec_replace:
        os.execvpe(argv[0], argv, env)  # noqa: S606 - fixed argv, replaces process
        return 0  # pragma: no cover - execvpe never returns
    completed = subprocess.run(argv, env=env, check=False)  # noqa: S603
    return completed.returncode
