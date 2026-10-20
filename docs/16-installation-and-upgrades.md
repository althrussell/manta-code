# Installation and upgrades

Manta v0.4.0 is distributed as a GitHub release and immutable Git tag, with a
wheel, source archive, dependency constraints, release notes, and SHA-256
checksums. There is no PyPI publishing step in this release process.

The runtime requires Python 3.12 or newer. Python 3.12 and 3.14 are qualified in
CI. All commands below install the `agent` extra, which includes the interactive
runtime and Databricks support; installing only the base package omits them.
Examples use Bash unless marked PowerShell.

v0.4.0 includes [dependable foundations](19-dependable-foundations.md), the
[visible agent team](20-visible-agent-team.md), a Gateway-only Databricks model
picker, and restored Manta branding. The release includes the Manta-owned updater,
`/agents` authoring, `/tasks` tracking, isolated detached work and reviewed
integration. [Measured model efficiency](17-coding-agent-harness-review.md)
remains the next milestone.

## Recommended: isolated uv tool

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git,
then run:

```bash
uv python install 3.12
uv tool install --python 3.12 \
  --constraints https://raw.githubusercontent.com/althrussell/manta-code/v0.4.0/requirements-dev.lock \
  "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.4.0"
uv tool update-shell
manta --version
```

Open a new terminal after updating your shell if necessary. The version command
should print `0.4.0`. The constraints file selects the dependency versions used
for qualification; it does not install the development tools by itself.

## Upgrade an older installation

**v0.3.0 and earlier do not have the Manta-owned updater.** Save your work and
exit interactive sessions, then replace the tool with the new tag and its
matching constraints:

```bash
uv tool install --force --upgrade --python 3.12 \
  --constraints https://raw.githubusercontent.com/althrussell/manta-code/v0.4.0/requirements-dev.lock \
  "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.4.0"
manta --version
manta doctor
```

The version should print `0.4.0`. `uv tool upgrade manta-code` stays within the
source you originally selected: an installation pinned to `@v0.3.0` does not
select `@v0.4.0`. For subsequent manual upgrades, change both the package URL's
tag and the constraints URL's tag. For a pip installation, use the new virtual
environment instructions below.

Once v0.4.0 or newer is installed, check stable releases without modifying
packages:

```bash
manta update --check
manta update --check --version v0.4.0
```

Save work and exit interactive sessions before installing the chosen release:

```bash
manta update --version v0.4.0
manta --version
manta doctor
```

For a later release, replace `v0.4.0` with its published stable tag. Without
`--version`, `manta update` selects the latest stable release and installs only
if newer. An explicit tag can reinstall/select that release. The updater detects
its own uv tool or pip environment, installs `manta-code[agent]` from the
immutable tag with that tag's full constraints, and preserves installer failures
as failures. It never upgrades `deepagents-code` independently. `/update` and
`--update` use the same Manta release contract; `/update` checks and gives the
shell action so an active session is not replaced underneath you. Start a fresh
`manta` process after installation; runtime restart paths also re-enter Manta.

Since v0.3.0, the Python floor is 3.12 rather than 3.11, and the Databricks
bridge packages were replaced by `langchain-openai` plus the Databricks SDK. A new tool environment
avoids retaining the old bridge's MCP 1 dependencies alongside the new runtime's
MCP 2 stack. The install does not remove your Manta configuration, registry,
usage ledger, or saved sessions.

The updater refuses to replace editable source checkouts. Review and update the
intended Git revision, synchronize that revision's `requirements-dev.lock`, then
reinstall `.[agent]` in its development environment. Keep the source, lockfile
and pinned runtime together. Manta disables independent automatic dcode
installation while running, without editing your stored dcode preferences.
Upstream extra/package installers, uninstalls and dependency-recovery refreshes
also refuse unqualified package changes. Add optional integrations deliberately
against the matching Manta release lock; do not run an installer that replaces
the bundled runtime with the latest dcode independently. Manta update operations
are serialized for an installation so concurrent processes cannot replace its
environment at the same time.

User-owned defaults and saved agent pins retain their existing values. Inspect
those choices before moving existing agents to the new services:

```bash
manta -p my-workspace doctor
manta agents list
manta -p my-workspace agents set-model swe system.ai.gpt-6-1-sol
manta -p my-workspace agents set-model review system.ai.claude-sonnet-5-5
```

The `set-model` commands deliberately change the named agents. Project
`.manta/config.toml` can override your user configuration, including the launch
default. Resume a saved thread with `manta -p my-workspace -r`; it retains its
protocol. If a switch would replay signed/opaque state to an incompatible model,
API, or workspace, start a fresh thread with `/clear`.

## pip in a virtual environment

Use a separate environment, especially when migrating from a pre-v0.3.0 runtime:

```bash
python3.12 -m venv ~/.venvs/manta-0.4.0
source ~/.venvs/manta-0.4.0/bin/activate
python -m pip install --upgrade pip
python -m pip install \
  --constraint https://raw.githubusercontent.com/althrussell/manta-code/v0.4.0/requirements-dev.lock \
  "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.4.0"
python -m pip check
manta --version
```

On Windows, install Git and Python 3.12, then use PowerShell:

```powershell
py -3.12 -m venv "$env:USERPROFILE\.venvs\manta-0.4.0"
& "$env:USERPROFILE\.venvs\manta-0.4.0\Scripts\Activate.ps1"
python -m pip install --upgrade pip
python -m pip install --constraint https://raw.githubusercontent.com/althrussell/manta-code/v0.4.0/requirements-dev.lock "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.4.0"
python -m pip check
manta --version
```

Keep all commands in the same environment. Avoid installing into a
system-managed Python.

## Install a release wheel

The GitHub release also provides a wheel for environments that do not use Git.
With the [GitHub CLI](https://cli.github.com/) installed:

```bash
mkdir manta-0.4.0-release
cd manta-0.4.0-release
gh release download v0.4.0 --repo althrussell/manta-code
if command -v sha256sum >/dev/null; then
  sha256sum --check SHA256SUMS
else
  shasum -a 256 --check SHA256SUMS
fi
```

After every checksum passes, install the wheel:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --constraint requirements-dev.lock \
  './manta_code-0.4.0-py3-none-any.whl[agent]'
python -m pip check
manta --version
```

The wheel contains Manta; runtime dependencies still require an available
Python package index or a separately prepared wheelhouse.

## Authenticate and verify

For Databricks, install the
[Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install) and run:

```bash
databricks auth login --host https://YOUR-WORKSPACE --profile my-workspace
manta -p my-workspace doctor
manta -p my-workspace gateway --target system.ai.gpt-6-1-sol
manta -p my-workspace doctor --probe
manta -p my-workspace -M databricks:system.ai.gpt-6-1-sol
```

Doctor performs no inference unless `--probe` is supplied. A probe makes two
requests per distinct configured target and incurs model charges. Metadata
visibility does not establish inference permission; a caller may also have
inference access without metadata access. Models must be available to your
workspace and principal.

Doctor reports required adapter health and whether this process has compiled
and validated an effective control manifest. A preflight-only process reports
that graph validation is pending; available symbols alone do not prove the
running graph is protected. Required control construction failures stop the
runtime with a diagnosis. Optional UI adapters can report degraded operation.

The qualified Gemini services use unified Chat because their unified Responses
tool continuation currently fails in the gateway. See the
[qualification report](15-unity-gateway-qualification.md) for exact targets and
other limits. Unknown service prices remain unknown in local cost reports.
The [model controls report](18-model-capability-qualification.md) separately
records effort/context evidence; metadata visibility and successful basic
inference do not qualify every advanced feature.

## Configuration and memory migration

Generated role instructions now live in
`~/.deepagents/<agent>/MANTA_ROLE.md`; `AGENTS.md` holds mutable learned context
and survives `manta agents sync`. Old combined files are retained under
`~/.deepagents/.state/manta-profile-backups/<agent>/<content-hash>/`, with
provenance. Deleted managed profile directories move to
`~/.deepagents/.state/manta-profile-archive/`, preserving memory and skills.
Unowned profiles are not adopted implicitly.

Legacy SQLite notes receive a recoverable `memory.db.pre-v2.bak` beside
`$MANTA_HOME/.state/memory.db` (default `~/.manta/.state/`). They remain
inspectable with `manta agents memory <agent>`, tagged legacy/unassigned and
excluded from automatic recall. Runtime note tools and recall use the same
compiled agent namespace. These are agent namespaces, not automatic project
scopes; use separate namespaces/agents where projects must not share notes.

Budget mode defaults to `checkpoint`, including unattended runs. Inspect a
budget-paused task before approving `manta task resume <id>`. Resume validates
the captured project/workspace/agent context and continues the saved checkpoint.
Choose `[budget] mode = "advisory"` only when continuation at a recorded
threshold is intended. Known spend is estimated between requests; unknown
prices/usage cannot enforce a hard dollar ceiling. See
[configuration](10-config-schema.md) for exact semantics and `manta pricing`
for effective rate provenance.

## Troubleshooting and development

If the shell finds an old binary, run `command -v manta` (PowerShell:
`Get-Command manta`), activate the intended environment, or refresh the uv tool
PATH. `manta --version` identifies the package being executed.

For runtime commands and their help, use `manta runtime --help`,
`manta runtime mcp --help` and `manta runtime plugin --help`. The explicit
runtime route also reaches upstream `doctor`/`agents` groups shadowed by Manta.
Help does not synchronize profiles, discover workspace models or call inference.

If resolution reports an MCP 1/MCP 2 conflict, recreate the environment using the
v0.4.0 commands rather than adding the old Databricks bridge packages.

For an editable checkout and offline regression tests, follow
[CONTRIBUTING.md](../CONTRIBUTING.md). `scripts/bootstrap.sh` sets up the locked
development environment. To install directly from an existing checkout:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.lock
uv pip install --python .venv/bin/python --no-deps -e '.[agent,dev]'
.venv/bin/manta -p my-workspace
```

These commands use the checked-out revision. Keep `MANTA_HOME` and
`DEEPAGENTS_HOME` separate for disposable agent/session state while testing.
Continue using your normal Databricks profile for authentication.
