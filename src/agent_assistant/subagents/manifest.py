"""Organizer move manifest: canonicalization, hashing, validation, apply.

The manifest is the ONLY thing an approval covers. Every operation carries the
source file's fingerprint (size + mtime_ns) captured at planning time; apply
revalidates jail membership, fingerprint, destination absence, and the
manifest hash immediately before each move. Nothing is ever deleted or
overwritten — conflicts default to skip.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import stat as stat_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_assistant.tools.permission import path_in_allowed_roots

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
APPROVAL_TTL_HOURS = 24


class ManifestError(ValueError):
    """Invalid or unsafe manifest input (caller-safe message)."""


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag) or stat_module.S_ISLNK(metadata.st_mode)


def _fingerprint(path: Path) -> dict[str, int]:
    info = path.stat()
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _operation_id(src: str, dst: str) -> str:
    digest = hashlib.sha256(f"{src}\n{dst}".encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass(slots=True)
class ManifestBuild:
    """Canonicalization result: safe operations + skipped proposals."""

    operations: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def canonicalize_moves(proposals: list[dict[str, Any]]) -> ManifestBuild:
    """Validate raw model-proposed moves into canonical operations.

    Unsafe or impossible proposals are not errors — they land in ``skipped``
    with a reason so the user sees the full picture at approval time.
    """
    build = ManifestBuild()
    seen_sources: set[str] = set()
    seen_destinations: set[str] = set()
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        raw_src = str(proposal.get("src") or "").strip()
        raw_dst = str(proposal.get("dst") or "").strip()
        if not raw_src or not raw_dst:
            build.skipped.append({"src": raw_src, "dst": raw_dst, "reason": "路径缺失"})
            continue
        try:
            src = Path(raw_src).expanduser().resolve(strict=True)
        except OSError:
            build.skipped.append({"src": raw_src, "dst": raw_dst, "reason": "源文件不存在"})
            continue
        if not src.is_file() or _is_reparse(src):
            build.skipped.append(
                {"src": str(src), "dst": raw_dst, "reason": "源不是普通文件"}
            )
            continue
        dst = Path(raw_dst).expanduser()
        try:
            dst = dst.resolve(strict=False)
        except OSError:
            build.skipped.append({"src": str(src), "dst": raw_dst, "reason": "目标路径无效"})
            continue
        if not path_in_allowed_roots(str(src)) or not path_in_allowed_roots(str(dst)):
            build.skipped.append(
                {"src": str(src), "dst": str(dst), "reason": "超出允许目录范围"}
            )
            continue
        if dst.exists():
            build.skipped.append(
                {"src": str(src), "dst": str(dst), "reason": "目标已存在（不覆盖）"}
            )
            continue
        src_text = str(src)
        dst_text = str(dst)
        if src_text.casefold() == dst_text.casefold():
            build.skipped.append({"src": src_text, "dst": dst_text, "reason": "源与目标相同"})
            continue
        if src_text.casefold() in seen_sources:
            build.skipped.append({"src": src_text, "dst": dst_text, "reason": "重复的源文件"})
            continue
        if dst_text.casefold() in seen_destinations:
            build.skipped.append({"src": src_text, "dst": dst_text, "reason": "重复的目标路径"})
            continue
        try:
            fingerprint = _fingerprint(src)
        except OSError:
            build.skipped.append({"src": src_text, "dst": dst_text, "reason": "无法读取源文件"})
            continue
        seen_sources.add(src_text.casefold())
        seen_destinations.add(dst_text.casefold())
        build.operations.append(
            {
                "operation_id": _operation_id(src_text, dst_text),
                "src": src_text,
                "dst": dst_text,
                "source_fingerprint": fingerprint,
                "conflict": "skip",
            }
        )
    return build


def build_manifest(task_id: str, goal: str, build: ManifestBuild) -> dict[str, Any]:
    operations = sorted(build.operations, key=lambda op: op["operation_id"])
    now = datetime.now(timezone.utc)
    manifest = {
        "version": MANIFEST_VERSION,
        "task_id": task_id,
        "goal": goal[:400],
        "operations": operations,
        "skipped": build.skipped,
        "created_at": now.isoformat(),
        "approval_expires_at": (now + timedelta(hours=APPROVAL_TTL_HOURS)).isoformat(),
    }
    return manifest


def manifest_hash(manifest: dict[str, Any]) -> str:
    """Stable hash over the approval-relevant core (version/task/operations)."""
    core = {
        "version": manifest.get("version"),
        "task_id": manifest.get("task_id"),
        "operations": sorted(
            manifest.get("operations") or [], key=lambda op: op.get("operation_id", "")
        ),
    }
    canonical = json.dumps(
        core, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def approval_expired(manifest: dict[str, Any], now: datetime | None = None) -> bool:
    raw = str(manifest.get("approval_expires_at") or "")
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if expires.tzinfo is None:
        return True
    return (now or datetime.now(timezone.utc)) >= expires


@dataclass(slots=True)
class ApplyReport:
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"applied": self.applied, "skipped": self.skipped}


def apply_manifest(manifest: dict[str, Any], expected_hash: str) -> ApplyReport:
    """Execute exactly the approved operations; revalidate each one first."""
    if manifest_hash(manifest) != expected_hash:
        raise ManifestError("整理计划与批准内容不一致，已中止")
    report = ApplyReport()
    for operation in manifest.get("operations") or []:
        src_text = str(operation.get("src") or "")
        dst_text = str(operation.get("dst") or "")
        entry = {"src": src_text, "dst": dst_text}

        src = Path(src_text)
        dst = Path(dst_text)
        # Revalidate everything the approval was based on.
        if not path_in_allowed_roots(src_text) or not path_in_allowed_roots(dst_text):
            report.skipped.append({**entry, "reason": "超出允许目录范围"})
            continue
        try:
            if _is_reparse(src) or not src.is_file():
                report.skipped.append({**entry, "reason": "源不是普通文件"})
                continue
            current = _fingerprint(src)
        except OSError:
            report.skipped.append({**entry, "reason": "源文件已不存在"})
            continue
        recorded = operation.get("source_fingerprint") or {}
        if (
            current.get("size") != recorded.get("size")
            or current.get("mtime_ns") != recorded.get("mtime_ns")
        ):
            report.skipped.append({**entry, "reason": "源文件在批准后被修改"})
            continue
        if dst.exists():
            report.skipped.append({**entry, "reason": "目标已存在（不覆盖）"})
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src_text, dst_text)
        except OSError:
            report.skipped.append({**entry, "reason": "移动失败"})
            continue
        report.applied.append(
            {
                "operation_id": operation.get("operation_id"),
                "src": src_text,
                "dst": dst_text,
                "moved_fingerprint": recorded,
            }
        )
    return report


def rollback_applied(applied: list[dict[str, Any]]) -> ApplyReport:
    """Move applied files back. Refuses overwrite and externally modified files."""
    report = ApplyReport()
    for entry in reversed(applied or []):
        src_text = str(entry.get("src") or "")  # original location
        dst_text = str(entry.get("dst") or "")  # current location
        item = {"src": dst_text, "dst": src_text}
        dst = Path(dst_text)
        original = Path(src_text)
        if not dst.is_file():
            report.skipped.append({**item, "reason": "文件已不在整理后位置"})
            continue
        recorded = entry.get("moved_fingerprint") or {}
        try:
            current = _fingerprint(dst)
        except OSError:
            report.skipped.append({**item, "reason": "无法读取文件"})
            continue
        if current.get("size") != recorded.get("size"):
            report.skipped.append({**item, "reason": "文件在整理后被修改"})
            continue
        if original.exists():
            report.skipped.append({**item, "reason": "原位置已被占用（不覆盖）"})
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dst_text, src_text)
        except OSError:
            report.skipped.append({**item, "reason": "移动失败"})
            continue
        report.applied.append({"src": dst_text, "dst": src_text})
    return report
