from __future__ import annotations

from pathlib import Path

from backend.app.media import prepare_burned_subtitle, render_mock_project_short
from backend.app.media.ffmpeg import ffmpeg_filter_path, find_chinese_font
from backend.app.media.subtitles import wrap_subtitle_text


def _compact(value: str) -> str:
    return "".join(value.split())


def test_chinese_subtitle_wrap_keeps_order_and_prefers_punctuation() -> None:
    narration = (
        "少女翻开画册，蓝色鲸鱼突然游出书页，在旧书店上空盘旋。"
        "她追着微光跑向屋顶，看见城市灯火化成星辰。"
    )
    wrapped = wrap_subtitle_text(narration, max_display_columns=30)

    assert "\n" in wrapped
    assert _compact(wrapped) == _compact(narration)
    assert all(line.strip() for line in wrapped.splitlines())
    assert any(line[-1] in "，。！？；：" for line in wrapped.splitlines()[:-1])


def test_prepare_burned_subtitle_writes_distinct_utf8_lf_textfiles(
    tmp_path: Path,
) -> None:
    font = find_chinese_font()
    first = prepare_burned_subtitle(
        narration="雨夜里，少女打开一本会发光的画册。\r\n鲸鱼游出书页。",
        text_path=tmp_path / "subtitles" / "shot_01.txt",
        width=1280,
        height=720,
        font_path=font,
    )
    second = prepare_burned_subtitle(
        narration="黎明时，蓝色鲸鱼重新游回书页。",
        text_path=tmp_path / "subtitles" / "shot_02.txt",
        width=1280,
        height=720,
        font_path=font,
    )

    first_bytes = first.text_path.read_bytes()
    second_bytes = second.text_path.read_bytes()
    assert first_bytes != second_bytes
    assert b"\r" not in first_bytes
    assert b"\r" not in second_bytes
    assert first_bytes.endswith(b"\n")
    assert second_bytes.endswith(b"\n")
    assert _compact(first.text_path.read_text(encoding="utf-8")) == _compact(
        first.narration
    )
    assert _compact(second.text_path.read_text(encoding="utf-8")) == _compact(
        second.narration
    )
    assert f"textfile={ffmpeg_filter_path(first.text_path)}" in first.filter_expression
    assert f"textfile={ffmpeg_filter_path(second.text_path)}" in second.filter_expression
    assert "expansion=none" in first.filter_expression
    assert "borderw=" in first.filter_expression
    assert "boxcolor=black@" in first.filter_expression


def test_project_renderer_traces_each_dynamic_burned_subtitle(
    tmp_path: Path,
) -> None:
    narrations = [
        "第一幕，少女在雨夜旧书店发现发光画册。",
        "第二幕，蓝色鲸鱼游出书页，带她飞向屋顶。",
        "第三幕，黎明到来，鲸鱼回到画册，城市恢复原样。",
    ]
    shots = [
        {
            "shot_id": f"dynamic_{index}",
            "sequence_no": index,
            "title": f"动态镜头 {index}",
            "visual_description": f"用于验证第 {index} 个动态字幕。",
            "subtitle_text": narration,
            "duration_seconds": 7.0 if index < 3 else 6.0,
            "provider_id": "mock",
            "source_type": "DETERMINISTIC_FALLBACK",
            "generation_parameters": {},
        }
        for index, narration in enumerate(narrations, start=1)
    ]

    result = render_mock_project_short(
        root=tmp_path,
        project_id="dynamic-subtitle-test",
        project_title="动态字幕链路测试",
        shots=shots,
        output_dir=tmp_path / "generated",
        output_filename="dynamic_subtitles.mp4",
        width=320,
        height=180,
        fps=12,
    )
    manifest = result["manifest"]
    assert manifest["pipeline"]["subtitle_rendering"] == "burned_in"
    assert manifest["shot_count"] == 3

    paths: list[Path] = []
    for narration, shot in zip(narrations, manifest["shots"], strict=True):
        path = Path(shot["subtitle_text_path"])
        if not path.is_absolute():
            path = tmp_path / path
        paths.append(path)
        assert path.is_file()
        assert b"\r" not in path.read_bytes()
        assert _compact(path.read_text(encoding="utf-8")) == _compact(narration)
        assert shot["narration"] == narration
        assert shot["subtitle_rendering"] == "burned_in"
        assert Path(shot["font_path"]).is_file()
        assert f"textfile={ffmpeg_filter_path(path)}" in shot["subtitle_filter"]

    assert len(set(paths)) == 3
    assert len({path.read_text(encoding="utf-8") for path in paths}) == 3
    render_commands = [
        command
        for command in manifest["safe_command_log"]
        if "drawtext=" in command and "textfile=" in command
    ]
    assert len(render_commands) == 3
    for path in paths:
        escaped = ffmpeg_filter_path(path)
        assert sum(escaped in command for command in render_commands) == 1
