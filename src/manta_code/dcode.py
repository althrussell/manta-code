"""Launch ``deepagents-code``'s interactive TUI, preconfigured for Databricks.

Manta adopts `deepagents-code <https://pypi.org/project/deepagents-code/>`_ as
its interactive coding-agent surface (the thing you get when you type ``manta``).
Rather than forking its model layer, we drive it through its public extension
points:

- **Model** — Databricks is registered as a ``deepagents-code`` *provider* in
  ``~/.deepagents/config.toml`` via a ``class_path`` that points at
  :class:`manta_code.databricks_chat.MantaChatDatabricks` (a thin
  ``ChatDatabricks`` subclass that unpacks reasoning-model content blocks).
  ``deepagents-code``'s ``create_model("databricks:<endpoint>")`` then
  instantiates it directly, with no fork of its model registry.
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
from .subagents import ensure_manta_subagents

#: Boot shim run instead of ``deepagents_code`` directly, so Manta can rebrand
#: the splash wordmark in-process before handing off to upstream's CLI entry.
DCODE_BOOT_MODULE = "manta_code._boot"

#: Provider key registered in ``deepagents-code``'s ``config.toml``.
DATABRICKS_PROVIDER = "databricks"

#: ``deepagents-code`` startup warnings Manta suppresses by default. Tavily
#: web search is not part of Manta's Databricks-focused offering, so the
#: "TAVILY_API_KEY is not set" notice is noise. Suppressed via
#: ``[warnings].suppress``; user-added entries are always preserved.
DEFAULT_SUPPRESSED_WARNINGS = ("tavily",)

#: ``module:ClassName`` that ``deepagents-code`` instantiates for the provider.
#: Manta's :class:`manta_code.databricks_chat.MantaChatDatabricks` subclass
#: (not stock ``ChatDatabricks``) so reasoning-model endpoints that return
#: content as a serialized reasoning/text block list render their answer
#: cleanly instead of dumping raw JSON.
DATABRICKS_CLASS_PATH = "manta_code.databricks_chat:MantaChatDatabricks"

#: Location of ``deepagents-code``'s user config (hard-coded upstream).
DEEPAGENTS_CONFIG_DIR = Path.home() / ".deepagents"
DEEPAGENTS_CONFIG_PATH = DEEPAGENTS_CONFIG_DIR / "config.toml"

#: deepagents-code's state dir and first-run onboarding marker. Writing the
#: marker suppresses the onboarding wizard (whose model picker only offers
#: upstream's curated providers, not Manta's Databricks ``class_path`` provider).
DEEPAGENTS_STATE_DIR = DEEPAGENTS_CONFIG_DIR / ".state"
ONBOARDING_MARKER_PATH = DEEPAGENTS_STATE_DIR / "onboarding_complete"

#: Branding: the startup splash subheader is overridden via this upstream env
#: hook; the splash wordmark is rebranded in-process by ``manta_code._boot``
#: (see :data:`DCODE_BOOT_MODULE`). In-app help text and tips still reference
#: "Deep Agents" — rebranding those would require forking the TUI.
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
    default_endpoint: str | None = None,
) -> dict:
    """Merge Manta's Databricks provider into an existing config dict.

    Returns a new dict (the input is not mutated). All unrelated user settings
    are preserved. Manta owns ``class_path`` and unions ``models`` with whatever
    the user already had; user ``params`` are preserved and shallow-overridden
    by ``params`` when provided.

    When ``default_endpoint`` is given and the user has not already set
    ``[models].default``, it is set to ``databricks:<default_endpoint>`` so
    ``deepagents-code`` resolves a Databricks model without prompting for an
    Anthropic/Google key. An existing user default is never overwritten.
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
    if default_endpoint and not models_tbl.get("default"):
        models_tbl["default"] = f"{DATABRICKS_PROVIDER}:{default_endpoint}"

    warnings_tbl = result.setdefault("warnings", {})
    if not isinstance(warnings_tbl, dict):
        raise LauncherError(
            "Existing [warnings] section in ~/.deepagents/config.toml is not a table; "
            "refusing to overwrite. Please fix or remove it."
        )
    existing_suppress = warnings_tbl.get("suppress", [])
    if not isinstance(existing_suppress, list):
        existing_suppress = []
    warnings_tbl["suppress"] = _dedupe(
        [*existing_suppress, *DEFAULT_SUPPRESSED_WARNINGS]
    )
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
    default_endpoint: str | None = None,
) -> Path:
    """Idempotently ensure the Databricks provider exists in deepagents-code config.

    Reads the user's existing ``~/.deepagents/config.toml`` (if any), merges in
    Manta's Databricks provider (and a Databricks ``[models].default`` when
    ``default_endpoint`` is given and the user has not set one), and writes it
    back. Returns the config path.
    """
    path = config_path or DEEPAGENTS_CONFIG_PATH
    merged = merge_databricks_provider(
        _read_toml(path),
        endpoints,
        params=params,
        default_endpoint=default_endpoint,
    )
    _write_toml(path, merged)
    return path


def mark_onboarding_complete(*, marker_path: Path | None = None) -> Path:
    """Suppress ``deepagents-code``'s first-run onboarding wizard.

    Writes the onboarding-complete marker so the upstream wizard — whose model
    picker only lists upstream's curated providers (Anthropic, Google) and would
    otherwise force the user to configure an Anthropic key — does not run. Manta
    has already provisioned the Databricks provider and default model, so the
    wizard adds nothing here. Idempotent.
    """
    path = marker_path or ONBOARDING_MARKER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n", encoding="utf-8")
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
    """Build the argv to launch deepagents-code via Manta's branded boot shim.

    Runs ``python -m manta_code._boot`` (which rebrands the splash then hands
    off to ``deepagents-code``'s CLI). Injects ``-M databricks:<endpoint>`` only
    when the user did not pass their own ``-M/--model``. Extra args are
    forwarded verbatim.
    """
    argv = [python or sys.executable, "-m", DCODE_BOOT_MODULE]
    extras = list(passthrough)
    if default_endpoint and not _has_model_flag(extras):
        argv += ["-M", f"{DATABRICKS_PROVIDER}:{default_endpoint}"]
    argv += extras
    return argv


#: Default wall-clock timeout (seconds) for a headless run. A bounded timeout is
#: deliberate: ``deepagents-code -n`` can hang on startup in some environments
#: (docs/13 "Known gaps"), and an unbounded scripted/CI run is worse than one
#: that fails fast with exit code 124. Override with ``manta run --timeout``.
DEFAULT_RUN_TIMEOUT = 600

#: Default agentic turn cap for a headless run (prevents runaway loops in CI).
DEFAULT_RUN_MAX_TURNS = 50


def build_run_argv(
    default_endpoint: str | None,
    message: str,
    passthrough: Sequence[str],
    *,
    quiet: bool = True,
    no_stream: bool = True,
    max_turns: int | None = DEFAULT_RUN_MAX_TURNS,
    timeout: int | None = DEFAULT_RUN_TIMEOUT,
    json_output: str | None = None,
    shell_allow_list: str | None = None,
    python: str | None = None,
) -> list[str]:
    """Build argv for a headless one-shot run via Manta's boot shim.

    Wraps ``deepagents-code``'s non-interactive surface (``-n/--non-interactive``)
    with CI-safe defaults: a bounded ``--timeout`` (so a startup hang fails fast),
    a ``--max-turns`` cap, quiet/buffered output for clean piping, and the
    Databricks model pin. The user's ``passthrough`` is appended verbatim and can
    override any of these.
    """
    argv = [python or sys.executable, "-m", DCODE_BOOT_MODULE]
    extras = list(passthrough)
    if default_endpoint and not _has_model_flag(extras):
        argv += ["-M", f"{DATABRICKS_PROVIDER}:{default_endpoint}"]
    argv += ["-n", message]
    if quiet:
        argv.append("-q")
    if no_stream:
        argv.append("--no-stream")
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]
    if timeout is not None:
        argv += ["--timeout", str(timeout)]
    if json_output:
        argv += ["--json-output", json_output]
    if shell_allow_list:
        argv += ["--shell-allow-list", shell_allow_list]
    argv += extras
    return argv


def run_headless(
    *,
    profile: str | None,
    default_endpoint: str | None,
    endpoints: Sequence[str],
    message: str,
    passthrough: Sequence[str] = (),
    config_path: Path | None = None,
    params: Mapping[str, object] | None = None,
    timeout: int | None = DEFAULT_RUN_TIMEOUT,
    max_turns: int | None = DEFAULT_RUN_MAX_TURNS,
    quiet: bool = True,
    no_stream: bool = True,
    json_output: str | None = None,
    shell_allow_list: str | None = None,
) -> int:
    """Provision config + env and run a single headless task, returning its code.

    Unlike :func:`launch`, this never replaces the process (so the caller — a CI
    script or ``manta run``) gets the exit code), and it provisions the same
    Databricks config/onboarding/subagents so Manta's control plane (enforced
    agents, token economy) applies in headless mode too.
    """
    ensure_dcode_config(
        endpoints,
        config_path=config_path,
        params=params,
        default_endpoint=default_endpoint,
    )
    mark_onboarding_complete()
    ensure_manta_subagents()
    env = build_launch_env(profile)
    argv = build_run_argv(
        default_endpoint,
        message,
        passthrough,
        quiet=quiet,
        no_stream=no_stream,
        max_turns=max_turns,
        timeout=timeout,
        json_output=json_output,
        shell_allow_list=shell_allow_list,
    )
    completed = subprocess.run(argv, env=env, check=False)  # noqa: S603
    return completed.returncode


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

    Also provisions Manta's default planning/SWE/review subagents on first run
    (see :func:`manta_code.subagents.ensure_manta_subagents`); this is
    marker-gated so user edits are never clobbered.

    When ``exec_replace`` is true (the default) the current process is replaced
    via :func:`os.execvpe` so the TUI owns the terminal cleanly and this call
    never returns. When false (used by tests), the launcher runs the subprocess
    and returns its exit code.
    """
    ensure_dcode_config(
        endpoints,
        config_path=config_path,
        params=params,
        default_endpoint=default_endpoint,
    )
    mark_onboarding_complete()
    ensure_manta_subagents()
    env = build_launch_env(profile)
    argv = build_dcode_argv(default_endpoint, passthrough)
    if exec_replace:
        os.execvpe(argv[0], argv, env)  # noqa: S606 - fixed argv, replaces process
        return 0  # pragma: no cover - execvpe never returns
    completed = subprocess.run(argv, env=env, check=False)  # noqa: S603
    return completed.returncode
