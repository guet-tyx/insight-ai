"""pdf_parser 单元测试：标题树语义分块 / 元数据 / 超长切分 / 空文档。"""
from __future__ import annotations

import pytest

from app.services.pdf_parser import _split_overflow, parse_pdf_bytes


def test_heading_tree_chunking(sample_pdf_bytes: bytes) -> None:
    chunks = parse_pdf_bytes(sample_pdf_bytes, doc_id="doc-abc")
    # 3 个 H2 章节标题应产生至少 3 个语义块
    assert len(chunks) >= 3
    headers = {c.parent_header for c in chunks if c.parent_header}
    assert any("第一章 系统架构" in h for h in headers)
    assert any("第二章 知识库 Pipeline" in h for h in headers)
    assert any("第三章 检索问答" in h for h in headers)

    # H1 作为根标题：所有块的最外层祖先都是 H1
    for c in chunks:
        assert c.parent_header.startswith("Insight AI 用户手册")


def test_chunk_metadata(sample_pdf_bytes: bytes) -> None:
    chunks = parse_pdf_bytes(sample_pdf_bytes, doc_id="doc-xyz")
    assert all(c.doc_id == "doc-xyz" for c in chunks)
    assert all(c.page_number == 1 for c in chunks)  # 单页测试 PDF
    # chunk_index 严格递增且连续
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    # 每个块非空
    assert all(c.text.strip() for c in chunks)


def test_overflow_splitting_by_paragraph() -> None:
    text = "\n".join(f"第{i}段内容" + "核心信息" * 100 for i in range(10))
    parts = _split_overflow(text, max_chars=500, overlap=0)
    assert len(parts) > 1
    assert all(len(p) <= 500 for p in parts)
    # 分块按段落边界切分，内容可还原
    assert "".join(parts) == text.replace("\n", "")


def test_overflow_splitting_hard_cut_with_overlap() -> None:
    long_para = "长" * 3000
    parts = _split_overflow(long_para, max_chars=1000, overlap=100)
    # 3000 字符 / 1000 上限 / 100 重叠 → 数学上恰好 4 块
    assert len(parts) == 4
    assert all(len(p) <= 1000 for p in parts)
    # 相邻块存在重叠，保证切分处语义不断裂
    assert parts[0][-100:] == parts[1][:100]
    assert parts[1][-100:] == parts[2][:100]


def test_short_text_not_split() -> None:
    assert _split_overflow("短文本", max_chars=1000) == ["短文本"]


def test_empty_pdf_returns_no_chunks() -> None:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    assert parse_pdf_bytes(data, doc_id="doc-empty") == []


def test_pdf_without_headings_creates_one_chunk() -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 60), "这是一段没有标题的正文内容。", fontsize=11, fontname="china-s")
    data = doc.tobytes()
    doc.close()
    chunks = parse_pdf_bytes(data, doc_id="doc-noheading")
    assert len(chunks) == 1
    assert chunks[0].parent_header == ""