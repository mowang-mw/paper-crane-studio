from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

from backend.app.providers.base import (
    GeneratedImageAsset,
    ImageGenerationOptions,
    ImageGenerationRequest,
    ScriptShot,
)
from backend.app.providers.comfyui import (
    NEGATIVE_PROMPT,
    ComfyUIImageProvider,
    ImageProviderError,
    _SessionFailure,
    build_positive_prompt,
    character_anchor,
    deterministic_shot_seed,
    make_workflow,
)
from backend.app.providers.mock import MockImageProvider
from backend.app.script_schema import Character, Scene, ScriptV1, Shot


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _write_rgb_png(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scanline = b"\x00" + bytes((24, 52, 96)) * width
    payload = b"\x89PNG\r\n\x1a\n"
    payload += _png_chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
    )
    payload += _png_chunk(b"IDAT", zlib.compress(scanline * height, level=1))
    payload += _png_chunk(b"IEND", b"")
    path.write_bytes(payload)


def _script(shot_count: int = 3) -> ScriptV1:
    character = Character(
        id="girl",
        name="原创少女",
        role="主角",
        appearance="原创少女，蓝色短发，蓝色眼睛。",
        personality="好奇而勇敢",
        costume="黑色外套与长裙",
        consistency_prompt="同一原创少女，蓝色短发，黑色外套。",
    )
    scenes = [
        Scene(
            id=f"scene{index}",
            name=f"故事场景{index}",
            description=(
                "深夜旧书店，少女翻开发光画册，蓝色发光鲸鱼从书页中游出。"
                if index == 1
                else "少女跟随蓝色鲸鱼穿过城市、屋顶与云层。"
            ),
            time="黎明" if index == 3 else "深夜",
            lighting="蓝紫色微光",
            consistency_prompt="旧书店与蓝色鲸鱼保持相同视觉设计。",
        )
        for index in range(1, shot_count + 1)
    ]
    shots = [
        Shot(
            id=f"shot{index}",
            index=index,
            title=f"镜头{index}",
            scene_id=f"scene{index}",
            character_ids=["girl"],
            visual_description=(
                "少女翻开发光画册，蓝色鲸鱼从书页中游出。"
                if index == 1
                else "少女注视蓝色鲸鱼飞过城市屋顶与云层。"
            ),
            camera="缓慢推进",
            image_prompt="横向动漫电影关键帧，无文字，无水印。",
            negative_prompt="文字，水印",
            narration=f"故事进入第{index}幕。",
            duration_seconds=8.0 if shot_count == 3 else 7.0,
        )
        for index in range(1, shot_count + 1)
    ]
    return ScriptV1(
        schema_version="script.v1",
        title="画册里的蓝鲸",
        synopsis="少女在旧书店打开画册，跟随鲸鱼夜航后迎来黎明。",
        characters=[character],
        scenes=scenes,
        shots=shots,
    )


def _requests(
    tmp_path: Path,
    *,
    job_id: str = "image-job",
    output_name: str = "images",
    shot_count: int = 3,
) -> tuple[ImageGenerationRequest, ...]:
    script = _script(shot_count)
    options = ImageGenerationOptions(
        width=320,
        height=184,
        steps=24,
        base_seed=7_000,
        startup_timeout_seconds=2.0,
        generation_timeout_seconds=5.0,
        job_timeout_seconds=20.0,
        http_timeout_seconds=1.0,
    )
    characters = {item.id: item for item in script.characters}
    scenes = {item.id: item for item in script.scenes}
    return tuple(
        ImageGenerationRequest(
            project_id="project-1",
            job_id=job_id,
            script=script,
            shot=shot,
            characters=tuple(characters[item] for item in shot.character_ids),
            scene=scenes[shot.scene_id],
            output_dir=tmp_path / output_name,
            options=options,
        )
        for shot in script.shots
    )


def _station_requests(tmp_path: Path) -> tuple[ImageGenerationRequest, ...]:
    character = Character(
        id="girl",
        name="短发少女",
        role="主角",
        appearance="短发，穿着深色雨衣，面容清秀",
        personality="安静、好奇",
        costume="深色雨衣",
        consistency_prompt="保持短发、深色雨衣和清秀面容",
    )
    scene_descriptions = (
        "雨夜，末班车站，少女独自等待列车",
        "少女发现长椅下有一只发着微光的纸飞机",
        "雨渐渐停下，远处列车驶来，她拿着纸飞机走向亮起的车门",
    )
    visuals = (
        "雨夜，少女站在长椅旁等待列车，聚焦在少女身上。",
        "少女蹲下身，发现长椅下有一只发着微光的纸飞机。",
        "雨渐渐停下，远处列车驶来，少女拿着纸飞机走向亮起的车门。",
    )
    cameras = (
        "缓慢推进，聚焦在少女身上",
        "镜头从纸飞机缓缓升起",
        "镜头从车门缓缓推进",
    )
    scenes = [
        Scene(
            id=f"scene{index}",
            name=f"场景{index}",
            description=scene_descriptions[index - 1],
            time="雨停后" if index == 3 else "雨夜",
            lighting=(
                "明亮的车门灯光，雨停后的晴朗天空"
                if index == 3
                else "阴暗的天色，车站灯光微弱"
            ),
            consistency_prompt="保持车站环境和少女外观",
        )
        for index in range(1, 4)
    ]
    shots = [
        Shot(
            id=f"shot{index}",
            index=index,
            title=f"镜头{index}",
            scene_id=f"scene{index}",
            character_ids=["girl"],
            visual_description=visuals[index - 1],
            camera=cameras[index - 1],
            image_prompt=visuals[index - 1],
            negative_prompt=None,
            narration=f"镜头{index}旁白。",
            duration_seconds=8.0 if index == 1 else 6.0,
        )
        for index in range(1, 4)
    ]
    script = ScriptV1(
        schema_version="script.v1",
        title="车站故事",
        synopsis="少女在车站发现纸飞机，并走向驶来的列车。",
        characters=[character],
        scenes=scenes,
        shots=shots,
    )
    options = ImageGenerationOptions()
    return tuple(
        ImageGenerationRequest(
            project_id="project-station",
            job_id="job-station",
            script=script,
            shot=shot,
            characters=(character,),
            scene=scenes[shot.index - 1],
            output_dir=tmp_path / "station-images",
            options=options,
        )
        for shot in shots
    )


class _FakeSession:
    def __init__(self, owner: "_FakeSessionFactory", **kwargs: Any) -> None:
        self.owner = owner
        self.kwargs = kwargs
        self.stdout_path = Path(kwargs["run_dir"]) / "comfyui.stdout.log"
        self.stderr_path = Path(kwargs["run_dir"]) / "comfyui.stderr.log"
        self.system_stats = {"system": {"comfyui_version": "test"}}

    def __enter__(self) -> "_FakeSession":
        self.owner.enter_count += 1
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.owner.exit_count += 1

    def generate(
        self,
        *,
        workflow: dict[str, Any],
        output_path: Path,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del timeout_seconds
        self.owner.generate_count += 1
        self.owner.workflows.append(workflow)
        if self.owner.fail_on_call == self.owner.generate_count:
            raise _SessionFailure("GPU_OOM", "CUDA out of memory", oom=True)
        latent = workflow["4"]["inputs"]
        _write_rgb_png(output_path, latent["width"], latent["height"])
        return {
            "prompt_id": f"fake-{self.owner.generate_count}",
            "generation_seconds": float(self.owner.generate_count),
            "output_descriptor": {"filename": output_path.name},
        }


class _FakeSessionFactory:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.construct_count = 0
        self.enter_count = 0
        self.exit_count = 0
        self.generate_count = 0
        self.fail_on_call = fail_on_call
        self.workflows: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _FakeSession:
        self.construct_count += 1
        return _FakeSession(self, **kwargs)


def _provider(tmp_path: Path, factory: _FakeSessionFactory) -> ComfyUIImageProvider:
    model = tmp_path / "animagine-xl-4.0-opt.safetensors"
    model.write_bytes(b"small deterministic test model stand-in")
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    return ComfyUIImageProvider(
        comfy_python=tmp_path / "comfy-python.exe",
        comfy_root=tmp_path / "ComfyUI",
        model_path=model,
        model_sha256=digest,
        session_factory=factory,
    )


def test_mock_image_plan_remains_backward_compatible() -> None:
    plan = MockImageProvider().plan(
        shot=ScriptShot(
            provider_shot_id="shot_01",
            shot_index=1,
            title="开始",
            visual_description="窗边",
            narration="夜航开始。",
            duration_seconds=7.0,
        )
    )
    assert plan.provider_id == "mock"
    assert plan.parameters["seed"] == 4101
    assert plan.parameters["composition_template"] == "rainy_window"


def test_options_and_deterministic_seed_boundaries() -> None:
    assert deterministic_shot_seed(7_000, 1) == 7_001
    assert deterministic_shot_seed(7_000, 5) == 7_005
    with pytest.raises(ValueError, match="8 的倍数"):
        ImageGenerationOptions(width=321, height=576)
    with pytest.raises(ValueError, match="batch_size=1"):
        ImageGenerationOptions(batch_size=2)
    with pytest.raises(ValueError, match="shot_index"):
        deterministic_shot_seed(7_000, 0)


def test_prompt_layers_repeat_character_anchor_and_preserve_required_negative(
    tmp_path: Path,
) -> None:
    requests = _requests(tmp_path)
    prompts = [build_positive_prompt(request) for request in requests]
    anchors = [layers["shared_character_anchors"] for _, layers in prompts]
    assert len(set(anchors)) == 1
    assert "1girl" in anchors[0]
    assert "dark blue bob cut" in anchors[0]
    assert "black coat" in anchors[0]
    assert "masterpiece" in prompts[0][0]
    assert "old bookstore" in prompts[0][0]
    assert "glowing blue whale" in prompts[0][0]
    assert "horizontal composition" in prompts[0][0]
    assert "少女" not in prompts[0][0]
    for required in ("bad hands", "extra fingers", "text", "watermark", "cropped"):
        assert required in NEGATIVE_PROMPT


def test_character_anchor_preserves_common_script_v1_appearance_details() -> None:
    anchor = character_anchor(
        Character(
            id="girl",
            name="原创少女",
            role="主角",
            appearance="头发略长，眼神略带疲惫",
            personality="好奇",
            costume="黑色连帽卫衣和深色牛仔裤",
            consistency_prompt="保持黑色连帽卫衣、深色牛仔裤和略长头发",
        )
    )
    for expected in (
        "medium-length dark hair",
        "slightly tired eyes",
        "black hoodie",
        "dark denim jeans",
    ):
        assert expected in anchor


def test_station_story_prompt_preserves_entities_actions_and_bound_relations(
    tmp_path: Path,
) -> None:
    requests = _station_requests(tmp_path)
    prompts = [build_positive_prompt(request) for request in requests]

    for prompt, layers in prompts:
        assert "1girl" in prompt
        assert "short hair" in layers["character_anchor"]
        assert "dark raincoat" in layers["character_anchor"]

    shot1 = prompts[0][0]
    assert "train station" in shot1
    assert "waiting" in shot1
    assert "standing beside the bench" in shot1
    assert "character-focused composition" in shot1

    shot2 = prompts[1][0]
    assert "glowing paper airplane" in shot2
    assert "glowing paper airplane under the bench" in shot2
    assert "crouching" in shot2
    assert "discovering" in shot2
    assert "low-angle composition" in shot2
    assert "paper airplane in foreground" in shot2

    shot3 = prompts[2][0]
    assert "holding a paper airplane" in shot3
    assert "walking toward an illuminated train door" in shot3
    assert "approaching train in the distance" in shot3
    assert "after the rain" in shot3
    assert "illuminated train door prominent in frame" in shot3
    assert "blue and violet night lighting" not in shot3


def test_prompt_and_seed_are_deterministic_and_workflow_parameters_are_unchanged(
    tmp_path: Path,
) -> None:
    request = _station_requests(tmp_path)[1]
    first_prompt, first_layers = build_positive_prompt(request)
    second_prompt, second_layers = build_positive_prompt(request)
    seed = deterministic_shot_seed(request.options.base_seed, request.shot.index)

    assert (first_prompt, first_layers) == (second_prompt, second_layers)
    assert seed == 20_260_804
    workflow = make_workflow(
        model_filename="animagine-xl-4.0-opt.safetensors",
        positive_prompt=first_prompt,
        negative_prompt=NEGATIVE_PROMPT,
        seed=seed,
        request=request,
        filename_prefix="station-shot2",
    )
    assert workflow["1"]["inputs"]["ckpt_name"] == "animagine-xl-4.0-opt.safetensors"
    assert workflow["3"]["inputs"]["text"] == NEGATIVE_PROMPT
    assert workflow["4"]["inputs"] == {
        "width": 1024,
        "height": 576,
        "batch_size": 1,
    }
    assert workflow["5"]["inputs"] == {
        "seed": 20_260_804,
        "steps": 24,
        "cfg": 5.0,
        "sampler_name": "euler_ancestral",
        "scheduler": "normal",
        "denoise": 1.0,
        "model": ["1", 0],
        "positive": ["2", 0],
        "negative": ["3", 0],
        "latent_image": ["4", 0],
    }


def test_station_trace_records_compound_and_unrecognized_semantics(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory()
    provider = _provider(tmp_path, factory)
    request = _station_requests(tmp_path)[1]

    asset = provider.generate(request=request)

    trace = json.loads(asset.trace_path.read_text(encoding="utf-8"))
    audit = trace["semantic_audit"]
    compounds = audit["compound_semantic_cues"]["visual_description"]
    assert any(
        item["tags"] == "glowing paper airplane under the bench"
        for item in compounds
    )
    assert "recognized_semantic_cues" in audit
    assert "unrecognized_text_segments" in audit


def test_batch_starts_one_session_generates_sequentially_and_writes_trace(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory()
    provider = _provider(tmp_path, factory)
    requests = _requests(tmp_path)
    progress: list[tuple[int, int, str]] = []

    assets = provider.generate_batch(
        requests=requests,
        progress_callback=lambda completed, total, asset: progress.append(
            (completed, total, asset.shot_id)
        ),
    )

    assert provider.provider_id == "comfyui-animagine-xl-4"
    assert factory.construct_count == factory.enter_count == factory.exit_count == 1
    assert factory.generate_count == 3
    assert [item.shot_id for item in assets] == ["shot1", "shot2", "shot3"]
    assert [item.seed for item in assets] == [7001, 7002, 7003]
    assert progress == [(1, 3, "shot1"), (2, 3, "shot2"), (3, 3, "shot3")]
    assert all(item.image_path.is_file() for item in assets)
    assert all(item.workflow_path.is_file() for item in assets)
    assert all(item.trace_path.is_file() for item in assets)
    assert all(item.image_sha256 == hashlib.sha256(item.image_path.read_bytes()).hexdigest() for item in assets)
    assert all(workflow["1"]["class_type"] == "CheckpointLoaderSimple" for workflow in factory.workflows)
    assert all(workflow["7"]["class_type"] == "SaveImage" for workflow in factory.workflows)
    assert [workflow["5"]["inputs"]["seed"] for workflow in factory.workflows] == [7001, 7002, 7003]

    report = json.loads(
        (requests[0].output_dir.parent / "image_generation_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["completed_count"] == 3
    assert report["comfyui_start_count"] == 1
    assert report["sequential_generation"] is True
    assert report["automatic_parameter_downgrade"] is False
    assert report["mock_fallback"] is False
    assert {item["shot_id"] for item in report["assets"]} == {"shot1", "shot2", "shot3"}

    request_trace = json.loads(
        (requests[0].output_dir / "shot-01.request.json").read_text(encoding="utf-8")
    )
    assert request_trace["original_chinese"]["visual_description"]
    assert request_trace["prompt_layers"]["shared_character_anchors"] == request_trace[
        "prompt_layers"
    ]["shared_character_anchors"]
    assert request_trace["base_seed"] == 7000
    assert request_trace["seed"] == 7001
    assert request_trace["semantic_audit"]["recognized_semantic_cues"]
    assert "unrecognized_text_segments" in request_trace["semantic_audit"]


@pytest.mark.parametrize("shot_count", [3, 4, 5])
def test_batch_accepts_every_script_v1_shot_count(
    tmp_path: Path,
    shot_count: int,
) -> None:
    factory = _FakeSessionFactory()
    provider = _provider(tmp_path, factory)
    requests = _requests(
        tmp_path,
        job_id=f"count-{shot_count}",
        output_name=f"count-{shot_count}/images",
        shot_count=shot_count,
    )

    assets = provider.generate_batch(requests=requests)

    assert len(assets) == shot_count
    assert factory.construct_count == 1
    assert factory.generate_count == shot_count
    assert [asset.seed for asset in assets] == [
        7000 + index for index in range(1, shot_count + 1)
    ]


def test_retry_reuses_valid_asset_and_only_generates_missing_shots(tmp_path: Path) -> None:
    initial_factory = _FakeSessionFactory()
    provider = _provider(tmp_path, initial_factory)
    first_requests = _requests(tmp_path, job_id="first", output_name="first-images")
    original = provider.generate_batch(requests=first_requests)

    retry_factory = _FakeSessionFactory()
    retry_provider = _provider(tmp_path, retry_factory)
    retry_requests = _requests(tmp_path, job_id="retry", output_name="retry-images")
    progress: list[tuple[int, str, bool]] = []
    retried = retry_provider.generate_batch(
        requests=retry_requests,
        reusable_assets=(original[0], original[2]),
        progress_callback=lambda completed, _total, asset: progress.append(
            (completed, asset.shot_id, asset.reused)
        ),
    )

    assert retry_factory.construct_count == 1
    assert retry_factory.generate_count == 1
    assert [item.reused for item in retried] == [True, False, True]
    assert retried[0].image_path == original[0].image_path
    assert retried[2].image_path == original[2].image_path
    assert progress == [(1, "shot1", True), (2, "shot3", True), (3, "shot2", False)]


def test_all_valid_reusable_assets_do_not_start_comfyui(tmp_path: Path) -> None:
    first_factory = _FakeSessionFactory()
    provider = _provider(tmp_path, first_factory)
    first_requests = _requests(tmp_path, job_id="first", output_name="first-images")
    original = provider.generate_batch(requests=first_requests)

    forbidden_factory = _FakeSessionFactory()
    retry_provider = _provider(tmp_path, forbidden_factory)
    retry_requests = _requests(tmp_path, job_id="retry", output_name="retry-images")
    reused = retry_provider.generate_batch(
        requests=retry_requests,
        reusable_assets=original,
    )

    assert forbidden_factory.construct_count == 0
    assert all(item.reused for item in reused)
    report = json.loads(
        (retry_requests[0].output_dir.parent / "image_generation_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["comfyui_start_count"] == 0


def test_oom_is_structured_at_failed_shot_without_mock_fallback(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(fail_on_call=2)
    provider = _provider(tmp_path, factory)
    requests = _requests(tmp_path)

    with pytest.raises(ImageProviderError) as caught:
        provider.generate_batch(requests=requests)

    error = caught.value.generation_error
    assert error["code"] == "GPU_OOM"
    assert error["failed_shot_id"] == "shot2"
    assert error["completed_image_count"] == 1
    assert error["oom"] is True
    assert error["retryable"] is True
    assert factory.construct_count == factory.enter_count == factory.exit_count == 1
    assert factory.generate_count == 2
    report = json.loads(
        (requests[0].output_dir.parent / "image_generation_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["completed_count"] == 1
    assert report["mock_fallback"] is False
    assert report["error"]["code"] == "GPU_OOM"


def test_model_missing_and_hash_mismatch_fail_before_session(tmp_path: Path) -> None:
    factory = _FakeSessionFactory()
    missing = ComfyUIImageProvider(
        comfy_python=tmp_path / "python.exe",
        comfy_root=tmp_path / "ComfyUI",
        model_path=tmp_path / "missing.safetensors",
        model_sha256="0" * 64,
        session_factory=factory,
    )
    with pytest.raises(ImageProviderError) as missing_error:
        missing.generate_batch(requests=_requests(tmp_path))
    assert missing_error.value.generation_error["code"] == "MODEL_NOT_FOUND"

    model = tmp_path / "wrong.safetensors"
    model.write_bytes(b"wrong")
    mismatch = ComfyUIImageProvider(
        comfy_python=tmp_path / "python.exe",
        comfy_root=tmp_path / "ComfyUI",
        model_path=model,
        model_sha256="0" * 64,
        session_factory=factory,
    )
    with pytest.raises(ImageProviderError) as hash_error:
        mismatch.generate_batch(requests=_requests(tmp_path, output_name="other"))
    assert hash_error.value.generation_error["code"] == "MODEL_HASH_MISMATCH"
    assert factory.construct_count == 0
