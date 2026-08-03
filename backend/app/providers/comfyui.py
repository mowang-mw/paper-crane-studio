"""有界 ComfyUI 本地 API ImageProvider；不导入 PyTorch 或 ComfyUI 代码。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Protocol

from .base import (
    GeneratedImageAsset,
    ImageGenerationRequest,
    ImageProvider,
    ScriptShot,
    VisualPlan,
)


PROVIDER_ID = "comfyui-animagine-xl-4"
MODEL_ID = "cagliostrolab/animagine-xl-4.0:animagine-xl-4.0-opt.safetensors"
SOURCE_TYPE = "REAL_LOCAL_MODEL"

QUALITY_TAGS = "masterpiece, high score, great score, absurdres"
PROJECT_STYLE_TAGS = (
    "original character, cinematic anime keyframe, polished anime film still, "
    "detailed background, blue and violet night lighting, safe"
)
NEGATIVE_PROMPT = (
    "lowres, bad anatomy, bad hands, extra fingers, missing fingers, malformed hands, "
    "text, watermark, logo, signature, blurry, worst quality, low quality, low score, "
    "bad score, average score, cropped"
)

ProgressCallback = Callable[[int, int, GeneratedImageAsset], None]


class ImageProviderError(RuntimeError):
    """可安全写入 Job.result_json 的真实图像生成错误。"""

    def __init__(
        self,
        code: str,
        summary: str,
        *,
        shot_id: str | None = None,
        completed_image_count: int = 0,
        retryable: bool = True,
        oom: bool = False,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        report_path: Path | None = None,
    ) -> None:
        super().__init__(summary)
        suggestions = []
        if code == "GPU_OOM":
            suggestions.append("确认 Qwen 已停止并释放显存后手动重试。")
            suggestions.append("本轮不会静默降低分辨率、步数或切换 Mock。")
        elif code in {"COMFYUI_START_FAILED", "COMFYUI_TIMEOUT"}:
            suggestions.append("检查 ComfyUI 独立环境、8188 端口及日志后手动重试。")
        elif code in {"MODEL_NOT_FOUND", "MODEL_HASH_MISMATCH"}:
            suggestions.append("检查官方模型文件及 SHA256；不会下载或替换模型。")
        else:
            suggestions.append("保留已完成且校验通过的图片，从失败镜头手动重试。")
        self.generation_error: dict[str, Any] = {
            "code": code,
            "stage": "IMAGE_GENERATION",
            "summary": summary,
            "provider_id": PROVIDER_ID,
            "model_id": MODEL_ID,
            "failed_shot_id": shot_id,
            "completed_image_count": completed_image_count,
            "retryable": retryable,
            "requires_qwen_shutdown": False,
            "oom": oom,
            "stdout_path": str(stdout_path) if stdout_path else None,
            "stderr_path": str(stderr_path) if stderr_path else None,
            "report_path": str(report_path) if report_path else None,
            "suggestions": suggestions,
        }


class _SessionFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, oom: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.oom = oom


class ComfySession(Protocol):
    stdout_path: Path
    stderr_path: Path
    system_stats: dict[str, Any] | None

    def __enter__(self) -> "ComfySession": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def generate(
        self,
        *,
        workflow: dict[str, Any],
        output_path: Path,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


def _utc_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(value.rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_stem(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return normalized or fallback


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _wait_for_port_release(host: str, port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _port_is_free(host, port):
            return True
        time.sleep(0.25)
    return _port_is_free(host, port)


def _png_info(path: Path) -> dict[str, Any]:
    """仅用标准库校验 PNG 分块 CRC、IDAT zlib 数据与完整 IEND。"""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise _SessionFailure("IMAGE_OUTPUT_MISSING", f"无法读取 PNG：{path}") from exc
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _SessionFailure("IMAGE_DECODE_FAILED", "输出不是有效 PNG 签名")

    position = 8
    width = height = bit_depth = color_type = interlace = None
    idat = bytearray()
    seen_iend = False
    while position < len(payload):
        if position + 12 > len(payload):
            raise _SessionFailure("IMAGE_DECODE_FAILED", "PNG 分块被截断")
        length = struct.unpack(">I", payload[position : position + 4])[0]
        chunk_type = payload[position + 4 : position + 8]
        data_start = position + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(payload):
            raise _SessionFailure("IMAGE_DECODE_FAILED", "PNG 分块数据不完整")
        chunk_data = payload[data_start:data_end]
        expected_crc = struct.unpack(">I", payload[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise _SessionFailure("IMAGE_DECODE_FAILED", "PNG 分块 CRC 校验失败")
        if chunk_type == b"IHDR":
            if length != 13:
                raise _SessionFailure("IMAGE_DECODE_FAILED", "PNG IHDR 长度无效")
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
        elif chunk_type == b"IDAT":
            idat.extend(chunk_data)
        elif chunk_type == b"IEND":
            seen_iend = True
            position = crc_end
            break
        position = crc_end

    if not seen_iend or position != len(payload):
        raise _SessionFailure("IMAGE_DECODE_FAILED", "PNG 缺少完整 IEND 或包含尾随损坏数据")
    if not width or not height or not idat:
        raise _SessionFailure("IMAGE_DECODE_FAILED", "PNG 缺少 IHDR 或图像数据")
    try:
        decoded = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise _SessionFailure("IMAGE_DECODE_FAILED", "PNG IDAT 无法完整解压") from exc
    if not decoded:
        raise _SessionFailure("IMAGE_DECODE_FAILED", "PNG 解压后没有像素扫描线")
    if interlace == 0:
        channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
        channels = channels_by_type.get(int(color_type))
        if channels is None or int(bit_depth) not in {1, 2, 4, 8, 16}:
            raise _SessionFailure("IMAGE_DECODE_FAILED", "PNG 色彩类型或位深不受支持")
        row_bytes = (int(width) * channels * int(bit_depth) + 7) // 8
        expected_length = (row_bytes + 1) * int(height)
        if len(decoded) != expected_length:
            raise _SessionFailure("IMAGE_DECODE_FAILED", "PNG 解压扫描线长度不完整")
    return {
        "format": "PNG",
        "width": int(width),
        "height": int(height),
        "complete_decode": True,
        "interlaced": bool(interlace),
    }


_TAG_TRANSLATIONS: tuple[tuple[str, str], ...] = (
    ("深夜旧书店", "inside an old bookstore at midnight, wooden bookshelves"),
    ("旧书店", "old bookstore, wooden bookshelves"),
    ("发光画册", "open glowing picture book, magical blue light"),
    ("发光的画册", "open glowing picture book, magical blue light"),
    ("蓝色发光鲸鱼", "glowing blue whale"),
    ("蓝色鲸鱼", "blue whale"),
    ("鲸鱼", "whale"),
    ("从书页中游出", "emerging and floating above the book"),
    ("从书页", "emerging from book pages"),
    ("少女", "1girl, original young woman"),
    ("女孩", "1girl, original young woman"),
    ("少年", "1boy, original teenage boy"),
    ("男孩", "1boy, original teenage boy"),
    ("深色短发", "dark short hair"),
    ("蓝色短发", "dark blue bob cut"),
    ("白色长发", "long white hair"),
    ("银色长发", "long silver hair"),
    ("棕色长发", "long brown hair"),
    ("黑色长发", "long black hair"),
    ("头发略长", "medium-length dark hair"),
    ("双马尾", "twin tails"),
    ("高马尾", "high ponytail"),
    ("马尾", "ponytail"),
    ("黑色连帽卫衣", "black hoodie"),
    ("连帽卫衣", "hoodie"),
    ("深色牛仔裤", "dark denim jeans"),
    ("牛仔裤", "denim jeans"),
    ("眼神略带疲惫", "slightly tired eyes"),
    ("琥珀色", "amber eyes"),
    ("通体蓝色", "blue body"),
    ("身体修长", "slender body"),
    ("蓝色眼睛", "blue eyes"),
    ("棕色眼睛", "brown eyes"),
    ("浅色居家服", "light-colored modest home clothes"),
    ("黑色外套", "black coat"),
    ("深色披肩", "dark shawl"),
    ("长裙", "long skirt"),
    ("雨夜", "rainy night"),
    ("夜晚", "night"),
    ("深夜", "midnight"),
    ("黎明", "dawn"),
    ("晨光", "warm dawn light"),
    ("月光", "soft moonlight"),
    ("蓝紫色", "blue and violet lighting"),
    ("城市", "city skyline"),
    ("屋顶", "rooftops"),
    ("云层", "clouds"),
    ("星光", "starlight"),
    ("画册", "picture book"),
    ("书页", "book pages"),
    ("微光", "soft magical glow"),
    ("窗边", "beside a window"),
    ("纸鹤", "glowing paper crane"),
    ("银杏叶", "golden ginkgo leaf"),
    ("灯火", "warm city lights"),
)


def _deduplicate_tags(values: list[str]) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        for tag in (item.strip() for item in value.split(",")):
            key = tag.casefold()
            if tag and key not in seen:
                seen.add(key)
                result.append(tag)
    return ", ".join(result)


def _semantic_tags(value: str, *, fallback: str) -> str:
    remaining = value
    translated: list[str] = []
    for chinese, english in _TAG_TRANSLATIONS:
        if chinese in remaining:
            translated.append(english)
            remaining = remaining.replace(chinese, " ")
    ascii_fragments = re.findall(r"[A-Za-z][A-Za-z0-9 _-]{1,80}", remaining)
    translated.extend(fragment.strip().lower() for fragment in ascii_fragments)
    return _deduplicate_tags(translated) or fallback


def character_anchor(character: Any) -> str:
    """相同 Character 总是得到逐字一致的基础外观锚点。"""

    combined = ", ".join(
        [
            str(character.appearance),
            str(character.costume),
            str(character.consistency_prompt),
        ]
    )
    return _semantic_tags(combined, fallback="original character, consistent appearance")


def build_prompt_layers(request: ImageGenerationRequest) -> dict[str, str]:
    character_tags = [character_anchor(item) for item in request.characters]
    scene_source = ", ".join(
        [
            request.scene.description,
            request.scene.time,
            request.scene.lighting,
            request.scene.consistency_prompt,
        ]
    )
    camera_tags = _semantic_tags(request.shot.camera, fallback="medium wide shot")
    return {
        "quality": QUALITY_TAGS,
        "project_style": PROJECT_STYLE_TAGS,
        "shared_character_anchors": _deduplicate_tags(character_tags),
        "scene": _semantic_tags(scene_source, fallback="detailed story environment"),
        "visual_description": _semantic_tags(
            request.shot.visual_description,
            fallback="clear narrative action",
        ),
        "shot_image_prompt": _semantic_tags(
            request.shot.image_prompt,
            fallback="anime story illustration",
        ),
        "composition": camera_tags,
        "format": "horizontal composition, 16:9, anime movie keyframe, no text, no watermark",
    }


def build_positive_prompt(request: ImageGenerationRequest) -> tuple[str, dict[str, str]]:
    layers = build_prompt_layers(request)
    return _deduplicate_tags(list(layers.values())), layers


def deterministic_shot_seed(base_seed: int, shot_index: int) -> int:
    if shot_index < 1:
        raise ValueError("shot_index 必须从 1 开始")
    seed = base_seed + shot_index
    if seed >= 2**63:
        raise ValueError("派生 shot seed 超出 ComfyUI 支持范围")
    return seed


def make_workflow(
    *,
    model_filename: str,
    positive_prompt: str,
    negative_prompt: str,
    seed: int,
    request: ImageGenerationRequest,
    filename_prefix: str,
) -> dict[str, Any]:
    options = request.options
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": model_filename},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": positive_prompt, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": options.width,
                "height": options.height,
                "batch_size": options.batch_size,
            },
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": options.steps,
                "cfg": options.cfg,
                "sampler_name": options.sampler,
                "scheduler": options.scheduler,
                "denoise": options.denoise,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": filename_prefix, "images": ["6", 0]},
        },
    }


class ComfyUIJobSession:
    """一次 Job 一个进程；上下文退出时总是回收进程树并确认端口释放。"""

    def __init__(
        self,
        *,
        python_executable: Path,
        comfy_root: Path,
        model_path: Path,
        run_dir: Path,
        host: str,
        port: int,
        lowvram: bool,
        startup_timeout_seconds: float,
        http_timeout_seconds: float,
    ) -> None:
        self.python_executable = Path(python_executable).resolve()
        self.comfy_root = Path(comfy_root).resolve()
        self.model_path = Path(model_path).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.host = host
        self.port = port
        self.lowvram = lowvram
        self.startup_timeout_seconds = startup_timeout_seconds
        self.http_timeout_seconds = http_timeout_seconds
        self.base_url = f"http://{host}:{port}"
        self.stdout_path = self.run_dir / "comfyui.stdout.log"
        self.stderr_path = self.run_dir / "comfyui.stderr.log"
        self.system_stats: dict[str, Any] | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout_handle: Any = None
        self._stderr_handle: Any = None

    def __enter__(self) -> "ComfyUIJobSession":
        try:
            self._start()
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _start(self) -> None:
        if not self.python_executable.is_file():
            raise _SessionFailure(
                "COMFYUI_START_FAILED",
                f"ComfyUI 独立 Python 不存在：{self.python_executable}",
            )
        if not (self.comfy_root / "main.py").is_file():
            raise _SessionFailure(
                "COMFYUI_START_FAILED",
                f"ComfyUI main.py 不存在：{self.comfy_root}",
            )
        if not _port_is_free(self.host, self.port):
            raise _SessionFailure(
                "COMFYUI_START_FAILED",
                f"ComfyUI 端口 {self.port} 已被占用，拒绝启动第二个实例",
            )

        self.run_dir.mkdir(parents=True, exist_ok=True)
        output_dir = self.run_dir / "comfyui-output"
        temp_dir = self.run_dir / "comfyui-temp"
        user_dir = self.run_dir / "comfyui-user"
        for path in (output_dir, temp_dir, user_dir):
            path.mkdir(parents=True, exist_ok=True)
        extra_paths = self.run_dir / "extra_model_paths.yaml"
        _atomic_text(
            extra_paths,
            "anime_platform:\n"
            f"  base_path: {self.model_path.parent.as_posix()}\n"
            "  checkpoints: .",
        )
        command = [
            str(self.python_executable),
            str(self.comfy_root / "main.py"),
            "--listen",
            self.host,
            "--port",
            str(self.port),
            "--disable-auto-launch",
            "--disable-all-custom-nodes",
            "--preview-method",
            "none",
            "--extra-model-paths-config",
            str(extra_paths),
            "--output-directory",
            str(output_dir),
            "--temp-directory",
            str(temp_dir),
            "--user-directory",
            str(user_dir),
            "--database-url",
            "sqlite:///:memory:",
        ]
        if self.lowvram:
            command.append("--lowvram")
        _atomic_json(self.run_dir / "comfyui-command.json", {"args": command})

        self._stdout_handle = self.stdout_path.open("wb")
        self._stderr_handle = self.stderr_path.open("wb")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.comfy_root,
                stdin=subprocess.DEVNULL,
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                shell=False,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise _SessionFailure(
                "COMFYUI_START_FAILED", f"无法启动 ComfyUI：{exc}"
            ) from exc
        self.system_stats = self._wait_for_health()

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout or self.http_timeout_seconds,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise _SessionFailure(
                "COMFYUI_TIMEOUT", f"ComfyUI API 请求失败：{method} {path}: {exc}"
            ) from exc

    def _wait_for_health(self) -> dict[str, Any]:
        assert self.process is not None
        deadline = time.monotonic() + self.startup_timeout_seconds
        last_error = "尚未响应"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise _SessionFailure(
                    "COMFYUI_START_FAILED",
                    f"ComfyUI 在健康检查前退出，退出码 {self.process.returncode}",
                )
            try:
                value = self._request_json(
                    "GET", "/system_stats", timeout=min(5.0, self.http_timeout_seconds)
                )
                if isinstance(value, dict):
                    return value
                last_error = "system_stats 不是 JSON 对象"
            except _SessionFailure as exc:
                last_error = str(exc)
            time.sleep(1.0)
        raise _SessionFailure(
            "COMFYUI_TIMEOUT",
            f"ComfyUI 健康检查超时（{self.startup_timeout_seconds:g}s）：{last_error}",
        )

    @staticmethod
    def _image_descriptor(history_entry: dict[str, Any]) -> dict[str, Any]:
        outputs = history_entry.get("outputs")
        if isinstance(outputs, dict):
            for node_output in outputs.values():
                if not isinstance(node_output, dict):
                    continue
                images = node_output.get("images")
                if isinstance(images, list) and images and isinstance(images[0], dict):
                    return images[0]
        raise _SessionFailure("IMAGE_OUTPUT_MISSING", "ComfyUI history 没有 SaveImage 输出")

    def _download(self, descriptor: dict[str, Any], output_path: Path) -> None:
        query = urllib.parse.urlencode(
            {
                "filename": descriptor.get("filename", ""),
                "subfolder": descriptor.get("subfolder", ""),
                "type": descriptor.get("type", "output"),
            }
        )
        request = urllib.request.Request(f"{self.base_url}/view?{query}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.http_timeout_seconds) as response:
                content = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise _SessionFailure(
                "IMAGE_OUTPUT_MISSING", f"下载 ComfyUI 图片失败：{exc}"
            ) from exc
        if not content:
            raise _SessionFailure("IMAGE_OUTPUT_MISSING", "ComfyUI 返回空图片")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + ".part")
        temporary.write_bytes(content)
        os.replace(temporary, output_path)

    def generate(
        self,
        *,
        workflow: dict[str, Any],
        output_path: Path,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise _SessionFailure("COMFYUI_START_FAILED", "ComfyUI 进程未运行")
        started = time.monotonic()
        response = self._request_json(
            "POST",
            "/prompt",
            {"prompt": workflow, "client_id": str(uuid.uuid4())},
            timeout=min(self.http_timeout_seconds, timeout_seconds),
        )
        prompt_id = str(response.get("prompt_id") or "") if isinstance(response, dict) else ""
        if not prompt_id:
            raise _SessionFailure("IMAGE_GENERATION_FAILED", "ComfyUI 未返回 prompt_id")
        deadline = started + timeout_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise _SessionFailure(
                    "COMFYUI_START_FAILED",
                    f"ComfyUI 在图片生成期间退出，退出码 {self.process.returncode}",
                )
            history = self._request_json(
                "GET",
                f"/history/{prompt_id}",
                timeout=min(self.http_timeout_seconds, max(0.1, deadline - time.monotonic())),
            )
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status")
                status_text = json.dumps(status, ensure_ascii=False)
                status_string = status.get("status_str") if isinstance(status, dict) else None
                if status_string == "error":
                    oom = "out of memory" in status_text.casefold()
                    raise _SessionFailure(
                        "GPU_OOM" if oom else "IMAGE_GENERATION_FAILED",
                        f"ComfyUI 工作流执行失败：{status_text}",
                        oom=oom,
                    )
                if isinstance(entry.get("outputs"), dict) and entry["outputs"]:
                    descriptor = self._image_descriptor(entry)
                    self._download(descriptor, output_path)
                    return {
                        "prompt_id": prompt_id,
                        "generation_seconds": round(time.monotonic() - started, 3),
                        "output_descriptor": descriptor,
                        "status": status,
                    }
            time.sleep(1.0)
        raise _SessionFailure(
            "COMFYUI_TIMEOUT",
            f"单张图片生成超时（{timeout_seconds:g}s），prompt_id={prompt_id}",
        )

    def close(self) -> None:
        process = self.process
        owned_process = process is not None
        if process is not None and process.poll() is None:
            if os.name == "nt":
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                    process.wait(timeout=15.0)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        subprocess.run(
                            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                            check=False,
                            capture_output=True,
                            timeout=30.0,
                            shell=False,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            else:
                process.terminate()
            try:
                process.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10.0)
        for handle_name in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, handle_name)
            if handle is not None and not handle.closed:
                handle.close()
        if owned_process and not _wait_for_port_release(self.host, self.port, 30.0):
            raise _SessionFailure(
                "COMFYUI_START_FAILED",
                f"ComfyUI 结束后端口 {self.port} 未在超时内释放",
            )


SessionFactory = Callable[..., ComfySession]


class ComfyUIImageProvider(ImageProvider):
    """Animagine XL 4.0：一次有界 ComfyUI 生命周期顺序生成所有镜头。"""

    provider_id = PROVIDER_ID
    model_id = MODEL_ID
    source_type = SOURCE_TYPE
    _generation_lock = threading.Lock()

    def __init__(
        self,
        *,
        comfy_python: Path,
        comfy_root: Path,
        model_path: Path,
        model_sha256: str,
        host: str = "127.0.0.1",
        port: int = 8188,
        session_factory: SessionFactory | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("ComfyUI 只允许绑定本机回环地址")
        if not 1 <= port <= 65_535:
            raise ValueError("ComfyUI 端口无效")
        expected_hash = model_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("model_sha256 必须是 64 位十六进制 SHA256")
        self.comfy_python = Path(comfy_python).resolve()
        self.comfy_root = Path(comfy_root).resolve()
        self.model_path = Path(model_path).resolve()
        self.expected_model_sha256 = expected_hash
        self.host = host
        self.port = port
        self.session_factory = session_factory or ComfyUIJobSession

    def plan(self, *, shot: ScriptShot) -> VisualPlan:
        """兼容既有 GenerationService；这里只规划，绝不启动 ComfyUI。"""

        return VisualPlan(
            provider_id=self.provider_id,
            source_type=self.source_type,
            parameters={
                "seed_strategy": "base_seed_plus_shot_index",
                "shot_index": shot.shot_index,
                "image_prompt": shot.image_prompt,
                "negative_prompt": shot.negative_prompt,
            },
        )

    def generate(self, *, request: ImageGenerationRequest) -> GeneratedImageAsset:
        return self._generate_requests(
            requests=(request,),
            reusable_assets=(),
            progress_callback=None,
            enforce_job_shot_count=False,
        )[0]

    def generate_batch(
        self,
        *,
        requests: tuple[ImageGenerationRequest, ...],
        reusable_assets: tuple[GeneratedImageAsset, ...] = (),
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[GeneratedImageAsset, ...]:
        return self._generate_requests(
            requests=requests,
            reusable_assets=reusable_assets,
            progress_callback=progress_callback,
            enforce_job_shot_count=True,
        )

    def _validate_requests(
        self,
        requests: tuple[ImageGenerationRequest, ...],
        *,
        enforce_job_shot_count: bool,
    ) -> None:
        if not requests:
            raise ValueError("至少需要一个 ImageGenerationRequest")
        if enforce_job_shot_count and not 3 <= len(requests) <= 5:
            raise ValueError("真实图像 Job 必须顺序生成 3—5 个镜头")
        expected_indices = list(range(1, len(requests) + 1))
        actual_indices = [request.shot.index for request in requests]
        if enforce_job_shot_count and actual_indices != expected_indices:
            raise ValueError(
                f"图片镜头必须按 1—{len(requests)} 连续排序，实际 {actual_indices}"
            )
        first = requests[0]
        for request in requests[1:]:
            if request.project_id != first.project_id or request.job_id != first.job_id:
                raise ValueError("同一批 ImageGenerationRequest 必须属于同一 Project 和 Job")
            if request.options != first.options:
                raise ValueError("同一 Job 的所有镜头必须使用相同 GenerationOptions")
            if request.output_dir != first.output_dir:
                raise ValueError("同一 Job 的所有镜头必须写入同一受控输出目录")

    def _model_hash(self) -> str:
        if not self.model_path.is_file():
            raise ImageProviderError(
                "MODEL_NOT_FOUND",
                f"Animagine 模型文件不存在：{self.model_path}",
                retryable=False,
            )
        actual = _sha256_file(self.model_path)
        if actual != self.expected_model_sha256:
            raise ImageProviderError(
                "MODEL_HASH_MISMATCH",
                "Animagine 模型 SHA256 与配置的官方值不一致，已拒绝加载。",
                retryable=False,
            )
        return actual

    def _valid_reusable(
        self,
        asset: GeneratedImageAsset,
        *,
        request: ImageGenerationRequest,
        model_sha256: str,
        positive_prompt: str,
        seed: int,
    ) -> GeneratedImageAsset | None:
        if (
            asset.provider_id != self.provider_id
            or asset.model_id != self.model_id
            or asset.shot_id != request.shot.id
            or asset.model_sha256 != model_sha256
            or asset.width != request.options.width
            or asset.height != request.options.height
            or asset.seed != seed
            or asset.positive_prompt != positive_prompt
            or asset.negative_prompt != NEGATIVE_PROMPT
            or not asset.workflow_path.is_file()
            or not asset.trace_path.is_file()
            or not asset.image_path.is_file()
        ):
            return None
        try:
            png = _png_info(asset.image_path)
            actual_hash = _sha256_file(asset.image_path)
        except _SessionFailure:
            return None
        if (
            png["width"] != request.options.width
            or png["height"] != request.options.height
            or actual_hash != asset.image_sha256
        ):
            return None
        return replace(
            asset,
            warnings=tuple(asset.warnings) + ("reused_existing_verified_asset",),
            reused=True,
        )

    def _generate_requests(
        self,
        *,
        requests: tuple[ImageGenerationRequest, ...],
        reusable_assets: tuple[GeneratedImageAsset, ...],
        progress_callback: ProgressCallback | None,
        enforce_job_shot_count: bool,
    ) -> tuple[GeneratedImageAsset, ...]:
        self._validate_requests(requests, enforce_job_shot_count=enforce_job_shot_count)
        model_sha256 = self._model_hash()
        first = requests[0]
        output_dir = first.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        job_dir = output_dir.parent
        job_dir.mkdir(parents=True, exist_ok=True)
        report_path = job_dir / "image_generation_report.json"
        reusable_by_shot = {asset.shot_id: asset for asset in reusable_assets}
        completed: dict[str, GeneratedImageAsset] = {}
        pending: list[tuple[ImageGenerationRequest, str, dict[str, str], int]] = []
        for request in requests:
            positive, layers = build_positive_prompt(request)
            seed = deterministic_shot_seed(request.options.base_seed, request.shot.index)
            candidate = reusable_by_shot.get(request.shot.id)
            reused = (
                self._valid_reusable(
                    candidate,
                    request=request,
                    model_sha256=model_sha256,
                    positive_prompt=positive,
                    seed=seed,
                )
                if candidate is not None
                else None
            )
            if reused is not None:
                completed[request.shot.id] = reused
                if progress_callback:
                    progress_callback(len(completed), len(requests), reused)
            else:
                pending.append((request, positive, layers, seed))

        started = time.monotonic()
        session_started = False
        current_request: ImageGenerationRequest | None = None
        stdout_path = job_dir / "comfyui.stdout.log"
        stderr_path = job_dir / "comfyui.stderr.log"
        try:
            if pending:
                if not self._generation_lock.acquire(timeout=1.0):
                    raise _SessionFailure(
                        "COMFYUI_START_FAILED",
                        "已有真实 ImageProvider Job 正在运行，并发固定为 1",
                    )
                try:
                    options = first.options
                    session = self.session_factory(
                        python_executable=self.comfy_python,
                        comfy_root=self.comfy_root,
                        model_path=self.model_path,
                        run_dir=job_dir,
                        host=self.host,
                        port=self.port,
                        lowvram=options.lowvram,
                        startup_timeout_seconds=options.startup_timeout_seconds,
                        http_timeout_seconds=options.http_timeout_seconds,
                    )
                    stdout_path = session.stdout_path
                    stderr_path = session.stderr_path
                    session_started = True
                    with session:
                        for request, positive, layers, seed in pending:
                            current_request = request
                            elapsed = time.monotonic() - started
                            remaining = request.options.job_timeout_seconds - elapsed
                            if remaining <= 0:
                                raise _SessionFailure(
                                    "COMFYUI_TIMEOUT",
                                    "真实图像 Job 已超过总超时",
                                )
                            asset = self._generate_one(
                                session=session,
                                request=request,
                                positive_prompt=positive,
                                prompt_layers=layers,
                                seed=seed,
                                model_sha256=model_sha256,
                                timeout_seconds=min(
                                    request.options.generation_timeout_seconds,
                                    remaining,
                                ),
                            )
                            completed[request.shot.id] = asset
                            if progress_callback:
                                progress_callback(len(completed), len(requests), asset)
                        current_request = None
                finally:
                    self._generation_lock.release()
        except ImageProviderError:
            raise
        except _SessionFailure as exc:
            error = ImageProviderError(
                exc.code,
                str(exc),
                shot_id=current_request.shot.id if current_request else None,
                completed_image_count=len(completed),
                retryable=exc.code not in {"MODEL_NOT_FOUND", "MODEL_HASH_MISMATCH"},
                oom=exc.oom,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                report_path=report_path,
            )
            self._write_report(
                report_path,
                requests=requests,
                assets=completed,
                model_sha256=model_sha256,
                session_started=session_started,
                started=started,
                error=error.generation_error,
            )
            raise error from exc
        except Exception as exc:
            error = ImageProviderError(
                "IMAGE_GENERATION_FAILED",
                f"真实图像生成失败：{type(exc).__name__}: {exc}",
                shot_id=current_request.shot.id if current_request else None,
                completed_image_count=len(completed),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                report_path=report_path,
            )
            self._write_report(
                report_path,
                requests=requests,
                assets=completed,
                model_sha256=model_sha256,
                session_started=session_started,
                started=started,
                error=error.generation_error,
            )
            raise error from exc

        ordered = tuple(completed[request.shot.id] for request in requests)
        self._write_report(
            report_path,
            requests=requests,
            assets=completed,
            model_sha256=model_sha256,
            session_started=session_started,
            started=started,
            error=None,
        )
        return ordered

    def _generate_one(
        self,
        *,
        session: ComfySession,
        request: ImageGenerationRequest,
        positive_prompt: str,
        prompt_layers: dict[str, str],
        seed: int,
        model_sha256: str,
        timeout_seconds: float,
    ) -> GeneratedImageAsset:
        stem = f"shot-{request.shot.index:02d}"
        image_path = request.output_dir / f"{stem}.png"
        workflow_path = request.output_dir / f"{stem}.workflow.json"
        request_path = request.output_dir / f"{stem}.request.json"
        trace_path = request.output_dir / f"{stem}.result.json"
        positive_path = request.output_dir / f"{stem}.positive.txt"
        negative_path = request.output_dir / f"{stem}.negative.txt"
        workflow = make_workflow(
            model_filename=self.model_path.name,
            positive_prompt=positive_prompt,
            negative_prompt=NEGATIVE_PROMPT,
            seed=seed,
            request=request,
            filename_prefix=(
                f"{_safe_stem(request.job_id, 'job')}_{_safe_stem(request.shot.id, stem)}"
            ),
        )
        trace_input = {
            "created_at": _utc_timestamp(),
            "project_id": request.project_id,
            "job_id": request.job_id,
            "shot_id": request.shot.id,
            "shot_index": request.shot.index,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_path": str(self.model_path),
            "model_sha256": model_sha256,
            "seed_strategy": "base_seed_plus_shot_index",
            "base_seed": request.options.base_seed,
            "seed": seed,
            "options": request.options.as_dict(),
            "original_chinese": {
                "character_appearance": [item.appearance for item in request.characters],
                "character_costume": [item.costume for item in request.characters],
                "character_consistency_prompt": [
                    item.consistency_prompt for item in request.characters
                ],
                "scene_description": request.scene.description,
                "scene_time": request.scene.time,
                "scene_lighting": request.scene.lighting,
                "scene_consistency_prompt": request.scene.consistency_prompt,
                "visual_description": request.shot.visual_description,
                "image_prompt": request.shot.image_prompt,
                "negative_prompt": request.shot.negative_prompt,
                "camera": request.shot.camera,
            },
            "prompt_layers": prompt_layers,
            "positive_prompt": positive_prompt,
            "negative_prompt": NEGATIVE_PROMPT,
            "workflow_path": str(workflow_path),
        }
        _atomic_json(workflow_path, workflow)
        _atomic_json(request_path, trace_input)
        _atomic_text(positive_path, positive_prompt)
        _atomic_text(negative_path, NEGATIVE_PROMPT)

        try:
            session_result = session.generate(
                workflow=workflow,
                output_path=image_path,
                timeout_seconds=timeout_seconds,
            )
            if not image_path.is_file() or image_path.stat().st_size <= 0:
                raise _SessionFailure(
                    "IMAGE_OUTPUT_MISSING", f"镜头 {request.shot.id} 未生成 PNG"
                )
            png = _png_info(image_path)
            if (
                png["width"] != request.options.width
                or png["height"] != request.options.height
            ):
                raise _SessionFailure(
                    "IMAGE_DECODE_FAILED",
                    f"镜头 {request.shot.id} PNG 分辨率不符："
                    f"要求 {request.options.width}x{request.options.height}，"
                    f"实际 {png['width']}x{png['height']}",
                )
            image_sha256 = _sha256_file(image_path)
            generation_seconds = float(session_result.get("generation_seconds", 0.0))
            warnings = (
                "prompt_translation_is_deterministic_tag_mapping_not_an_llm_translation",
                "fixed_seed_is_not_a_strict_character_consistency_solution",
            )
            result_payload = {
                **trace_input,
                "success": True,
                "finished_at": _utc_timestamp(),
                "generation_seconds": generation_seconds,
                "image_path": str(image_path),
                "image_sha256": image_sha256,
                "png_validation": png,
                "session_result": session_result,
                "warnings": list(warnings),
                "source_type": self.source_type,
                "lowvram": request.options.lowvram,
                "oom_retry": False,
            }
            _atomic_json(trace_path, result_payload)
            return GeneratedImageAsset(
                provider_id=self.provider_id,
                model_id=self.model_id,
                shot_id=request.shot.id,
                image_path=image_path,
                width=request.options.width,
                height=request.options.height,
                seed=seed,
                positive_prompt=positive_prompt,
                negative_prompt=NEGATIVE_PROMPT,
                generation_seconds=generation_seconds,
                image_sha256=image_sha256,
                model_sha256=model_sha256,
                workflow_path=workflow_path,
                trace_path=trace_path,
                warnings=warnings,
            )
        except _SessionFailure as exc:
            _atomic_json(
                trace_path,
                {
                    **trace_input,
                    "success": False,
                    "finished_at": _utc_timestamp(),
                    "error_code": exc.code,
                    "error": str(exc),
                    "oom": exc.oom,
                },
            )
            raise

    def _write_report(
        self,
        path: Path,
        *,
        requests: tuple[ImageGenerationRequest, ...],
        assets: dict[str, GeneratedImageAsset],
        model_sha256: str,
        session_started: bool,
        started: float,
        error: dict[str, Any] | None,
    ) -> None:
        ordered_assets = [
            assets[request.shot.id].as_dict()
            for request in requests
            if request.shot.id in assets
        ]
        _atomic_json(
            path,
            {
                "report_version": "m4.image-generation-report.v1",
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "model_path": str(self.model_path),
                "model_sha256": model_sha256,
                "project_id": requests[0].project_id,
                "job_id": requests[0].job_id,
                "base_seed": requests[0].options.base_seed,
                "requested_count": len(requests),
                "completed_count": len(ordered_assets),
                "generation_seconds_total": round(time.monotonic() - started, 3),
                "comfyui_started": session_started,
                "comfyui_start_count": 1 if session_started else 0,
                "sequential_generation": True,
                "max_concurrency": 1,
                "lowvram": requests[0].options.lowvram,
                "automatic_parameter_downgrade": False,
                "mock_fallback": False,
                "assets": ordered_assets,
                "error": error,
                "finished_at": _utc_timestamp(),
            },
        )


__all__ = [
    "ComfyUIImageProvider",
    "ComfyUIJobSession",
    "ImageProviderError",
    "MODEL_ID",
    "NEGATIVE_PROMPT",
    "PROVIDER_ID",
    "SOURCE_TYPE",
    "build_positive_prompt",
    "build_prompt_layers",
    "character_anchor",
    "deterministic_shot_seed",
    "make_workflow",
]
