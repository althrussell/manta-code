# Installation and upgrades

Manta v0.3.0 is distributed as a GitHub release and immutable Git tag, with a
wheel, source archive, dependency constraints, release notes, and SHA-256
checksums. There is no PyPI publishing step in this release process.

The runtime requires Python 3.12 or newer. Python 3.12 and 3.14 are qualified in
CI. All commands below install the `agent` extra, which includes the interactive
runtime and Databricks support; installing only the base package omits them.
Examples use Bash unless marked PowerShell.

## Recommended: isolated uv tool

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git,
then run:

```bash
uv python install 3.12
uv tool install --python 3.12 \
  --constraints https://raw.githubusercontent.com/althrussell/manta-code/v0.3.0/requirements-dev.lock \
  "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.3.0"
uv tool update-shell
manta --version
```

Open a new terminal after updating your shell if necessary. The version command
should print `0.3.0`. The constraints file selects the dependency versions used
for qualification; it does not install the development tools by itself.

## Upgrade an older installation

Replace the existing tool with the new tag and a Python 3.12+ environment:

```bash
uv tool install --force --upgrade --python 3.12 \
  --constraints https://raw.githubusercontent.com/althrussell/manta-code/v0.3.0/requirements-dev.lock \
  "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.3.0"
manta --version
```

`uv tool upgrade manta-code` upgrades within the source you originally selected.
If that source is `@v0.2.0`, it does not discover and select `@v0.3.0`. Explicitly
install the new tag as above. For subsequent releases, change both the tag in
the package URL and the tag in the constraints URL.

The Python floor increased from 3.11 to 3.12, and the Databricks bridge packages
were replaced by `langchain-openai` plus the Databricks SDK. A new tool environment
avoids retaining the old bridge's MCP 1 dependencies alongside the new runtime's
MCP 2 stack. The install does not remove your Manta configuration, registry,
usage ledger, or saved sessions.

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

Use a new environment for this runtime migration:

```bash
python3.12 -m venv ~/.venvs/manta-0.3.0
source ~/.venvs/manta-0.3.0/bin/activate
python -m pip install --upgrade pip
python -m pip install \
  --constraint https://raw.githubusercontent.com/althrussell/manta-code/v0.3.0/requirements-dev.lock \
  "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.3.0"
python -m pip check
manta --version
```

On Windows, install Git and Python 3.12, then use PowerShell:

```powershell
py -3.12 -m venv "$env:USERPROFILE\.venvs\manta-0.3.0"
& "$env:USERPROFILE\.venvs\manta-0.3.0\Scripts\Activate.ps1"
python -m pip install --upgrade pip
python -m pip install --constraint https://raw.githubusercontent.com/althrussell/manta-code/v0.3.0/requirements-dev.lock "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git@v0.3.0"
python -m pip check
manta --version
```

Keep all commands in the same environment. Avoid installing into a
system-managed Python.

## Install a release wheel

The GitHub release also provides a wheel for environments that do not use Git.
With the [GitHub CLI](https://cli.github.com/) installed:

```bash
mkdir manta-0.3.0-release
cd manta-0.3.0-release
gh release download v0.3.0 --repo althrussell/manta-code
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
  './manta_code-0.3.0-py3-none-any.whl[agent]'
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

The qualified Gemini services use unified Chat because their unified Responses
tool continuation currently fails in the gateway. See the
[qualification report](15-unity-gateway-qualification.md) for exact targets and
other limits. Unknown service prices remain unknown in local cost reports.

## Troubleshooting and development

If the shell finds an old binary, run `command -v manta` (PowerShell:
`Get-Command manta`), activate the intended environment, or refresh the uv tool
PATH. `manta --version` identifies the package being executed.

If resolution reports an MCP 1/MCP 2 conflict, recreate the environment using the
v0.3.0 commands rather than adding the old Databricks bridge packages.

For an editable checkout and offline regression tests, follow
[CONTRIBUTING.md](../CONTRIBUTING.md). `scripts/bootstrap.sh` sets up the locked
development environment; it is not the end-user installer.
