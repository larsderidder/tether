"""Release builds must consume the SDK source covered by verification jobs."""

import re
from pathlib import Path

import yaml


def test_release_build_uses_the_verified_codex_sdk() -> None:
    workflow_path = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert {"test", "test-sidecars", "test-ui"}.issubset(jobs["build"]["needs"])
    assert re.fullmatch(r"[a-f0-9]{40}", workflow["env"]["CODEX_SDK_REF"])
    checkouts = []
    for job in ("test-sidecars", "build"):
        sdk = [
            step["with"]
            for step in jobs[job]["steps"]
            if step.get("with", {}).get("repository") == "openai/codex"
        ]
        assert len(sdk) == 1
        checkouts.append(sdk[0])
    assert checkouts[0] == checkouts[1]
    assert checkouts[0]["ref"] == "${{ env.CODEX_SDK_REF }}"
    assert checkouts[0]["path"] == "codex-src"
