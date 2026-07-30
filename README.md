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

当前为需求分析、技术调研、架构设计和开发计划阶段。仓库中尚无前后端业务工程、依赖安装、模型权重或 Docker 容器；本文档中的真实模型均是候选方案，不代表已经安装、接通或在本机验证。

调研基线日期为 ****。模型许可、远程 API 型号、价格、额度和地区可用性会变化，在实际接入或公开展示前必须再次核实。

## 已知硬件与软件环境

| 项目 | 当前环境 | 设计影响 |
|---|---|---|
| 操作系统 | Windows | 优先原生可运行的单机架构；所有路径、进程终止和 FFmpeg 转义均按 Windows 验证 |
| GPU | NVIDIA RTX 4060，8GB 显存 | 单 Worker 串行使用 GPU；不把重型视频模型放入成功关键路径 |
| 系统内存 | 约 31.6GB | 可做有限 CPU offload，但不能把大量内存交换当作稳定方案 |
| Python | 3.11.15，Conda 环境 `anime-platform` | 计划使用 FastAPI、SQLAlchemy；具体依赖版本待下一阶段锁定 |
| Node.js / npm | 24.15.0 / 11.12.1 | 计划使用 React、Vite、TypeScript；兼容性须先做最小验证 |
| FFmpeg | Conda 环境内 8.0 | 本轮只读预检确认 libx264/libopenh264/h264_nvenc、AAC、drawtext、zoompan、concat、xfade；未发现 subtitles/ass（构建未启用 libass）。基础 PATH 不可见，须激活 `anime-platform` 或配置已验证绝对路径 |
| 可用模型服务 | 当前无独立文本、图像、视频或语音 API | 全 Mock 离线链路是无条件基线 |


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
- [当前环境](ENVIRONMENT.md)

## 成功判断

项目成功不以“耗满当前范围”或“接入模型数量”衡量，而以尽早得到可播放成片并逐步提升为准：工程保底可离线稳定导出；真实 Provider 目标要求真实文本与真实图像均接入通过；图像硬件阻塞证据只能形成诚实的未达成说明和替代演示，不能冒充完成；替换 Provider 不改变业务契约；生成结果能够追溯模型、提示词、参数、种子和素材来源；真实模型失败不破坏现场演示。
