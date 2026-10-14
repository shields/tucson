import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def workflows() -> dict[str, dict[str, Any]]:
    executable = shutil.which("bun")
    assert executable is not None, "Bun is required for workflow checks"
    return {
        path.name: json.loads(
            subprocess.check_output(
                [
                    executable,
                    "-e",
                    (
                        "const text = await Bun.file(process.argv.at(-1)).text();"
                        "console.log(JSON.stringify(Bun.YAML.parse(text)))"
                    ),
                    str(path),
                ],
                text=True,
                timeout=10,
            )
        )
        for path in (ROOT / ".github" / "workflows").glob("*.yml")
    }


def test_publication_requires_every_gate_in_the_same_source_revision_run() -> None:
    definitions = workflows()
    workflow = definitions["checks.yml"]
    assert workflow["on"]["push"] == {"branches": ["**"]}
    assert "pull_request" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    publish = workflow["jobs"]["publish"]
    assert set(publish["needs"]) == set(workflow["jobs"]) - {"publish"}
    assert publish["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    privileged = [
        (name, identifier)
        for name, definition in definitions.items()
        for identifier, job in definition["jobs"].items()
        if job.get("permissions", definition["permissions"]).get("packages") == "write"
    ]
    assert privileged == [("checks.yml", "publish")]
    for name, job in workflow["jobs"].items():
        checkout = next(
            step
            for step in job["steps"]
            if step.get("uses", "").startswith("actions/checkout@")
        )
        history = {"fetch-depth": 0} if name in ("image", "publish") else {}
        assert checkout["with"] == history | {"persist-credentials": False}
    steps = publish["steps"]
    download = next(
        step
        for step in steps
        if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert download["with"] == {
        "pattern": "image-*",
        "path": ".build/images",
        "merge-multiple": True,
    }
    version = next(step for step in steps if step.get("id") == "version")
    assert version["env"] == {"REVISION": "${{ github.sha }}"}
    assert version["run"].splitlines() == [
        'version=$(./scripts/gitcalver.sh "$REVISION")',
        'echo "version=$version" >> "$GITHUB_OUTPUT"',
    ]
    assert "shell" not in version
    assert steps[-1]["env"]["REVISION"] == "${{ github.sha }}"
    assert steps[-1]["env"]["RELEASE_TAG"] == "${{ steps.version.outputs.version }}"
    assert not any("make image" in step.get("run", "") for step in steps)


def test_native_container_matrix_exports_only_after_smoke_tests() -> None:
    workflow = workflows()["checks.yml"]
    image = workflow["jobs"]["image"]
    assert image["strategy"]["matrix"]["include"] == [
        {"arch": "amd64", "runner": "ubuntu-24.04"},
        {"arch": "arm64", "runner": "ubuntu-24.04-arm"},
    ]
    assert image["env"]["REVISION"] == "${{ github.sha }}"
    operations = [step["run"] for step in image["steps"] if "run" in step]
    assert operations[-3:] == ["make image", "make test-image", "make image-export"]
    assert image["steps"][-1]["with"]["if-no-files-found"] == "error"
    security = workflow["jobs"]["zizmor"]["steps"][-1]
    assert security["with"]["persona"] == "pedantic"
    assert security["with"]["advanced-security"] is False


def test_renovate_covers_runtime_pins_and_uses_platform_automerge() -> None:
    executable = shutil.which("bun")
    assert executable is not None
    data = json.loads(
        subprocess.check_output(
            [
                executable,
                "-e",
                """
import prettier from 'prettier';
const raw = await Bun.file('.github/renovate.json5').text();
const config = JSON.parse(await prettier.format(raw, {parser: 'json-stringify'}));
const matches = {};
for (const manager of config.customManagers) {
    const source = await Bun.file(manager.depNameTemplate === 'python'
        ? 'Dockerfile' : '.github/workflows/checks.yml').text();
    matches[manager.depNameTemplate] = manager.matchStrings.flatMap(pattern =>
        [...source.matchAll(new RegExp(pattern, 'g'))].map(match =>
            match.groups.currentValue));
}
console.log(JSON.stringify({config, matches}));
""",
            ],
            cwd=ROOT,
            text=True,
            timeout=10,
        )
    )
    assert data["matches"] == {
        "python": ["3.15.0"],
        "astral-sh/uv": ["0.13.0", "0.13.0"],
        "oven-sh/bun": ["1.4.3"],
        "zizmorcore/zizmor": ["1.30.1"],
        "moby/buildkit": ["v0.34.0"],
    }
    config = data["config"]
    assert config["platformAutomerge"] is True
    assert config["ignoreTests"] is False
    rules = config["packageRules"]
    assert any(
        "dependency-groups" in rule.get("matchDepTypes", []) and rule["automerge"]
        for rule in rules
    )
    assert rules[-1]["matchPackageNames"] == ["tinytuya"]
    assert rules[-1]["automerge"] is False
