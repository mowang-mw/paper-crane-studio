# 纸鹤工坊：本地多模型动漫制作平台

纸鹤工坊是一个可运行、可演示的多模型动漫制作平台原型。它面向“使用大语言模型和开源模型完成动漫生产流程”的当前开发范围，将故事输入、结构化剧本、动漫关键帧、中文旁白和最终成片组织为可追踪的本地工作流，而不是把若干模型调用拼成一次性脚本。

## 已验证的真实链路

```text
Qwen3-4B（结构化剧本）
  -> Animagine XL 4.0（动漫关键帧）
  -> Qwen3-TTS 0.6B CustomVoice / Serena（中文旁白）
  -> FFmpeg（字幕、镜头运动、混音、Poster、封装）
  -> MP4 + Manifest
```

这条链路已在本机 RTX 4060 8GB 环境中实际运行并产出成片。真实任务失败时会明确失败，不会静默改报为 Mock 成功。Mock Provider 仍被保留，用于离线开发、快速回归和无模型环境演示。

> 模型决定生成质量上限，平台决定模型能否被稳定、可追溯、可组合地用于生产流程。

## 核心能力

- 项目与故事管理，以及面向演示的项目搜索、真实成片筛选、排序和分页。
- Provider 抽象：Script、Image、Audio 均有 Mock/真实实现和统一状态展示。
- SQLite Job 队列、单 Worker 顺序执行、请求快照、失败信息和重试。
- `ScriptV1` 结构化剧本，以及角色、场景和逐镜头数据。
- Animagine XL 4.0 真实关键帧和逐镜头 Qwen3-TTS WAV 旁白。
- FFmpeg 中文字幕、`static` / `gentle_zoom` / `cinematic_pan` 镜头运动。
- 用户自有背景音上传、循环/裁剪、淡入淡出和旁白 ducking。
- 1280x720 Poster、MP4 与 Manifest 公共下载入口。
- `MEDIA_RERENDER`：复用现有 ScriptV1、PNG 和 WAV，仅重新运行 FFmpeg；三类模型 Provider 调用数均为 0。
- Manifest 记录来源 Job、Provider、模型、素材哈希、时序和复用关系。

## 快速启动

### 环境前提

已验证环境见 [ENVIRONMENT.md](ENVIRONMENT.md)：Windows、Conda 环境 `anime-platform`、Python 3.11、Node.js 24、npm 11、FFmpeg 8.0。首次使用需按仓库既有依赖文件安装 Python 与前端依赖；模型权重不纳入 Git。

打开三个 PowerShell 终端，均从项目根目录运行：

```powershell
conda activate anime-platform
.\scripts\run_backend.ps1
```

```powershell
conda activate anime-platform
.\scripts\run_worker.ps1
```

```powershell
.\scripts\run_frontend.ps1
```

浏览器打开 `http://127.0.0.1:5173`。后端默认地址为 `http://127.0.0.1:8000/api`。

查看已有演示项目、播放现有 WAV/MP4、下载 Manifest，或执行“仅重新合成成片”，都不需要启动 Qwen 或 ComfyUI，也不会触发 Qwen3-TTS 推理子进程。只有重新生成对应真实素材时才需要准备相应模型运行时：剧本阶段启动 llama.cpp，图片阶段启动 ComfyUI；TTS 阶段由运行在 Python 3.11 环境的 Worker 受控调用独立 Python 3.12 环境中的 Qwen3-TTS 子进程。不要在 8GB 显存上同时常驻所有模型。

### 推荐演示项目

- 项目：`深夜少女`
- Project ID：`36d4bdd5-0e88-4509-a2f5-eba7727fd38b`
- 直达地址：`http://127.0.0.1:5173/?project=36d4bdd5-0e88-4509-a2f5-eba7727fd38b`

页面顶部会根据项目现有数据展示剧本、真实图片、真实旁白和最终成片状态；左侧也可通过搜索或“有真实成片”筛选找到该项目。

## 8GB 显存运行方式

本项目采用阶段式 GPU 资源交接，而非让所有模型同时驻留：

1. 启动 llama.cpp / Qwen3-4B，生成并保存 ScriptV1，然后停止服务、释放显存。
2. 运行 ComfyUI / Animagine XL 4.0，保存关键帧，然后结束该阶段、释放显存。
3. Worker 调用独立 Python 3.12 环境中的 Qwen3-TTS 子进程生成 WAV，完成后释放显存。
4. FFmpeg 在已有素材上完成媒体合成。

这种调度使真实多模型链路能在 8GB 显存设备上演示；当前采用 Q4 量化的 4B 文本模型、0.6B TTS，并在受限显存下运行单帧图像工作流，因此演示配置存在质量与速度上限。详细流程见 [系统架构](docs/architecture.md)。

## 模型可替换性

平台通过 Provider 契约、Job 请求快照和统一媒体输入输出，把模型运行时与工作流解耦。替换模型时仍需实现相应 Provider 契约、保留可追溯元数据，并验证输出结构；这不等于任意模型可以零代码无缝接入。

- 当前 Provider 与验证状态：[模型与 Provider](docs/model-providers.md)
- 8GB、本地工作站和云端扩展方案：[模型升级指南](docs/model-upgrade-guide.md)

## 当前边界

当前版本定位为单机、单用户、单 Worker 的当前开发范围演示原型，使用静态动漫关键帧配合 FFmpeg 镜头运动，不包含角色级连续动作视频生成、专业非线性时间线或多用户协作。角色一致性和连续视频属于后续平台能力建设，不会仅靠替换更大模型自动解决。完整说明见 [已知限制](docs/limitations.md)。

## 演示与验收

- [三分钟演示指南](docs/demo-guide.md)
- [验收标准](docs/acceptance-criteria.md)
- [M6 媒体打磨记录](docs/m6-media-polish.md)

## 文档索引

- [系统架构](docs/architecture.md)
- [数据模型](docs/data-model.md)
- [ScriptV1 规范](docs/script-v1-schema.md)
- [需求说明](docs/requirements.md)
- [模型评估](docs/model-evaluation.md)
