"""第 0 期 smoke test：验证骨架基本完整性。"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "custom_components" / "openhealthrouter" / "manifest.json"
SAMPLE_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "synthetic" / "scale-weight-normal.json"
)


def test_manifest_is_valid_json() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["domain"] == "openhealthrouter"
    assert manifest["requirements"] == ["pydantic>=2.12.2,<3"]


def test_synthetic_fixture_is_valid_json() -> None:
    sample = json.loads(SAMPLE_FIXTURE.read_text(encoding="utf-8"))
    assert sample["metric"] == "body.weight"
    assert sample["subjectRef"] == "subject_local_1"
