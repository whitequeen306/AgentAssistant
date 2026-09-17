"""File operation tools: read_file, write_file, edit_file, list_files, move_file.

Per docs/06-tool-spec.md §1.1–1.5.

read_file understands document formats, not just plain text: PDF, Word,
Excel, PowerPoint, RTF are extracted to text (offset/limit paginate lines
of the extracted text exactly like plain files).
"""

from __future__ import annotations

import fnmatch
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.sanitize import sanitize_error

# Hard cap on chars extracted from one document — keeps the model's context
# sane on 300-page PDFs; offset/limit let the agent paginate further.
_DOC_EXTRACT_CHAR_CAP = 160_000


class ReadFileTool(Tool):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the content of a local file. Supports plain text (txt/md/code/"
            "json/csv/log), PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx) "
            "and RTF — document formats are auto-extracted to text (PDF pages "
            "are marked, Excel sheets/rows preserved). "
            "When to call: view file content, read papers/homework/notes/code/data. "
            "When NOT to call: reading web pages → use read_page; "
            "grabbing screen-selected text → use get_selection; "
            "listing a directory → use list_files. "
            "Returns the first `limit` lines from `offset`; call again with a "
            "larger offset to continue long documents."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="path", type="string", description="Absolute file path"),
            ToolParameter(name="offset", type="integer", description="Starting line (1-indexed), default 1", required=False, default=1),
            ToolParameter(name="limit", type="integer", description="Number of lines, default 2000", required=False, default=2000),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        path_str: str = kwargs.get("path", "")
        offset: int = kwargs.get("offset", 1)
        limit: int = kwargs.get("limit", 2000)

        if not path_str:
            return ToolResult.failure("parameter 'path' is required")

        p = Path(path_str)
        if not p.exists():
            return ToolResult.failure("file not found", code=404)
        if not p.is_file():
            return ToolResult.failure("path is not a file")

        try:
            text, meta = _extract_file_text(p)
        except PermissionError:
            return ToolResult.failure("permission denied", code=403)
        except _UnsupportedBinaryError as e:
            return ToolResult.failure(str(e), code=415, error_category="unsupported_format")
        except Exception as e:
            sanitized = sanitize_error(str(e), context="read_file")
            return ToolResult.failure(sanitized.safe_message)

        lines = text.splitlines()
        total = len(lines)
        start = max(0, offset - 1)
        end = start + limit
        selected = lines[start:end]

        return ToolResult.success(data={
            "content": "\n".join(selected),
            "total_lines": total,
            "showing": f"{start + 1}-{min(end, total)}",
            **meta,
        })


# ─── text vs binary detection: try utf-8 → gbk strict-decode on a sample;
# if neither fits and the utf-8 replace-decode is mostly U+FFFD, it's binary
# → refuse with a friendly message instead of dumping mojibake into the model.
def _detect_text_encoding(p: Path) -> str:
    head = p.open("rb").read(4096)
    for enc in ("utf-8", "gbk"):
        try:
            head.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    replaced = head.decode("utf-8", "replace").count("\ufffd")
    if len(head) and replaced / len(head) > 0.15:
        raise _UnsupportedBinaryError(
            f"'{p.name}' looks like a binary file this reader can't parse "
            f"(.{p.suffix.lstrip('.') or '?'}); no text extracted"
        )
    return "utf-8"


def _extract_file_text(p: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from any supported file. Returns (text, metadata)."""
    suffix = p.suffix.lower()

    extractor = _DOC_EXTRACTORS.get(suffix)
    if extractor is not None:
        text, meta = extractor(p)
        meta["format"] = suffix.lstrip(".")
        return text[:_DOC_EXTRACT_CHAR_CAP], meta

    # Text formats (incl. code/config) — read as text.
    encoding = _detect_text_encoding(p)
    text = p.read_text(encoding=encoding, errors="replace")
    return text, {"encoding": encoding}


class _UnsupportedBinaryError(Exception):
    pass


# Public aliases — practice room reuses the extraction pipeline.
UnsupportedBinaryError = _UnsupportedBinaryError


def extract_file_text(p: Path) -> tuple[str, dict[str, Any]]:
    """Public wrapper over the internal extraction pipeline (PDF/Word/Excel/
    PPT/RTF → text; plain text read with encoding detection)."""
    return _extract_file_text(p)


def _extract_pdf(p: Path) -> tuple[str, dict[str, Any]]:
    try:
        import fitz  # PyMuPDF

        pages_text: list[str] = []
        meta: dict[str, Any] = {}
        with fitz.open(p) as doc:
            meta["pages"] = doc.page_count
            for i, page in enumerate(doc, start=1):
                pages_text.append(f"── 第 {i} 页 ──\n{page.get_text('text')}")
                if sum(len(t) for t in pages_text) > _DOC_EXTRACT_CHAR_CAP:
                    meta["truncated"] = True
                    break
        return "\n\n".join(pages_text), meta
    except ImportError:
        from pypdf import PdfReader  # fallback

        reader = PdfReader(str(p))
        pages_text = [
            f"── 第 {i} 页 ──\n{page.extract_text() or ''}"
            for i, page in enumerate(reader.pages, start=1)
        ]
        return "\n\n".join(pages_text), {"pages": len(reader.pages)}


def _extract_docx(p: Path) -> tuple[str, dict[str, Any]]:
    import docx  # python-docx

    d = docx.Document(str(p))
    parts: list[str] = []
    for para in d.paragraphs:
        if para.text.strip():
            parts.append(para.text)
    for table in d.tables:
        rows = ["\t".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        parts.append("\n".join(rows))
    return "\n".join(parts), {"paragraphs": len(d.paragraphs), "tables": len(d.tables)}


def _extract_xlsx(p: Path) -> tuple[str, dict[str, Any]]:
    from openpyxl import load_workbook

    wb = load_workbook(str(p), read_only=True, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets:
        parts.append(f"── 工作表: {ws.title} ──")
        for row in ws.iter_rows(max_row=500, values_only=True):
            if any(v is not None for v in row):
                parts.append("\t".join("" if v is None else str(v) for v in row))
    wb.close()
    return "\n".join(parts), {"sheets": len(wb.sheetnames)}


def _extract_pptx(p: Path) -> tuple[str, dict[str, Any]]:
    from pptx import Presentation

    prs = Presentation(str(p))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame]
        if texts:
            parts.append(f"── 幻灯片 {i} ──\n" + "\n".join(t for t in texts if t.strip()))
    return "\n\n".join(parts), {"slides": len(prs.slides)}


def _extract_rtf(p: Path) -> tuple[str, dict[str, Any]]:
    raw = p.read_text(encoding="utf-8", errors="replace")
    # Crude RTF strip: drop groups/control words, keep readable text.
    text = re.sub(r"\\[a-z]+-?\d* ?", "", raw)
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, {}


_DOC_EXTRACTORS: dict[str, Callable[[Path], tuple[str, dict[str, Any]]]] = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".xlsm": _extract_xlsx,
    ".pptx": _extract_pptx,
    ".rtf": _extract_rtf,
}


class WriteFileTool(Tool):
    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Create a new file and write content. "
            "When to call: generate notes/config/code/reports as new files. "
            "When NOT to call: modifying an existing file → use edit_file; "
            "if the file already exists this tool errors. "
            "Parent directories are auto-created."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="path", type="string", description="Absolute file path"),
            ToolParameter(name="content", type="string", description="Full content to write"),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        path_str: str = kwargs.get("path", "")
        content: str = kwargs.get("content", "")

        if not path_str:
            return ToolResult.failure("parameter 'path' is required")

        p = Path(path_str)
        if p.exists():
            return ToolResult.failure("file exists, use edit_file to modify")

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except PermissionError:
            return ToolResult.failure("permission denied", code=403)
        except Exception as e:
            sanitized = sanitize_error(str(e), context="write_file")
            return ToolResult.failure(sanitized.safe_message)

        return ToolResult.success(data={"path": str(p), "bytes": len(content.encode("utf-8"))})


class EditFileTool(Tool):
    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Replace an exact string in an existing file. "
            "When to call: modify local content of code/config/notes. "
            "You must read_file first before calling. "
            "old_string must match exactly (including indentation/spaces). "
            "When NOT to call: full rewrite → use write_file; creating new → use write_file."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="path", type="string", description="Absolute file path"),
            ToolParameter(name="old_string", type="string", description="Exact text to replace"),
            ToolParameter(name="new_string", type="string", description="Replacement content"),
            ToolParameter(name="replaceAll", type="boolean", description="Replace all matches, default false", required=False, default=False),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        path_str: str = kwargs.get("path", "")
        old_string: str = kwargs.get("old_string", "")
        new_string: str = kwargs.get("new_string", "")
        replace_all: bool = kwargs.get("replaceAll", False)

        if not path_str:
            return ToolResult.failure("parameter 'path' is required")
        if not old_string:
            return ToolResult.failure("parameter 'old_string' is required")

        p = Path(path_str)
        if not p.exists():
            return ToolResult.failure("file not found", code=404)

        try:
            content = p.read_text(encoding="utf-8")
        except PermissionError:
            return ToolResult.failure("permission denied", code=403)

        count = content.count(old_string)
        if count == 0:
            return ToolResult.failure("old_string not found in file")
        if count > 1 and not replace_all:
            return ToolResult.failure(f"multiple matches ({count}), provide more context or set replaceAll=true")

        new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

        try:
            p.write_text(new_content, encoding="utf-8")
        except Exception as e:
            sanitized = sanitize_error(str(e), context="edit_file")
            return ToolResult.failure(sanitized.safe_message)

        return ToolResult.success(data={"replacements": count if replace_all else 1})


class ListFilesTool(Tool):
    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return (
            "List files/subdirectories in a directory. "
            "When to call: scanning before file classification, finding files, "
            "understanding directory structure. "
            "Typical next step: propose a classification, then move_file per file. "
            "When NOT to call: reading file content → use read_file. "
            "Returns names only, not content."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="path", type="string", description="Absolute directory path"),
            ToolParameter(name="pattern", type="string", description="Glob filter e.g. '*.txt' (optional)", required=False),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        path_str: str = kwargs.get("path", "")
        pattern: str = kwargs.get("pattern", "")

        if not path_str:
            return ToolResult.failure("parameter 'path' is required")

        p = Path(path_str)
        if not p.exists():
            return ToolResult.failure("directory not found", code=404)
        if not p.is_dir():
            return ToolResult.failure("path is not a directory")

        try:
            entries = []
            for item in sorted(p.iterdir()):
                if pattern and not fnmatch.fnmatch(item.name, pattern):
                    continue
                entries.append({
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                })
        except PermissionError:
            return ToolResult.failure("permission denied", code=403)

        return ToolResult.success(data={"path": str(p), "entries": entries, "count": len(entries)})


class MoveFileTool(Tool):
    @property
    def name(self) -> str:
        return "move_file"

    @property
    def description(self) -> str:
        return (
            "Move or rename a file. "
            "When to call: file classification, organizing desktop/downloads. "
            "Source must exist; if target exists this refuses to overwrite. "
            "When NOT to call: modifying file content → use edit_file. "
            "Call per-file for batch classification. "
            "Moves into system directories require user confirmation "
            "(framework permission policy)."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="src", type="string", description="Source absolute path"),
            ToolParameter(name="dst", type="string", description="Destination absolute path"),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        src_str: str = kwargs.get("src", "")
        dst_str: str = kwargs.get("dst", "")

        if not src_str or not dst_str:
            return ToolResult.failure("both 'src' and 'dst' are required")

        src = Path(src_str)
        dst = Path(dst_str)

        if not src.exists():
            return ToolResult.failure("src not found", code=404)
        if dst.exists():
            return ToolResult.failure("dst exists, won't overwrite")

        # System-dir destinations are ASK'd by PermissionPolicy in the registry.

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except PermissionError:
            return ToolResult.failure("permission denied (cross-drive or protected)", code=403)
        except Exception as e:
            sanitized = sanitize_error(str(e), context="move_file")
            return ToolResult.failure(sanitized.safe_message)

        return ToolResult.success(data={"moved": str(src), "to": str(dst)})
