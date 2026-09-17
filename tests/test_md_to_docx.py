"""markdown_to_docx —— 导出排版化 Word 的转换器测试。

用户诉求：导出到本地的 md 可读性极差，希望是「有结构、有排版」的 docx。
核心保命点：走程序转换而非模型生成——内容必须零漂移。
"""

from __future__ import annotations

import re

import pytest

from agent_assistant.export import markdown_to_docx

docx = pytest.importorskip("docx")

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _hyperlinks(d):
    return d.element.body.findall(f".//{_W_NS}hyperlink")


def _convert(tmp_path, md: str, **kw):
    out = tmp_path / "out.docx"
    markdown_to_docx(md, out, **kw)
    return docx.Document(str(out))


def _all_text(d) -> str:
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.extend(c.text for c in row.cells)
    return "\n".join(parts)


class TestBasic:
    def test_frontmatter_is_stripped(self, tmp_path):
        md = "---\ntitle: x\ndate: y\n---\n\n# 正文标题\n"
        d = _convert(tmp_path, md)
        texts = [p.text for p in d.paragraphs]
        assert "title: x" not in "\n".join(texts)
        assert any(p.text == "正文标题" for p in d.paragraphs)

    def test_headings_map_to_heading_styles(self, tmp_path):
        d = _convert(tmp_path, "# 大标题\n## 小标题\n正文")
        styles = [p.style.name for p in d.paragraphs if p.text.strip()]
        assert "Heading 1" in styles
        assert "Heading 2" in styles

    def test_inline_bold_italic_code(self, tmp_path):
        d = _convert(tmp_path, "这是 **粗体** 和 *斜体* 以及 `code` 混排")
        p = next(p for p in d.paragraphs if "粗体" in p.text)
        bolds = [r.text for r in p.runs if r.bold]
        assert "粗体" in bolds
        assert any(r.italic for r in p.runs)

    def test_links_become_real_hyperlinks(self, tmp_path):
        d = _convert(tmp_path, "来源：[哈工大研招网](https://yzb.hit.edu.cn/x)")
        assert len(_hyperlinks(d)) == 1
        all_text = "\n".join(p.text for p in d.paragraphs)
        # 链接文字保留，裸 URL 不进正文
        assert "哈工大研招网" in all_text
        assert "https://yzb.hit.edu.cn/x" not in all_text

    def test_content_zero_drift(self, tmp_path):
        """转换不得增删内容（链接 URL 除外——它们藏在 hyperlink 关系里）。"""
        md = "# A\n\n段落一 **重要**。\n\n- 甲\n- 乙\n"
        d = _convert(tmp_path, md)
        text = _all_text(d)
        for frag in ("A", "段落一 重要。", "甲", "乙"):
            assert frag in text


class TestTables:
    def test_table_extracted(self, tmp_path):
        md = "| 院校 | 人数 |\n|---|---|\n| 哈工大 | 132 |\n"
        d = _convert(tmp_path, md)
        assert len(d.tables) == 1
        t = d.tables[0]
        assert [c.text for c in t.rows[0].cells] == ["院校", "人数"]
        assert t.rows[1].cells[1].text == "132"

    def test_header_row_is_bold_and_shaded(self, tmp_path):
        md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        d = _convert(tmp_path, md)
        row = d.tables[0].rows[0]
        assert all(r.bold for c in row.cells for p in c.paragraphs for r in p.runs)

    def test_escaped_pipes_do_not_split_cells(self, tmp_path):
        md = "| 表达 |\n|---|\n| a\\|b |\n"
        d = _convert(tmp_path, md)
        assert d.tables[0].rows[1].cells[0].text == "a|b"

    def test_wide_table_switches_to_landscape(self, tmp_path):
        """10 列择校表在 A4 竖排必然挤成竖字 → 自动切横排。"""
        cols = "| c1 | c2 | c3 | c4 | c5 | c6 | c7 | c8 |"
        sep = "|---|" * 8
        row = "| 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |"
        md = f"{cols}\n{sep}\n{row}\n"
        d = _convert(tmp_path, md)
        from docx.enum.section import WD_ORIENT

        assert any(s.orientation == WD_ORIENT.LANDSCAPE for s in d.sections)

    def test_narrow_table_stays_portrait(self, tmp_path):
        md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        d = _convert(tmp_path, md)
        from docx.enum.section import WD_ORIENT

        assert all(s.orientation != WD_ORIENT.LANDSCAPE for s in d.sections)

    def test_pipe_line_without_separator_is_not_a_table(self, tmp_path):
        d = _convert(tmp_path, "a | b 只是普通文本\n")
        assert len(d.tables) == 0


class TestStructure:
    def test_lists_use_list_styles(self, tmp_path):
        d = _convert(tmp_path, "- 甲\n- 乙\n\n1. 一\n2. 二\n")
        styles = [p.style.name for p in d.paragraphs if p.text.strip()]
        assert "List Bullet" in styles
        assert "List Number" in styles

    def test_blockquote_rendered(self, tmp_path):
        d = _convert(tmp_path, "> 冲突时必须标红，禁止静默取值")
        assert any("冲突时必须标红" in p.text for p in d.paragraphs)

    def test_code_block_keeps_lines(self, tmp_path):
        d = _convert(tmp_path, "```\nline1\nline2\n```\n")
        text = "\n".join(p.text for p in d.paragraphs)
        assert "line1" in text and "line2" in text

    def test_hr_does_not_become_table_or_crash(self, tmp_path):
        d = _convert(tmp_path, "上文\n\n---\n\n下文\n")
        text = _all_text(d)
        assert "上文" in text and "下文" in text
        assert len(d.tables) == 0

    def test_core_properties_title(self, tmp_path):
        d = _convert(tmp_path, "# x\n", title="四校对比")
        assert d.core_properties.title == "四校对比"


class TestRealReport:
    def test_full_research_report_converts(self, tmp_path):
        """端到端：带 10 列表 + 64 个链接的真实调研报告。"""
        md = (
            "# 四校对比\n\n| 院校 | 专业(代码) | 院校层次 | 学科评估 | 招生人数(推免占比) "
            "| 初试科目(代码) | 参考书目 | 近三年复试线 | 复试权重 | 就业去向 |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
            "| [哈工大](https://yzb.hit.edu.cn) | 085400 | 985 | A | 未获取到 "
            "| 854 | 未获取到 | 310 | 50% | 未获取到 |\n\n"
            "## 二、关键差异解读\n\n- 初试科目差异 **408 vs 854**\n"
        )
        d = _convert(tmp_path, md, title="四校对比")
        assert len(d.tables) == 1
        assert d.tables[0].rows[1].cells[1].text == "085400"
        # 单元格刻意只留链接文字（纯文本），真超链接在正文段落里——
        # 否则长 URL 会把 10 列表的列宽全部撑爆。
        assert d.tables[0].rows[1].cells[0].text == "哈工大"
        assert "085400" in _all_text(d)
        # 数字没被改动
        assert re.search(r"310", _all_text(d))
