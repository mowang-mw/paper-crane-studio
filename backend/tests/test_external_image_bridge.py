from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Sequence

from fastapi.testclient import TestClient

from backend.app import crud
from backend.app.database import Database
from backend.app.media.ffmpeg import resolve_media_tools, run_command, sha256_file
from backend.app.models import Asset, JobStatus
from backend.app.providers.mock import MockVideoProvider
from backend.app.script_schema import Character, Scene, ScriptV1, Shot
from backend.app.worker import Worker


def _script() -> ScriptV1:
    character = Character(
        id="girl",
        name="原创少女",
        role="主角",
        appearance="黑色短发，神情坚定",
        personality="安静",
        costume="深色雨衣",
        consistency_prompt="所有镜头保持黑色短发和深色雨衣",
    )
    scenes = [
        Scene(
            id=f"scene{index}",
            name=f"场景{index}",
            description=f"只属于场景{index}的环境标记",
            time="夜晚",
            lighting="柔和站台灯光",
            consistency_prompt="保持同一座车站",
        )
        for index in range(1, 4)
    ]
    shots = [
        Shot(
            id=f"shot{index}",
            index=index,
            title=f"镜头{index}",
            scene_id=f"scene{index}",
            character_ids=[character.id],
            visual_description=f"只属于镜头{index}的动作标记",
            narration=f"第{index}镜旁白。",
            duration_seconds=7.0,
            camera=f"只属于镜头{index}的构图标记",
            image_prompt=f"只属于镜头{index}的补充视觉标记",
            negative_prompt="文字，水印",
        )
        for index in range(1, 4)
    ]
    return ScriptV1(
        schema_version="script.v1",
        title="外部图片桥接测试",
        synopsis="三个镜头已经完成结构化规划。",
        characters=[character],
        scenes=scenes,
        shots=shots,
    )


def _seed_project(database: Database) -> tuple[str, ScriptV1]:
    script = _script()
    with database.session() as session:
        project = crud.create_project(
            session,
            title=script.title,
            story="原始故事唯一全文标记，不应被直接复制成单镜头提示词。",
        )
        crud.replace_shots(
            session,
            project=project,
            script_json=script.model_dump(mode="json"),
            shots=[
                {
                    "shot_index": shot.index,
                    "title": shot.title,
                    "visual_description": shot.visual_description,
                    "narration": shot.narration,
                    "duration_seconds": shot.duration_seconds,
                    "provider_id": "llamacpp",
                    "parameters_json": {
                        "provider_shot_id": shot.id,
                        "camera": shot.camera,
                        "image_prompt": shot.image_prompt,
                    },
                }
                for shot in script.shots
            ],
        )
        session.commit()
        return project.id, script


def _make_image(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            resolve_media_tools().ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=64x40:d=0.04",
            "-frames:v",
            "1",
            path,
        ],
        timeout_seconds=30,
    )
    return path.read_bytes()


def _import_image(
    client: TestClient,
    project_id: str,
    shot_id: str,
    payload: bytes,
    *,
    filename: str,
    source_type: str = "AI_GENERATED",
    provider_hint: str | None = "ChatGPT Images",
):
    params = {"filename": filename, "external_source_type": source_type}
    if provider_hint is not None:
        params["provider_hint"] = provider_hint
    return client.post(
        f"/api/projects/{project_id}/shots/{shot_id}/external-images",
        params=params,
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )


def _seed_source_job(database: Database, settings, project_id: str, script: ScriptV1) -> str:
    with database.session() as session:
        project = crud.get_project(session, project_id)
        assert project is not None
        job = crud.create_job(
            session,
            project=project,
            provider_id="comfyui-animagine-xl-4",
            job_type="GENERATE_REAL_IMAGE_VIDEO",
        )
        image_shots = []
        for shot in script.shots:
            path = settings.project_dir(project_id) / "source" / f"{shot.id}.png"
            _make_image(path)
            image_shots.append(
                {
                    "shot_id": shot.id,
                    "shot_index": shot.index,
                    "status": "SUCCEEDED",
                    "image_path": path.relative_to(settings.data_dir).as_posix(),
                    "image_asset_id": f"legacy-{shot.id}",
                    "provider_id": "comfyui-animagine-xl-4",
                    "source_type": "REAL_LOCAL_MODEL",
                }
            )
        job.status = JobStatus.SUCCEEDED
        job.progress = 100
        job.result_json = {"image_shots": image_shots}
        session.commit()
        return job.id


def _fake_video_runner(command: Sequence[str]) -> None:
    Path(command[-1]).write_bytes(b"external-image-mock-video")


def test_external_prompt_is_deterministic_and_selected_shot_scoped(
    client: TestClient, database: Database
) -> None:
    project_id, _script_value = _seed_project(database)
    first = client.get(
        f"/api/projects/{project_id}/shots/shot1/external-image-prompt"
    )
    second = client.get(
        f"/api/projects/{project_id}/shots/shot1/external-image-prompt"
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    prompt = first.json()["prompt"]
    assert first.json()["adapter"] == "external-natural-language-v1"
    assert "静态关键帧" in prompt
    assert "只属于镜头1的动作标记" in prompt
    assert "只属于镜头1的构图标记" in prompt
    assert "只属于镜头2" not in prompt
    assert "原始故事唯一全文标记" not in prompt
    shot2 = client.get(
        f"/api/projects/{project_id}/shots/shot2/external-image-prompt"
    ).json()
    assert "只属于镜头2的动作标记" in shot2["prompt"]
    assert shot2["source_fields"]["visual_description"] != first.json()[
        "source_fields"
    ]["visual_description"]


def test_production_override_separates_keyframe_and_motion_without_rewriting_script(
    client: TestClient, database: Database
) -> None:
    project_id, script = _seed_project(database)
    keyframe = "人物站在原地，列车已经停靠，车门区域亮起但尚未打开。"
    motion = "车门缓缓打开，人物保持原地，不向前移动。"
    updated = client.put(
        f"/api/projects/{project_id}/shots/shot2/planning",
        json={"keyframe_description": keyframe, "motion_description": motion},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["planning_source"] == "LLM_WITH_HUMAN_OVERRIDE"
    prompt = client.get(
        f"/api/projects/{project_id}/shots/shot2/external-image-prompt"
    ).json()
    assert keyframe in prompt["prompt"]
    assert motion not in prompt["prompt"]
    assert prompt["source_fields"]["motion_description"] == motion
    assert prompt["lineage"]["planning_source"] == "LLM_WITH_HUMAN_OVERRIDE"
    with database.session() as session:
        project = crud.get_project(session, project_id)
        assert project is not None
        assert project.script_json == script.model_dump(mode="json")


def test_png_and_jpeg_import_preserve_existing_asset_and_provenance(
    client: TestClient, database: Database, settings, tmp_path: Path
) -> None:
    project_id, _script_value = _seed_project(database)
    png = _make_image(tmp_path / "valid.png")
    jpeg = _make_image(tmp_path / "valid.jpg")
    with database.session() as session:
        database_shot = crud.list_shots(session, project_id)[0]
        local_path = settings.project_dir(project_id) / "existing" / "animagine.png"
        local_bytes = _make_image(local_path)
        local = crud.create_asset(
            session,
            project_id=project_id,
            shot_id=database_shot.id,
            asset_type="KEYFRAME_IMAGE",
            provider_id="comfyui-animagine-xl-4",
            source_type="REAL_LOCAL_MODEL",
            file_path=local_path.relative_to(settings.data_dir).as_posix(),
            sha256=sha256_file(local_path),
            metadata_json={"shot_id": "shot1", "width": 64, "height": 40},
        )
        local_id = local.id
        session.commit()

    imported_png = _import_image(
        client, project_id, "shot1", png, filename="external.png"
    )
    imported_jpeg = _import_image(
        client,
        project_id,
        "shot2",
        jpeg,
        filename="external.jpeg",
        source_type="HUMAN_CREATED",
        provider_hint=None,
    )
    assert imported_png.status_code == imported_jpeg.status_code == 201
    payload = imported_png.json()
    assert payload["source_type"] == "EXTERNAL_IMPORT"
    assert payload["provider_id"] == "external-import"
    assert payload["generation_mode"] == "HUMAN_IN_THE_LOOP"
    assert payload["external_source_type"] == "AI_GENERATED"
    assert payload["provider_hint"] == "ChatGPT Images"
    assert payload["sha256"] == hashlib.sha256(png).hexdigest()
    assert (payload["width"], payload["height"]) == (64, 40)
    assert payload["exported_prompt"]["shot_id"] == "shot1"
    with database.session() as session:
        imported_asset = crud.get_asset(session, payload["asset_id"])
        assert imported_asset is not None
        metadata_path = Path(settings.data_dir) / imported_asset.metadata_json["sidecar_path"]
        assert metadata_path.is_file()
        assert payload["sha256"] in metadata_path.read_text(encoding="utf-8")
    assert imported_jpeg.json()["provider_hint"] is None
    assert imported_jpeg.json()["sha256"] == hashlib.sha256(jpeg).hexdigest()

    detail = client.get(f"/api/projects/{project_id}").json()
    ids = {item["asset_id"] for item in detail["image_assets"]}
    assert {local_id, payload["asset_id"], imported_jpeg.json()["asset_id"]} <= ids
    assert detail["visual_selection"] == {
        "source_image_asset_ids": {
            "shot1": payload["asset_id"],
            "shot2": imported_jpeg.json()["asset_id"],
        },
        "source_video_job_id": None,
    }
    selected = client.put(
        f"/api/projects/{project_id}/visual-selection",
        json={
            "source_image_asset_ids": {"shot1": payload["asset_id"]},
            "source_video_job_id": None,
        },
    )
    assert selected.status_code == 200, selected.text
    refreshed = client.get(f"/api/projects/{project_id}").json()
    assert refreshed["visual_selection"] == selected.json()
    assert refreshed["visual_selection"]["source_image_asset_ids"]["shot1"] == payload[
        "asset_id"
    ]
    refreshed_ids = {item["asset_id"] for item in refreshed["image_assets"]}
    assert {local_id, payload["asset_id"], imported_jpeg.json()["asset_id"]} <= refreshed_ids
    assert local_path.read_bytes() == local_bytes
    with database.session() as session:
        assert session.get(Asset, local_id) is not None


def test_external_import_rejects_empty_malformed_and_path_traversal(
    client: TestClient, database: Database
) -> None:
    project_id, _script_value = _seed_project(database)
    empty = _import_image(client, project_id, "shot1", b"", filename="empty.png")
    malformed = _import_image(
        client, project_id, "shot1", b"not-an-image", filename="broken.png"
    )
    traversal = _import_image(
        client, project_id, "shot1", b"not-an-image", filename="../escape.png"
    )
    assert empty.status_code == 422
    assert malformed.status_code == 422
    assert traversal.status_code == 409


def test_explicit_asset_binding_rejects_cross_project_wrong_shot_and_missing(
    client: TestClient, database: Database, tmp_path: Path
) -> None:
    project_id, _script_value = _seed_project(database)
    other_project_id, _other_script = _seed_project(database)
    png = _make_image(tmp_path / "binding.png")
    own = _import_image(client, project_id, "shot1", png, filename="own.png").json()
    other = _import_image(
        client, other_project_id, "shot1", png, filename="other.png"
    ).json()

    cross = client.post(
        f"/api/projects/{project_id}/render-video",
        json={"source_image_asset_ids": {"shot1": other["asset_id"]}},
    )
    wrong_shot = client.post(
        f"/api/projects/{project_id}/render-video",
        json={"source_image_asset_ids": {"shot2": own["asset_id"]}},
    )
    missing = client.post(
        f"/api/projects/{project_id}/render-video",
        json={"source_image_asset_ids": {"shot1": "missing-asset"}},
    )
    incomplete = client.post(
        f"/api/projects/{project_id}/render-video",
        json={"source_image_asset_ids": {"shot1": own["asset_id"]}},
    )
    assert cross.status_code == wrong_shot.status_code == missing.status_code == 409
    assert incomplete.status_code == 409


def test_explicit_asset_binding_rejects_missing_file(
    client: TestClient, database: Database, settings, tmp_path: Path
) -> None:
    project_id, _script_value = _seed_project(database)
    png = _make_image(tmp_path / "deleted-after-import.png")
    imported = _import_image(
        client,
        project_id,
        "shot1",
        png,
        filename="deleted-after-import.png",
    ).json()
    with database.session() as session:
        asset = crud.get_asset(session, imported["asset_id"])
        assert asset is not None
        (Path(settings.data_dir) / asset.file_path).unlink()

    response = client.post(
        f"/api/projects/{project_id}/render-video",
        json={"source_image_asset_ids": {"shot1": imported["asset_id"]}},
    )
    assert response.status_code == 409
    assert "图片资产不可用" in response.json()["detail"]


def test_singular_asset_overrides_one_shot_and_old_job_supplies_the_rest(
    client: TestClient, database: Database, settings, tmp_path: Path
) -> None:
    project_id, script = _seed_project(database)
    source_job_id = _seed_source_job(database, settings, project_id, script)
    png = _make_image(tmp_path / "singular.png")
    external = _import_image(
        client, project_id, "shot1", png, filename="singular.png"
    ).json()
    queued = client.post(
        f"/api/projects/{project_id}/render-video",
        json={
            "source_image_job_id": source_job_id,
            "source_image_asset_id": external["asset_id"],
            "video_provider": "mock-video",
        },
    )
    assert queued.status_code == 202
    with database.session() as session:
        job = crud.get_job(session, queued.json()["job_id"])
        assert job is not None
        assert job.request_json["source_image_asset_ids"] == {
            "shot1": external["asset_id"]
        }


def test_mock_video_provider_consumes_all_external_assets_without_image_job(
    client: TestClient, database: Database, settings, tmp_path: Path
) -> None:
    project_id, script = _seed_project(database)
    png = _make_image(tmp_path / "all-external.png")
    selected: dict[str, str] = {}
    for shot in script.shots:
        response = _import_image(
            client,
            project_id,
            shot.id,
            png,
            filename=f"{shot.id}.png",
        )
        assert response.status_code == 201
        selected[shot.id] = response.json()["asset_id"]

    queued = client.post(
        f"/api/projects/{project_id}/render-video",
        json={
            "source_image_asset_ids": selected,
            "video_provider": "mock-video",
        },
    )
    assert queued.status_code == 202, queued.text
    worker = Worker(
        settings=settings,
        database=database,
        video_provider_factory=lambda _settings: MockVideoProvider(
            ffmpeg_path=Path("ffmpeg.exe"), command_runner=_fake_video_runner
        ),
    )
    assert worker.run_once() is True
    job = client.get(f"/api/jobs/{queued.json()['job_id']}").json()
    assert job["status"] == "SUCCEEDED"
    assert job["result_json"]["source_image_job_id"] is None
    assert job["result_json"]["source_image_asset_ids"] == selected
    assert job["result_json"]["mock_video_fallback"] is True
    assert all(
        item["source_image_asset_id"] == selected[item["shot_id"]]
        and item["source_image_provider_id"] == "external-import"
        and item["source_image_source_type"] == "EXTERNAL_IMPORT"
        for item in job["result_json"]["video_shots"]
    )
