"""版面感知 PDF 解析与语义分块（纯函数，无 IO 依赖，便于单元测试）。

策略：
1. 逐页提取文本块（get_text("dict")），记录 内容 / 字号 / 页码
2. 按字号聚类识别标题层级：正文常用字号为基准，明显更大的字号
   依次映射为 H1（最大）/ H2 / H3，构成标题树
3. 以标题为天然边界切分语义块；超长节按 MAX_CHUNK_CHARS 二次切分
   （按段落边界优先，单段超长则硬切，保留 OVERLAP_CHARS 重叠）
4. 每个 Chunk 注入富元数据：doc_id / page_number / parent_header /
   chunk_index（created_at 由入库层填充）
"""
from __future__ import annotations

import io
from collections import Counter

import pymupdf
from pydantic import BaseModel, Field

MAX_CHUNK_CHARS = 1000      # 单块字符数上限（bge-m3 8192 token 上限的余量保护）
OVERLAP_CHARS = 100         # 硬切分时的前后重叠字符
MAX_HEADING_CHARS = 80      # 标题文本长度上限（超长视为正文而非标题）
HEADING_SIZE_DELTA = 1.0    # 标题字号须比正文字号大至少该值


class ParsedBlock(BaseModel):
    """版面中的一个文本块。"""

    text: str
    font_size: float
    page_number: int


class ParsedChunk(BaseModel):
    """语义分块结果（含富元数据，供向量化与溯源使用）。"""

    doc_id: str
    text: str
    page_number: int
    parent_header: str = ""
    chunk_index: int = Field(default=0, ge=0)
    created_at: str | None = None  # ISO 时间，由入库层填充


def _extract_blocks(content: bytes) -> list[ParsedBlock]:
    """从 PDF 字节流提取带版面信息的文本块。"""
    doc = pymupdf.open(stream=content, filetype="pdf")
    blocks: list[ParsedBlock] = []
    for page_no, page in enumerate(doc, start=1):
        data = page.get_text("dict")
        for blk in data.get("blocks", []):
            if blk.get("type") != 0:  # 0=文本块，跳过图片
                continue
            lines: list[str] = []
            max_size = 0.0
            for line in blk.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s["text"] for s in spans).strip()
                if not text:
                    continue
                lines.append(text)
                max_size = max(max_size, max(s["size"] for s in spans))
            if lines:
                blocks.append(
                    ParsedBlock(
                        text="".join(lines),  # CJK 无空格拼接；西文由 PDF 自带空格
                        font_size=max_size,
                        page_number=page_no,
                    )
                )
    doc.close()
    return blocks


def _map_heading_sizes(blocks: list[ParsedBlock]) -> dict[float, int]:
    """将字号映射为标题层级：{font_size: 层级(1=H1, 2=H2, 3=H3)}。

    基准 = 出现频率最高的字号（正文）；大于基准 + HEADING_SIZE_DELTA
    的去重字号按从大到小依次映射为 H1/H2/H3。
    """
    if not blocks:
        return {}
    sizes = [b.font_size for b in blocks]
    body_size = Counter(sizes).most_common(1)[0][0]
    heading_sizes = sorted({s for s in sizes if s > body_size + HEADING_SIZE_DELTA}, reverse=True)
    return {size: level for level, size in enumerate(heading_sizes[:3], start=1)}


def _split_overflow(text: str, max_chars: int = MAX_CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """超长文本二次切分：优先按段落边界，单段超长则硬切并保留重叠。"""
    if len(text) <= max_chars:
        return [text]
    parts = [p for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in parts:
        if len(para) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            # 单段硬切
            start = 0
            while start < len(para):
                end = min(start + max_chars, len(para))
                piece = para[start:end]
                if end < len(para):
                    start = end - overlap
                else:
                    start = len(para)
                chunks.append(piece)
            continue
        if len(buf) + len(para) + 1 > max_chars and buf:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{para}".strip()
    if buf:
        chunks.append(buf)
    return chunks


def _build_chunks(blocks: list[ParsedBlock], doc_id: str, heading_map: dict[float, int]) -> list[ParsedChunk]:
    """按标题树遍历文本块，产出语义分块。"""
    chunks: list[ParsedChunk] = []
    header_stack: list[str] = []   # 最近一层 H1/H2/H3 标题
    buf: list[str] = []
    buf_page = 1

    def flush() -> None:
        nonlocal buf
        text = "\n".join(buf).strip()
        if text:
            for part in _split_overflow(text):
                chunks.append(
                    ParsedChunk(
                        doc_id=doc_id,
                        text=part,
                        page_number=buf_page,
                        parent_header=" > ".join(header_stack),
                        chunk_index=len(chunks),
                    )
                )
        buf = []

    for blk in blocks:
        level = heading_map.get(blk.font_size)
        if level is not None and len(blk.text) <= MAX_HEADING_CHARS:
            flush()
            # 更新标题栈：同级或更高级标题出现时，弹出更深层级
            while header_stack and len(header_stack) >= level:
                header_stack.pop()
            header_stack.append(blk.text)
            if not buf:
                buf_page = blk.page_number
            continue
        if not buf:
            buf_page = blk.page_number
        buf.append(blk.text)
    flush()
    return chunks


def parse_pdf_bytes(content: bytes, doc_id: str) -> list[ParsedChunk]:
    """解析 PDF 字节流为语义分块列表。"""
    blocks = _extract_blocks(content)
    heading_map = _map_heading_sizes(blocks)
    return _build_chunks(blocks, doc_id, heading_map)


def parse_pdf_file(path: str | io.IOBase, doc_id: str) -> list[ParsedChunk]:
    """解析 PDF 文件路径或已打开的二进制流。"""
    if isinstance(path, io.IOBase):
        return parse_pdf_bytes(path.read(), doc_id)
    with open(path, "rb") as f:
        return parse_pdf_bytes(f.read(), doc_id)