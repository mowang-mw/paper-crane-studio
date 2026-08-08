from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.app.config import Settings
from backend.app.providers.llama_cpp import (
    LlamaCppOutputError,
    LlamaCppScriptProvider,
    LlamaCppTransportError,
)
from backend.app.providers.llama_server import (
    LlamaServerInspection,
    LlamaServerJobSession,
    LlamaServerLifecycleError,
)
from backend.app.providers.registry import check_llamacpp
from backend.tests.test_m3_script_provider import (
    completion_envelope,
    valid_script_payload,
)


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 43210
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def _session_files(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    executable.write_bytes(b"fake-executable")
    model.write_bytes(b"fake-model")
    return executable, model


def test_job_session_starts_waits_and_cleans_up_owned_server(tmp_path: Path) -> None:
    executable, model = _session_files(tmp_path)
    inspections = iter(
        [
            LlamaServerInspection("READY_TO_START", "not running"),
            LlamaServerInspection(
                "ONLINE", "ready", server_version="test-build", model_ids=("model",)
            ),
        ]
    )
    process = FakeProcess()
    started: list[dict[str, Any]] = []
    terminated: list[int] = []

    def process_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        started.append({"command": command, "kwargs": kwargs})
        return process

    def terminate(candidate: FakeProcess) -> None:
        terminated.append(candidate.pid)
        candidate.returncode = 0

    session = LlamaServerJobSession(
        executable=executable,
        model_path=model,
        model_id="model",
        base_url="http://127.0.0.1:8081",
        run_dir=tmp_path / "run",
        context_size=8192,
        gpu_layers=99,
        startup_timeout_seconds=2,
        health_timeout_seconds=0.1,
        process_factory=process_factory,
        inspection_factory=lambda: next(inspections),
        process_terminator=terminate,
        port_release_waiter=lambda _host, _port, _timeout: True,
        sleep=lambda _seconds: None,
    )

    with session:
        assert session.ownership == "OWNED_BY_JOB"
        assert session.ready_inspection is not None
        assert session.ready_inspection.state == "ONLINE"

    assert len(started) == 1
    assert "--reasoning" in started[0]["command"]
    assert terminated == [process.pid]
    assert session.snapshot()["cleaned_up"] is True


def test_external_compatible_server_is_reused_and_never_terminated(tmp_path: Path) -> None:
    process_calls: list[object] = []
    terminated: list[object] = []
    session = LlamaServerJobSession(
        executable=tmp_path / "missing.exe",
        model_path=tmp_path / "missing.gguf",
        model_id="model",
        base_url="http://127.0.0.1:8081",
        run_dir=tmp_path / "run",
        context_size=8192,
        gpu_layers=99,
        startup_timeout_seconds=2,
        health_timeout_seconds=0.1,
        process_factory=lambda *args, **kwargs: process_calls.append((args, kwargs)),
        inspection_factory=lambda: LlamaServerInspection(
            "ONLINE", "external ready", server_version="build", model_ids=("model",)
        ),
        process_terminator=lambda process: terminated.append(process),
    )

    with session:
        assert session.ownership == "EXTERNAL_REUSED"

    assert process_calls == []
    assert terminated == []
    assert session.snapshot()["external_server_reused"] is True


def test_unknown_process_on_port_is_rejected_without_kill(tmp_path: Path) -> None:
    process_calls: list[object] = []
    terminated: list[object] = []
    session = LlamaServerJobSession(
        executable=tmp_path / "llama-server.exe",
        model_path=tmp_path / "model.gguf",
        model_id="model",
        base_url="http://127.0.0.1:8081",
        run_dir=tmp_path / "run",
        context_size=8192,
        gpu_layers=99,
        startup_timeout_seconds=2,
        health_timeout_seconds=0.1,
        process_factory=lambda *args, **kwargs: process_calls.append((args, kwargs)),
        inspection_factory=lambda: LlamaServerInspection(
            "PORT_CONFLICT", "8081 belongs to an unknown process"
        ),
        process_terminator=lambda process: terminated.append(process),
    )

    with pytest.raises(LlamaServerLifecycleError) as captured:
        with session:
            pass

    assert captured.value.code == "LLAMA_SERVER_PORT_CONFLICT"
    assert process_calls == []
    assert terminated == []


def test_qwen_session_refuses_to_overlap_with_comfyui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_calls: list[object] = []
    monkeypatch.setattr(
        "backend.app.providers.llama_server._port_is_listening",
        lambda _host, port, *args: port == 8188,
    )
    session = LlamaServerJobSession(
        executable=tmp_path / "llama-server.exe",
        model_path=tmp_path / "model.gguf",
        model_id="model",
        base_url="http://127.0.0.1:8081",
        run_dir=tmp_path / "run",
        context_size=8192,
        gpu_layers=99,
        startup_timeout_seconds=2,
        health_timeout_seconds=0.1,
        blocked_gpu_ports=(8188,),
        process_factory=lambda *args, **kwargs: process_calls.append((args, kwargs)),
    )

    with pytest.raises(LlamaServerLifecycleError) as captured:
        with session:
            pass

    assert captured.value.code == "GPU_HANDOFF_REQUIRED"
    assert process_calls == []


class RecordingSession:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def __enter__(self) -> "RecordingSession":
        self.entered += 1
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.exited += 1

    def snapshot(self) -> dict[str, object]:
        return {"ownership": "OWNED_BY_JOB", "cleaned_up": self.exited == 1}


def _provider(
    tmp_path: Path,
    handler: Any,
    session: RecordingSession,
) -> LlamaCppScriptProvider:
    return LlamaCppScriptProvider(
        base_url="http://127.0.0.1:8081/v1",
        model="model",
        response_dir=tmp_path / "responses",
        timeout_seconds=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        server_session_factory=lambda: session,
    )


def test_provider_cleanup_runs_after_success_and_trace_records_ownership(
    tmp_path: Path,
) -> None:
    session = RecordingSession()
    content = json.dumps(valid_script_payload(), ensure_ascii=False)
    provider = _provider(
        tmp_path,
        lambda _request: httpx.Response(200, json=completion_envelope(content)),
        session,
    )

    provider.generate(title="夜航", story="少女与纸鹤飞过夜空。")

    assert session.entered == 1
    assert session.exited == 1
    assert provider.last_trace is not None
    assert provider.last_trace["server_lifecycle"]["cleaned_up"] is True


def test_provider_cleanup_runs_after_generation_failure(tmp_path: Path) -> None:
    session = RecordingSession()

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection lost", request=request)

    provider = _provider(tmp_path, fail, session)
    with pytest.raises(LlamaCppTransportError):
        provider.generate(title="夜航", story="少女与纸鹤飞过夜空。")

    assert session.exited == 1
    assert provider.last_trace is not None
    assert provider.last_trace["server_lifecycle"]["cleaned_up"] is True


def test_provider_cleanup_runs_after_all_repairs_fail(tmp_path: Path) -> None:
    session = RecordingSession()
    provider = _provider(
        tmp_path,
        lambda _request: httpx.Response(
            200, json=completion_envelope("```json\n{}\n```")
        ),
        session,
    )

    with pytest.raises(LlamaCppOutputError) as captured:
        provider.generate(title="夜航", story="少女与纸鹤飞过夜空。")

    assert captured.value.generation_error is not None
    assert captured.value.generation_error["code"] == "REPAIR_FAILED"
    assert session.exited == 1
    assert provider.last_trace is not None
    assert provider.last_trace["server_lifecycle"]["cleaned_up"] is True


def test_configured_offline_provider_is_ready_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, model = _session_files(tmp_path)
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        llama_server_executable=executable,
        llama_model_path=model,
    )
    monkeypatch.setattr(
        "backend.app.providers.registry.inspect_llama_server",
        lambda **_kwargs: LlamaServerInspection(
            "READY_TO_START", "configured and idle"
        ),
    )

    status = check_llamacpp(settings)

    assert status["configured"] is True
    assert status["available"] is True
    assert status["runtime_state"] == "READY_TO_START"
