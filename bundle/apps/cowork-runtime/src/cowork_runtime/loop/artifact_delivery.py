"""User-facing terminal summaries for files created by a Runtime run."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from cowork_runtime.contracts import ArtifactRef
from cowork_runtime.contracts.enums import ArtifactAccessKind


def build_artifact_delivery_summary(artifacts: Iterable[ArtifactRef]) -> str:
    """Describe persisted artifacts without exposing remote object URLs or credentials."""

    unique_artifacts: list[ArtifactRef] = []
    artifact_ids: set[str] = set()
    for artifact in artifacts:
        if artifact.artifact_id in artifact_ids:
            continue
        artifact_ids.add(artifact.artifact_id)
        unique_artifacts.append(artifact)

    if not unique_artifacts:
        return ""

    lines = ["已生成交付文件："]
    for artifact in unique_artifacts:
        lines.append(f"- {artifact.title.strip() or '未命名文件'}")
        local_path = _local_artifact_path(artifact)
        if local_path:
            lines.append(f"  路径：{local_path}")
        formatted_size = _format_size(artifact.size_bytes)
        if formatted_size:
            lines.append(f"  大小：{formatted_size}")
    return "\n".join(lines)


def append_artifact_delivery_summary(content: str, summary: str) -> str:
    """Keep a model's final explanation while making the delivery address explicit."""

    final_content = content.strip()
    if not summary or summary in final_content:
        return final_content
    return f"{final_content}\n\n{summary}" if final_content else summary


def _local_artifact_path(artifact: ArtifactRef) -> str | None:
    if artifact.access.kind is not ArtifactAccessKind.LOCAL_FILE:
        return None
    if artifact.access.local_path and artifact.access.local_path.strip():
        return artifact.access.local_path.strip()
    if not artifact.access.uri:
        return None
    parsed = urlsplit(artifact.access.uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    return url2pathname(unquote(parsed.path)) or None


def _format_size(size_bytes: int | None) -> str | None:
    if size_bytes is None:
        return None
    if size_bytes < 1024:
        return f"{size_bytes} B"
    value = float(size_bytes)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return None
