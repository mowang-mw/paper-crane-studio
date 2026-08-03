from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import textwrap

import pytest

from backend.app.providers.base import (
    AudioGenerationOptions,
    AudioGenerationRequest,
)
from backend.app.providers.qwen3_tts import (
    ModelFileExpectation,
    Qwen3TTSAudioProvider,
)
from backend.app.schemas import RealAudioRenderRequest
from backend.app.script_schema import Character, Scene, ScriptV1, Shot
from backend.app.services.audio_jobs import (
    RealAudioJobError,
    audio_gpu_handoff_status,
    build_media_timing_plan,
    create_audio_source_snapshot,
    load_audio_source_snapshot,
    validate_reusable_audio_asset,
)


REVISION = "a" * 40


def _script(shot_count: int = 3) -> ScriptV1:
    return ScriptV1(
        schema_version="script.v1",
        title="画册里的蓝鲸",
        synopsis="少女在旧书店打开画册，跟随鲸鱼夜航后迎来黎明。",
        characters=[
            Character(
                id="girl",
                name="原创少女",
                role="主角",
                appearance="原创少女，蓝色短发，蓝色眼睛。",
                personality="好奇而勇敢",
                costume="黑色外套与长裙",
                consistency_prompt="同一原创少女，蓝色短发，黑色外套。",
            )
        ],
        scenes=[
            Scene(
                id=f"scene{index}",
                name=f"场景{index}",
                description="少女与蓝色鲸鱼穿过夜色。",
                time="深夜",
                lighting="蓝紫色微光",
                consistency_prompt="旧书店与蓝色鲸鱼保持相同视觉设计。",
            )
            for index in range(1, shot_count + 1)
        ],
        shots=[
            Shot(
                id=f"shot{index}",
                index=index,
                title=f"镜头{index}",
                scene_id=f"scene{index}",
                character_ids=["girl"],
                visual_description="少女注视蓝色鲸鱼飞过旧书店。",
                camera="缓慢推进",
                image_prompt="横向动漫电影关键帧，无文字，无水印。",
                negative_prompt="文字，水印",
                narration=f"蓝鲸夜航进入第{index}幕。",
                duration_seconds=8.0,
            )
            for index in range(1, shot_count + 1)
        ],
    )


def _requests(
    tmp_path: Path,
    *,
    script: ScriptV1,
    job_id: str,
) -> tuple[AudioGenerationRequest, ...]:
    options = AudioGenerationOptions(
        speaker="Serena",
        language="Chinese",
        base_seed=7_000,
        model_load_timeout_seconds=2.0,
        generation_timeout_seconds=2.0,
        job_timeout_seconds=10.0,
    )
    output_dir = tmp_path / job_id / "audio"
    return tuple(
        AudioGenerationRequest(
            project_id="project-1",
            job_id=job_id,
            source_script_job_id="script-job-1",
            source_image_job_id="image-job-1",
            script=script,
            shot=shot,
            output_dir=output_dir,
            options=options,
        )
        for shot in script.shots
    )


def _write_fake_runner(path: Path, *, fail_index: int | None = None) -> None:
    source = f'''
import argparse, hashlib, json, math, os, struct, sys, wave
from pathlib import Path

def atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

parser = argparse.ArgumentParser()
parser.add_argument("--request", type=Path, required=True)
parser.add_argument("--progress", type=Path, required=True)
parser.add_argument("--summary", type=Path, required=True)
args = parser.parse_args()
request = json.loads(args.request.read_text(encoding="utf-8"))
results = []
for item in request["shots"]:
    atomic(args.progress, {{"stage": "AUDIO_GENERATION", "shot_id": item["shot_id"], "shot_index": item["shot_index"]}})
    if item["shot_index"] == {fail_index!r}:
        atomic(args.summary, {{"status": "FAILED", "model_load_count": 1, "error_code": "TTS_GENERATION_FAILED", "error_stage": "AUDIO_GENERATION", "error": "fake bounded failure", "failed_shot_id": item["shot_id"], "failed_shot_index": item["shot_index"], "oom": False}})
        raise SystemExit(1)
    audio_path = Path(item["audio_path"])
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 16000
    samples = [int(6000 * math.sin(2 * math.pi * 440 * i / sample_rate)) for i in range(4000)]
    with wave.open(str(audio_path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    trace = {{
        "provider_id": request["provider_id"], "model_id": request["model_id"],
        "model_revision": request["model_revision"], "model_sha256": request["model_sha256"],
        "project_id": request["project_id"], "job_id": request["job_id"],
        "source_script_job_id": request["source_script_job_id"],
        "source_image_job_id": request["source_image_job_id"],
        "shot_id": item["shot_id"], "shot_index": item["shot_index"],
        "text": item["text"], "text_sha256": item["text_sha256"],
        "speaker": request["speaker"], "language": request["language"], "seed": item["seed"],
        "audio_path": str(audio_path.resolve()), "audio_sha256": sha(audio_path),
        "sample_rate": sample_rate, "channels": 1, "sample_width_bytes": 2,
        "duration_seconds": 0.25, "generation_seconds": 0.01, "warnings": []
    }}
    atomic(Path(item["trace_path"]), trace)
    results.append(trace)
atomic(args.summary, {{
    "status": "SUCCEEDED", "model_load_count": 1, "completed_audio_count": len(results),
    "results": results, "gpu_memory_baseline_bytes": 100, "gpu_peak_allocated_bytes": 200,
    "gpu_peak_reserved_bytes": 300, "gpu_memory_after_cleanup_bytes": 0
}})
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


def _provider(tmp_path: Path, *, fail_index: int | None = None) -> Qwen3TTSAudioProvider:
    model_path = tmp_path / "fake-model"
    model_file = model_path / "model.safetensors"
    model_file.parent.mkdir(parents=True, exist_ok=True)
    model_file.write_bytes(b"small deterministic model stand-in")
    digest = hashlib.sha256(model_file.read_bytes()).hexdigest()
    metadata = (
        model_path
        / ".cache"
        / "huggingface"
        / "download"
        / "model.safetensors.metadata"
    )
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(f"{REVISION}\n{digest}\n", encoding="utf-8")
    runner = tmp_path / f"runner-{fail_index}.py"
    _write_fake_runner(runner, fail_index=fail_index)
    return Qwen3TTSAudioProvider(
        tts_python=Path(sys.executable),
        model_path=model_path,
        runner_path=runner,
        model_id="test/qwen3-tts",
        model_revision=REVISION,
        model_sha256=digest,
        model_expectations={
            "model.safetensors": ModelFileExpectation(
                size_bytes=model_file.stat().st_size,
                sha256=digest,
            )
        },
        expected_model_file_count=1,
        expected_model_total_size_bytes=model_file.stat().st_size,
        handoff_check=lambda: {"conflict": False},
    )


@pytest.mark.parametrize("shot_count", [3, 4, 5])
def test_provider_runs_one_bounded_child_and_generates_sequential_audio(
    tmp_path: Path,
    shot_count: int,
) -> None:
    script = _script(shot_count)
    requests = _requests(tmp_path, script=script, job_id="audio-job")
    provider = _provider(tmp_path)
    progress: list[tuple[int, str, bool]] = []

    assets = provider.generate_batch(
        requests=requests,
        progress_callback=lambda completed, total, asset: progress.append(
            (completed, asset.shot_id, asset.reused)
        ),
    )

    assert [item[0] for item in progress] == list(range(1, shot_count + 1))
    assert [item.shot_id for item in assets] == [
        f"shot{index}" for index in range(1, shot_count + 1)
    ]
    assert all(item.model_sha256 == provider.model_sha256 for item in assets)
    assert all(item.audio_path.is_file() for item in assets)
    assert all(item.audio_sha256 == hashlib.sha256(item.audio_path.read_bytes()).hexdigest() for item in assets)
    for index in range(1, shot_count + 1):
        prefix = requests[0].output_dir / f"shot-{index:02d}"
        assert prefix.with_suffix(".text.txt").is_file()
        assert prefix.with_suffix(".request.json").is_file()
        assert prefix.with_suffix(".result.json").is_file()
        assert prefix.with_suffix(".wav").is_file()
    report = json.loads(
        (tmp_path / "audio-job" / "audio_generation_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["model_load_count"] == 1
    assert report["sequential_generation"] is True
    assert report["max_audio_concurrency"] == 1
    assert report["child_process_exited"] is True
    assert report["gpu_memory_observed"]["peak_allocated_bytes"] == 200
    assert (tmp_path / "audio-job" / "tts.stdout.log").is_file()
    assert (tmp_path / "audio-job" / "tts.stderr.log").is_file()


def test_speaker_contract_defaults_to_serena_and_accepts_vivian() -> None:
    default_request = RealAudioRenderRequest(source_image_job_id="image-job")
    vivian_request = RealAudioRenderRequest(
        source_image_job_id="image-job",
        speaker="Vivian",
    )

    assert default_request.speaker == "Serena"
    assert default_request.language == "Chinese"
    assert vivian_request.speaker == "Vivian"


def test_gpu_handoff_detects_unknown_high_memory_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.app.services.audio_jobs._port_listening",
        lambda _host, _port: False,
    )
    monkeypatch.setattr(
        "backend.app.services.audio_jobs._known_gpu_model_processes",
        lambda: [],
    )

    status = audio_gpu_handoff_status(
        gpu_memory_used_mib=3_000,
        gpu_memory_limit_mib=2_048,
    )

    assert status["conflict"] is True
    assert status["gpu_memory_conflict"] is True
    assert status["gpu_memory_used_mib"] == 3_000


def test_retry_reuses_valid_audio_without_tts_environment(tmp_path: Path) -> None:
    script = _script()
    first_requests = _requests(tmp_path, script=script, job_id="first")
    assets = _provider(tmp_path).generate_batch(requests=first_requests)
    retry_requests = _requests(tmp_path, script=script, job_id="retry")
    provider = Qwen3TTSAudioProvider(
        tts_python=tmp_path / "missing-python.exe",
        model_path=tmp_path / "missing-model",
        runner_path=tmp_path / "missing-runner.py",
        model_id=assets[0].model_id,
        model_revision=assets[0].model_revision,
        model_sha256=assets[0].model_sha256,
        model_expectations={
            "model.safetensors": ModelFileExpectation(1, assets[0].model_sha256)
        },
        handoff_check=lambda: {"conflict": True},
    )

    reused = provider.generate_batch(
        requests=retry_requests,
        reusable_assets=assets,
    )

    assert all(item.reused for item in reused)
    assert provider.last_run_report is not None
    assert provider.last_run_report["model_load_count"] == 0
    assert provider.last_run_report["child_started"] is False


def test_reusable_audio_rejects_different_source_job(tmp_path: Path) -> None:
    script = _script()
    original_request = _requests(tmp_path, script=script, job_id="source")[0]
    asset = _provider(tmp_path).generate(request=original_request)
    mismatched_request = AudioGenerationRequest(
        project_id=original_request.project_id,
        job_id="retry",
        source_script_job_id="different-script-job",
        source_image_job_id=original_request.source_image_job_id,
        script=script,
        shot=original_request.shot,
        output_dir=tmp_path / "retry" / "audio",
        options=original_request.options,
    )

    reused, reason = validate_reusable_audio_asset(
        asset=asset,
        request=mismatched_request,
        provider_id=asset.provider_id,
        model_id=asset.model_id,
        model_revision=asset.model_revision,
        model_sha256=asset.model_sha256,
    )

    assert reused is None
    assert reason is not None
    assert "source_script_job_id" in reason


def test_corrupt_reusable_wav_regenerates_only_missing_shot(tmp_path: Path) -> None:
    script = _script()
    first = _requests(tmp_path, script=script, job_id="first-partial")
    assets = _provider(tmp_path).generate_batch(requests=first)
    assets[1].audio_path.write_bytes(b"corrupt")
    retry = _requests(tmp_path, script=script, job_id="retry-partial")
    provider = _provider(tmp_path / "second-provider")

    regenerated = provider.generate_batch(requests=retry, reusable_assets=assets)

    assert [item.reused for item in regenerated] == [True, False, True]
    assert provider.last_run_report is not None
    assert provider.last_run_report["generated_count"] == 1
    assert provider.last_run_report["reused_count"] == 2


def test_failure_is_structured_and_preserves_completed_wav(tmp_path: Path) -> None:
    script = _script()
    requests = _requests(tmp_path, script=script, job_id="failed")
    provider = _provider(tmp_path, fail_index=2)

    with pytest.raises(RealAudioJobError) as captured:
        provider.generate_batch(requests=requests)

    assert captured.value.generation_error["code"] == "TTS_GENERATION_FAILED"
    report = json.loads(
        (tmp_path / "failed" / "audio_generation_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "FAILED"
    assert report["generation_error"]["failed_shot_id"] == "shot2"
    assert report["completed_audio_count"] == 1
    assert report["reusable_wavs"][0]["shot_id"] == "shot1"
    assert report["child_process_exited"] is True


def test_media_timing_plan_uses_real_audio_duration(tmp_path: Path) -> None:
    script = _script()
    assets = _provider(tmp_path).generate_batch(
        requests=_requests(tmp_path, script=script, job_id="timing")
    )

    plan = build_media_timing_plan(script=script, audio_assets=assets)

    assert plan["source_total_duration_seconds"] == 24.0
    assert plan["rendered_total_duration_seconds"] == 24.0
    assert [item["audio_duration_seconds"] for item in plan["shots"]] == [
        0.25,
        0.25,
        0.25,
    ]


class _Settings:
    def __init__(self, root: Path) -> None:
        self.data_dir = root
        self.root = root

    def project_dir(self, project_id: str) -> Path:
        return self.root / "projects" / project_id


def test_audio_source_snapshot_binds_script_and_real_images(tmp_path: Path) -> None:
    settings = _Settings(tmp_path)
    script = _script()
    images = [
        {
            "shot_id": shot.id,
            "shot_index": shot.index,
            "status": "SUCCEEDED",
            "provider_id": "comfyui-animagine-xl-4",
            "image_path": f"projects/project-1/images/{shot.id}.png",
            "image_sha256": str(shot.index) * 64,
        }
        for shot in script.shots
    ]
    path, digest = create_audio_source_snapshot(
        settings,
        project_id="project-1",
        audio_job_id="audio-job",
        source_script_job_id="script-job",
        source_image_job_id="image-job",
        source_script_provider="llamacpp",
        source_image_provider="comfyui-animagine-xl-4",
        script=script,
        source_images=images,
        source_trace={"script_provider_calls": 0},
    )

    loaded_script, payload = load_audio_source_snapshot(
        settings,
        project_id="project-1",
        audio_job_id="audio-job",
        request_snapshot={
            "audio_source_snapshot_path": str(path),
            "audio_source_snapshot_sha256": digest,
            "source_script_job_id": "script-job",
            "source_image_job_id": "image-job",
        },
    )

    assert loaded_script == script
    assert [item["shot_id"] for item in payload["source_images"]] == [
        "shot1",
        "shot2",
        "shot3",
    ]
