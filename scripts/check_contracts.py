"""契约检查入口：HealthEvent JSON Schema 生成与漂移检查。

用法：
    uv run python scripts/check_contracts.py          # 默认：生成到内存逐字节 diff
    uv run python scripts/check_contracts.py --fix    # 重新生成并写入 contracts/

模型变更后必须运行 --fix 并提交生成物；CI 中默认模式不一致即失败（design D6）。
"""

import argparse
import json
import sys
from pathlib import Path

SCHEMA_NAME = "health-event.v1.schema.json"
_REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = _REPO_ROOT / "contracts" / SCHEMA_NAME
_SCHEMA_DOLLAR = "https://json-schema.org/draft/2020-12/schema"


def _load_model():
    """延迟导入，保证 --help 等路径不依赖 pydantic 环境。"""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from custom_components.openhealthrouter.models import HealthEvent

    return HealthEvent


def render_schema() -> bytes:
    """由模型生成 Schema：camelCase 属性名、注入 $schema、固定序列化（design D6）。"""
    model = _load_model()
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = _SCHEMA_DOLLAR
    text = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def cmd_fix() -> int:
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_bytes(render_schema())
    print(f"OK: 已生成 {SCHEMA_NAME}")
    return 0


def cmd_check() -> int:
    if not SCHEMA_PATH.exists():
        print(f"FAIL: {SCHEMA_NAME} 不存在，请先运行 --fix")
        return 1
    committed = SCHEMA_PATH.read_bytes()
    generated = render_schema()
    if committed != generated:
        print(
            f"FAIL: {SCHEMA_NAME} 与模型不一致（可能已过期或手改），"
            "请运行 `uv run python scripts/check_contracts.py --fix` 并提交生成物"
        )
        return 1
    print(f"OK: {SCHEMA_NAME} 与模型一致")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="重新生成并写入 contracts/（默认只检查漂移）",
    )
    args = parser.parse_args(argv)
    return cmd_fix() if args.fix else cmd_check()


if __name__ == "__main__":
    sys.exit(main())
