import hashlib
import json
import os
import shutil
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REVISION = "1" * 40
VERSION = "20261014.12"
# Pinned so make never runs git in the temporary directory.
IMAGE_OVERRIDES = (f"REVISION={REVISION}", "SOURCE_DATE_EPOCH=0")
GITCALVER_SHA256 = "7f16ff978449aba9c6b5303ba160b365fb2cc891dd6a756ac196c54e61abec8c"
CRANE = """
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ['CRANE_CALLS'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
mode = os.environ.get('CRANE_MODE', '')
if args[0] == mode:
    raise SystemExit(2)
if args[0] == 'config':
    architecture = args[1].rsplit('-', 1)[1]
    revision = os.environ['REVISION']
    version = os.environ['RELEASE_TAG']
    if mode == 'wrong_revision':
        revision = '0' * 40
    if mode == 'wrong_version':
        version = '19990101.1'
    if mode == 'wrong_architecture':
        architecture = 'other'
    print(json.dumps({'architecture': architecture, 'os': 'linux',
        'config': {'Labels': {'org.opencontainers.image.revision': revision,
            'org.opencontainers.image.version': version}}}))
elif args[0] == 'digest':
    if mode == 'conflict' and ':sha-' in args[1]:
        print('sha256:' + '0' * 64)
    elif mode == 'conflict_version' and args[1].endswith(
            ':' + os.environ['RELEASE_TAG']):
        print('sha256:' + '0' * 64)
    else:
        print('sha256:' + '1' * 64)
elif args[0] == 'ls':
    print('sha-' + os.environ['REVISION'])
    print(os.environ['RELEASE_TAG'])
"""
TOOL = """
import json
import os
import sys
with open(os.environ['TOOL_CALLS'], 'a') as stream:
    stream.write(json.dumps([os.path.basename(sys.argv[0]),
        os.environ.get('TUCSON_VERSION', ''), *sys.argv[1:]]) + '\\n')
"""


def isolated_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name != "VERSION" and not name.startswith(("GIT_", "TUCSON_"))
    } | {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_DATE": "2026-01-02T12:00:00+00:00",
    }


def install(directory: Path, name: str, source: str) -> None:
    executable = directory / name
    executable.write_text(f"#!{sys.executable}\n{source}")
    executable.chmod(0o755)


def git(repository: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    return subprocess.run(
        [executable, *arguments],
        cwd=repository,
        env=isolated_environment(),
        stdout=subprocess.PIPE,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()


@pytest.fixture
def release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, str]]:
    install(tmp_path, "crane", CRANE)
    directory = tmp_path / ".build" / "images"
    directory.mkdir(parents=True)
    for architecture in ("amd64", "arm64"):
        (directory / f"{architecture}.tar").touch()
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    environment = os.environ | {
        "CRANE_CALLS": str(tmp_path / "calls.jsonl"),
        "IMAGE_REPOSITORY": "example.invalid/tucson",
        "RELEASE_TAG": VERSION,
        "RUN_SUFFIX": "test-1",
        "REVISION": REVISION,
    }
    return tmp_path, environment


@pytest.fixture
def tools(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    for name in ("docker", "uv"):
        install(tmp_path, name, TOOL)
    return tmp_path, isolated_environment() | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TOOL_CALLS": str(tmp_path / "calls.jsonl"),
    }


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    directory = tmp_path / "repository"
    shutil.copytree(ROOT / "scripts", directory / "scripts")
    git(directory, "init", "--initial-branch=main")
    git(directory, "add", "scripts")
    git(directory, "commit", "--message", "first")
    git(directory, "commit", "--allow-empty", "--message", "second")
    return directory


def make(
    target: str, directory: Path, environment: dict[str, str], *overrides: str
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("make")
    assert executable is not None
    return subprocess.run(
        [executable, "--file", str(ROOT / "Makefile"), target, *overrides],
        cwd=directory,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )


def publish(
    directory: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return make("release", directory, environment)


def calls(directory: Path) -> list[list[str]]:
    path = directory / "calls.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def build_arguments(directory: Path) -> list[tuple[str, str]]:
    [(tool, _, *arguments)] = calls(directory)
    assert tool == "docker"
    return list(pairwise(arguments))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("RELEASE_TAG", "v1.2.3"),
        ("RELEASE_TAG", "20261014"),
        ("RELEASE_TAG", "20261014.0"),
        ("RELEASE_TAG", "20261014.01"),
        ("RELEASE_TAG", "2026101.1"),
        ("RELEASE_TAG", "202610145.1"),
        ("RELEASE_TAG", "2026101411"),
        ("RELEASE_TAG", "20261014.1.2"),
        ("RELEASE_TAG", "20261014.1-dev.abc1234"),
        ("RELEASE_TAG", "20261014.1;exit 0"),
        ("RELEASE_TAG", f"{VERSION}\nmain"),
        ("REVISION", "short"),
        ("REVISION", REVISION[:-1]),
        ("REVISION", f"{REVISION}1"),
        ("REVISION", f"{REVISION}\nshort"),
        ("RUN_SUFFIX", ""),
        ("IMAGE_REPOSITORY", ""),
    ],
)
def test_release_rejects_invalid_identity_before_registry_operations(
    field: str, value: str, release: tuple[Path, dict[str, str]]
) -> None:
    directory, environment = release
    result = publish(directory, environment | {field: value})
    assert result.returncode != 0
    assert calls(directory) == []


def test_release_requires_both_archives_before_registry_operations(
    release: tuple[Path, dict[str, str]],
) -> None:
    directory, environment = release
    (directory / ".build" / "images" / "arm64.tar").unlink()
    assert publish(directory, environment).returncode != 0
    assert calls(directory) == []


def test_release_treats_tag_contents_as_data(
    release: tuple[Path, dict[str, str]],
) -> None:
    directory, environment = release
    injection_marker = directory / "injected"
    result = publish(
        directory,
        environment | {"RELEASE_TAG": f"{VERSION}$(touch {injection_marker})"},
    )
    assert result.returncode != 0
    assert not injection_marker.exists()
    assert calls(directory) == []


@pytest.mark.parametrize(
    "mode",
    [
        "push",
        "config",
        "index",
        "digest",
        "ls",
        "wrong_revision",
        "wrong_version",
        "wrong_architecture",
        "conflict",
        "conflict_version",
    ],
)
def test_release_failure_never_promotes_version_tags(
    mode: str, release: tuple[Path, dict[str, str]]
) -> None:
    directory, environment = release
    assert publish(directory, environment | {"CRANE_MODE": mode}).returncode != 0
    assert not any(command[0] == "tag" for command in calls(directory))


@pytest.mark.parametrize(
    ("mode", "found"),
    [
        ("wrong_revision", "0" * 40),
        ("wrong_version", "19990101.1"),
        ("wrong_architecture", "other"),
    ],
)
def test_release_reports_the_mismatched_image(
    mode: str, found: str, release: tuple[Path, dict[str, str]]
) -> None:
    directory, environment = release
    result = publish(directory, environment | {"CRANE_MODE": mode})
    assert result.returncode != 0
    assert "build-test-1-amd64" in result.stdout
    assert found in result.stdout


def test_release_promotes_matching_images_and_allows_an_identical_rerun(
    release: tuple[Path, dict[str, str]],
) -> None:
    directory, environment = release
    assert publish(directory, environment).returncode == 0
    tags = [command[-1] for command in calls(directory) if command[0] == "tag"]
    assert tags == [f"sha-{REVISION}", VERSION]


@pytest.mark.parametrize("target", ["image", "test-image"])
def test_image_targets_refuse_an_empty_version_before_running_tools(
    target: str, tools: tuple[Path, dict[str, str]]
) -> None:
    directory, environment = tools
    result = make(target, directory, environment, "VERSION=", *IMAGE_OVERRIDES)
    assert result.returncode != 0
    assert calls(directory) == []


def test_image_build_stamps_the_version(tools: tuple[Path, dict[str, str]]) -> None:
    directory, environment = tools
    result = make(
        "image", directory, environment, f"VERSION={VERSION}", *IMAGE_OVERRIDES
    )
    assert result.returncode == 0
    assert ("--build-arg", f"VERSION={VERSION}") in build_arguments(directory)


@pytest.mark.parametrize("state", ["clean", "dirty"])
def test_image_build_derives_the_default_version_from_history(
    state: str, repository: Path, tools: tuple[Path, dict[str, str]]
) -> None:
    directory, environment = tools
    expected = "20260102.2"
    if state == "dirty":
        (repository / "untracked").touch()
        expected += f"-dev.{git(repository, 'rev-parse', 'HEAD')[:7]}"
    assert make("image", repository, environment).returncode == 0
    assert ("--build-arg", f"VERSION={expected}") in build_arguments(directory)


def test_image_tests_receive_the_version(tools: tuple[Path, dict[str, str]]) -> None:
    directory, environment = tools
    result = make(
        "test-image", directory, environment, f"VERSION={VERSION}", *IMAGE_OVERRIDES
    )
    assert result.returncode == 0
    [(tool, version, *_)] = calls(directory)
    assert (tool, version) == ("uv", VERSION)


def test_vendored_gitcalver_is_the_published_release_asset() -> None:
    script = ROOT / "scripts" / "gitcalver.sh"
    assert hashlib.sha256(script.read_bytes()).hexdigest() == GITCALVER_SHA256
    assert os.access(script, os.X_OK)
