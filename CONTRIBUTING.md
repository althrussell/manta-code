# Contributing

## Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.lock
pip install --no-deps -e '.[dev,agent]'
pip check
pytest
ruff check .
```

Python 3.12 or newer is required. CI checks Python 3.12 and 3.14. The lock
records the latest dependency versions compatible with the pinned Deep Agents
runtime, including its Pydantic and filelock upper bounds. Refresh it with
`uv pip compile pyproject.toml --extra dev --extra agent --upgrade --universal --python-version 3.12 --no-header --output-file requirements-dev.lock`.

Alternatively, run `MANTA_PYTHON=python3.12 bash scripts/bootstrap.sh`. This
creates the checkout's development environment, installs the lock, checks setup,
and runs tests and lint. End users should follow the
[installation guide](docs/16-installation-and-upgrades.md).

The unreleased foundation candidate's fresh-install evidence used a qualified
package proxy and local wheelhouse. Public-index availability of every pinned
version has not been established; see the
[qualification record](docs/19-dependable-foundations.md). If a pinned runtime
is unavailable, use your approved source for that exact version rather than
downgrading the runtime independently of Manta's lock and adapters.

## Branch naming

```text
codex/<short-description>
feature/<short-description>
fix/<short-description>
docs/<short-description>
```

## Pull request expectations

Every PR should include:

- a clear summary,
- test evidence (`pytest` + `ruff check .`),
- docs updates for config/schema or CLI changes,
- a note if the change affects how Manta launches or configures `deepagents-code`.

## Review gates

- At least one reviewer approval for source changes.
- No direct commits to main.

## GitHub releases

Manta is distributed through GitHub Releases and tagged source installs. There
is no PyPI publishing step. The release workflow builds a source distribution,
a wheel from that source, the qualified dependency constraints, release notes,
and SHA-256 checksums.

For a release:

1. Update `pyproject.toml` and `src/manta_code/__init__.py` to the same version,
   add `docs/releases/v<version>.md`, and update the changelog, README, install
   guide, and CI installation example to the same tag and constraints URL.
2. Install the locked development environment, run `pytest -q`, `ruff check .`,
   and dependency checks, then validate release assets locally:

   ```bash
   python -m pip install 'setuptools==84.0.0' 'wheel==0.48.0'
   python scripts/build_release.py --output-dir dist --tag v0.3.0
   ```

   Use an empty output directory; the builder rejects existing files so stale
   assets cannot enter the release.

3. Open a PR. CI runs Python 3.12 and 3.14 and exposes the aggregate `test`
   status required by branch protection. Obtain the required reviewer approval
   and merge the PR before tagging.
4. Tag the reviewed commit on `main` and push the tag. The tag-triggered release
   workflow verifies its version and main-branch ancestry, repeats CI, builds
   and validates assets, and publishes the GitHub release. If a draft release
   already exists for that tag, the workflow updates and publishes that draft.

Never move an existing published version tag or replace published assets with
different content. Correct a released package with a new patch version. Draft
releases may be prepared while the release PR is under review; their tags and
installation URLs become available when publication completes.
