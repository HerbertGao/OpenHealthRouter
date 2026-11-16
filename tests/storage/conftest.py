"""存储层测试夹具：用真实契约模型构造合成事件。"""

from __future__ import annotations

import copy
from collections.abc import Callable

import pytest

from custom_components.openhealthrouter.models import HealthEvent

BASE: dict = {
    "schemaVersion": "1.0",
    "subjectRef": "subject_local_1",
    "metric": "body.weight",
    "value": 72.4,
    "unit": "kg",
    "observedAt": {
        "start": "2026-11-12T08:00:00+08:00",
        "precision": "second",
        "timezone": "Asia/Shanghai",
    },
    "recordedAt": "2026-11-12T08:00:01+08:00",
    "source": {
        "kind": "ha_entity",
        "integration": "synthetic_scale",
        "entityId": "sensor.synthetic_scale_weight",
    },
    "provenance": {"type": "observation", "capturedBy": "device"},
    "quality": {"issues": []},
}


@pytest.fixture
def make_event() -> Callable[..., HealthEvent]:
    """构造合成事件；可用 overrides 改任意顶层字段（深拷贝，互不影响）。"""

    def factory(**overrides: object) -> HealthEvent:
        data = copy.deepcopy(BASE)
        data.update(overrides)
        return HealthEvent.model_validate(data)

    return factory
