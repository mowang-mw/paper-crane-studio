"""FFmpeg 烧录字幕的公共准备逻辑。

动态字幕必须使用 UTF-8 + LF。Windows 默认的 CRLF 会让当前环境中的
FFmpeg ``drawtext=textfile`` 实际渲染为空，因此这里显式控制换行符，
并让 M0/M1/M2/M3 共用同一个入口。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg import MediaToolError, ffmpeg_filter_path, find_chinese_font


_BREAK_PUNCTUATION = frozenset("，。！？；：、,.!?;:")
_TOKEN_PATTERN = re.compile(r"[^，。！？；：、,.!?;:]+[，。！？；：、,.!?;:]*|[，。！？；：、,.!?;:]+")


@dataclass(frozen=True)
class BurnedSubtitle:
    """一次烧录字幕的可追溯参数。"""

    narration: str
    rendered_text: str
    text_path: Path
    font_path: Path
    filter_expression: str
    font_size: int
    max_display_columns: int


def _character_width(character: str) -> int:
    if character == "\t":
        return 4
    return 2 if unicodedata.east_asian_width(character) in {"W", "F", "A"} else 1


def _display_width(text: str) -> int:
    return sum(_character_width(character) for character in text)


def _hard_wrap(token: str, max_columns: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    width = 0
    for character in token:
        character_width = _character_width(character)
        if current and width + character_width > max_columns:
            chunks.append("".join(current).rstrip())
            current = []
            width = 0
        current.append(character)
        width += character_width
    if current:
        chunks.append("".join(current).rstrip())
    return [chunk for chunk in chunks if chunk]


def _wrap_paragraph(paragraph: str, max_columns: int) -> list[str]:
    tokens = _TOKEN_PATTERN.findall(paragraph)
    if not tokens:
        return []

    lines: list[str] = []
    current = ""
    for token in tokens:
        if not current and _display_width(token) <= max_columns:
            current = token
            continue
        if current and _display_width(current + token) <= max_columns:
            current += token
            continue
        if current:
            lines.append(current.rstrip())
            current = ""

        chunks = _hard_wrap(token, max_columns)
        if not chunks:
            continue
        lines.extend(chunks[:-1])
        current = chunks[-1]

    if current:
        lines.append(current.rstrip())

    # 避免标点单独出现在下一行；只允许轻微超过目标宽度。
    merged: list[str] = []
    for line in lines:
        if (
            merged
            and line
            and line[0] in _BREAK_PUNCTUATION
            and _display_width(merged[-1] + line[0]) <= max_columns + 2
        ):
            merged[-1] += line[0]
            line = line[1:].lstrip()
        if line:
            merged.append(line)
    return merged


def wrap_subtitle_text(narration: str, *, max_display_columns: int) -> str:
    """按近似显示宽度和中文标点换行，不改变文字顺序。"""

    if max_display_columns < 8:
        raise MediaToolError("字幕每行显示宽度至少为 8")
    if "\x00" in narration:
        raise MediaToolError("字幕不得包含 NUL 字符")

    normalized = narration.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise MediaToolError("字幕或旁白不得为空")

    wrapped_lines: list[str] = []
    for raw_paragraph in normalized.split("\n"):
        paragraph = " ".join(raw_paragraph.split())
        if paragraph:
            wrapped_lines.extend(_wrap_paragraph(paragraph, max_display_columns))
    if not wrapped_lines:
        raise MediaToolError("字幕或旁白不得为空")
    return "\n".join(wrapped_lines)


def _subtitle_font_size(height: int) -> int:
    return max(22, min(48, round(height * 38 / 720)))


def _subtitle_max_columns(width: int, font_size: int) -> int:
    # CJK 全角字符约占一个 em，即本函数中的两个显示列。
    usable_width = max(160, width - max(96, round(width * 0.12)))
    return max(16, int(usable_width / (font_size / 2)))


def build_burned_subtitle_filter(
    *,
    font_path: Path,
    text_path: Path,
    width: int,
    height: int,
) -> tuple[str, int, int]:
    """构建使用独立 UTF-8 textfile 的下方安全区字幕滤镜。"""

    if width < 320 or height < 180:
        raise MediaToolError("字幕画布至少为 320x180")
    font = font_path.resolve()
    text_file = text_path.resolve()
    if not font.is_file():
        raise MediaToolError(f"字幕字体不存在：{font}")
    if not text_file.is_file():
        raise MediaToolError(f"字幕文本文件不存在：{text_file}")

    font_size = _subtitle_font_size(height)
    max_columns = _subtitle_max_columns(width, font_size)
    bottom_margin = max(18, round(height * 0.08))
    box_border = max(8, round(font_size * 0.42))
    border_width = max(1, round(font_size * 0.06))
    line_spacing = max(4, round(font_size * 0.24))
    expression = (
        "drawtext="
        f"fontfile={ffmpeg_filter_path(font)}:"
        f"textfile={ffmpeg_filter_path(text_file)}:"
        "expansion=none:"
        f"fontcolor=white:fontsize={font_size}:line_spacing={line_spacing}:"
        f"x=(w-text_w)/2:y=h-text_h-{bottom_margin}:"
        f"borderw={border_width}:bordercolor=black@0.95:"
        f"box=1:boxcolor=black@0.62:boxborderw={box_border}"
    )
    return expression, font_size, max_columns


def prepare_burned_subtitle(
    *,
    narration: str,
    text_path: Path,
    width: int,
    height: int,
    font_path: Path | None = None,
) -> BurnedSubtitle:
    """写入可由 FFmpeg 稳定读取的字幕文件，并返回完整烧录规格。"""

    font = (font_path or find_chinese_font()).resolve()
    font_size = _subtitle_font_size(height)
    max_columns = _subtitle_max_columns(width, font_size)
    rendered_text = wrap_subtitle_text(
        narration,
        max_display_columns=max_columns,
    )

    target = text_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" 是修复 Windows 动态字幕不可见的关键，不可省略。
    with target.open("w", encoding="utf-8", newline="\n") as output:
        output.write(rendered_text)
        output.write("\n")

    filter_expression, resolved_font_size, resolved_max_columns = (
        build_burned_subtitle_filter(
            font_path=font,
            text_path=target,
            width=width,
            height=height,
        )
    )
    return BurnedSubtitle(
        narration=narration.replace("\r\n", "\n").replace("\r", "\n").strip(),
        rendered_text=rendered_text,
        text_path=target,
        font_path=font,
        filter_expression=filter_expression,
        font_size=resolved_font_size,
        max_display_columns=resolved_max_columns,
    )
