"""Execute release gates locally without contacting or mutating GitHub."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def run_step(script, directory, **env):
    return subprocess.run(
        ["bash", "-e", "-c", script], cwd=directory, capture_output=True, text=True,
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}", **env},
    )


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path):
    checkout, remote = tmp_path / "checkout", tmp_path / "remote.git"
    checkout.mkdir()
    git(checkout, "init", "--quiet", "--initial-branch=main")
    git(checkout, "config", "user.email", "release-test@example.invalid")
    git(checkout, "config", "user.name", "Release Test")
    (checkout / "source").write_text("before release")
    git(checkout, "add", "source")
    git(checkout, "commit", "--quiet", "-m", "initial")
    git(checkout, "branch", "pre-merge-draft")
    (checkout / "source").write_text("qualified release")
    git(checkout, "commit", "--quiet", "-am", "release")
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    git(checkout, "remote", "add", "origin", str(remote))
    git(checkout, "push", "--quiet", "origin", "main")
    return checkout


@pytest.mark.parametrize("annotated", [False, True])
def test_release_tag_peels_to_reviewed_main_commit(repository, tmp_path, annotated):
    commit = git(repository, "rev-parse", "HEAD")
    args = ["tag", "v0.3.0"] + (["--annotate", "--message", "Manta v0.3.0"] if annotated else [])
    git(repository, *args)
    git(repository, "push", "--quiet", "origin", "refs/tags/v0.3.0")
    git(repository, "checkout", "--quiet", "--detach", "v0.3.0")
    step = workflow("release.yml")["jobs"]["release"]["steps"][1]
    output = tmp_path / "github-env"
    result = run_step(step["run"], repository, RELEASE_TAG="v0.3.0", GITHUB_ENV=str(output))
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"RELEASE_COMMIT={commit}\n"


def test_release_tag_outside_main_is_rejected(repository, tmp_path):
    git(repository, "checkout", "--quiet", "-b", "unreviewed")
    (repository / "source").write_text("unreviewed work")
    git(repository, "commit", "--quiet", "-am", "not merged")
    git(repository, "tag", "v0.3.0")
    git(repository, "push", "--quiet", "origin", "refs/tags/v0.3.0")
    step = workflow("release.yml")["jobs"]["release"]["steps"][1]
    result = run_step(step["run"], repository, RELEASE_TAG="v0.3.0", GITHUB_ENV=str(tmp_path / "github-env"))
    assert result.returncode != 0


@pytest.mark.parametrize("matrix", ["success", "failure", "cancelled", "skipped"])
@pytest.mark.parametrize("assets", ["success", "failure", "cancelled", "skipped"])
def test_required_test_aggregate_only_passes_complete_success(tmp_path, matrix, assets):
    ci = workflow("ci.yml")
    gate = ci["jobs"]["test"]
    assert gate["name"] == "test"
    assert set(gate["needs"]) == {"tests", "release-assets"}
    assert gate["if"] == "${{ always() }}"
    result = run_step(gate["steps"][0]["run"], tmp_path, MATRIX_RESULT=matrix, ASSET_RESULT=assets)
    assert (result.returncode == 0) is (matrix == assets == "success")


@pytest.fixture
def publisher(repository, tmp_path):
    """Fake only GitHub; execute shell, Git identity checks, and hashes as shipped."""
    commit = git(repository, "rev-parse", "HEAD")
    git(repository, "tag", "--annotate", "v0.3.0", "--message", "Manta v0.3.0")
    git(repository, "push", "--quiet", "origin", "refs/tags/v0.3.0")
    scripts = tmp_path / "bin"
    scripts.mkdir()
    state = tmp_path / "github-state.json"
    log = tmp_path / "github-operations.jsonl"
    gh = scripts / "gh"
    gh.write_text(f"#!{sys.executable}\n" + r'''
import json
import os
from pathlib import Path
import sys
args = sys.argv[1:]
path = Path(os.environ["GH_TEST_STATE"])
state = json.loads(path.read_text())
with open(os.environ["GH_TEST_LOG"], "a") as output:
    output.write(json.dumps(args) + "\n")
def option(name):
    return args[args.index(name) + 1]
if args[:2] == ["release", "view"]:
    if state is None:
        sys.exit(1)
    print(json.dumps(state))
elif args[:2] == ["release", "create"]:
    assert state is None
    state = {"isDraft": True, "targetCommitish": option("--target")}
elif args[:2] == ["release", "edit"]:
    assert state["isDraft"], "Cannot mutate a published release"
    if "--target" in args and not os.environ.get("GH_TEST_IGNORE_TARGET"):
        state["targetCommitish"] = option("--target")
    if "--draft=false" in args:
        state["isDraft"] = False
elif args[:2] == ["release", "upload"]:
    assert state["isDraft"], "Cannot mutate published assets"
else:
    raise AssertionError(args)
path.write_text(json.dumps(state))
''')
    gh.chmod(0o755)
    dist = repository / "dist"
    dist.mkdir()
    for name in ("manta_code-0.3.0-py3-none-any.whl", "manta_code-0.3.0.tar.gz", "requirements-dev.lock", "release-notes.md"):
        (dist / name).write_text(name)
    checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in sorted(dist.iterdir()))
    (dist / "SHA256SUMS").write_text(checksums)
    script = workflow("release.yml")["jobs"]["release"]["steps"][-1]["run"]

    def publish(release, ignore_target=False):
        state.write_text(json.dumps(release))
        log.write_text("")
        result = run_step(
            script, repository, PATH=f"{scripts}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
            GH_TEST_STATE=str(state), GH_TEST_LOG=str(log),
            GH_TEST_IGNORE_TARGET="1" if ignore_target else "", RELEASE_TAG="v0.3.0", RELEASE_COMMIT=commit,
        )
        operations = [json.loads(line) for line in log.read_text().splitlines()]
        return result, json.loads(state.read_text()), operations

    return publish, commit


@pytest.mark.parametrize("existing_draft", [False, True])
def test_publish_creates_or_retargets_draft_before_upload(publisher, existing_draft):
    publish, commit = publisher
    draft = {"isDraft": True, "targetCommitish": "pre-merge-draft"} if existing_draft else None
    result, state, operations = publish(draft)
    assert result.returncode == 0, result.stderr
    assert state == {"isDraft": False, "targetCommitish": commit}
    assert any(args[:2] == ["release", "upload"] for args in operations)
    assert operations[-1][:2] == ["release", "edit"]
    assert "--draft=false" in operations[-1]


@pytest.mark.parametrize("optimized", [False, True])
def test_published_release_rerun_never_changes_metadata_or_assets(publisher, monkeypatch, optimized):
    if optimized:
        monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    publish, commit = publisher
    released = {"isDraft": False, "targetCommitish": commit}
    result, state, operations = publish(released)
    assert result.returncode != 0
    assert state == released
    assert [args[:2] for args in operations] == [["release", "view"]]


def test_stale_draft_target_is_rejected_when_github_ignores_update(publisher):
    publish, _ = publisher
    result, state, operations = publish({"isDraft": True, "targetCommitish": "pre-merge-draft"}, ignore_target=True)
    assert result.returncode != 0
    assert state["isDraft"] is True
    assert not any(args[:2] == ["release", "upload"] for args in operations)


def test_draft_main_target_resolves_remote_branch_on_detached_tag_checkout(publisher, repository):
    publish, _ = publisher
    git(repository, "checkout", "--quiet", "--detach", "v0.3.0")
    git(repository, "branch", "--delete", "--force", "main")
    result, state, operations = publish({"isDraft": True, "targetCommitish": "main"}, ignore_target=True)
    assert result.returncode == 0, result.stderr
    assert state["isDraft"] is False
    assert any(args[:2] == ["release", "upload"] for args in operations)


def test_remote_tag_moved_after_checkout_blocks_all_github_mutations(publisher, repository):
    publish, _ = publisher
    git(repository, "push", "--quiet", "--force", "origin", "refs/heads/pre-merge-draft:refs/tags/v0.3.0")
    draft = {"isDraft": True, "targetCommitish": "main"}
    result, state, operations = publish(draft)
    assert result.returncode != 0
    assert state == draft
    assert operations == []


def test_tag_publication_waits_for_reusable_ci_and_queues_duplicate_runs():
    release = workflow("release.yml")
    assert release["jobs"]["checks"]["uses"] == "./.github/workflows/ci.yml"
    assert release["jobs"]["release"]["needs"] == "checks"
    assert release["concurrency"]["cancel-in-progress"] == "false"
    assert release["on"]["push"]["tags"] == ["v*"]
