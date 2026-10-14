"""Deterministic checks for explicitly modular development deliveries.

The model remains responsible for choosing component boundaries. Runtime only
checks durable facts observed from successful file/tool receipts, so prose
cannot claim a multi-file implementation that never happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModularDeliveryVerdict:
    satisfied: bool
    reason: str
    changed_files: tuple[str, ...] = ()
    minimum_changed_files: int = 1


def evaluate_modular_delivery(metadata: Mapping[str, Any]) -> ModularDeliveryVerdict:
    raw = metadata.get("module_constraints")
    if not isinstance(raw, Mapping) or raw.get("development_mode") != "modular":
        return ModularDeliveryVerdict(True, "not_required")
    minimum = _bounded_int(raw.get("minimum_changed_files"), default=2)
    changed = _changed_local_files(metadata)
    if len(changed) < minimum:
        return ModularDeliveryVerdict(
            False,
            "modular_structure_unsatisfied",
            changed_files=tuple(changed),
            minimum_changed_files=minimum,
        )
    return ModularDeliveryVerdict(True, "satisfied", changed_files=tuple(changed), minimum_changed_files=minimum)


def modular_delivery_instruction(verdict: ModularDeliveryVerdict) -> str:
    files = "、".join(verdict.changed_files) if verdict.changed_files else "暂无"
    return (
        "当前任务要求采用分组、模块化开发，但目前只观察到 "
        f"{len(verdict.changed_files)} 个有实际职责的文件发生变化，至少需要 "
        f"{verdict.minimum_changed_files} 个。当前已变化的文件：{files}。\n"
        "请先检查项目结构，按不同职责拆分组件、组合式函数、样式或工具文件，"
        "完成必要修改并运行相关验证后再回复。不要反复覆盖同一个入口文件。"
    )


def _changed_local_files(metadata: Mapping[str, Any]) -> list[str]:
    raw = metadata.get("changed_file_uris")
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value:
            continue
        parsed = urlsplit(value)
        if parsed.scheme and parsed.scheme != "file":
            continue
        canonical = value.split("?", 1)[0].rstrip("/")
        if canonical and canonical not in result:
            result.append(canonical)
    return result


def _bounded_int(value: object, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(2, min(32, number))


__all__ = ["ModularDeliveryVerdict", "evaluate_modular_delivery", "modular_delivery_instruction"]
