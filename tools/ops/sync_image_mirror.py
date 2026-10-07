"""Copy a GHCR digest to ACR and verify a complete anonymous OCI pull."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

logger = logging.getLogger(__name__)
SOURCES = {"ghcr.io/aihohu/hohu-admin", "ghcr.io/aihohu/hohu-admin-web"}
REQUIRED_PLATFORMS = {"linux/amd64", "linux/arm64"}


class ImageMirrorError(RuntimeError):
    """A pinned image could not be copied and anonymously verified."""


def test_tag(sha: str, run_id: str, run_attempt: str) -> str:
    """Generate a unique test tag without publishing a release alias."""
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", sha) or not all(
        re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, run_attempt)
    ):
        raise ValueError("Invalid commit SHA or workflow run identifier")
    tag = f"test-{sha[:12]}-{run_id}-{run_attempt}"
    _validate_tags([tag])
    return tag


def validate_target(source: str, registry: str, repository: str) -> str:
    """Limit writes to a matching application repository on an ACR domain."""
    if source not in SOURCES:
        raise ValueError(
            "Source must be an official hohu-admin or hohu-admin-web image"
        )
    if not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+aliyuncs\.com", registry
    ):
        raise ValueError("ACR_REGISTRY must be an Alibaba Cloud public hostname")
    basename = source.rsplit("/", 1)[1]
    if not re.fullmatch(
        rf"[a-z0-9]+(?:[._-][a-z0-9]+)*/{re.escape(basename)}", repository
    ):
        raise ValueError(f"ACR_REPOSITORY must be namespace/{basename}")
    return f"{registry}/{repository}"


def _validate_tags(tags: list[str]) -> None:
    if not tags or not all(
        re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag) for tag in tags
    ):
        raise ValueError("At least one valid image tag is required")


def parse_tags(source: str, text: str) -> list[str]:
    """Read only metadata tags belonging to the pinned source repository."""
    references = text.splitlines()
    if not references or not all(
        value.startswith(f"{source}:") for value in references
    ):
        raise ValueError("Tags must be full references to the configured GHCR image")
    tags = list(dict.fromkeys(value[len(source) + 1 :] for value in references))
    _validate_tags(tags)
    return tags


def _run(arguments: list[str], *, timeout: float) -> bytes:
    try:
        result = subprocess.run(
            ["skopeo", *arguments],
            stdin=subprocess.DEVNULL,
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        logger.error(
            "Skopeo %s failed (timeout limit %s seconds)", arguments[0], timeout
        )
        if error.stderr:
            diagnostic = error.stderr
            if isinstance(diagnostic, bytes):
                diagnostic = diagnostic.decode("utf-8", errors="replace")
            logger.error("%s", diagnostic.strip())
        raise
    return result.stdout


def _inspect(reference: str, timeout: float, *, anonymous: bool = False) -> bytes:
    arguments = ["inspect", "--raw", "--tls-verify=true"]
    if anonymous:
        arguments.append("--no-creds")
    return _run([*arguments, f"docker://{reference}"], timeout=timeout)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _verify_index(raw: bytes, digest: str) -> list[str]:
    if _digest(raw) != digest:
        raise ImageMirrorError("Manifest digest differs from the pinned build digest")
    index = json.loads(raw)
    platforms = {
        f"{entry.get('platform', {}).get('os')}/{entry.get('platform', {}).get('architecture')}"
        for entry in index.get("manifests", [])
    }
    missing = REQUIRED_PLATFORMS - platforms
    if missing:
        raise ImageMirrorError(
            f"Missing required image platforms: {', '.join(sorted(missing))}"
        )
    return sorted(REQUIRED_PLATFORMS)


def _anonymous_pull(target: str, digest: str, work_dir: Path, timeout: float) -> None:
    with TemporaryDirectory(prefix="anonymous-pull-", dir=work_dir) as temporary:
        _run(
            [
                "copy",
                "--all",
                "--preserve-digests",
                "--src-no-creds",
                "--src-tls-verify=true",
                f"docker://{target}@{digest}",
                f"oci:{Path(temporary).as_posix()}/image:verified",
            ],
            timeout=timeout,
        )


def _sync_tag(
    source: str, target: str, tag: str, state: dict, work_dir: Path, limits: dict
) -> bool:
    digest = state["digest"]
    for attempt in range(1, limits["attempts"] + 1):
        stage = "source inspection"
        try:
            if not state["platforms"]:
                state["platforms"] = _verify_index(
                    _inspect(f"{source}@{digest}", limits["command_timeout"]), digest
                )
            if (
                _digest(_inspect(f"{source}:{tag}", limits["command_timeout"]))
                != digest
            ):
                logger.warning(
                    "Skipped %s: source tag has moved to a different digest", tag
                )
                return False
            stage = "target copy"
            logger.info(
                "Copying %s to %s:%s (attempt %d)", digest, target, tag, attempt
            )
            _run(
                [
                    "copy",
                    "--all",
                    "--preserve-digests",
                    "--src-tls-verify=true",
                    "--dest-tls-verify=true",
                    f"docker://{source}@{digest}",
                    f"docker://{target}:{tag}",
                ],
                timeout=limits["copy_timeout"],
            )
            stage = "anonymous manifest verification"
            _verify_index(
                _inspect(f"{target}:{tag}", limits["command_timeout"], anonymous=True),
                digest,
            )
            if not state["anonymous_pull"]:
                stage = "anonymous blob pull"
                _anonymous_pull(target, digest, work_dir, limits["copy_timeout"])
                state["anonymous_pull"] = True
            logger.info("Verified %s:%s @%s", target, tag, digest)
            return True
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            ImageMirrorError,
            ValueError,
        ) as error:
            if attempt == limits["attempts"]:
                raise ImageMirrorError(
                    f"Image mirror failed after {attempt} attempts during {stage}: {error}"
                ) from error
            logger.warning(
                "Attempt %d failed during %s: %s; retrying", attempt, stage, error
            )
            time.sleep(limits["retry_delay"])
    raise AssertionError("Image mirror attempts exhausted without a result")


def sync_image(
    source: str,
    target: str,
    digest: str,
    tags: list[str],
    work_dir: Path,
    *,
    attempts: int = 3,
    retry_delay: float = 15,
    command_timeout: float = 120,
    copy_timeout: float = 600,
) -> dict:
    """Copy a digest once built, preserving all architectures and public access."""
    registry, repository = target.split("/", 1)
    validate_target(source, registry, repository)
    _validate_tags(tags)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("A SHA256 build digest is required")
    if (
        attempts < 1
        or retry_delay < 0
        or command_timeout <= 0
        or copy_timeout <= 0
        or not all(
            math.isfinite(value)
            for value in (retry_delay, command_timeout, copy_timeout)
        )
    ):
        raise ValueError("Invalid retry or timeout limits")
    work_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "source": source,
        "target": target,
        "digest": digest,
        "platforms": [],
        "verified_tags": [],
        "skipped_tags": [],
        "anonymous_pull": False,
    }
    limits = {
        "attempts": attempts,
        "retry_delay": retry_delay,
        "command_timeout": command_timeout,
        "copy_timeout": copy_timeout,
    }
    for tag in dict.fromkeys(tags):
        verified = _sync_tag(source, target, tag, result, work_dir, limits)
        result["verified_tags" if verified else "skipped_tags"].append(tag)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tag_parser = commands.add_parser("test-tag")
    for name in ("sha", "run-id", "run-attempt"):
        tag_parser.add_argument(f"--{name}", required=True)
    for name in ("validate", "sync"):
        command = commands.add_parser(name)
        for field in ("source", "registry", "repository"):
            command.add_argument(f"--{field}", required=True)
        if name == "sync":
            command.add_argument("--digest", required=True)
            for field in ("tags-file", "work-dir", "report"):
                command.add_argument(f"--{field}", required=True, type=Path)
            command.add_argument("--attempts", type=int, default=3)
            command.add_argument("--retry-delay", type=float, default=15)
            command.add_argument("--command-timeout", type=float, default=120)
            command.add_argument("--copy-timeout", type=float, default=600)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if arguments.command == "test-tag":
            print(test_tag(arguments.sha, arguments.run_id, arguments.run_attempt))
        else:
            target = validate_target(
                arguments.source, arguments.registry, arguments.repository
            )
            if arguments.command == "sync":
                tags = parse_tags(
                    arguments.source, arguments.tags_file.read_text(encoding="utf-8")
                )
                result = sync_image(
                    arguments.source,
                    target,
                    arguments.digest,
                    tags,
                    arguments.work_dir,
                    attempts=arguments.attempts,
                    retry_delay=arguments.retry_delay,
                    command_timeout=arguments.command_timeout,
                    copy_timeout=arguments.copy_timeout,
                )
                arguments.report.parent.mkdir(parents=True, exist_ok=True)
                arguments.report.write_text(
                    json.dumps(result, indent=2) + "\n", encoding="utf-8"
                )
    except (ImageMirrorError, OSError, ValueError) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
