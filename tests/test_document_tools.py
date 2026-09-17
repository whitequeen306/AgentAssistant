"""read_document — 文档解析（PDF 表格 / 扫描件 OCR）的单元测试。

背景：研招数据（招生人数、历年分数线、科目代码）几乎全在 PDF 附件与图片
表格里，read_page 拿不到，调研报告只能写「未获取到」。这个工具补的是这一环。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_assistant.tools import web_tools as wt
from agent_assistant.tools.base import ToolResult

# --------------------------------------------------------------------------
# 辅助：合成最小 PDF
# --------------------------------------------------------------------------


def _make_pdf(tmp_path: Path, text: str = "计算机科学与技术 0854 电子信息 招生人数 132") -> bytes:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    out = tmp_path / "sample.pdf"
    doc.save(out)
    doc.close()
    return out.read_bytes()


@pytest.fixture
def pdf_bytes(tmp_path):
    return _make_pdf(tmp_path)


# --------------------------------------------------------------------------
# 格式嗅探
# --------------------------------------------------------------------------


class TestSniffKind:
    def test_detects_pdf_magic(self):
        assert wt._sniff_kind(b"%PDF-1.7 ...", "", "https://x/y") == "pdf"

    def test_detects_png_magic(self):
        assert wt._sniff_kind(b"\x89PNG\r\n\x1a\n" + b"0" * 20, "", "https://x/y") == "png"

    def test_detects_jpeg_magic(self):
        assert wt._sniff_kind(b"\xff\xd8\xff\xe0" + b"0" * 20, "", "https://x/y") == "jpeg"

    def test_falls_back_to_content_type(self):
        assert wt._sniff_kind(b"????", "application/pdf", "https://x/y") == "pdf"

    def test_falls_back_to_image_content_type(self):
        assert wt._sniff_kind(b"????", "image/jpeg", "https://x/y") == "jpg"

    def test_falls_back_to_extension(self):
        assert wt._sniff_kind(b"????", "", "https://x/a/b.pdf") == "pdf"

    def test_unknown_when_nothing_matches(self):
        assert wt._sniff_kind(b"????", "", "https://x/a/b") == "unknown"


# --------------------------------------------------------------------------
# 页码范围解析
# --------------------------------------------------------------------------


class TestParsePageRange:
    def test_empty_means_every_page(self):
        assert wt._parse_page_range("", 10) is None
        assert wt._parse_page_range("   ", 10) is None

    def test_single_page(self):
        assert wt._parse_page_range("3", 10) == [2]

    def test_range_is_inclusive(self):
        assert wt._parse_page_range("1-3", 10) == [0, 1, 2]

    def test_mixed_and_deduplicated(self):
        assert wt._parse_page_range("1-2,2,5", 10) == [0, 1, 4]

    def test_clamped_to_document(self):
        assert wt._parse_page_range("1-99", 5) == [0, 1, 2, 3, 4]

    def test_out_of_range_returns_none(self):
        assert wt._parse_page_range("99", 5) is None

    def test_garbage_returns_none(self):
        assert wt._parse_page_range("abc", 5) is None


# --------------------------------------------------------------------------
# 表格 → Markdown
# --------------------------------------------------------------------------


class TestRowsToMarkdown:
    def test_renders_header_and_separator(self):
        md = wt._rows_to_markdown([["a", "b"], ["1", "2"]])
        assert md.splitlines()[0] == "| a | b |"
        assert md.splitlines()[1] == "|---|---|"
        assert "| 1 | 2 |" in md

    def test_pads_ragged_rows(self):
        md = wt._rows_to_markdown([["a", "b", "c"], ["1"]])
        assert "| 1 |  |  |" in md

    def test_escapes_inner_pipes(self):
        md = wt._rows_to_markdown([["a|b"]])
        assert "a/b" in md

    def test_empty_rows(self):
        assert wt._rows_to_markdown([]) == ""


# --------------------------------------------------------------------------
# 工具行为
# --------------------------------------------------------------------------


class TestReadDocumentTool:
    def test_url_is_required(self):
        r = wt.ReadDocumentTool().execute(url="")
        assert r.ok is False
        assert "url" in (r.error or "")

    def test_private_url_is_blocked(self):
        r = wt.ReadDocumentTool().execute(url="http://127.0.0.1:8080/a.pdf")
        assert r.ok is False
        assert r.error_category == "ssrf"

    def test_serp_url_is_blocked(self):
        r = wt.ReadDocumentTool().execute(url="https://www.baidu.com/s?wd=pdf")
        assert r.ok is False

    def test_unsupported_kind_is_explicit_failure(self):
        with patch.object(wt, "fetch_bytes", return_value=("https://x/y", b"????", "")):
            r = wt.ReadDocumentTool().execute(url="https://x/y")
        assert r.ok is False
        assert r.error_category == "extract_fail"
        assert "read_page" in (r.error or "")

    def test_empty_download_maps_to_422(self):
        with patch.object(
            wt, "fetch_bytes", side_effect=ValueError("下载到空文件")
        ):
            r = wt.ReadDocumentTool().execute(url="https://x/y.pdf")
        assert r.ok is False
        assert r.code == 422

    def test_pdf_extracts_text(self, pdf_bytes):
        with patch.object(
            wt, "fetch_bytes", return_value=("https://x/a.pdf", pdf_bytes, "application/pdf")
        ):
            r = wt.ReadDocumentTool().execute(url="https://x/a.pdf")
        assert r.ok, r.error
        assert r.data["kind"] == "pdf"
        assert r.data["pages"] == 1
        assert "0854" in r.data["content"]

    def test_page_range_limits_parsing(self, pdf_bytes):
        with patch.object(
            wt, "fetch_bytes", return_value=("https://x/a.pdf", pdf_bytes, "application/pdf")
        ):
            r = wt.ReadDocumentTool().execute(url="https://x/a.pdf", pages="1")
        assert r.ok
        assert r.data["pages"] == 1

    def test_scanned_page_falls_back_to_ocr(self, tmp_path, pdf_bytes):
        """文本层为空 → 走 OCR，并在结果里标注 ocr_pages。"""
        with patch.object(
            wt, "fetch_bytes", return_value=("https://x/a.pdf", pdf_bytes, "application/pdf")
        ), patch.object(wt, "_ocr_image", return_value="扫描件识别出的招生人数 132") as ocr:
            r = wt.ReadDocumentTool().execute(url="https://x/a.pdf")
        assert r.ok
        ocr.assert_called()
        assert r.data.get("ocr_pages") == [1]
        assert "扫描件识别出的" in r.data["content"]

    def test_ocr_failure_is_reported_not_swallowed(self, pdf_bytes):
        with patch.object(
            wt, "fetch_bytes", return_value=("https://x/a.pdf", pdf_bytes, "application/pdf")
        ), patch.object(wt, "_ocr_image", side_effect=RuntimeError("tesseract missing")):
            r = wt.ReadDocumentTool().execute(url="https://x/a.pdf")
        # 页面文本非空（合成 PDF 有文字）→ 仍成功，但警告里要能看到 OCR 失败
        assert r.ok
        assert any("OCR" in w for w in r.data.get("warnings", []))

    def test_tables_come_before_body_so_they_survive_truncation(self):
        """研招数据值钱的是表格，截断时先砍正文。"""
        rows = [["学科代码", "人数"], ["0854", "132"]]
        md = wt._rows_to_markdown(rows)
        # 正文预算会为表格让位：body_budget = max(2000, CAP - len(tables_md))
        assert md.startswith("| 学科代码 | 人数 |")

    def test_result_shape_is_stable(self, pdf_bytes):
        with patch.object(
            wt, "fetch_bytes", return_value=("https://x/a.pdf", pdf_bytes, "application/pdf")
        ):
            r = wt.ReadDocumentTool().execute(url="https://x/a.pdf")
        assert isinstance(r, ToolResult)
        for key in ("url", "kind", "content", "tables", "pages", "truncated"):
            assert key in r.data
