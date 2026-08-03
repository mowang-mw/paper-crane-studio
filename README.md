# AniFlow Studio（动画流影工坊，暂定）

## 项目目标

AniFlow Studio 是一个面向本地单用户的、工作流驱动的动漫概念短片制作平台。它把“短篇故事”逐步转换为可审核的结构化剧本、角色与场景、3—5 个分镜、关键帧、镜头片段、旁白与字幕，最后自动合成为 20—40 秒 MP4。

本项目对项目目标的收敛定义是：**尽快完成一个可离线演示、可追溯、可替换模型 Provider，并至少验证一个真实文本模型和一个真实图像模型的动漫短片生产工作台；当前范围是最大时间窗口和范围上限，而不是必须耗尽的固定工期。**

## 核心工作流

```text
输入短篇故事
  -> 生成并审核结构化剧本
  -> 提取并编辑角色、场景和 3—5 个分镜
  -> 为每个分镜生成候选关键帧并选择一个
  -> 用真实视频 Provider 或 FFmpeg 静帧运镜生成镜头片段
  -> 生成或选择旁白、台词音频与字幕
  -> FFmpeg 统一规格、拼接、混音和烧录字幕
  -> ffprobe 验证并导出 20—40 秒 MP4 与追溯清单
```

所有阶段都消费上一步“已审核或已选择”的产物。修改上游内容会使相关下游素材失效，用户必须重新生成并选择与当前输入匹配的素材，因而系统呈现的是一条制作流水线，而不是模型功能陈列页。

## 当前阶段

**M0、M1、M2、M3、M4-A 与 M4-B 已完成。** M4-B 已把 `ComfyUI + Animagine XL 4.0 Opt` 集成为正式 `ImageProvider`：从成功 Job 的受控 ScriptV1 快照创建新的真实图像 Job，不调用 Qwen；一个 Job 只启动一次有界 ComfyUI，按镜头顺序生成 3—5 张 PNG，再交给公共 FFmpeg 管线制作带 Mock 音轨和烧录中文字幕的 MP4。

M3 的全部 Mock + FFmpeg 离线保底仍然保留，并接入本地 `Qwen3-4B Q4_K_M`、`llama.cpp` Script Provider、Provider 即时健康检查和前端显式选择。生成任务可选择自动 3—5 镜头或固定 3/4/5 镜头，默认固定 4；故事去除首尾空白后硬限制为 10—3000 字符，界面建议 50—1000 字符。真实文本或真实图像 Provider 失败都不会静默改用 Mock 并伪装成功。

RTX 4060 8GB 模式固定采用分阶段 GPU 工作流：先用 Qwen 生成并保存 ScriptV1，再停止 `llama-server`、释放 8081 和显存，最后启动真实图像 Job。API 入队和 Worker 执行前都会检查冲突；平台只报告 `GPU_HANDOFF_REQUIRED`，不会擅自结束用户进程。三镜头真实 E2E 已复用 `llamacpp` 来源 ScriptV1，ScriptProvider 调用为 0；三张图均为 1024×576、24 步，图像阶段 51.266 秒，无 OOM，最终 20.021333 秒 MP4 完整解码并完成中文字幕抽帧。

媒体业务时长与编码时长现已分开验收：ScriptV1 计划总时长仍严格限制为 20—40 秒；最终 MP4 允许一个视频帧、一个 AAC 采样帧与小量封装舍入共同决定的量化容差。`MEDIA_RENDER` 失败任务手动重试时会优先从来源 Job 的严格 ScriptV1 与已有 MP4 恢复，不调用 ScriptProvider、不改变镜头，并记录 `resumed_from_stage=MEDIA_RENDER`。自动镜头 Prompt 现明确要求覆盖开端、主要发展和结局；这只是提示增强，尚未实现独立语义覆盖评分。

公共媒体管线现已把每个 `shot.narration` 作为独立 UTF-8 LF 文本文件烧录到对应镜头，并在 Manifest 记录字幕文件、字体和滤镜。最新 M2 黑盒 E2E 生成了 28.021333 秒、H.264/AAC、1280×720、24 fps 成片；真实 M3 固定 4 镜头测试生成 27.979329 秒成片，自动模式生成 3 镜头、20.021333 秒成片。两者均完成完整解码和镜头中点抽帧。当前执行环境仍没有可绑定浏览器实例，因此真实点击、折叠详情、播放和下载需现场人工确认；HTTP/E2E 和抽帧结果没有被冒充为浏览器验证。

调研基线日期为 ****。模型许可、远程 API 型号、价格、额度和地区可用性会变化，在实际接入或公开展示前必须再次核实。

## 已知硬件与软件环境

| 项目 | 当前环境 | 设计影响 |
|---|---|---|
| 操作系统 | Windows | 优先原生可运行的单机架构；所有路径、进程终止和 FFmpeg 转义均按 Windows 验证 |
| GPU | NVIDIA RTX 4060，8GB 显存 | 单 Worker 串行使用 GPU；不把重型视频模型放入成功关键路径 |
| 系统内存 | 约 31.6GB | 可做有限 CPU offload，但不能把大量内存交换当作稳定方案 |
| Python | 3.11.15，Conda 环境 `anime-platform` | FastAPI 0.116.1、SQLAlchemy 2.0.43、Pydantic 2.13.4、Uvicorn 0.35.0 已锁定并实测 |
| 图像运行环境 | 项目内独立 `.venv-comfyui`：Python 3.13.3、PyTorch 2.11.0+cu128、CUDA runtime 12.8 | 与 `anime-platform` 隔离；不把 PyTorch 或 ComfyUI 依赖装入后端环境 |
| Node.js / npm | 24.15.0 / 11.12.1 | `package-lock.json` 已锁定 React 19.2.8、Vite 7.3.6、TypeScript 5.9.3，生产构建通过 |
| FFmpeg | Conda 环境内 8.0 | 本轮只读预检确认 libx264/libopenh264/h264_nvenc、AAC、drawtext、zoompan、concat、xfade；未发现 subtitles/ass（构建未启用 libass）。基础 PATH 不可见，须激活 `anime-platform` 或配置已验证绝对路径 |
| 可用模型服务 | 项目内本地 `llama.cpp + Qwen3-4B Q4_K_M` 文本链路；按 Job 有界启动的 `ComfyUI + Animagine XL 4.0 Opt` 图像链路；无真实视频或语音模型 API | Qwen 与 ComfyUI 不得同时驻留 8GB GPU；全 Mock 离线链路仍是无条件基线 |


## M1 本地运行

在 PowerShell 中进入项目目录并激活既有环境：

```powershell
conda activate anime-platform
python --version
where.exe ffmpeg
where.exe ffprobe
```

运行 M0、生成 M1 和验证 M1：

```powershell
python scripts\media_smoke_test.py
python scripts\generate_m1_short.py
python scripts\verify_m1_output.py
python scripts\verify_subtitle_burnin.py
```

若当前终端没有加载 Conda 激活脚本，可使用不修改全局 PATH 的等价命令：

```powershell
conda run -n anime-platform python scripts\media_smoke_test.py
conda run -n anime-platform python scripts\generate_m1_short.py
conda run -n anime-platform python scripts\verify_m1_output.py
conda run -n anime-platform python scripts\verify_subtitle_burnin.py
```

主要输出位于：

- `data/generated/m0/smoke_test.mp4`
- `data/generated/m1/paper_crane_night_flight.mp4`
- `data/generated/m1/manifest.json`
- `data/generated/subtitle-check/paper_crane_night_flight/`

`data/` 已由 `.gitignore` 忽略。当前画面是确定性的几何 Mock 构图，音频是标准库生成的 Mock 提示音，镜头运动、H.264/AAC 编码、拼接和动态中文字幕烧录由 FFmpeg 完成；它们均不代表真实图像、视频或 TTS 模型能力。Windows 下字幕文件必须由公共模块写成 UTF-8 LF；不能改回平台默认 CRLF。

## M2 本地运行

首次准备 M2 依赖时，在已激活的 `anime-platform` 环境执行：

```powershell
python -m pip install -r backend\requirements.txt
npm --prefix frontend ci
```

随后分别打开三个 PowerShell 终端，均先进入项目根目录并执行 `conda activate anime-platform`，再依次启动：

```powershell
# 终端 1：API
powershell -ExecutionPolicy Bypass -File scripts\run_backend.ps1

# 终端 2：唯一的 M2 Worker
powershell -ExecutionPolicy Bypass -File scripts\run_worker.ps1

# 终端 3：前端
powershell -ExecutionPolicy Bypass -File scripts\run_frontend.ps1
```

打开 `http://127.0.0.1:5173`，载入《纸鹤的夜航》Demo，提交生成任务；页面应依次显示 `QUEUED`、`RUNNING`、`SUCCEEDED`，随后出现 4 个分镜、视频播放器、MP4 和 manifest 下载入口。若任务失败，页面会显示后端错误，只有用户点击“手动重试”才会创建一个新的 `QUEUED` Job。

自动验证命令：

```powershell
python -m pytest backend\tests -q
npm --prefix frontend run build

# 先仅启动 API；该参数会用独立子进程逐个处理队列任务
python scripts\m2_e2e_test.py --worker-once
```

若已经启动持续运行的 Worker，执行 E2E 时不要再加 `--worker-once`，以保持第一版“单 Worker”约束。默认数据位于 `data/app.db` 和 `data/projects/<project-id>/exports/<job-id>/`；整个 `data/`、`node_modules/`、前端构建产物和 TypeScript 构建缓存均不会进入 Git。

## M3 本地运行与 Provider 选择

M3 不替换 Mock 保底，而是在相同生成接口中增加 `llamacpp`。所有终端均从项目根目录启动，并先执行：

```powershell
conda activate anime-platform
```

### Mock 模式：三个终端

Mock 模式不需要启动模型服务。分别打开三个 PowerShell 终端：

```powershell
# 终端 1：API
powershell -ExecutionPolicy Bypass -File scripts\run_backend.ps1

# 终端 2：唯一 Worker
powershell -ExecutionPolicy Bypass -File scripts\run_worker.ps1

# 终端 3：前端
powershell -ExecutionPolicy Bypass -File scripts\run_frontend.ps1
```

打开 `http://127.0.0.1:5173`，在“选择 Script Provider”中选择“Mock（离线保底）”后生成。该路径不需要网络、API Key 或模型服务。

### 本地 Qwen 模式：四个终端

按默认配置，真实文本模式要求项目内已经存在 `tools/llama.cpp/llama-server.exe` 和 `models/text/Qwen3-4B-Q4_K_M.gguf`。分别打开四个 PowerShell 终端：

```powershell
# 终端 1：本地 Qwen3-4B Q4_K_M + llama.cpp
powershell -ExecutionPolicy Bypass -File scripts\run_llm_server.ps1

# 终端 2：先检查模型服务，再启动 API
powershell -ExecutionPolicy Bypass -File scripts\check_llm_server.ps1
powershell -ExecutionPolicy Bypass -File scripts\run_backend.ps1

# 终端 3：唯一 Worker
powershell -ExecutionPolicy Bypass -File scripts\run_worker.ps1

# 终端 4：前端
powershell -ExecutionPolicy Bypass -File scripts\run_frontend.ps1
```

打开页面后选择“本地 Qwen（llama.cpp）”。前端会读取 `GET /api/providers` 展示在线/离线、是否配置、模型 ID、默认 Provider 和最近检查时间；生成区另行选择自动或固定镜头数，不需要把“生成 4 镜头”写进故事正文。若本地服务离线，页面会禁用该选项并提示运行 `scripts\run_llm_server.ps1`；用户仍可显式切回 Mock 完整生成成片。

运行状态、构建和真实文本纵向测试可使用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check_llm_server.ps1
Invoke-RestMethod http://127.0.0.1:8000/api/providers | ConvertTo-Json -Depth 6
python -m pytest backend\tests -q
npm --prefix frontend run build
python scripts\m3_real_llm_test.py --desired-shot-count 4
python scripts\m3_real_llm_test.py --desired-shot-count auto --title "雨夜的纸灯" --story "雨夜，小男孩在巷口捡到一盏会飞的纸灯。他追着纸灯穿过屋顶，最后在晨光里找到了回家的路。"
```

执行 `m3_real_llm_test.py` 时保留 llama.cpp 与 API 运行，但应先停止持续 Worker；测试脚本会自行启动一次 `Worker --once`，避免两个 Worker 同时领取任务。

交互生成结果位于 `data/projects/<project-id>/exports/<job-id>/`。每次真实文本调用的受控追溯目录包含 `first_raw_response.json`、可选的 `repair_raw_response.json`、`validation_report.json`、请求快照和 trace；数据库只保存路径、摘要与结构化诊断，不保存大段原始模型响应。`m3_real_llm_test.py` 的独立证据位于 `data/generated/m3/real-llm-test/<project-id>/`，包括 `script.v1.json`、MP4、manifest、Worker 日志和 `summary.json`。这些目录均属于本地生成数据，不应提交到 Git。

## M4-B 本地真实图像运行

M4-B 不把 ComfyUI 当作常驻平台服务。Worker 为一个真实图像 Job 启动一个受控子进程，使用 `--lowvram`、禁用预览和自定义节点，顺序生成全部镜头，并在成功或失败后回收进程树、确认 8188 释放。默认参数固定为 1024×576、batch size 1、24 steps、CFG 5、`euler_ancestral`、`normal`、denoise 1.0；不会在 OOM 后悄悄降低规格。

运行前先在项目根目录核对既有 M4-A 环境，不需要也不应重新安装：

```powershell
conda activate anime-platform
Get-FileHash -Algorithm SHA256 models\image\animagine-xl-4.0-opt.safetensors
git -C tools\ComfyUI rev-parse HEAD
.\.venv-comfyui\Scripts\python.exe -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
Get-NetTCPConnection -State Listen -LocalPort 8081,8188 -ErrorAction SilentlyContinue
nvidia-smi
```

期望模型 SHA256 为 `6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac`，ComfyUI commit 为 `f06a187f50f896e4a0ba5be1ce1f2d2dcd13b77b`。如不一致应停止，不自动下载、换模或覆盖文件。

实际交互流程：

1. 按 M3 方式用 Qwen 或 Mock 成功生成结构化剧本和原始成片。
2. 若使用 Qwen，主动停止 `llama-server`，确认 8081 不再监听且推理显存已释放；不要手动启动 ComfyUI。
3. 保持 API、唯一 Worker 和前端运行，在成功剧本下点击“使用当前剧本生成真实动漫画面”。
4. 页面显示真实 ImageProvider、base seed、逐张状态和缩略图；成功后播放或下载新的真实关键帧 MP4 与 manifest。

该按钮调用 `POST /api/projects/{project-id}/render-real-images`，请求只引用成功来源 Job。新 Job 保存独立 ScriptV1 快照，并明确记录 `script_provider=reused`、`source_script_job_id`、来源文本 Provider、`script_provider_calls=0`、`image_provider=comfyui-animagine-xl-4` 和 `audio_provider=mock`。Mock 文本来源会按 Mock 记录，不会被包装成 Qwen。

自动检查与真实三镜头 E2E 命令：

```powershell
python -m compileall backend\app backend\tests scripts
python -m pytest backend\tests -q
npm --prefix frontend run build
python scripts\m4_real_image_e2e.py
```

最后一条命令是有界真实 GPU 测试：优先选择已有的成功三镜头 ScriptV1，通过 API 入队并在进程内执行一次 Worker，不启动 Qwen。真实 E2E 运行前应停止持续 Worker，以免两个 Worker 同时领取任务。M4-B 结果位置如下：

- 逐镜头 PNG 与追溯：`data/projects/<project-id>/jobs/<job-id>/images/`
- ComfyUI 日志与 Job 级图像报告：`data/projects/<project-id>/jobs/<job-id>/`
- MP4、分镜片段、字幕与 manifest：`data/projects/<project-id>/exports/<job-id>/`
- 本次真实 E2E 汇总：`data/projects/36d4bdd5-0e88-4509-a2f5-eba7727fd38b/exports/11c1b83a-f5b7-4511-b7db-2e1056ef2160/m4b-e2e-summary.json`
- 本次成片与抽帧：同一目录下的 `short_11c1b83a-f5b7-4511-b7db-2e1056ef2160.mp4` 与 `e2e-frames/`

M4-B 只提供共享角色外观标签的基础提示一致性，不等于严格角色一致性；本阶段没有 IP-Adapter、ControlNet、LoRA、TTS 或视频生成模型。详见 [M4-B ImageProvider 实施记录](docs/m4-image-provider-implementation.md)。

## 人工可用性测试修复

在不改变 M2/M3 总体架构和视觉体系的前提下，当前交互包括：

- 故事输入只承载故事内容，实时显示字符数、建议 50—1000 和硬限制 10—3000；镜头数在生成区单独选择，默认固定 4。
- Job 明确展示要求镜头数、最终镜头数、故事字符数、是否发生一次修复、是否发生确定性时长规范化和未使用实体警告。
- 成功 Job 记录并展示计划时长、编码时长、差值、媒体容差和验收结论；帧量化范围内的毫秒级偏差不再误报失败。
- `MEDIA_RENDER` 手动重试创建新的恢复 Job，来源失败记录保持不变；合法 ScriptV1 和已编码 MP4 可复用时不会再次请求 Qwen 或重新编码。
- 失败卡先显示中文主因，并在折叠区展示首次/修复错误、阶段、代码和建议；合法输入长度与模型输出校验失败明确区分。
- 成功摘要区分固定模式和自动模式，不再让用户猜测系统是否固定生成 3 个镜头。
- Demo 按钮只把标题和故事填入表单，并提示“演示故事已填入，请确认内容后点击创建项目”；重复点击不会创建项目。
- 创建期间由同步 ref 与按钮禁用共同防止双击重复 POST；创建成功后自动选中新项目、滚动到项目任务区域并聚焦项目标题。
- 项目列表提供独立的“删除”按钮和二次确认。`DELETE /api/projects/{project_id}` 会拒绝仍有 `QUEUED`/`RUNNING` Job 的项目；确认删除会级联清理数据库记录和 `data/projects/<project-id>`，且不可恢复。
- 创建区下方提供 01—04 阶段导航。Job 首次从非成功状态变为 `SUCCEEDED` 时只自动定位结果一次；刷新后读取历史成功任务不会强制滚动。
- 镜头区域底部明确提供“前往播放成片”入口，结果区标题改为“最终成片”，并显示已生成、实际时长和镜头数。
- 页面右下角保留轻量任务状态；运行时显示进度，成功后可随时点击“查看成片”。

人工回归建议：

1. 打开浏览器开发者工具 Network，连续点击 Demo，确认没有 `POST /api/projects`；点击创建后只出现一次 POST。
2. 确认创建成功后项目被选中，页面移动到“选择一个项目生成”，键盘焦点位于项目标题。
3. 对普通项目打开删除确认，先取消，再确认删除；确认当前项目切换正确且其他项目仍存在。
4. 对 `QUEUED` 或 `RUNNING` 项目尝试删除，确认页面显示等待任务结束的 409 提示。
5. 等待一次新 Job 完成，确认只自动滚动一次、焦点位于“最终成片”；刷新页面后不得再次强制滚动。
6. 从阶段导航、镜头底部和右下角快捷提示分别进入同一个结果区，播放视频并下载 MP4 与 manifest。
7. 在窄屏和“减少动态效果”模式下重复上述关键操作，确认按钮不重叠，滚动改为即时定位。

## 当前范围内的 MVP 概述

开发第一目标不是先铺满页面或可靠性机制，而是在**里程碑 M1**形成最小纵向链路，生成第一段可播放的 Mock + FFmpeg MP4。随后再补齐 React 工作区、SQLite 持久化、完整业务实体、手动重试、追溯与演示质量。

完成标准分为两层：**工程保底**是在断网、无 API Key、无真实模型时，全 Mock + FFmpeg 仍能离线导出 20—40 秒 MP4；**真实 Provider 完成目标**是在此基础上至少真实接入一个文本模型和一个图像模型。若 RTX 4060 8GB 经实测最终无法完成真实图像链路，必须保留资源与错误证据、可用的 ImageProvider 接口和 Mock/自制素材替代演示，且如实说明未达成项；不能从一开始把全部真实模型降为纯可选加分项。

第一版 Job 仅实现 `QUEUED`、`RUNNING`、`SUCCEEDED`、`FAILED` 和显式手动重试。取消、自动重试等待、租约/心跳、崩溃恢复、客户端幂等键、复杂 CAS 与两阶段素材归档均保留在目标架构中，作为首段成片之后的稳定性增强。

## 推荐技术栈

- 前端：React + Vite + TypeScript。
- 后端：FastAPI + Python 3.11 + Pydantic。
- 持久化：SQLite + SQLAlchemy，开启外键、WAL 和合理的 busy timeout。
- 后台任务：SQLite `GenerationJob` 表 + 独立单进程 Python Worker。
- 素材：项目本地 `data/` 目录，数据库只保存受控相对路径和 SHA-256。
- 媒体：FFmpeg 负责规格统一、静帧运镜、拼接、混音与字幕；ffprobe 负责机器可读验证。
- 模型：按文本、图像、视频、TTS 分设强类型 Provider；每类均有 Mock，视频另有正式的 FFmpeg 确定性兜底。

第一版不引入 Redis、Celery、Kubernetes、微服务或 Docker 运行依赖。

## 文档索引

- [需求分析](docs/requirements.md)
- [MVP 范围](docs/mvp-scope.md)
- [系统架构](docs/architecture.md)
- [数据模型](docs/data-model.md)
- [模型评估](docs/model-evaluation.md)
- [风险登记册](docs/risk-register.md)
- [验收标准](docs/acceptance-criteria.md)
- [M1 实施记录](docs/m1-implementation-report.md)
- [M2 实施记录](docs/m2-implementation-report.md)
- [M3 实施记录](docs/m3-implementation-report.md)
- [M4-A 图像模型冒烟记录](docs/m4-image-model-spike.md)
- [M4-B ImageProvider 实施记录](docs/m4-image-provider-implementation.md)
- [ScriptV1 契约](docs/script-v1-schema.md)
- [当前环境](ENVIRONMENT.md)

## 成功判断

项目成功不以“耗满当前范围”或“接入模型数量”衡量，而以尽早得到可播放成片并逐步提升为准：工程保底可离线稳定导出；真实 Provider 目标要求真实文本与真实图像均接入通过；图像硬件阻塞证据只能形成诚实的未达成说明和替代演示，不能冒充完成；替换 Provider 不改变业务契约；生成结果能够追溯模型、提示词、参数、种子和素材来源；真实模型失败不破坏现场演示。
