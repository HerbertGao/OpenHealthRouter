"""契约检查入口。

第 0 期为占位：若 contracts/health-event.v1.schema.json 存在则校验其为合法
JSON，不存在则跳过。第 1 期替换为真实校验（Schema 生成 diff + golden vectors）。
"""

import json
import sys
from pathlib import Path

_SCHEMA_NAME = "health-event.v1.schema.json"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "contracts" / _SCHEMA_NAME


def main() -> int:
    if not SCHEMA_PATH.exists():
        print(f"SKIP: {_SCHEMA_NAME} 尚不存在（第 1 期契约变更交付）")
        return 0
    try:
        with SCHEMA_PATH.open(encoding="utf-8") as f:
            schema = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: {_SCHEMA_NAME} 无法解析: {exc}")
        return 1
    if not isinstance(schema, dict) or schema.get("$schema") is None:
        print(f"FAIL: {_SCHEMA_NAME} 不是合法的 JSON Schema 文档")
        return 1
    print(f"OK: {_SCHEMA_NAME} 存在且包含 $schema 声明")
    return 0


if __name__ == "__main__":
    sys.exit(main())
