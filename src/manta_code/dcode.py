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
SPLASH_SUBHEADER = "Tip: @<agent> to delegate - manta status for background tasks"

#: Branding theme key + definition. ``deepagents-code`` natively loads custom
#: themes from ``[themes.<name>]`` in its ``config.toml`` and applies them across
#: the whole TUI (splash banner, borders, links, spinner). Manta registers a
#: Databricks-red theme so the console is on-brand out of the box; ``primary`` is
#: Databricks "lava 600" red and the rest of the fields inherit the upstream dark
#: palette. Only ``primary`` is overridden to keep success/warning/error
#: semantics legible. Updated each launch so the brand colour stays current.
MANTA_THEME_KEY = "manta"
DATABRICKS_RED = "#FF3621"
MANTA_THEME = {
    "label": "Manta (Databricks)",
    "dark": True,
    "primary": DATABRICKS_RED,
}


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

    _merge_branding_theme(result)
    return result


def _merge_branding_theme(result: dict) -> None:
    """Register the Databricks-red ``manta`` theme and make it the default.

    Mutates ``result`` in place. Manta owns the ``[themes.manta]`` definition
    (rewritten each launch so the brand colour stays current) but preserves any
    other user themes. ``[ui].theme`` is set to ``manta`` **only when the user
    has no saved preference**, so switching themes via ``/theme`` is always
    respected. Non-table ``[themes]`` / ``[ui]`` sections are left untouched
    rather than overwritten (defensive: never clobber a hand-edited config).
    """
    themes_tbl = result.setdefault("themes", {})
    if not isinstance(themes_tbl, dict):
        return
    themes_tbl[MANTA_THEME_KEY] = dict(MANTA_THEME)

    ui_tbl = result.setdefault("ui", {})
    if not isinstance(ui_tbl, dict):
        return
    if not ui_tbl.get("theme"):
        ui_tbl["theme"] = MANTA_THEME_KEY


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


def _addressed_agent(args: Sequence[str]) -> str | None:
    """Return the agent named by ``-a/--agent`` in the passthrough args, if any."""
    args = list(args)
    for i, arg in enumerate(args):
        if arg in ("-a", "--agent") and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--agent="):
            return arg.split("=", 1)[1]
    return None


def _agent_model_spec(agent_name: str) -> str | None:
    """The full ``provider:model`` pin for a Manta agent, or ``None``.

    Guarded: an unknown agent or an unreadable registry returns ``None`` so
    the caller falls back to the configured default endpoint.
    """
    try:
        from .agents.defaults import merged_agents
        from .agents.registry import list_agents

        for defn in merged_agents(list_agents()):
            if defn.name == agent_name:
                return defn.model or None
    except Exception:  # noqa: BLE001 - pin lookup is best-effort
        pass
    return None


def _effective_initial_agent(extras: Sequence[str]) -> str | None:
    """The agent a launch will actually start as, mirroring upstream's order.

    ``deepagents-code`` resolves the initial assistant as: ``-a`` flag >
    persisted ``[agents].default`` > remembered ``[agents].recent`` > the base
    ``agent``. Manta must inject the session model for the *same* agent, or
    the footer shows the cheap default while a pinned specialist runs.
    Guarded: any failure resolves to ``None`` (base agent).
    """
    agent = _addressed_agent(extras)
    if agent:
        return agent
    try:
        from deepagents_code.model_config import load_default_agent, load_recent_agent

        return load_default_agent() or load_recent_agent() or None
    except Exception:  # noqa: BLE001 - upstream config is best-effort here
        return None


def _has_resume_flag(args: Sequence[str]) -> bool:
    """Return ``True`` when the launch resumes an existing thread (``-r``)."""
    return any(
        arg in ("-r", "--resume") or arg.startswith("--resume=") for arg in args
    )


def _session_model_spec(
    default_endpoint: str | None, extras: Sequence[str]
) -> str | None:
    """Resolve the ``-M`` spec to inject for a launch, or ``None``.

    The user's own ``-M/--model`` always wins (no injection). A **resumed**
    session gets no injection either: upstream adopts the resumed thread's own
    model, and an injected ``-M`` would mark the model "explicitly set" and
    silently switch the conversation. When the launch will start as a Manta
    agent — via ``-a <name>``, the persisted default agent, or the remembered
    recent agent — **that agent's model pin** is the session model, so the
    visible session model matches the agent actually running ("the right
    model for the role", VISION pillar 2) and the pin middleware becomes a
    backstop rather than the mechanism. Otherwise the configured default
    endpoint applies (cheap-by-default orchestration).

    ``default_endpoint is None`` means Databricks is not configured on this
    machine (ADR 0010 detect-and-enable): a ``databricks:`` pin would force
    the session onto an unreachable provider, so it is skipped and upstream's
    own provider resolution applies.
    """
    if _has_model_flag(extras) or _has_resume_flag(extras):
        return None
    agent = _effective_initial_agent(extras)
    if agent:
        pin = _agent_model_spec(agent)
        if pin and (
            default_endpoint is not None
            or not pin.startswith(f"{DATABRICKS_PROVIDER}:")
        ):
            return pin
    if default_endpoint:
        return f"{DATABRICKS_PROVIDER}:{default_endpoint}"
    return None


def build_dcode_argv(
    default_endpoint: str | None,
    passthrough: Sequence[str],
    *,
    python: str | None = None,
) -> list[str]:
    """Build the argv to launch deepagents-code via Manta's branded boot shim.

    Runs ``python -m manta_code._boot`` (which rebrands the splash then hands
    off to ``deepagents-code``'s CLI). Injects ``-M`` only when the user did
    not pass their own ``-M/--model``: the addressed agent's pin when
    launching with ``-a <agent>``, else ``databricks:<default endpoint>``.
    Extra args are forwarded verbatim.
    """
    argv = [python or sys.executable, "-m", DCODE_BOOT_MODULE]
    extras = list(passthrough)
    spec = _session_model_spec(default_endpoint, extras)
    if spec:
        argv += ["-M", spec]
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
    spec = _session_model_spec(default_endpoint, extras)
    if spec:
        argv += ["-M", spec]
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


def sync_agent_profiles() -> None:
    """Project the Manta registry into deepagents profiles (best-effort).

    Generates a top-level ``~/.deepagents/<name>/`` profile for every registry
    agent (built-ins + user-created) so they appear in the in-app ``/agents``
    picker, prunes profiles for deleted agents, and one-time-cleans the legacy
    prompt-only markdown subagents this replaces. Fully guarded: a failure here
    must never block a launch.
    """
    try:
        from .agents.defaults import merged_agents
        from .agents.profiles import (
            clean_legacy_subagents,
            sync_agent_profiles as _sync,
        )
        from .agents.registry import list_agents

        clean_legacy_subagents()
        _sync(merged_agents(list_agents()))
    except Exception:  # noqa: BLE001 - reliability: launch regardless
        pass


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
    env_extra: Mapping[str, str] | None = None,
) -> int:
    """Provision config + env and run a single headless task, returning its code.

    Unlike :func:`launch`, this never replaces the process (so the caller — a CI
    script or ``manta run``) gets the exit code), and it provisions the same
    Databricks config/onboarding/agent profiles so Manta's control plane
    (enforced agents, token economy) applies in headless mode too.
    """
    ensure_dcode_config(
        endpoints,
        config_path=config_path,
        params=params,
        default_endpoint=default_endpoint,
    )
    mark_onboarding_complete()
    sync_agent_profiles()
    env = build_launch_env(profile)
    # Every headless run is unattended by definition (ADR 0011): one explicit
    # marker so the ASK policy tier fails closed and the audit layer never
    # records a human approval that didn't happen — regardless of whether
    # upstream's auto-approve flag is set for this particular invocation.
    env["MANTA_UNATTENDED"] = "1"
    if env_extra:
        env.update(env_extra)
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

    Also syncs the Manta registry into top-level deepagents profiles (see
    :func:`sync_agent_profiles`), so built-in and user-created agents appear in
    the in-app ``/agents`` picker. Profiles are generated artifacts (refreshed
    each launch); user-authored profiles and the base ``agent`` are never
    touched.

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
    sync_agent_profiles()
    env = build_launch_env(profile)
    argv = build_dcode_argv(default_endpoint, passthrough)
    if exec_replace:
        os.execvpe(argv[0], argv, env)  # noqa: S606 - fixed argv, replaces process
        return 0  # pragma: no cover - execvpe never returns
    completed = subprocess.run(argv, env=env, check=False)  # noqa: S603
    return completed.returncode
