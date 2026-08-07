from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app import crud
from backend.app.media import MediaToolError, render_mock_project_short, resolve_media_tools, verify_media
from backend.app.media.background_audio import RIGHTS_NOTICE
from backend.app.media.ffmpeg import ffprobe_json, run_command, sha256_file
from backend.app.media.mock_pipeline import _motion_filter, generate_mock_wav
from backend.app.models import JobStatus
from backend.app.schemas import BackgroundAudioRead


def _shots() -> list[dict[str, object]]:
    return [
        {
            "shot_id": f"shot_{index:02d}",
            "sequence_no": index,
            "title": f"镜头 {index}",
            "visual_description": "短小的合成媒体 fixture",
            "subtitle_text": f"第 {index} 镜字幕继续保留。",
            "duration_seconds": duration,
            "provider_id": "mock",
            "source_type": "DETERMINISTIC_FALLBACK",
            "generation_parameters": {
                "composition_template": "rainy_window",
                "motion": "PUSH_IN",
                "background_color": "0x183f7a",
                "audio_frequency_hz": 440 + index * 20,
                "scene_label": "fixture",
            },
        }
        for index, duration in enumerate((7.0, 7.0, 6.0), start=1)
    ]


def test_motion_presets_are_stable_and_bounded() -> None:
    static = _motion_filter("PUSH_IN", 480, motion_preset="static")
    gentle = _motion_filter("PUSH_IN", 480, motion_preset="gentle_zoom")
    cinematic = _motion_filter("PAN_RIGHT", 480, motion_preset="cinematic_pan")
    assert "zoompan" not in static
    assert "crop=1280:720" in static
    assert static == _motion_filter("PUSH_IN", 480, motion_preset="static")
    assert "min(1.018" in gentle
    assert "x='(iw-iw/zoom)/2'" in gentle
    assert "scale=2560:1440:flags=lanczos" in gentle
    assert "d=1:s=2560x1440:fps=24" in gentle
    assert gentle.endswith("scale=1280:720:flags=lanczos")
    assert "zoompan=z=1.04" in cinematic
    assert "0.10+0.80" in cinematic
    assert "scale=2560:1440:flags=lanczos" in cinematic
    assert cinematic.endswith("scale=1280:720:flags=lanczos")
    assert cinematic == _motion_filter(
        "PAN_RIGHT", 480, motion_preset="cinematic_pan"
    )
    safe_pan_fraction = (1 - 1 / 1.04) * 0.80
    assert 0.02 <= safe_pan_fraction <= 0.04
    with pytest.raises(MediaToolError, match="未知 motion preset"):
        _motion_filter("PUSH_IN", 10, motion_preset="broken")


def test_generation_snapshot_defaults_and_retry_keeps_motion(client, database) -> None:
    project = client.post(
        "/api/projects",
        json={"title": "M6 快照", "story": "这是一个用于媒体设置快照的短故事。"},
    ).json()
    queued = client.post(
        f"/api/projects/{project['id']}/generate",
        json={"script_provider": "mock", "desired_shot_count": 3},
    )
    assert queued.status_code == 202
    job_id = queued.json()["job_id"]
    with database.session() as session:
        job = crud.get_job(session, job_id)
        assert job is not None
        job.status = JobStatus.FAILED
        session.commit()
    original = client.get(f"/api/jobs/{job_id}").json()
    assert original["request_json"]["motion_preset"] == "gentle_zoom"
    retried = client.post(f"/api/jobs/{job_id}/retry")
    assert retried.status_code == 202
    retried_job = client.get(f"/api/jobs/{retried.json()['job_id']}").json()
    assert retried_job["request_json"]["motion_preset"] == "gentle_zoom"
    assert retried_job["request_json"]["retry_of_job_id"] == job_id


def test_background_audio_upload_validation_delete_and_poster_url(
    client, tmp_path: Path, database, settings
) -> None:
    project = client.post(
        "/api/projects",
        json={"title": "背景音测试", "story": "这是一个用于验证背景音上传和删除的故事。"},
    ).json()
    project_id = project["id"]
    audio = tmp_path / "room-tone.wav"
    generate_mock_wav(audio, 1.2, 220)
    uploaded = client.post(
        f"/api/projects/{project_id}/background-audio",
        params={"filename": audio.name},
        content=audio.read_bytes(),
        headers={"content-type": "audio/wav"},
    )
    assert uploaded.status_code == 200
    asset = BackgroundAudioRead.model_validate(uploaded.json())
    assert asset.source_type == "USER_UPLOAD"
    assert asset.duration_seconds == pytest.approx(1.2, abs=0.05)
    assert asset.sha256 == sha256_file(audio)
    assert "不声明" in asset.rights_notice
    assert client.get(f"/api/projects/{project_id}/background-audio").json()["asset_id"] == asset.asset_id
    media_job = client.post(
        f"/api/projects/{project_id}/generate",
        json={
            "script_provider": "mock",
            "desired_shot_count": 3,
            "motion_preset": "cinematic_pan",
            "background_audio_enabled": True,
            "background_volume": 0.15,
        },
    )
    assert media_job.status_code == 202
    media_snapshot = client.get(f"/api/jobs/{media_job.json()['job_id']}").json()["request_json"]
    assert media_snapshot["motion_preset"] == "cinematic_pan"
    assert media_snapshot["background_audio"]["sha256"] == asset.sha256
    assert media_snapshot["background_audio"]["volume"] == 0.15

    assert client.post(
        f"/api/projects/{project_id}/background-audio",
        params={"filename": "bad.flac"},
        content=b"bad",
        headers={"content-type": "audio/flac"},
    ).status_code == 415
    assert client.post(
        f"/api/projects/{project_id}/background-audio",
        params={"filename": "broken.wav"},
        content=b"not-a-wave",
        headers={"content-type": "audio/wav"},
    ).status_code == 422
    assert client.post(
        f"/api/projects/{project_id}/background-audio",
        params={"filename": "large.wav"},
        content=b"0" * (20 * 1024 * 1024 + 1),
        headers={"content-type": "audio/wav"},
    ).status_code == 413

    export_dir = settings.project_dir(project_id) / "exports" / "poster-api"
    export_dir.mkdir(parents=True, exist_ok=True)
    video = export_dir / "video.mp4"
    poster = export_dir / "poster.jpg"
    manifest_path = export_dir / "manifest.json"
    video.write_bytes(b"test-video")
    poster.write_bytes(b"\xff\xd8\xff\xd9")
    data_root = Path(settings.data_dir).resolve()
    manifest_path.write_text(
        json.dumps(
            {
                "output": {
                    "poster_path": poster.resolve().relative_to(data_root).as_posix()
                }
            }
        ),
        encoding="utf-8",
    )
    with database.session() as session:
        db_project = crud.get_project(session, project_id)
        assert db_project is not None
        job = crud.create_job(session, project=db_project)
        crud.create_export(
            session,
            project_id=project_id,
            job_id=job.id,
            file_path=video.resolve().relative_to(data_root).as_posix(),
            manifest_path=manifest_path.resolve().relative_to(data_root).as_posix(),
            duration_seconds=1.0,
            sha256="a" * 64,
        )
        session.commit()
    detail = client.get(f"/api/projects/{project_id}").json()
    poster_url = detail["latest_export"]["poster_url"]
    assert client.get(poster_url).status_code == 200

    assert client.delete(f"/api/projects/{project_id}/background-audio").status_code == 204
    assert client.get(f"/api/projects/{project_id}/background-audio").json() is None


def test_mock_export_mixes_background_audio_and_generates_poster(tmp_path: Path) -> None:
    background = tmp_path / "user.mp3"
    tools = resolve_media_tools()
    generate_mock_wav(tmp_path / "source.wav", 2.0, 180)
    source_wav = tmp_path / "source.wav"
    run_command(
        [tools.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", source_wav, background],
        timeout_seconds=60,
    )
    digest = sha256_file(background)
    rendered = render_mock_project_short(
        root=tmp_path,
        project_id="m6-media",
        project_title="M6 媒体",
        shots=_shots(),
        output_dir=tmp_path / "export",
        width=320,
        height=180,
        fps=24,
        motion_preset="gentle_zoom",
        background_audio={
            "enabled": True,
            "volume": 0.12,
            "resolved_path": str(background),
            "storage_path": "projects/m6-media/background-audio/user.mp3",
            "original_filename": "user.mp3",
            "mime_type": "audio/mpeg",
            "format": "mp3",
            "duration_seconds": 2.0,
            "size_bytes": background.stat().st_size,
            "sha256": digest,
            "rights_notice": RIGHTS_NOTICE,
        },
    )
    manifest = json.loads(Path(rendered["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["motion_preset"] == "gentle_zoom"
    assert manifest["background_audio"]["source_type"] == "USER_UPLOAD"
    assert manifest["background_audio"]["loop_and_trim_to_seconds"] == pytest.approx(20.0)
    assert manifest["background_audio"]["ducking"]["method"] == "FFmpeg sidechaincompress"
    assert any("sidechaincompress" in command for command in manifest["safe_command_log"])
    assert manifest["output"]["poster_path"].endswith("poster.jpg")
    poster = Path(rendered["poster_path"])
    assert poster.is_file() and poster.stat().st_size > 0
    poster_probe = ffprobe_json(tools, poster)
    video_stream = next(stream for stream in poster_probe["streams"] if stream["codec_type"] == "video")
    assert (video_stream["width"], video_stream["height"]) == (1280, 720)
    validation = verify_media(tools, Path(rendered["output_path"]), expected_width=320, expected_height=180, expected_fps=24.0, planned_duration_seconds=20.0)
    assert validation["duration_validation"] in {"passed_exactly", "passed_with_media_tolerance"}
