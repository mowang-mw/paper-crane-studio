# 模型与 Provider

## 当前注册表

| 能力 | Provider ID | 运行时 / 模型 | 主要输入 | 主要输出 | 本机验证状态 | 资源 | 失败与 fallback |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 剧本 | `mock` | 确定性离线实现 | 故事、风格、镜头数 | ScriptV1 | 已验证 | CPU | 直接返回明确标记的 Mock 结果 |
| 剧本 | `llamacpp` | llama.cpp + Qwen3-4B Q4_K_M GGUF | 故事、风格、镜头数 | 结构化 ScriptV1 | 已在 RTX 4060 8GB 验证 | GPU 为主 | 失败即 Job 失败；不静默改报 Mock 成功 |
| 图片 | `mock` | 确定性 FFmpeg 几何画面 | ScriptV1 镜头 | 逐镜头占位图 | 已验证 | CPU | 仅在用户明确选择 Mock 时使用 |
| 图片 | `comfyui-animagine-xl-4` | ComfyUI + Animagine XL 4.0 | 镜头提示词、角色与场景 | 逐镜头 PNG、seed 与追溯信息 | 已在 RTX 4060 8GB 验证 | GPU | 缺图、损坏或服务失败即明确失败；不混入 Mock |
| 音频 | `mock` | 确定性 PCM WAV 提示音 | 逐镜头旁白文本 | 逐镜头 WAV | 已验证 | CPU | 仅在用户明确选择 Mock 时使用，不宣称是真实语音 |
| 音频 | `qwen3-tts-0.6b-customvoice` | Qwen3-TTS 12Hz 0.6B CustomVoice | 中文旁白、音色 | 逐镜头 PCM16 WAV | 已在 RTX 4060 8GB 验证 | GPU | 解码、时长或追溯校验失败即明确失败；不回退 Mock |
| 媒体 | `ffmpeg` | FFmpeg / ffprobe | ScriptV1、PNG、WAV、可选背景音 | MP4、Poster、Manifest | 已验证 | CPU / 媒体编码 | 返回结构化媒体错误；Poster 失败降级为 warning |

Qwen3-TTS 默认音色为 `Serena`，可选 `Vivian`，当前语言配置为中文。媒体层不是生成模型：它负责编排已有素材、字幕、运动、混音、封装与追溯。

## Mock 与真实结果的边界

Mock 是显式 Provider，不是异常兜底。Job 快照和结果会记录 Provider ID 与来源类型，界面也区分 Mock、真实本地模型和复用素材。真实 Job 一旦开始，就必须满足对应真实素材约束；服务离线、GPU 冲突、输出损坏或追溯字段缺失都会使任务失败，而不是生成一个 Mock 结果冒充成功。

## Provider 契约

### ScriptProvider

输入是项目故事、风格和镜头数等生成参数；输出必须符合 [ScriptV1 规范](script-v1-schema.md)，并通过结构、引用和镜头数量校验。下游只消费校验后的 ScriptV1，不直接解析模型自由文本。

### ImageProvider

输入是不可变的 ScriptV1 镜头及视觉上下文；输出是每个 shot 对应的 PNG 和 Provider/模型/seed/参数等追溯信息。真实媒体入口要求素材齐全、同一真实 Provider、路径受控且可以解码。

### AudioProvider

输入是逐镜头中文旁白和音色；输出是每个 shot 对应的 PCM16 WAV、实际时长和生成追溯。媒体 TimingPlan 可以为了容纳旁白延长镜头，但不会截断真实旁白。

## Provider 状态与 GPU 交接

Provider 注册表报告 `configured`、`available`、模型 ID、来源类型和 GPU 交接提示。可用性检查不会替用户强制启动或停止外部服务。8GB 环境下应按 Script -> Image -> Audio 的顺序运行并在阶段间释放 GPU，详见 [系统架构](architecture.md#8gb-gpu-交接)。

## MEDIA_RERENDER 的特殊语义

`MEDIA_RERENDER` 的 Job Provider ID 是 `ffmpeg`。快照中的 Script、Image、Audio Provider 均标记为 `reused`，实际来源 Provider 则另外保留。Manifest 的 `provider_calls` 对三类模型均为 0；这表示本次导出没有再次调用模型，不会抹去素材最初由 Qwen、Animagine 和 Qwen3-TTS 生成的历史事实。

## 更换模型

Provider 抽象降低了替换成本，但仍需实现接口、映射输入输出、保存版本与参数并通过契约测试。具体步骤和不同硬件档位见 [模型升级指南](model-upgrade-guide.md)。
