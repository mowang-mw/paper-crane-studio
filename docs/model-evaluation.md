# 模型与框架评估

## 1. 调研口径

本调研以 **** 为资料基线，只优先采用官方文档、官方 GitHub 仓库、发布者的 Hugging Face 模型卡和许可证。没有在当前 RTX 4060 8GB Windows 机器上下载或运行任何模型。

表格中的信息分为三类：

- **官方事实**：来源明确写出的参数、文件大小、许可证、平台或基准。
- **工程判断**：根据模型体量和本机约束做的选型判断，明确使用“预计、可能、建议”等表述。
- **待核实**：官方没有目标机数据，或实际接入前会变化的事实。

“开放权重”不自动等于 OSI 定义的开源软件，也不自动允许任意用途。CreativeML Open RAIL、Gemma、Llama、Stability Community 等许可证均有各自条款；代码许可证与权重许可证也可能不同。

## 2. 权限边界：必须分开理解

| 能力 | 本项目当前状态 | 能否供平台运行时调用 |
|---|---|---|
| 其他云模型 API | 当前均未配置 | 否。每家服务都需独立账户、密钥、额度、地区和条款 |
| 本地开放权重 | 当前未下载 | 只有在获准安装运行时、下载对应权重并通过本机验证后才可以 |


## 3. 评估结论摘要

| 类别 | MVP 无条件方案 | 第一真实候选 | 主要退出条件 |
|---|---|---|---|
| 文本/剧本 | `MockTextProvider` | `Qwen3-4B-GGUF Q4_K_M`；进度兜底 `Qwen2.5-1.5B-Instruct-GGUF` | Windows 服务不稳、超时、JSON Schema 通过率不足 |
| 图像/关键帧 | `MockImageProvider` + 用户选择 | `Animagine XL 4.0 Opt` | 8GB OOM、连续 4 张不稳、单图等待不适合演示、许可未复核 |
| 视频 | `FfmpegMotionVideoProvider` | 仅实验：Stable Video Diffusion XT | 权重/环境过大、低显存极慢、人物质量差；任何失败立即回静帧运镜 |
| TTS | `MockTTSProvider` + 可选自制旁白 | 仅有余量时试 MeloTTS | Windows 原生依赖失败或超过半天时间盒 |
| 字幕/合成 | 结构化时轴 + FFmpeg + ffprobe | 不需要模型 | 已确认当前构建缺少 libass/subtitles；P0 固定 drawtext + UTF-8 textfile + 微软雅黑系统字体，实际烧录仍待 smoke test |

最终第一版不应同时正式接入多种文本或图像模型。候选表用于展示调研与替换路径，不等于实施清单。

## 4. 文本和结构化剧本模型

### 4.1 候选对比

| 候选 | 开放性与许可证 | 官方规模/资源信息 | Windows 与服务化 | 中文、结构化与创作适配 | 难度/速度/风险 | 当前范围建议 |
|---|---|---|---|---|---|---|
| Qwen3-4B + 官方 GGUF | 开放权重，[Apache-2.0](https://github.com/QwenLM/Qwen3#license-agreement) | 4B；官方 `Q4_K_M.gguf` 为 [2.5GB](https://huggingface.co/Qwen/Qwen3-4B-GGUF/tree/main)。[官方基准](https://qwen.readthedocs.io/en/stable/getting_started/speed_benchmark.html)在 H20 96GB、Transformers、batch=1、输入 1 token、生成 2048 tokens 时记录 AWQ-INT4 GPU memory 2915MB；这不是 GGUF、常驻显存、4060 或 Windows 结论 | 官方 GGUF 页给出 Windows 与 `llama serve`；[llama.cpp 发布页](https://github.com/ggml-org/llama.cpp/releases)提供 Windows CUDA 二进制，服务可暴露 OpenAI-compatible API | [Qwen3 官方](https://qwenlm.github.io/blog/qwen3/)声明 119 种语言/方言含简繁中文；官方模型卡称整体在创意写作/角色扮演方面增强，但未证明思考模式更适合结构化剧本，JSON 必须外部校验 | 接入低到中；4B Q4 预计适合 8GB，但速度和 KV cache 仍待实测。思考块可能污染 JSON | **第一真实文本候选**；短上下文、强制非思考、Schema 校验 |
| Qwen2.5-1.5B-Instruct + 官方 GGUF | 开放权重，[Apache-2.0](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct/blob/main/LICENSE) | 1.54B；官方 `Q4_K_M` 为 [1.12GB](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/tree/main)。BF16 仓库约 3.1GB | 官方 GGUF 同样支持 Windows `llama serve` 和 OpenAI-compatible API | [模型卡](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct)明确列出中文和 structured outputs，尤其 JSON | 接入低，预计更快更省资源；1.5B 可能损失剧情细节和跨镜头一致性 | **最低风险进度兜底**；也可在进度优先时直接首选 |
| Phi-3.5-mini-instruct ONNX INT4 | 开放权重，[MIT](https://huggingface.co/microsoft/Phi-3.5-mini-instruct-onnx/blob/main/LICENSE) | 原始 3.8B、128K；[官方 ONNX 仓库](https://huggingface.co/microsoft/Phi-3.5-mini-instruct-onnx)的 GPU INT4 目录约 2.32GB | 官方 ONNX 路径支持 Windows、CUDA 和 DirectML；[ONNX Runtime GenAI](https://github.com/microsoft/onnxruntime-genai)可进程内调用。[Foundry Local](https://learn.microsoft.com/en-us/windows/ai/foundry-local/get-started)也能为其目录中的 `phi-3.5-mini` 暴露 OpenAI-compatible REST，但需另装运行时/目录模型，不能与该 HF artifact 混写，Windows 版本和 API 稳定性待核实 | [原始模型卡](https://huggingface.co/microsoft/Phi-3.5-mini-instruct)列出中文等多语言；没有官方证据表明中文剧本或 JSON 优于 Qwen | Provider 薄封装工作量较高；RTX 4060 未在官方列表中单独验证 | 保留为 Windows/DirectML 技术备选，不与 Qwen 同时接 |
| DeepSeek-R1-Distill-Qwen-7B | 开放权重，[MIT](https://github.com/deepseek-ai/DeepSeek-R1#7-license)；底座 Qwen2.5 为 Apache-2.0 | 模型卡显示约 8B，BF16 仓库约 [15.2GB](https://huggingface.co/deepseek-ai/DeepSeek-R1-Distill-Qwen-7B/tree/main)；官方无适合本机的自产 GGUF 资源结论 | 可按 Qwen 类框架运行；Windows/量化需另选实现并实测 | 中文推理能力强，但项目需要短、稳定的机器可读剧本，不需要长推理链 | 量化后才可能适配 8GB；官方提示可能重复，推理输出更长，增加 JSON 清洗成本 | 不进入 MVP，保留研究对照 |

另一个容易踩坑的候选是 Qwen2.5-3B-Instruct：模型大小合适，但其[权重许可证](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/blob/main/LICENSE)是 Qwen Research License，而不是同系列所有尺寸都统一 Apache-2.0。在已有许可证更直接的 1.5B 和 Qwen3-4B 时，不应引入这项额外负担。

### 4.2 推荐实施方式

1. 工程保底始终保留 `MockTextProvider`，根据输入哈希生成稳定合法的 `script.v1` JSON；完成首段 MP4 后，真实文本是真实 Provider 目标的第一项接入工作。
2. 真实接入只实现一个“OpenAI-compatible text endpoint Adapter”，先连接本机 `llama-server`。
3. 能力优先试官方 `Qwen3-4B-GGUF:Q4_K_M`。使用 llama.cpp 时固定自定义 chat template，使其等价于 `enable_thinking=False`；仅添加 `/no_think` 属于软切换，仍需验证，并把模板版本/hash 纳入追溯。不要把普通 Qwen3-4B GGUF 写成 `Qwen3-4B-Instruct-2507` 的官方量化版，两者不是同一发布物。
4. 若安装、启动、固定故事耗时或 Schema 稳定性超出时间盒，切换官方 `Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M`。
5. 输出必须先作为 Draft 经过 JSON 解析、Schema、3—5 镜头、20—40 秒和引用关系校验，再让用户审核入库。

本机实际首 token 时间、总耗时、峰值显存、RAM、合适上下文和连续运行稳定性均为**待核实**。官方在 H20/A100 上的 token/s 不能线性换算为 RTX 4060。

## 5. 动漫图像或关键帧模型

### 5.1 候选对比

| 候选 | 开放性与许可证 | 官方规模/磁盘 | Windows、API 与服务化 | 中文与动漫适配 | 难度/速度/风险 | 当前范围建议 |
|---|---|---|---|---|---|---|
| Animagine XL 4.0 Opt | 开放权重，[CreativeML Open RAIL++-M](https://huggingface.co/cagliostrolab/animagine-xl-4.0)；有用途限制，不称无条件开源 | SDXL 架构、约 3B、FP16；单个 Opt checkpoint [6.94GB](https://huggingface.co/cagliostrolab/animagine-xl-4.0/blob/main/animagine-xl-4.0-opt.safetensors)。[仓库](https://huggingface.co/cagliostrolab/animagine-xl-4.0/tree/main)总量约 20.8GB，含原版/Opt 两个单文件和 Diffusers 分组件布局，不能视为任一运行路径的最小下载量 | Diffusers/PyTorch 通用路径可在 Windows 运行，但模型未给 Windows/4060 承诺；当前模型卡未列托管推理 Provider，可由 Worker 内 Python Adapter 调用 | 动漫专用；模型卡标记英语并要求 tag-based prompt，自然语言可能不佳。需把中文镜头转换为英文标签并记录派生提示词 | 中等；官方建议 25—28 步、常用横图 1216×832。6.94GB 文件不等于显存，8GB 全量加载很可能余量不足；手、多人、文字、风格一致性均有官方限制 | **真实 Provider 目标中的第一真实图像候选**；先做 8GB 连续生成门槛，失败必须留实测证据与替代演示 |
| Stable Diffusion XL Base 1.0 | 开放权重，[CreativeML Open RAIL++-M](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/blob/main/LICENSE.md) | 约 3B；单 checkpoint 约 6.94GB，完整仓库含多格式远大于最小集 | [官方模型卡](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0)有 Diffusers、ONNX、OpenVINO 和 CPU offload 示例；未承诺 Windows/RTX 4060，通用 Windows 路径和 offload 稳定性待实测；远程 Provider 需独立账户与额度 | 通用图像，不声明中文或动漫专长；建议中文转英文。角色一致性无内建保证 | 生态成熟但 8GB 仍需 offload 实测；文字、复杂关系和人体可能异常 | 作为 Animagine 的生态对照/备选，不在当前范围内同时正式接入 |
| FLUX.1-schnell | 开放权重，[Apache-2.0](https://github.com/black-forest-labs/flux) | 12B、BF16；[模型卡](https://huggingface.co/black-forest-labs/FLUX.1-schnell)的 Transformer 约 23.8GB，另有 AE 与文本编码器 | 官方 Diffusers 示例支持 CPU offload；Hugging Face 仓库为 gated model，访问需登录并接受页面条件；模型卡列出远程 Provider，但账户、型号、额度和条款需另核；未见 Windows/4060 承诺 | 页面标记英语，不声明动漫专长 | 官方 1—4 步不等于低配墙钟快；23.8GB Transformer 远大于 8GB，且 31.6GB RAM 卸载余量也紧张 | 不做本地 MVP；仅保留未来远程 API 或高配设备扩展 |

### 5.2 推荐顺序与角色一致性策略

- 必达：Mock 关键帧产生真实 PNG；也允许用户选择项目内自制/获许可的 fixture。
- 首个真实候选：优先评估 Opt 单文件 checkpoint 路径；接入前固定 Diffusers 版本，验证 `from_single_file`、本地配置和 `local_files_only=True`，实测后再形成精确最小离线文件清单。任何下载必须在下一阶段另获许可。
- 启用门槛：固定参数下连续生成 4 个镜头无 OOM、文件均可解码、峰值显存/RAM/耗时已记录，且切回 Mock 不需改业务数据。
- 一致性只做工程缓解，不做质量承诺：原创单角色、固定角色描述块、固定服装/发色/道具、尽量复用场景标签、保存请求 seed、人工从候选中选择。
- 绝不使用知名动漫角色作为固定演示提示。模型训练集中存在角色标签不代表平台获得第三方 IP 权利。

本机 1216×832、25—28 步的峰值显存和单图耗时，CPU offload 的 RAM 与速度，以及中文直接提示效果均为**待核实**。

## 6. 图生视频或视频生成方案

### 6.1 候选对比

| 候选 | 开放性与许可证 | 官方资源与速度 | Windows、接口与服务化 | 中文/动漫/流程适配 | 主要风险 | 当前范围建议 |
|---|---|---|---|---|---|---|
| Stable Video Diffusion XT | 代码仓库 MIT；权重为 [Stability AI Community License](https://huggingface.co/stabilityai/stable-video-diffusion-img2vid-xt/blob/main/LICENSE.md)，有注册、收入门槛与归属等条款 | XT 为 25 帧、576×1024、约 4 秒；单一 `svd_xt.safetensors` 约 9.56GB，仓库页面约 32.6GB。[Diffusers 官方](https://huggingface.co/docs/diffusers/api/pipelines/stable_diffusion/svd)称 CPU offload + chunking + 小 decode chunk 可降到 8GB 以下 | 自托管 Diffusers；[Stability AI 的 SVD 托管端点已退役](https://kb.stability.ai/knowledge-base/how-to-access-stable-video-diffusion)，不代表所有第三方托管都不存在。模型未承诺 Windows，目标机待测 | I2V 直接消费关键帧，最符合平台数据流；不接受文本控制。官方明确人物/脸、动作量、慢速镜头和文字有局限 | “低于 8GB”仍不证明 4060 速度可演示；权重大、系统内存与耗时未知 | 三者中最值得做单镜头技术 spike，但未另立验收前不声称平台已支持 |
| CogVideoX-2B | [Apache-2.0](https://huggingface.co/zai-org/CogVideoX-2b)；不要与 5B 自定义许可证混写 | T2V，约 6 秒、720×480、8fps；SAT FP16 18GB；官方称 Diffusers 全优化 4GB 起、INT8 3.6GB 起，但只在 A100/H100 测；A100 约 90 秒，仓库约 13.8GB | Diffusers/CLI 可包装；无消费卡 Windows 保证 | 官方提示词为英语，需翻译；2B 不是 I2V，不能自然继承已选关键帧 | 低显存会显著降速；固定低分辨率；角色漂移；4060 时间未知 | 未来 T2V 对照，不做 MVP |
| Wan2.1 T2V-1.3B | 代码和官方模型标记 [Apache-2.0](https://github.com/Wan-Video/Wan2.1) | 官方要求 8.19GB VRAM，已高于物理 8GB；RTX 4090 生成 5 秒 480p 约 4 分钟；原始仓库约 17.6GB，Diffusers 仓库约 28.9GB | CLI、Gradio、Diffusers 可包装；没有 Windows/4060 承诺 | 中英文能力较好，但低资源版是 T2V；官方标准 I2V checkpoint 为 14B。VACE-1.3B 另有参考图/R2V 能力，但属于不同管线，资源与本机适配待核实 | 显存硬超、CPU offload 慢、模型大、4090 已需分钟级、角色漂移 | 不在当前范围内投入；保留未来远程 GPU 扩展 |

### 6.2 稳定视频兜底

MVP 的正式视频路径是：

```text
已选择关键帧
  -> 1280×720 scale/crop
  -> FFmpeg zoompan / 静止 / fade
  -> 固定规格镜头 MP4
  -> concat（可选短 xfade）
```

这不是“假装视频模型”，而是标记为 `DETERMINISTIC_FALLBACK` 的 VideoProvider。若未来真实 I2V 成功，只替换某个镜头的标准化片段；失败、超时或 OOM 后继续使用同一关键帧的运镜版本。

SVD、CogVideoX、Wan 在 RTX 4060 Windows 上的峰值显存、RAM、耗时、稳定性和实际动漫角色一致性全部**待核实**。

## 7. 语音合成候选

### 7.1 候选对比

| 候选 | 开放性与许可证 | 官方资源 | Windows 与服务化 | 中文/配音适配 | 风险与速度 | 当前范围建议 |
|---|---|---|---|---|---|---|
| MeloTTS Chinese | 库和官方中文模型标记 [MIT](https://github.com/myshell-ai/MeloTTS/blob/main/LICENSE) | [中文 checkpoint 约 208MB](https://huggingface.co/myshell-ai/MeloTTS-Chinese/tree/main)；官方称 CPU 足以实时推理，未给 RAM/VRAM 下限 | Python API、CLI、WebUI；官方主要 Ubuntu/Python 3.9 路径，Windows 原生需实测，本项目不为它引入 Docker | 支持中文和中英混读；有限预训练说话人，适合旁白，不是角色克隆 | 权重小、预计集成低到中；Windows 语言前端依赖可能卡住 | 有剩余时间时的第一真实 TTS 候选，严格半天时间盒 |
| Kokoro-82M-v1.1-zh | 开放权重，[Apache-2.0](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh) | 82M 参数；[v1.1-zh 仓库](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh/tree/main)当前约 394MB；官方未给 RAM/VRAM 下限 | Python 接口；[官方仓库](https://github.com/hexgrad/kokoro)给出 Windows eSpeak NG 安装说明，但中文分词/依赖仍需目标机实测 | 中文模型有 100 个中文说话人，处于较早发布状态；适合旁白候选 | 小模型但中文质量、专名发音和 Windows 依赖无本机证据 | 作为 MeloTTS 研究备选，不同时接入 |
| GPT-SoVITS | 代码与官方权重标记 [MIT](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/LICENSE) | 官方存在多代底模和附加预训练依赖；未固定 release/revision 前不写最小下载量。官方给出的部分训练配置超过 8GB，因此本机禁止训练；推理下限待核实 | 官方支持 Windows 10+、Python 3.11，并含 [`api_v2.py`](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py) | 多语、5 秒参考音频零样本，适合角色音色 | 依赖与版本组合复杂；音色克隆有同意、隐私和冒充风险；推理显存待测 | 未来配音加分项，只可用获授权参考，不做训练 |
| Azure AI Speech | 闭源远程服务，按 Microsoft 服务条款 | 本地无需权重；网络、区域、账户、Key 和额度为必要条件 | [Speech SDK](https://learn.microsoft.com/azure/ai-services/speech-service/speech-sdk)官方支持 Python/Windows，也有 REST | [语言表](https://learn.microsoft.com/azure/ai-services/speech-service/language-support)列出多种普通话/方言中文音色 | 当前无账户/Key；价格、区域、网络和隐私随服务变化；延迟待实测 | 只作为远程 Adapter 示例和未来扩展，不进入当前 MVP |

### 7.2 不推荐项

- ChatTTS：代码为 AGPL-3.0-or-later，权重 CC BY-NC 4.0，非商业限制和音质策略增加负担，不优于已有候选。
- Piper：当前 [`piper-tts` 官方 PyPI](https://pypi.org/project/piper-tts/)提供 CPython 3.9+ 的 Windows x86-64 wheel，元数据许可证为 GPL-3.0-or-later；但每个 voice 另有数据许可，中文 huayan [模型卡](https://huggingface.co/rhasspy/piper-voices/blob/main/zh/zh_CN/huayan/medium/MODEL_CARD)写明训练数据许可证 `Unknown`。Python 3.11 中文完整链路仍需实测，voice 许可不清，因此不进入 MVP。
- 非官方 `edge-tts` 封装：它不是有 SLA 和独立凭据的正式 Azure Speech API，不应用作现场关键路径。

P0 使用 Mock WAV 验证音频链路；为了现场观感，可以使用开发者本人录制且明确授权的固定旁白作为 `DEMO_FIXTURE`。它不能计为 TTS Provider 成功。真实 TTS 不能挤占文本、图像和端到端验收时间。

## 8. 字幕与 FFmpeg 合成方案

字幕无需 ASR：系统已经有 Shot 文本、时长和 TTS Asset，直接生成以下结构即可：

```text
speaker, text, start_ms, end_ms, shot_id, audio_asset_id
```

### 8.1 三种字幕路径

| 方案 | 开放性/依赖 | 优点 | 风险 | 推荐级别 |
|---|---|---|---|---|
| `drawtext` 逐条绘制 | 本机 FFmpeg 已列 drawtext，构建含 libfreetype/libharfbuzz/fontconfig | 不依赖 libass；UTF-8 `textfile` + 固定本机 `fontfile` 避免把中文直接拼进命令；同时保留边车 | cue 多时 filtergraph 变长，Windows 路径、换行和特殊字符仍须 fixture 实测；系统字体只引用，不复制/分发 | **当前 P0 主路径** |
| UTF-8 ASS/SRT + `subtitles` 烧录 | FFmpeg；滤镜依赖 libass | 样式和中文换行更易控制；同时保留边车 | 当前 Conda FFmpeg 构建未启用 libass，未列 subtitles/ass | 未来更换已验证构建后可选；当前不可用 |
| MP4 `mov_text` 软字幕 + 外部 SRT | FFmpeg | 不需画面文字滤镜，兼容的播放器可开关 | 浏览器/播放器不一定默认显示，不满足“画面内可见”单独要求 | 三级应急并保留边车；若使用需另有可见字幕兜底 |

若 drawtext fixture 仍失败，可在应用层用明确的文字栅格化库和许可清楚的字体预生成透明字幕 PNG 后 `overlay`；这不是零依赖方案，实施前须单独批准依赖。软字幕边车继续保留，但单独使用不满足“画面内可见”。

### 8.2 合成步骤与官方依据

1. 比特流和流布局完全同构时，才使用 [concat demuxer](https://ffmpeg.org/ffmpeg-formats.html#concat-1)。
2. 否则先逐段解码，在 filtergraph 内分别用 `scale/fps/format/setpts` 统一视频，用重采样/格式转换/`asetpts` 统一音频并补齐缺失音轨；确保对应流数量和参数一致、各段时间戳从 0 开始后，才进入 [concat filter](https://ffmpeg.org/ffmpeg-filters.html#concat) 并统一编码。静帧运镜使用 [`zoompan`](https://ffmpeg.org/ffmpeg-filters.html#zoompan)。
3. [`xfade`](https://ffmpeg.org/ffmpeg-filters.html#xfade)要求输入规格一致，且重叠转场会影响总时长，因此只作为增强并必须重新计算时间线。
4. 旁白与可选背景音可使用 [`amix`](https://ffmpeg.org/ffmpeg-filters.html#amix) 和 [`loudnorm`](https://ffmpeg.org/ffmpeg-filters.html#loudnorm)，第一版无获许可背景音乐也可只保留旁白/Mock 音轨。
5. [`subtitles`](https://ffmpeg.org/ffmpeg-filters.html#subtitles-1)烧录 ASS/SRT；输出目标 H.264/AAC MP4、`yuv420p`。
6. [ffprobe](https://ffmpeg.org/ffprobe.html)以 JSON 验证流和时长。

编码器按能力探测：首选已验证的 `libx264 + aac`；`h264_nvenc` 只作加速而非唯一方案。若分发 FFmpeg 二进制，需依据 [`ffmpeg -buildconf` 与官方法律说明](https://ffmpeg.org/legal.html)核对 LGPL/GPL 义务；当前项目只计划调用本机现有 FFmpeg，不把其二进制直接提交仓库。

## 9. 推荐运行时和 Adapter 选择

| 类别 | 推荐运行方式 | 原因 | 不选项 |
|---|---|---|---|
| 本地文本 | llama.cpp Windows 预编译服务 + OpenAI-compatible Adapter | 官方 Qwen GGUF 路径明确，后端与未来远程 API 可复用协议 | vLLM/SGLang 在原生 Windows 上风险更高；本阶段也不安装 Ollama |
| 本地图像 | Worker 中独立 Diffusers Provider 或独立子进程 | 能明确控制参数、临时目录、offload、错误和追溯 | ComfyUI 增加另一套工作流与服务运维，本阶段不安装 |
| 本地视频实验 | 独立子进程/服务，结果标准化后才归档 | 便于超时终止、释放显存和避免拖垮 Worker | 不在 API 请求中加载视频模型 |
| TTS | Python API 或本地 HTTP Provider | 输出简单 WAV，易做格式验证 | 不为单一 TTS 引入 Docker |
| 媒体 | FFmpeg 参数数组 + ffprobe JSON | 确定性、可审计、已有环境 | 不用 MoviePy 等额外层作为 P0 必需依赖 |

本表只描述下一阶段可能的实现路线，不是安装授权。本阶段未执行任何安装或下载。

## 10. 最终优先顺序

1. **先用最小 Mock 和 FFmpeg 链路尽早产出第一段可播放 MP4。** 再补齐工程保底所需工作流、持久化和验收，不等待完整 UI 或高级 Job 机制。
2. **紧接着真实接入一个文本模型。** 能力优先 Qwen3-4B 官方 GGUF Q4_K_M，进度优先或失败时用 Qwen2.5-1.5B-Instruct 官方 GGUF。
3. **再真实接入一个图像模型。** Animagine XL 4.0 Opt 必须通过本机显存、RAM、速度和连续 4 张稳定性门槛；若最终失败，保留完整测试证据、ImageProvider 接口和替代关键帧演示，且不得宣称已经完成真实图像接入。
4. **视频默认永远保留 FFmpegMotion。** 若有充足余量，只把 SVD XT 作为一个 2—4 秒镜头的技术 spike；未另立并通过验收前不登记为平台已支持的真实 Provider，失败不影响导出。
5. **TTS 优先级低于端到端和图像。** 可限时试 MeloTTS，也可用获授权的固定旁白提升演示。
6. Phi、SDXL、CogVideoX、Wan、GPT-SoVITS 等只保留清晰的未来 Provider 扩展位，不在当前范围内全部接入。

## 11. 真实模型进入 MVP 的门槛

任何候选只有满足以下条件才能在 README 或演示中写成“已支持”：

- 记录准确模型 ID、revision/commit、所用文件及 SHA-256。
- 许可证已针对代码、权重、voice 和演示用途分别复核。
- 获得用户对安装依赖和下载权重的明确许可，磁盘预算足够。
- 在本机而不是引用他人硬件完成峰值显存、RAM、磁盘、首个输出和总耗时记录。
- 固定 fixture 连续运行通过，输出契约、取消、超时和 fallback 均经过测试。
- 关闭真实 Provider 后，全 Mock 项目仍可从头导出。
- UI、日志和 manifest 不把 fallback、量化版本或远程 API 隐藏。

## 12. 待核实清单

- Qwen3-4B GGUF 与 Qwen2.5-1.5B GGUF 在 RTX 4060 上的实际 token/s、显存、上下文和固定故事 Schema 通过率。
- 固定 llama.cpp revision 后，Qwen3 自定义非思考模板、`reasoning_content` 处理及 JSON Schema/grammar 支持范围。
- Qwen3-4B-Instruct-2507 是否存在 Qwen 官方组织维护的对应量化仓库；当前推荐的普通 Qwen3-4B GGUF不能混写。
- Animagine XL 4.0 Opt 在 Windows、当前驱动/CUDA、待选 PyTorch/Diffusers 版本下的峰值资源和单图耗时。
- Animagine XL 4.0 Opt 的精确离线文件集，以及单文件路径是否确实加载 Opt 而非仓库默认组件。
- Foundry Local 所需 Windows 版本、目录模型 revision 与 Hugging Face ONNX artifact 的对应关系。
- FLUX Hugging Face gated access 的当期条件及联系信息共享要求。
- Animagine/SDXL 的模型许可证不等于训练数据或第三方角色 IP 授权；训练数据权利信息和公开演示输出风险仍需复核。
- SVD/CogVideoX/Wan 的 Windows 兼容性、最小实际下载集、4060 速度和动漫人物稳定性。
- MeloTTS/Kokoro 在 Python 3.11 Windows 原生环境中的依赖和中文专名发音。
- GPT-SoVITS 选定版本的推理显存和所有附带预训练依赖许可证。
- 当前构建已枚举 drawtext/xfade/zoompan/concat、libx264/libopenh264/h264_nvenc、AAC，并确认系统存在 `C:\Windows\Fonts\msyh.ttc`；仍待实际验证 drawtext 中文/换行/路径、H.264/AAC 编码和播放器结果。libass/subtitles 当前明确不可用。
- 任一远程 API 的当期模型 ID、价格、免费额度、地区、速率限制、数据使用和服务条款。
- 所有模型许可证在最终提交/公开演示日是否仍与本调研版本一致。
