# M4-B 真实 ImageProvider 实施记录

## 1. 本阶段结论与边界

M4-B 把 M4-A 已验证的 `ComfyUI + Animagine XL 4.0 Opt` 封装为平台正式 `ImageProvider`，形成以下纵向链路：

```text
成功 Job 的最终 ScriptV1
  -> 冻结新的受控 ScriptV1 快照（不调用 Qwen）
  -> ComfyUIImageProvider 在一次有界 ComfyUI 会话中顺序生成 3—5 张真实 PNG
  -> 公共 FFmpeg 管线做确定性轻运镜、Mock 音频和中文字幕烧录
  -> ffprobe 验证 1280×720、24 fps、H.264/AAC MP4
  -> 保存 Export、Assets、Manifest 与完整追溯
```

实现保留 M0—M3 的 Mock 工程保底，但两条路径始终显式区分。真实 ImageProvider 失败时 Job 必须 FAILED，不会静默生成 Mock 图、不会更换模型、不会降低参数后继续标记成功。

本阶段不包含角色一致性高级方案、IP-Adapter、ControlNet、LoRA、自定义节点、真实 TTS、视频生成模型或 M5。重复共享角色标签和固定 seed 只是基础提示一致性与可追溯手段，不能宣称解决严格角色一致性。

## 2. 固定环境与模型

M4-B 复用 M4-A 已安装并通过单张冒烟的独立环境，不重新安装依赖：

| 项目 | 固定值 |
|---|---|
| 平台后端环境 | Conda `anime-platform`，Python 3.11 |
| ComfyUI 环境 | 项目内 `.venv-comfyui`，Python 3.13.3 |
| ComfyUI commit | `f06a187f50f896e4a0ba5be1ce1f2d2dcd13b77b` |
| PyTorch | `2.11.0+cu128` |
| CUDA runtime | 12.8 |
| ImageProvider ID | `comfyui-animagine-xl-4` |
| 模型 | `cagliostrolab/animagine-xl-4.0` / `animagine-xl-4.0-opt.safetensors` |
| 模型路径 | `models/image/animagine-xl-4.0-opt.safetensors` |
| 模型 SHA256 | `6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac` |
| 许可证 | CreativeML Open RAIL++-M |
| GPU | NVIDIA RTX 4060 Laptop，8GB VRAM |

ComfyUI、独立环境、模型、临时目录和生成结果均在 `.gitignore` 范围内。后端 Python 不导入 PyTorch 或 ComfyUI 源码，只通过本机回环 HTTP API 调用由自己启动的有界子进程，因此不会污染 `anime-platform`。

## 3. 实现模块

| 模块 | M4-B 职责 |
|---|---|
| `backend/app/providers/base.py` | `ImageGenerationOptions`、`ImageGenerationRequest`、`GeneratedImageAsset` 与批量接口 |
| `backend/app/providers/comfyui.py` | 提示分层、seed、内置节点工作流、PNG 校验、复用判断、有界 ComfyUI 会话和错误映射 |
| `backend/app/providers/registry.py` | 返回 Mock/真实 ImageProvider 的配置、版本与 GPU 交接状态 |
| `backend/app/services/image_jobs.py` | GPU 冲突诊断、来源 ScriptV1 安全读取、快照写入和再次校验 |
| `backend/app/api/projects.py` | 创建 `GENERATE_REAL_IMAGE_VIDEO` Job 的 API 与不可变 request 快照 |
| `backend/app/worker.py` | 不调用 ScriptProvider 的真实图像编排、逐图进度/Asset、媒体导出、失败与手动恢复 |
| `backend/app/media/mock_pipeline.py` | 严格真实 PNG 入口、Ken Burns 运镜、Mock 音轨、烧录字幕、媒体容差与 manifest |
| `backend/app/api/media.py` | 按 Project/Asset 归属受控读取真实 PNG 缩略图 |
| `frontend/src/*` | Provider 状态、GPU 提示、真实图像按钮、逐图进度/缩略图、真实成片播放下载 |
| `scripts/m4_image_smoke_test.py` | M4-A 单张有界冒烟与历史证据入口；正式 Provider 抽取并沿用其已验证的生命周期原则和内置节点工作流 |
| `scripts/m4_real_image_e2e.py` | M4-B 有界三镜头真实 E2E；优先复用已有 ScriptV1，不启动 Qwen |

## 4. ImageProvider 契约

### 4.1 输入

`ImageGenerationRequest` 明确包含：

- `project_id`、`job_id`；
- 完整、严格通过校验的 `ScriptV1`；
- 当前 `shot`；
- 按 `shot.character_ids` 原顺序提供的 `characters`；
- 与 `shot.scene_id` 一致的 `scene`；
- 由平台分配的 `output_dir`；
- 不可变 `ImageGenerationOptions`。

请求构造时即验证 shot、scene、characters 和 ScriptV1 的归属关系。同一批 3—5 个请求还必须属于同一 Project/Job、使用相同选项和输出目录，并按 index 1…N 连续排序。

默认选项：

| 参数 | 默认值 |
|---|---:|
| width × height | 1024 × 576 |
| batch size | 1 |
| steps | 24 |
| CFG | 5.0 |
| sampler | `euler_ancestral` |
| scheduler | `normal` |
| denoise | 1.0 |
| lowvram | `true` |
| 启动超时 | 240 秒 |
| 单张生成超时 | 600 秒 |
| 整个图像 Job 超时 | 3600 秒 |
| 单次 HTTP 超时 | 30 秒 |
| 默认 base seed | 20260802 |

这些参数在 API 入队时写入 `request_json`，Worker 只从快照恢复，不读取前端后来变化的值。M4-B 不实现 OOM 多档自动降级。

### 4.2 输出

每张 `GeneratedImageAsset` 至少返回：

- Provider/model/shot ID；
- PNG 路径、宽高和 SHA256；
- base seed 派生后的 shot seed；
- 完整正向/负向提示；
- 单张生成耗时；
- 模型 SHA256；
- workflow 和 result trace 路径；
- 警告及是否来自合法重试复用。

媒体层不会仅相信 DTO：它会重新计算图片 SHA256，用 ffprobe 确认单 PNG 视频流及尺寸，并用 FFmpeg 完整解码一帧。

## 5. ScriptV1 复用与零 Qwen 调用

API 为：

```http
POST /api/projects/{project_id}/render-real-images
Content-Type: application/json

{
  "source_script_job_id": "<成功 Job ID>",
  "image_provider": "comfyui-animagine-xl-4",
  "base_seed": 20260802
}
```

`base_seed` 可省略，省略时使用受控配置默认值。来源必须是当前 Project 下状态为 `SUCCEEDED` 的 `GENERATE_SHORT_VIDEO` Job，且来源最终 ScriptV1 必须能从受控追溯读取并与 Project 当前剧本完全一致。

新 Job 的 request 快照记录：

- `script_provider=reused`；
- `source_script_provider` 与 `source_script_source_type`；
- `source_script_job_id` / `reuse_script_from_job_id`；
- ScriptV1 快照的相对路径、SHA256 与所有者 Job ID；
- `script_provider_calls_expected=0`；
- `image_provider=comfyui-animagine-xl-4`；
- `audio_provider=mock`；
- base seed、完整图像选项、输出规格和镜头数。

Worker 执行时再次校验快照路径仍位于当前 Job 受控目录、文件 SHA256、schema、来源 ID、Project 当前剧本、故事字符数和数据库镜头内容。规划阶段虽然复用既有 `GenerationService` 的 `prepare_validated_script()`，但该方法不调用 `ScriptProvider.generate()`；结果和 manifest 均记录 `script_provider_calls=0`。来源是 Mock 时继续写 `source_script_provider=mock`，不伪装成 Qwen。

## 6. 8GB GPU 互斥

用户流程固定为：

1. 使用 Qwen 或 Mock 生成并保存 ScriptV1；
2. 如使用 Qwen，停止 `llama-server`，确认 8081 和推理显存释放；
3. 点击“使用当前剧本生成真实动漫画面”；
4. Worker 启动有界 ComfyUI 并顺序生成关键帧；
5. ComfyUI 退出后才执行完整媒体导出。

FastAPI 在入队前、Worker 在真正生成前各检查一次 8081 和 `llama-server`。发现冲突时返回：

> 本机8GB显存模式需要先停止Qwen服务，再开始真实图像生成。

错误码为 `GPU_HANDOFF_REQUIRED`。检查只读，不调用 `Stop-Process` 或 `taskkill` 处理外部 Qwen；用户释放资源后可显式手动重试。

## 7. ComfyUI 有界生命周期

`ComfyUIJobSession` 使用 `subprocess.Popen` 参数列表、`shell=False` 和 Windows 新进程组启动：

```text
<.venv-comfyui python> tools/ComfyUI/main.py
  --listen 127.0.0.1 --port 8188
  --disable-auto-launch
  --disable-all-custom-nodes
  --preview-method none
  --lowvram
  --database-url sqlite:///:memory:
  --output-directory/--temp-directory/--user-directory <当前 Job 目录>
```

生命周期规则：

1. 启动前验证独立 Python、`main.py`、模型文件与模型 SHA256，且 8188 必须空闲；
2. 在启动超时内轮询 `/system_stats`；
3. 一个 Job 只进入一次 Session，上下文内按 shot index 调用 `/prompt` 和 `/history/{prompt_id}`；
4. 单图、HTTP 和整个 Job 都有独立上限，轮询不是无限等待；
5. 并发由进程内 generation lock 和唯一 Worker 双重限制为 1；
6. `finally` 中先尝试 `CTRL_BREAK_EVENT` 和有限等待，失败后才使用受控进程树终止；
7. 无论成功或失败，都关闭日志句柄并在有限时间内确认 8188 释放。

首次完整 3—5 镜头 Job 的 `comfyui_start_count` 应为 1。手动重试若所有图片均经严格校验后复用，则可以为 0；这表示没有无意义地重新加载模型，不表示跳过了图片真实性检查。

## 8. 内置节点工作流、提示词与 seed

每个镜头工作流仅含 ComfyUI 内置节点：

- `CheckpointLoaderSimple`；
- 正向/负向 `CLIPTextEncode`；
- `EmptyLatentImage`；
- `KSampler`；
- `VAEDecode`；
- `SaveImage`。

正向提示不是把整个中文 ScriptV1 原样交给模型，而是按顺序合并并去重：

1. `masterpiece, high score, great score, absurdres`；
2. 项目动漫电影风格和安全内容；
3. 当前镜头角色的共享外观锚点；
4. 当前 Scene 的描述、时间、光线和一致性提示；
5. `shot.visual_description`；
6. `shot.image_prompt`；
7. `shot.camera` 对应的构图/景别；
8. 横向 16:9 动漫电影关键帧、无文字和无水印。

当前中文语义通过固定、可审计的英文标签映射转换，不增加 LLM 请求。相同 Character 的 appearance、costume 和 consistency prompt 总是生成逐字一致的锚点；未知内容不会被伪造为精确翻译，而会使用明确 fallback 与 warning。

负向提示复用 M4-A：

```text
lowres, bad anatomy, bad hands, extra fingers, missing fingers, malformed hands,
text, watermark, logo, signature, blurry, worst quality, low quality, low score,
bad score, average score, cropped
```

seed 公式固定为：

```text
shot_seed = base_seed + shot.index
```

不使用 Python `hash()`。重试复用原 request 快照，因此 base/shot seed 保持不变；不同镜头使用不同 seed，固定 seed 不被宣传为角色一致性方案。

## 9. 文件与追溯

真实图像 Job 使用：

```text
data/projects/<project-id>/jobs/<job-id>/
  script-source.json
  images/
    shot-01.png
    shot-01.workflow.json
    shot-01.request.json
    shot-01.result.json
    shot-01.positive.txt
    shot-01.negative.txt
    ...
  comfyui-command.json
  comfyui.stdout.log
  comfyui.stderr.log
  image_generation_report.json
  comfyui-output/
  comfyui-temp/
  comfyui-user/

data/projects/<project-id>/exports/<job-id>/
  short_<job-id>.mp4
  manifest.json
  subtitles.srt
  shots/
  subtitles/
```

逐图 trace 保留原始中文输入、提示分层、最终正负提示、workflow、参数、base/shot seed、模型路径与 SHA256、耗时、PNG 完整性、图片 SHA256、ComfyUI history 摘要和警告。Job 级报告记录请求/完成数量、是否启动 ComfyUI、启动次数、顺序执行、最大并发 1、总图像耗时、是否自动降级、是否 Mock fallback 及失败结构。

数据库为每张成功或合法复用的 PNG 登记 `KEYFRAME_IMAGE` Asset，保存受控相对路径和 SHA256；前端只通过 `/api/projects/{project_id}/assets/{asset_id}/content` 获取图片，不接触绝对文件路径。

## 10. FFmpeg 真实图片成片

真实媒体入口要求所有 shot 都有且只有一张真实关键帧，禁止 Provider/model 混用和任何 `provider_id=mock` 图片。验证后每镜头执行：

1. 将 PNG 等比放大并裁切到带少量运镜余量的画布；
2. 按镜头参数应用轻微、确定性的推近、拉远或平移；
3. 左上角保留弱化的镜头编号/场景信息，不覆盖主体；
4. 用公共 Mock WAV 生成音轨；
5. 使用独立 UTF-8 LF textfile 和本机中文字体烧录 `shot.narration`；
6. 编码为 1280×720、24 fps、H.264、AAC、`yuv420p` 镜头片段；
7. 顺序拼接，完整 ffprobe 校验并保留 M3 已验证的媒体帧量化时长容差。

Manifest 明确记录：

- `manifest_version=m4.real-image-export.v1`；
- `script_provider=reused` 与 `source_script_job_id`；
- 原始 `source_script_provider`；
- `image_provider=comfyui-animagine-xl-4`；
- 每镜头真实 PNG 路径、SHA256、seed 和追溯；
- `audio_provider=mock`；
- `video_source_type=FFMPEG_KEYFRAME_MOTION`；
- `subtitle_rendering=burned_in`；
- planned/encoded/delta/tolerance/duration validation；
- MP4 SHA256、FFmpeg/ffprobe 版本与安全命令记录。

## 11. 前端交互

前端在原 M2/M3 工作区内做有限扩展：

- `GET /api/providers` 同时显示默认/可用 ImageProvider、真实模型 ID、是否配置和 GPU 交接要求；
- 成功 Script Job 下提供“使用当前剧本生成真实动漫画面”按钮；
- 按钮附近明确提示先停止 Qwen，后端仍会二次检查；
- Job 卡显示 `comfyui-animagine-xl-4`、真实/Mock 徽标、base seed、当前镜头和完成数量，例如 `2/5`；
- 已完成关键帧通过受控 URL 显示缩略图，并保留每张状态；
- 只有真实图像 Job 成功后，其新 Export 才标记“真实动漫视觉”，旧 Mock MP4 不会在运行期间被重命名为真实成片；
- 成功后继续提供视频播放、MP4 下载和 Manifest 下载。

## 12. 失败与手动重试

| 错误码 | 处理 |
|---|---|
| `GPU_HANDOFF_REQUIRED` | 停止 Qwen、释放 8081/显存后重试；平台不杀进程 |
| `COMFYUI_START_FAILED` | 检查独立环境、源码、8188 与 stdout/stderr |
| `COMFYUI_TIMEOUT` | 检查启动、HTTP、单图或总 Job 超时；保留已完成图 |
| `MODEL_NOT_FOUND` | 模型缺失，停止；不自动下载 |
| `MODEL_HASH_MISMATCH` | 模型 SHA256 不符，拒绝加载 |
| `IMAGE_GENERATION_FAILED` | 记录失败 shot、ComfyUI status 和已完成数量 |
| `IMAGE_OUTPUT_MISSING` | 输出/下载为空或 history 没有 SaveImage |
| `IMAGE_DECODE_FAILED` | PNG CRC/zlib/IEND、尺寸或媒体完整解码失败 |
| `GPU_OOM` | 明确 OOM；不静默降规格、不切 Mock |
| `MEDIA_RENDER` | 真实图已完成但 FFmpeg/ffprobe 失败，保留图片追溯 |

手动重试创建新 Job，原失败 Job 不变。新 Job 复制原参数和 base seed，并引用失败 Job 的 `image_shots`。Provider 对每张候选重新核对 Provider/model、shot、正负提示、seed、模型/图片 SHA、尺寸、PNG 完整解码、workflow 与 trace；合法图标记 `REUSED`，损坏或缺失图重新生成。任何 M4-B 重试都不调用 Qwen，也不静默加入 Mock 图。

## 13. 本地运行与验证

环境只读检查：

```powershell
conda activate anime-platform
git branch --show-current
git status --short
Get-FileHash -Algorithm SHA256 models\image\animagine-xl-4.0-opt.safetensors
git -C tools\ComfyUI rev-parse HEAD
.\.venv-comfyui\Scripts\python.exe -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
Get-NetTCPConnection -State Listen -LocalPort 8081,8188 -ErrorAction SilentlyContinue
nvidia-smi
```

单元、伪 ComfyUI 与媒体回归不启动真实 GPU 模型：

```powershell
python -m compileall backend\app backend\tests scripts
python -m pytest backend\tests -q
npm --prefix frontend run build
python scripts\media_smoke_test.py
python scripts\generate_m1_short.py
python scripts\verify_m1_output.py
python scripts\m2_e2e_test.py --worker-once
```

M3 回归按 [README](../README.md#m3-本地运行与-provider-选择) 执行；真实 Qwen 回归结束后必须停止 `llama-server`，再运行 M4-B。

真实三镜头 E2E：

```powershell
python scripts\m4_real_image_e2e.py
```

该脚本优先自动选择已有成功三镜头 ScriptV1，用 TestClient/API 入队并执行一次 `Worker.run_once`，不启动 Qwen；测试前应停止持续 Worker。脚本必须有总超时和 finally 清理，最终检查 3 张真实 PNG、1024×576、24 steps、顺序生成、ComfyUI 启动一次、无 Mock 图、MP4 完整解码、中文字幕抽帧、8188 释放与推理显存释放。

完成前还应执行：

```powershell
git diff --check
git diff -- ENVIRONMENT.md
git status --short
```

不得执行 Git commit。

## 14. 真实三镜头 E2E 结果

以下结果来自 有界真实运行及其 `m4b-e2e-summary.json`，不是 M4-A 单张数据：

| 验证项 | 实际结果 |
|---|---|
| 源 ScriptV1 / 来源 Job / 来源文本 Provider | 项目 `36d4bdd5-0e88-4509-a2f5-eba7727fd38b`；来源 Job `4f80ef8d-b38d-47c8-98e0-c90b61595801`；`llamacpp`；本 Job 的 ScriptProvider 调用数为 0 |
| Job ID / Job 状态 | `11c1b83a-f5b7-4511-b7db-2e1056ef2160` / `SUCCEEDED` |
| 三张 PNG 路径 | `data/projects/36d4bdd5-0e88-4509-a2f5-eba7727fd38b/jobs/11c1b83a-f5b7-4511-b7db-2e1056ef2160/images/shot-01.png`、`shot-02.png`、`shot-03.png` |
| 三张 seed | `20260803`、`20260804`、`20260805`（base seed `20260802`） |
| 三张生成耗时 | `15.156s`、`10.125s`、`10.157s` |
| 三张 SHA256 | `2ffe7744bc077cd0759a7caf06318fc6eaa7164a58c3861305f394850eef4270`；`73d3496e4d5a4d932ab0383674a49c04356b2d25411b47fcdc0c76fc02de6620`；`c8c659e843bfc16b4b1ce87f31a35d5eb314a970cac67e79fe8794d4499b1d9c` |
| ComfyUI 启动次数 | `1`；三图顺序生成，并发 `1` |
| 图像阶段总耗时 / 端到端总耗时 | `51.266s` / `56.485s` |
| 峰值 GPU 显存观测 | 基线 `411 MiB`，全卡采样峰值 `7332 MiB`，增加 `6921 MiB`；Windows WDDM 口径包含显示及其他 GPU 进程 |
| OOM / 降级 / Mock 图片数量 | 无 OOM；无参数降级；0 张 Mock 图片 |
| 最终 MP4 路径 / SHA256 / ffprobe 摘要 | `data/projects/36d4bdd5-0e88-4509-a2f5-eba7727fd38b/exports/11c1b83a-f5b7-4511-b7db-2e1056ef2160/short_11c1b83a-f5b7-4511-b7db-2e1056ef2160.mp4`；SHA256 `b53b517eb9ca47ddead4e56cc8297dbf7349edaa34f58ae295a3d9894b24771c`；20.021333s、1280×720、24fps、H.264/AAC、yuv420p，完整解码通过 |
| 中文字幕抽帧路径及人工查看 | 同一 Export 下 `e2e-frames/shot-01-midpoint.png` 至 `shot-03-midpoint.png`；三帧均可见清晰中文字幕。人物在三镜中保持黑色连帽卫衣与相近深色中短发，但脸型、发长和场景细节仍有变化，仍需人工审美确认，不宣称严格角色一致性 |
| ComfyUI 进程退出 / 8188 释放 / 显存释放 | 进程已退出，8188 已释放；结束后全卡显存观测约 `383 MiB` |

## 15. 已知限制

- Animagine 是单帧扩散模型；提示词重复不能严格锁定脸型、服装细节或跨镜头几何关系。
- 当前中文到标签是项目内固定映射，覆盖范围有限；它可审计但不是通用翻译或语义评分器。
- 单 Worker、进程内锁和 8GB GPU 互斥适合本地单用户演示，不是多用户调度方案。
- 模型 SHA256 每个真实 Job 启动前实算，安全但会增加数 GB 文件扫描时间；后续若优化必须保留可信校验证据。
- Mock 音频不是语义旁白；字幕文本来自 ScriptV1 并已烧录，但真实 TTS 不在 M4-B。
- 前端真实点击、三张图的构图语义、跨镜头人物相似度和字幕视觉可读性仍需人工查看；自动测试不能冒充审美验收。
- 本轮完成后停止，不进入 M5，也不加入 IP-Adapter、ControlNet、LoRA、TTS 或视频生成。
