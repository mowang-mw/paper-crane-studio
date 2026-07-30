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

**M0、M1 以及 M2 最小全栈纵向链路已经实现。** 浏览器页面可编辑故事；“载入《纸鹤的夜航》Demo”只填充表单，只有“创建项目”才会发出创建请求。FastAPI 只将生成任务写入 SQLite，独立单 Worker 调用 Mock Provider 和 M1 同一套 FFmpeg 媒体函数，最后持久化 4 个镜头、Asset、Export、manifest 和可播放 MP4。前端通过轮询展示四态 Job，并提供项目确认删除、失败后的显式手动重试、阶段导航、视频播放与下载入口。

本轮自动验证已经覆盖生产构建、API、SQLite 持久化、独立 Worker、真实 FFmpeg 导出、媒体下载、ffprobe 和 SHA-256；最近一次黑盒 E2E 生成了 28.021333 秒、H.264/AAC、1280×720、24 fps 的成片。当前执行环境没有可用浏览器绑定，因此“真实浏览器点击、播放与下载”仍需按下文步骤做一次现场人工确认，不能把 HTTP 媒体验证冒充为浏览器播放验证。M2 仍只使用 Mock Provider，尚未进入 M3 真实模型接入。

调研基线日期为 ****。模型许可、远程 API 型号、价格、额度和地区可用性会变化，在实际接入或公开展示前必须再次核实。

## 已知硬件与软件环境

| 项目 | 当前环境 | 设计影响 |
|---|---|---|
| 操作系统 | Windows | 优先原生可运行的单机架构；所有路径、进程终止和 FFmpeg 转义均按 Windows 验证 |
| GPU | NVIDIA RTX 4060，8GB 显存 | 单 Worker 串行使用 GPU；不把重型视频模型放入成功关键路径 |
| 系统内存 | 约 31.6GB | 可做有限 CPU offload，但不能把大量内存交换当作稳定方案 |
| Python | 3.11.15，Conda 环境 `anime-platform` | FastAPI 0.116.1、SQLAlchemy 2.0.43、Pydantic 2.13.4、Uvicorn 0.35.0 已锁定并实测 |
| Node.js / npm | 24.15.0 / 11.12.1 | `package-lock.json` 已锁定 React 19.2.8、Vite 7.3.6、TypeScript 5.9.3，生产构建通过 |
| FFmpeg | Conda 环境内 8.0 | 本轮只读预检确认 libx264/libopenh264/h264_nvenc、AAC、drawtext、zoompan、concat、xfade；未发现 subtitles/ass（构建未启用 libass）。基础 PATH 不可见，须激活 `anime-platform` 或配置已验证绝对路径 |
| 可用模型服务 | 当前无独立文本、图像、视频或语音 API | 全 Mock 离线链路是无条件基线 |


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
```

若当前终端没有加载 Conda 激活脚本，可使用不修改全局 PATH 的等价命令：

```powershell
conda run -n anime-platform python scripts\media_smoke_test.py
conda run -n anime-platform python scripts\generate_m1_short.py
conda run -n anime-platform python scripts\verify_m1_output.py
```

主要输出位于：

- `data/generated/m0/smoke_test.mp4`
- `data/generated/m1/paper_crane_night_flight.mp4`
- `data/generated/m1/manifest.json`

`data/` 已由 `.gitignore` 忽略。当前画面是确定性的几何 Mock 构图，音频是标准库生成的 Mock 提示音，镜头运动、H.264/AAC 编码、拼接和中文字幕烧录由 FFmpeg 完成；它们均不代表真实图像、视频或 TTS 模型能力。

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

## 人工可用性测试修复

本轮保持 M2 架构和媒体流程不变，只修复已经确认的交互问题：

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
- [当前环境](ENVIRONMENT.md)

## 成功判断

项目成功不以“耗满当前范围”或“接入模型数量”衡量，而以尽早得到可播放成片并逐步提升为准：工程保底可离线稳定导出；真实 Provider 目标要求真实文本与真实图像均接入通过；图像硬件阻塞证据只能形成诚实的未达成说明和替代演示，不能冒充完成；替换 Provider 不改变业务契约；生成结果能够追溯模型、提示词、参数、种子和素材来源；真实模型失败不破坏现场演示。
