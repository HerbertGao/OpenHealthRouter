"""Golden vectors 参数化测试（tasks 4.2/4.3）。

accept：经 Pydantic 实例化，精确匹配 event_id 与 issues；
reject：断言 ValidationError 且消息包含 expected_error_contains（若声明）；
双跑（4.3）：accept 向量的 input 另经独立 jsonschema 对照生成 Schema 复验。
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from custom_components.openhealthrouter.models import HealthEvent

_REPO_ROOT = Path(__file__).resolve().parents[2]
VECTORS_DIR = _REPO_ROOT / "tests" / "fixtures" / "synthetic" / "vectors"
SCHEMA_PATH = _REPO_ROOT / "contracts" / "health-event.v1.schema.json"


def _load_vectors() -> list[dict[str, Any]]:
    vectors = []
    for path in sorted(VECTORS_DIR.glob("*.json")):
        vectors.append(json.loads(path.read_text(encoding="utf-8")))
    return vectors


VECTORS = _load_vectors()
ACCEPT_VECTORS = [v for v in VECTORS if v["expect"] == "accept"]


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_vector(vector: dict[str, Any]) -> None:
    if vector["expect"] == "accept":
        event = HealthEvent.model_validate(vector["input"])
        assert event.event_id == vector["expected_id"], (
            f"{vector['name']}: event_id 与锁定值不一致"
        )
        assert event.quality.issues == vector["expected_issues"], (
            f"{vector['name']}: issues {event.quality.issues} "
            f"!= {vector['expected_issues']}"
        )
    else:
        with pytest.raises(ValidationError) as exc_info:
            HealthEvent.model_validate(vector["input"])
        contains = vector.get("expected_error_contains")
        if contains:
            assert contains in str(exc_info.value), (
                f"{vector['name']}: 错误消息不含 {contains!r}"
            )


@pytest.mark.parametrize("vector", ACCEPT_VECTORS, ids=lambda v: v["name"])
def test_accept_vector_passes_generated_schema(vector: dict[str, Any]) -> None:
    """双跑：accept 向量经独立 Schema 校验器对照生成 Schema 复验（design D7）。

    注意：不得启用 FormatChecker——date/datetime anyOf 与 precision 联动属注册表
    类规则，不在 JSON Schema 表达范围（design D7 显式声明）。
    """
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=vector["input"], schema=schema)
