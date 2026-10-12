import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REVISION = "1" * 40
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
    if mode == 'wrong_revision':
        revision = '0' * 40
    if mode == 'wrong_architecture':
        architecture = 'other'
    print(json.dumps({'architecture': architecture, 'os': 'linux',
        'config': {'Labels': {'org.opencontainers.image.revision': revision}}}))
elif args[0] == 'digest':
    if mode == 'conflict' and ':sha-' in args[1]:
        print('sha256:' + '0' * 64)
    else:
        print('sha256:' + '1' * 64)
elif args[0] == 'ls':
    print('sha-' + os.environ['REVISION'])
    print('v1.2.3')
"""


@pytest.fixture
def release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, str]]:
    executable = tmp_path / "crane"
    executable.write_text(f"#!{sys.executable}\n{CRANE}")
    executable.chmod(0o755)
    directory = tmp_path / ".build" / "images"
    directory.mkdir(parents=True)
    for architecture in ("amd64", "arm64"):
        (directory / f"{architecture}.tar").touch()
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    environment = os.environ | {
        "CRANE_CALLS": str(tmp_path / "calls.jsonl"),
        "IMAGE_REPOSITORY": "example.invalid/tucson",
        "RELEASE_TAG": "v1.2.3",
        "RUN_SUFFIX": "test-1",
        "REVISION": REVISION,
    }
    return tmp_path, environment


def publish(
    directory: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("make")
    assert executable is not None
    return subprocess.run(
        [executable, "--file", str(ROOT / "Makefile"), "release"],
        cwd=directory,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )


def calls(directory: Path) -> list[list[str]]:
    path = directory / "calls.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("RELEASE_TAG", "v1;exit 0"),
        ("RELEASE_TAG", "main"),
        ("REVISION", "short"),
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
        environment | {"RELEASE_TAG": f"v1.2.3$(touch {injection_marker})"},
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
        "wrong_architecture",
        "conflict",
    ],
)
def test_release_failure_never_promotes_version_tags(
    mode: str, release: tuple[Path, dict[str, str]]
) -> None:
    directory, environment = release
    assert publish(directory, environment | {"CRANE_MODE": mode}).returncode != 0
    assert not any(command[0] == "tag" for command in calls(directory))


def test_release_promotes_matching_images_and_allows_an_identical_rerun(
    release: tuple[Path, dict[str, str]],
) -> None:
    directory, environment = release
    assert publish(directory, environment).returncode == 0
    tags = [command[-1] for command in calls(directory) if command[0] == "tag"]
    assert tags == [f"sha-{REVISION}", "v1.2.3"]
