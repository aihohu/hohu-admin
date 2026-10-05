"""Mirror a fresh Git source snapshot, limited to branches and tags."""

from __future__ import annotations

import argparse
import logging
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

logger = logging.getLogger(__name__)


class MirrorError(RuntimeError):
    """The source snapshot could not be mirrored and verified."""


def _git(arguments: list[str], *, cwd: Path | None = None, timeout: float = 120) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as error:
        if error.stderr:
            logger.error("%s", error.stderr.strip())
        raise
    except subprocess.TimeoutExpired as error:
        logger.error("Git %s timed out after %s seconds", arguments[0], timeout)
        if error.stderr:
            diagnostic = error.stderr
            if isinstance(diagnostic, bytes):
                diagnostic = diagnostic.decode("utf-8", errors="replace")
            logger.error("%s", diagnostic.strip())
        raise
    return result.stdout.strip()


def _refs(output: str) -> dict[str, str]:
    return {ref: sha for sha, ref in (line.split() for line in output.splitlines())}


def sync_mirror(
    source: str,
    target: str,
    work_dir: Path,
    *,
    attempts: int = 3,
    retry_delay: float = 5,
    command_timeout: float = 120,
    push_timeout: float = 600,
) -> dict[str, str]:
    """Force/prune only heads and tags, then verify their exact object IDs.

    Each attempt fetches the current source again. A failed/empty fetch never
    reaches the push. Scratch repositories are isolated and always cleaned up.
    Authentication is supplied by Git/SSH environment, never embedded in URLs.
    """
    if attempts < 1 or retry_delay < 0 or command_timeout <= 0 or push_timeout <= 0:
        raise ValueError("Invalid attempt, retry delay, or command timeout limit")

    work_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mirror-", dir=work_dir) as temporary:
        repository = Path(temporary)
        _git(["init", "--bare", str(repository)], timeout=command_timeout)
        for attempt in range(1, attempts + 1):
            stage = "target connection check"
            try:
                logger.info(
                    "Attempt %d/%d: checking target connection", attempt, attempts
                )
                _git(
                    ["ls-remote", "--refs", "--heads", "--tags", target],
                    cwd=repository,
                    timeout=command_timeout,
                )
                stage = "source fetch"
                logger.info("Fetching current source snapshot")
                _git(
                    [
                        "fetch",
                        "--no-tags",
                        "--prune",
                        source,
                        "+refs/heads/*:refs/heads/*",
                        "+refs/tags/*:refs/tags/*",
                    ],
                    cwd=repository,
                    timeout=command_timeout,
                )
                stage = "source snapshot read"
                snapshot = _refs(
                    _git(
                        [
                            "for-each-ref",
                            "--format=%(objectname) %(refname)",
                            "refs/heads/",
                            "refs/tags/",
                        ],
                        cwd=repository,
                        timeout=command_timeout,
                    )
                )
                if not any(ref.startswith("refs/heads/") for ref in snapshot):
                    raise MirrorError("Source has no branches; refusing to push")
                stage = "target push"
                logger.info(
                    "Pushing %d refs (timeout %s seconds)", len(snapshot), push_timeout
                )
                _git(
                    [
                        "push",
                        "--prune",
                        target,
                        "+refs/heads/*:refs/heads/*",
                        "+refs/tags/*:refs/tags/*",
                    ],
                    cwd=repository,
                    timeout=push_timeout,
                )
                stage = "target verification"
                logger.info("Verifying target branches and tags")
                actual = _refs(
                    _git(
                        ["ls-remote", "--refs", "--heads", "--tags", target],
                        cwd=repository,
                        timeout=command_timeout,
                    )
                )
                if actual != snapshot:
                    raise MirrorError(
                        "Target branches or tags do not match the source snapshot"
                    )
                for ref, sha in sorted(snapshot.items()):
                    logger.info("Verified %s %s", ref, sha)
                logger.info("Mirror verified: %d refs", len(snapshot))
                return snapshot
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                MirrorError,
            ) as error:
                if attempt == attempts:
                    raise MirrorError(
                        f"Mirror failed after {attempts} attempts during {stage}: {error}"
                    ) from error
                logger.warning(
                    "Attempt %d failed during %s: %s; retrying with a fresh source",
                    attempt,
                    stage,
                    error,
                )
                time.sleep(retry_delay)
    raise AssertionError("Mirror attempts exhausted without a result")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=5)
    parser.add_argument("--command-timeout", type=float, default=120)
    parser.add_argument("--push-timeout", type=float, default=600)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        sync_mirror(
            arguments.source,
            arguments.target,
            arguments.work_dir,
            attempts=arguments.attempts,
            retry_delay=arguments.retry_delay,
            command_timeout=arguments.command_timeout,
            push_timeout=arguments.push_timeout,
        )
    except (MirrorError, OSError, ValueError) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
