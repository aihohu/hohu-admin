from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.ops import sync_git_mirror as mirror


def git(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def refs(repository: Path) -> dict[str, str]:
    output = git("ls-remote", "--refs", "--heads", "--tags", str(repository))
    return {ref: sha for sha, ref in (line.split() for line in output.splitlines())}


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    # Real local Git repositories, without network or user configuration/hooks.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    source = tmp_path / "source.git"
    target = tmp_path / "target.git"
    checkout = tmp_path / "checkout"
    scratch = tmp_path / "scratch"
    git("init", "--bare", str(source))
    git("init", "--bare", str(target))
    git("init", "--initial-branch=main", str(checkout))
    git("config", "user.name", "Mirror Test", cwd=checkout)
    git("config", "user.email", "mirror@example.invalid", cwd=checkout)
    (checkout / "README.md").write_text("initial\n", encoding="utf-8")
    git("add", "README.md", cwd=checkout)
    git("commit", "-m", "Initial", cwd=checkout)
    git("remote", "add", "origin", str(source), cwd=checkout)
    git("push", "origin", "main", cwd=checkout)
    return source, target, checkout, scratch


def sync(repositories, **options):
    source, target, _, scratch = repositories
    return mirror.sync_mirror(
        str(source), str(target), scratch, attempts=1, retry_delay=0, **options
    )


def test_initial_sync_preserves_history_branches_and_annotated_tags(repositories):
    source, target, checkout, scratch = repositories
    (checkout / "README.md").write_text("second\n", encoding="utf-8")
    git("commit", "-am", "Second", cwd=checkout)
    git("branch", "feature/example", cwd=checkout)
    git("tag", "-a", "v1.0", "-m", "Release", cwd=checkout)
    git("push", "origin", "--all", cwd=checkout)
    git("push", "origin", "--tags", cwd=checkout)

    snapshot = sync(repositories)

    assert snapshot == refs(source) == refs(target)
    assert git("--git-dir", str(target), "rev-list", "--count", "main") == "2"
    assert git("--git-dir", str(target), "cat-file", "-t", "refs/tags/v1.0") == "tag"
    assert list(scratch.iterdir()) == []


def test_force_updates_branch_and_replaced_tag(repositories):
    source, target, checkout, _ = repositories
    git("tag", "v1.0", cwd=checkout)
    git("push", "origin", "--tags", cwd=checkout)
    sync(repositories)
    original = refs(target)["refs/heads/main"]
    (checkout / "README.md").write_text("rewritten\n", encoding="utf-8")
    git("commit", "--amend", "-am", "Rewritten", cwd=checkout)
    git("tag", "--force", "v1.0", cwd=checkout)
    git("push", "--force", "origin", "main", "refs/tags/v1.0", cwd=checkout)

    sync(repositories)

    assert refs(target) == refs(source)
    assert refs(target)["refs/heads/main"] != original


def test_prunes_deleted_source_branches_tags_and_target_only_branch(repositories):
    source, target, checkout, _ = repositories
    git("branch", "obsolete", cwd=checkout)
    git("tag", "obsolete", cwd=checkout)
    git("push", "origin", "--all", cwd=checkout)
    git("push", "origin", "--tags", cwd=checkout)
    sync(repositories)
    git("--git-dir", str(target), "branch", "target-only", "main")
    git(
        "push",
        "origin",
        "--delete",
        "refs/heads/obsolete",
        "refs/tags/obsolete",
        cwd=checkout,
    )

    sync(repositories)

    assert refs(target) == refs(source)
    assert set(refs(target)) == {"refs/heads/main"}


def test_does_not_copy_platform_private_refs(repositories):
    source, target, checkout, _ = repositories
    sha = git("rev-parse", "HEAD", cwd=checkout)
    for ref in ("refs/pull/1/head", "refs/remotes/legacy/main", "refs/notes/example"):
        git("--git-dir", str(source), "update-ref", ref, sha)

    sync(repositories)

    assert git("--git-dir", str(target), "for-each-ref", "--format=%(refname)") == (
        "refs/heads/main"
    )


def test_source_fetch_failure_preserves_target_and_cleans_scratch(repositories):
    _, target, _, scratch = repositories
    sync(repositories)
    before = refs(target)

    with pytest.raises(mirror.MirrorError, match="failed after 1 attempt"):
        mirror.sync_mirror(
            str(scratch / "missing.git"), str(target), scratch, attempts=1
        )

    assert refs(target) == before
    assert list(scratch.iterdir()) == []


def test_empty_source_is_rejected_without_deleting_target(repositories):
    source, target, _, scratch = repositories
    sync(repositories)
    before = refs(target)
    git("--git-dir", str(source), "update-ref", "-d", "refs/heads/main")

    with pytest.raises(mirror.MirrorError, match="no branches"):
        sync(repositories)

    assert refs(target) == before
    assert list(scratch.iterdir()) == []


def test_retry_fetches_latest_source_instead_of_reusing_stale_snapshot(
    repositories, monkeypatch
):
    source, target, checkout, scratch = repositories
    original_git = mirror._git
    pushes = 0

    def interrupted_push(arguments, **options):
        nonlocal pushes
        if arguments[0] == "push":
            pushes += 1
            if pushes == 1:
                (checkout / "README.md").write_text("updated during retry\n")
                git("commit", "-am", "New source state", cwd=checkout)
                git("push", "origin", "main", cwd=checkout)
                raise subprocess.CalledProcessError(128, arguments)
        return original_git(arguments, **options)

    monkeypatch.setattr(mirror, "_git", interrupted_push)

    snapshot = mirror.sync_mirror(
        str(source), str(target), scratch, attempts=2, retry_delay=0
    )

    assert pushes == 2
    assert snapshot == refs(source) == refs(target)


def test_verification_failure_is_not_reported_as_success(repositories, monkeypatch):
    original_git = mirror._git

    def mismatched_target(arguments, **options):
        if arguments[0] == "ls-remote":
            return ""
        return original_git(arguments, **options)

    monkeypatch.setattr(mirror, "_git", mismatched_target)

    with pytest.raises(mirror.MirrorError, match="do not match"):
        sync(repositories)


def test_failed_push_stops_after_configured_attempts(repositories, monkeypatch):
    source, target, _, scratch = repositories
    original_git = mirror._git
    pushes = 0

    def failed_push(arguments, **options):
        nonlocal pushes
        if arguments[0] == "push":
            pushes += 1
            raise subprocess.CalledProcessError(128, arguments)
        return original_git(arguments, **options)

    monkeypatch.setattr(mirror, "_git", failed_push)

    with pytest.raises(mirror.MirrorError, match="failed after 2 attempts"):
        mirror.sync_mirror(str(source), str(target), scratch, attempts=2, retry_delay=0)

    assert pushes == 2
    assert refs(target) == {}
    assert list(scratch.iterdir()) == []


def test_target_probe_failure_retries_without_fetching_or_pushing(
    repositories, monkeypatch, caplog
):
    source, target, _, scratch = repositories
    original_git = mirror._git
    operations = []

    def unreachable_target(arguments, **options):
        operations.append(arguments[0])
        if arguments[0] == "ls-remote":
            raise subprocess.TimeoutExpired(arguments, options["timeout"])
        return original_git(arguments, **options)

    monkeypatch.setattr(mirror, "_git", unreachable_target)

    with pytest.raises(mirror.MirrorError, match="failed after 2 attempts"):
        mirror.sync_mirror(str(source), str(target), scratch, attempts=2, retry_delay=0)

    assert operations == ["init", "ls-remote", "ls-remote"]
    assert "target connection check" in caplog.text
    assert list(scratch.iterdir()) == []


def test_slow_push_has_separate_deadline_and_probe_precedes_transfer(
    repositories, monkeypatch
):
    source, target, _, scratch = repositories
    original_git = mirror._git
    operations = []

    def observed_git(arguments, **options):
        operations.append((arguments[0], options["timeout"]))
        return original_git(arguments, **options)

    monkeypatch.setattr(mirror, "_git", observed_git)

    snapshot = mirror.sync_mirror(
        str(source),
        str(target),
        scratch,
        attempts=1,
        command_timeout=30,
        push_timeout=600,
    )

    assert operations == [
        ("init", 30),
        ("ls-remote", 30),
        ("fetch", 30),
        ("for-each-ref", 30),
        ("push", 600),
        ("ls-remote", 30),
    ]
    assert snapshot == refs(source) == refs(target)


@pytest.mark.parametrize(
    "stderr", [b"SSH connection stalled", "SSH connection stalled"]
)
def test_git_timeout_preserves_transport_diagnostic(monkeypatch, caplog, stderr):
    def stalled_git(command, **options):
        raise subprocess.TimeoutExpired(command, options["timeout"], stderr=stderr)

    monkeypatch.setattr(mirror.subprocess, "run", stalled_git)

    with pytest.raises(subprocess.TimeoutExpired):
        mirror._git(["push", "git@gitee.com:example/mirror.git"], timeout=12)

    assert "Git push timed out after 12 seconds" in caplog.text
    assert "SSH connection stalled" in caplog.text


@pytest.mark.parametrize(
    "options",
    [
        {"attempts": 0},
        {"retry_delay": -1},
        {"command_timeout": 0},
        {"push_timeout": 0},
    ],
)
def test_invalid_limits_are_rejected_before_git_runs(repositories, options):
    source, target, _, scratch = repositories

    with pytest.raises(ValueError):
        mirror.sync_mirror(str(source), str(target), scratch, **options)

    assert not scratch.exists()


@pytest.mark.parametrize("source_exists", [True, False])
def test_workflow_cli_exit_status_reflects_verified_result(repositories, source_exists):
    source, target, _, scratch = repositories
    if not source_exists:
        source = scratch / "missing.git"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(mirror.__file__)),
            "--source",
            str(source),
            "--target",
            str(target),
            "--work-dir",
            str(scratch),
            "--attempts",
            "1",
            "--retry-delay",
            "0",
            "--command-timeout",
            "30",
            "--push-timeout",
            "60",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if source_exists:
        assert result.returncode == 0, result.stderr
        assert "Mirror verified" in result.stderr
        assert refs(target) == refs(source)
    else:
        assert result.returncode == 1
        assert "Mirror failed" in result.stderr
        assert refs(target) == {}
    assert list(scratch.iterdir()) == []
