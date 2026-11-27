"""A partial or altered private panel must fail before either engine runs."""

import json
from unittest.mock import AsyncMock

import pytest

from scripts.conformance import m1_replay


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["partial", "extra", "unhashed", "symlink"])
async def test_replay_checks_inventory_and_integrity_before_execution(tmp_path, monkeypatch, mutation):
    run = AsyncMock()
    monkeypatch.setattr(m1_replay, "replay_frozen_case", run)
    manifest = {
        "schema": "m1-frozen-after-retrieval-v1",
        "question_ids": list(range(1, 99)),
        "files": {f"q{i}.json": "0" * 64 for i in range(1, 99)},
    }
    if mutation == "partial":
        manifest["question_ids"].pop()
    elif mutation == "extra":
        manifest["files"]["extra.json"] = "0" * 64
    elif mutation == "unhashed":
        (tmp_path / "q1.json").write_text("{}")
    else:
        target = tmp_path / "elsewhere.json"
        target.write_text("{}")
        (tmp_path / "q1.json").symlink_to(target)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(AssertionError):
        await m1_replay.replay(tmp_path)
    run.assert_not_awaited()
