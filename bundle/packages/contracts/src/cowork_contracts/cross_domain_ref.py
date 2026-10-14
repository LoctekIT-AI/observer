"""跨业务域 trace/evidence 关联引用契约（MCP-TRACE-030）。

ADR-003 冻结首个跨域场景（RMA ↔ 销售订单）。本契约是**单向引用**：RMA 侧 Evidence
携带指向销售订单域的 origin/target 引用，不内联对方数据、不共享凭证、不建立第二套
权限事实源。

安全边界：契约字段只含引用与摘要 digest，不含原始数据/SQL/凭证/租户业务明细。
目标域不可用时引用降级为 ``unavailable``，不伪造成功。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CROSS_DOMAIN_REF_SCHEMA_VERSION = "cowork.cross-domain-ref.v1"

CrossDomainRefKind = Literal["metric_association", "dimension_association"]

ALLOWED_REF_KINDS = frozenset({"metric_association", "dimension_association"})


class CrossDomainRef(BaseModel):
    """一条单向跨域引用（origin 发起方 → target 被关联方）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["cowork.cross-domain-ref.v1"]
    ref_kind: CrossDomainRefKind
    origin: dict[str, str]
    target: dict[str, str]
    # 引用摘要 digest（origin+target 稳定哈希），供完整性校验；不含正文。
    digest: str = Field(min_length=64, max_length=64)

    @field_validator("origin", "target")
    @classmethod
    def require_known_keys(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {"trace_id", "run_ref", "evidence_ref", "domain"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"cross-domain ref contains unknown keys: {sorted(unknown)}")
        if not value.get("domain"):
            raise ValueError("cross-domain ref target must declare a domain")
        return value

    @field_validator("ref_kind")
    @classmethod
    def require_known_kind(cls, value: str) -> str:
        if value not in ALLOWED_REF_KINDS:
            raise ValueError(f"unknown ref_kind: {value}")
        return value


def parse_cross_domain_ref(payload: dict) -> CrossDomainRef:
    """schema mismatch 时 fail-closed（抛 pydantic ValidationError）。"""
    return CrossDomainRef.model_validate(payload)


__all__ = [
    "ALLOWED_REF_KINDS",
    "CROSS_DOMAIN_REF_SCHEMA_VERSION",
    "CrossDomainRef",
    "CrossDomainRefKind",
    "parse_cross_domain_ref",
]
