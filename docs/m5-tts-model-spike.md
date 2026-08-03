# M5-A 本地中文 TTS 模型可行性冒烟记录

## 1. 结论

`Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` 已在当前 Windows 11、RTX 4060 Laptop 8GB 环境中完成一次有界、全本地、双音色真实生成。模型在同一进程内只加载一次，依次使用 Serena 和 Vivian 对完全相同的中文原文生成 WAV；两段音频均可完整解码、非静音、没有 NaN/Inf 或明显数字削波，且文件内容与 SHA256 不同。最终进程正常退出，显存从本轮监督器基线 780 MiB、峰值 4893 MiB 回落到 512 MiB。

本结论只证明“0.6B CustomVoice 可以在本机稳定完成这次技术冒烟”。它不自动证明中文发音、断句、情感、音色偏好或长文本质量合格；Serena 与 Vivian 仍必须人工试听。本轮没有实现正式 `AudioProvider`，没有修改 Worker 或前端，没有生成视频，没有启动 Web 服务，也没有使用声音克隆、参考声音、VoiceDesign 或云 API。

## 2. 固定选型与官方来源

| 项目 | 本轮固定值 |
|---|---|
| 官方代码仓库 | [QwenLM/Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) |
| 官方 Python 包 | [qwen-tts 0.1.1](https://pypi.org/project/qwen-tts/) |
| 模型仓库 | [Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice) |
| 固定模型 revision | `85e237c12c027371202489a0ec509ded67b5e4b5` |
| 模型下载源 | Hugging Face 官方 Qwen 仓库；未使用镜像、转换版或第三方重打包 |
| 代码与模型许可证 | Apache License 2.0 |
| 语言 | `Chinese` |
| 音色 | Serena：官方预置温暖柔和中文女声；Vivian：官方预置明亮中文女声 |
| 禁止能力 | 未使用 Base/声音克隆、VoiceDesign、1.7B 模型或真人参考声音 |

Qwen 官方 README 推荐创建独立 Python 3.12 环境后安装 `qwen-tts`。官方包未把 FlashAttention 列为硬依赖，官方模型实现声明支持 PyTorch SDPA；因此本轮在 Windows 上显式使用 `attn_implementation="sdpa"`，没有编译或安装 FlashAttention。

## 3. 准入检查与磁盘预算

开始时的只读检查结果：

- Git 分支：`feat/m5-real-audio-provider`；HEAD 为 M4-B 完成提交 `4ebf043`，标签 `m4-b-complete`。
- Git 工作区：开始时洁净。
- GPU：NVIDIA GeForce RTX 4060 Laptop GPU，8GB；驱动 576.88。
- 8081、8188 均无监听；没有 Qwen、ComfyUI、Uvicorn 或其他 Python 模型推理进程。
- 初次观察约 1.7GB 显存来自桌面与模拟器等图形进程，不是 Qwen/Animagine 服务；正式运行前自然回落到约 655 MiB，因此没有擅自关闭用户应用。

安装前保守预留 15 GiB：独立 Python/CUDA/PyTorch 环境预计 5—7 GiB，模型约 2.5GB decimal，下载缓存和临时空间预计 3—5 GiB。安装后的实测占用为：

| 目录 | 实际大小 |
|---|---:|
| `.venv-qwen3-tts/` | 5,459,903,070 bytes（5.085 GiB） |
| `models/audio/` | 2,498,389,786 bytes（含极小下载 metadata，2.327 GiB） |
| `.cache-qwen3-tts/` | 2,968,380,664 bytes（2.765 GiB） |
| 最终成功运行目录 | 1,023,265 bytes |

## 4. 隔离环境与安装方式

本轮创建项目内独立 Conda prefix `.venv-qwen3-tts`，没有修改 `anime-platform` 或 `.venv-comfyui`。只选择一套已由本机 M4 环境证明兼容当前驱动的 CUDA 12.8 PyTorch wheel，没有尝试或反复安装其他 CUDA 大包。

实际安装命令：

```powershell
<CONDA_ROOT>\Scripts\conda.exe create --prefix `
  <PROJECT_ROOT>\.venv-qwen3-tts python=3.12 pip -y

.\.venv-qwen3-tts\python.exe -m pip install `
  --cache-dir .\.cache-qwen3-tts\pip `
  torch==2.11.0 torchaudio==2.11.0 `
  --index-url https://download.pytorch.org/whl/cu128

.\.venv-qwen3-tts\python.exe -m pip install `
  --cache-dir .\.cache-qwen3-tts\pip qwen-tts==0.1.1
```

最终环境：

| 项目 | 实测值 |
|---|---|
| Python | 3.12.13（Conda 独立环境） |
| qwen-tts | 0.1.1（官方 PyPI wheel） |
| Transformers | 4.57.3 |
| PyTorch | 2.11.0+cu128 |
| CUDA runtime | 12.8 |
| `torch.cuda.is_available()` | `true` |
| GPU | NVIDIA GeForce RTX 4060 Laptop GPU |
| Attention | PyTorch SDPA |
| FlashAttention | 未安装 |
| CPU offload | 未使用 |

`qwen-tts` 导入时会检查系统 SoX 命令并在日志中提示未找到。本轮 CustomVoice Python 路径直接返回波形并由 SoundFile 保存，两个音色均成功生成，因此没有为一个非阻断提示额外安装系统 SoX。

## 5. 模型下载、revision 与文件校验

实际下载命令使用官方 Hugging Face CLI、完整 40 位 revision、项目内缓存和 4 个下载 worker；网络短暂超时时 CLI 自动从不完整文件继续：

```powershell
.\.venv-qwen3-tts\Scripts\hf.exe download `
  Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice `
  --revision 85e237c12c027371202489a0ec509ded67b5e4b5 `
  --local-dir models\audio\Qwen3-TTS-12Hz-0.6B-CustomVoice `
  --cache-dir .cache-qwen3-tts\huggingface `
  --max-workers 4
```

本地 13 个正式模型文件的逻辑大小合计 2,498,388,392 bytes（2.498GB decimal / 2.327GiB），与固定 revision 的官方文件树一致。脚本还读取 13 个 Hugging Face `*.metadata` 文件的首行，全部等于固定 revision，不是只相信脚本常量。

关键权重：

| 文件 | 大小 | 官方与本地 SHA256 |
|---|---:|---|
| `model.safetensors` | 1,811,626,576 bytes | `bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb` |
| `speech_tokenizer/model.safetensors` | 682,293,092 bytes | `836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258` |

完整逐文件路径、大小、SHA256 和 revision 证据保存在最终运行目录的 `model_files.json`。

## 6. 有界冒烟脚本

脚本为 `scripts/m5_tts_smoke_test.py`，采用监督进程与隐藏 `--child` 双模式：

1. 父进程仅使用标准库，先完整核对模型文件、关键 SHA256 与下载 revision。
2. 父进程确认 8081/8188 空闲，再以参数列表和 `shell=False` 启动独立环境子进程；stdout/stderr 直接写文件，避免 PIPE 堵塞。
3. 子进程设置 Hugging Face 与 Transformers 离线模式，仅从本地模型目录加载；不可能在推理时调用云模型。
4. 子进程只加载一次模型，依次执行 Serena、Vivian；每阶段原子更新 `progress.json`。
5. 模型加载超时 300 秒、每个音色生成超时 300 秒、子进程总超时 900 秒。
6. 输出先写 `.part.wav`，确认浮点波形非空且无 NaN/Inf、峰值不超出 PCM 范围后，再以 WAV/PCM16 保存并原子改名。
7. 父子两侧均完整读取 WAV；核对帧数、采样率、声道、样本宽度、时长、非静音、削波比例和 SHA256。
8. `finally` 中删除模型引用、执行 GC 和 CUDA cache 清理；监督器只管理自己启动的 PID，不扫描后杀死其他 Python 进程。
9. 监督器以 `Popen.poll()/returncode` 验证自有子进程退出，并等待整卡显存回到基线加 512 MiB 的噪声余量内。

运行命令：

```powershell
<CONDA_ROOT>\envs\anime-platform\python.exe scripts\m5_tts_smoke_test.py
```

## 7. 固定输入与生成参数

- 原文：`深夜的旧书店里，少女翻开一本会发光的画册。蓝色鲸鱼从书页中游出，带她穿过寂静的星光。`
- 原文 SHA256：`f8526e52ea74e059d18bb9a8dfbf3c245b60d7cd1a8b13e90cac3b0c0bff6ee0`
- 语言：`Chinese`
- 顺序：Serena → Vivian
- 两次生成分别重置同一 PyTorch seed：`20260803`，用于减少比较时的无关采样差异。
- dtype：`bfloat16`
- device map：`cuda:0`
- attention：`sdpa`
- instruct：未提供；没有笑声、喘息、拟声词或特殊控制标记。
- 文本在代码中整段传入，没有切片、改写或截断。未下载 ASR 模型，因此“实际朗读内容是否完整正确”保留为人工试听项。

## 8. 最终实测结果

最终成功 run ID：`20260803T004650Z-f8f39d01`，运行目录：

`data/generated/m5/tts-smoke/20260803T004650Z-f8f39d01/`

模型加载耗时 4.167398 秒，加载次数为 1。两个文件均为 24,000Hz、单声道、16-bit PCM WAV。

| 指标 | Serena | Vivian |
|---|---:|---:|
| WAV 时长 | 8.880000 秒 | 11.920000 秒 |
| 生成耗时 | 49.474325 秒 | 66.046714 秒 |
| Real-time factor | 5.571433 | 5.540832 |
| 文件大小 | 426,284 bytes | 572,204 bytes |
| 峰值振幅 | 0.298828125 | 0.796875000 |
| RMS | 0.042636713 | 0.093181521 |
| 全幅采样比例 | 0 | 0 |
| SHA256 | `f27d4724794613ec78eed536a4a155ef025897e4236cfc3049cee694c28f18cd` | `da2dfa981048f2bcf4ab057028b7e2d5e8918795a441703e4aab9d1ecf14fa68` |

两个 SHA256 和字节内容均不同。自动技术检查没有发现静音、NaN、Inf、截断写文件或明显数字削波。FFprobe 独立复核也得到 Serena 8.88 秒、Vivian 11.92 秒、`pcm_s16le`、24kHz、单声道；FFmpeg 全量解码到 null 均返回 0。

整次监督器墙钟耗时 136.828764 秒，其中包含模型文件复核、进程启动、模型加载、两次生成、WAV 验收与清理。因为 RTF 约 5.5，本模型在当前 SDPA/8GB 设备上明显慢于实时播放速度，适合离线 Job，不适合被宣传为实时 TTS。

## 9. GPU、进程与离线边界

| 指标 | 实测值 |
|---|---:|
| GPU-wide 基线 | 780 MiB |
| GPU-wide 峰值 | 4893 MiB |
| 观测增量 | 4113 MiB |
| 清理后 | 512 MiB |
| OOM | 否 |
| CPU offload | 否 |
| 云 API | 否 |
| 进程残留 | 否 |
| 8081 / 8188 | 运行前后均空闲 |

显存来自 `nvidia-smi memory.used` 每秒整卡采样，是 Windows WDDM 下的 GPU-wide 观测，包含桌面等其他进程，不能冒充 TTS 子进程独占显存。最终监督器记录子进程返回码 0；外部 `Get-Process` 复核也没有 Python、llama-server 或 TTS 进程。

## 10. 追溯文件

最终成功目录包含：

- `serena.wav`
- `vivian.wav`
- `request.json`
- `result.json`
- `environment.json`
- `model_files.json`
- `serena.result.json`
- `vivian.result.json`
- `stdout.log`
- `stderr.log`
- `progress.json`
- `child-result.json`

模型、独立环境、下载缓存、日志和所有 WAV/JSON 结果均由 `.gitignore` 排除，不进入 Git。

## 11. Windows 清理判定修正

第一次真实生成已产出技术合格的两段 WAV，子进程也返回 0、显存已释放；但受限终端拒绝执行 `tasklist`，脚本把“无法进行系统级查询”误判为“进程仍存在”，因此监督器返回失败。修正后，监督器使用其持有的 `subprocess.Popen.poll()` 和 `returncode` 作为自有进程退出的权威证据，只有丢失自有句柄时才尝试系统查询。随后重新完整执行，最终 run 以退出码 0 成功。这项修正没有放宽 WAV、GPU、端口或模型来源验收。

## 12. 已知限制

- 没有自动判断中文发音、断句、语气、底噪或主观自然度；必须人工试听。
- 没有 ASR 回读，因此只证明程序没有改写或截断输入字符串，不声称自动证明每个字都被正确朗读。
- 只测试一段约 9—12 秒输出，没有覆盖超长旁白、标点密集文本、数字/英文混读或连续多镜头稳定性。
- 固定 seed 只服务于本次对比，不代表音色质量或未来版本完全可复现。
- PyTorch SDPA 避免了 Windows FlashAttention 编译风险，但当前 RTF 约 5.5；M5-B 必须把它当作离线后台任务。
- qwen-tts 导入时的 SoX 缺失提示本轮不阻断 CustomVoice；如果未来正式路径调用需要 SoX 的功能，应单独验证，不能把本轮成功泛化到所有包能力。
- Apache-2.0 允许使用不等于无需履行归属、NOTICE 或发布场景合规义务。

## 13. 是否建议进入 M5-B

从本地运行、显存、技术音频完整性和可回收性看，建议进入范围受控的 M5-B `AudioProvider` 集成，但应先由项目成员人工试听 Serena 与 Vivian，选择默认音色并记录主观理由。M5-B 应继续保留 Mock 音频离线保底、使用独立子进程和单 GPU 分阶段运行，不允许与 Qwen 文本模型或 Animagine 同时驻留，也不应加入声音克隆、VoiceDesign 或实时服务承诺。

## 14. M5-B 承接边界

M5-B 按上述建议把本模型接到正式平台契约，但不改变 M5-A 的实测结论边界：

- `Serena` 作为默认旁白音色；`Vivian` 作为用户创建 Job 前可选的第二个预置音色。一个 Job 内所有镜头共用同一音色，语言固定为 `Chinese`。
- Python 3.11 的 `anime-platform` 后端不直接导入 `qwen_tts`。`Qwen3TTSAudioProvider` 启动独立 `.venv-qwen3-tts` 一次性子进程；一个 3—5 镜头 Job 只加载一次模型，随后单并发顺序生成全部镜头。
- 音频 Job 只能复用成功 M4-B Job 已冻结的 ScriptV1 与已验收真实 PNG；不再次调用文本 Qwen，不启动 ComfyUI，也不重新生成图片。
- 每个镜头直接使用原始 `shot.narration`，不改写文本。真实 WAV 经过完整解码、非静音与 SHA256 校验后才能参与媒体合成。
- 独立 `MediaTimingPlan` 保持源 ScriptV1 时长不可变，并用 `max(源镜头时长, WAV 时长 + 0.20 秒 lead-in + 0.35 秒 lead-out)` 向上对齐到 24 fps 帧边界。旁白较长时允许透明延长镜头，不截断、不循环、不自动变速；最终渲染计划默认硬上限为 60 秒。
- Qwen 文本、ComfyUI/Animagine 和 Qwen3-TTS 在 8GB GPU 上严格分阶段。发现 8081、8188 或相应模型冲突时只返回 `GPU_HANDOFF_REQUIRED`，平台不终止外部进程。
- 真实 TTS 失败保持 Job `FAILED`，不会静默产生 Mock WAV。Mock 音频仍作为另一条显式工程保底路径存在，但不能冒充真实旁白。
- M5-B 仍不涉及声音克隆、真人参考声音、VoiceDesign、云 API、多角色对白分配、背景音乐模型或实时 TTS 服务。

M5-B 已完成真实 Serena 三镜头 E2E：Job `511262cc-ccf3-4038-878d-2b0037d737ee` 只加载一次模型，三段 WAV 分别为 2.960 秒 / 8.953 秒生成 / RTF 3.024662、4.000 秒 / 11.391 秒 / RTF 2.847750、3.920 秒 / 14.312 秒 / RTF 3.651020。源与渲染计划均为 20.000 秒，MP4 编码时长 20.021333 秒；总墙钟 88.235 秒，GPU-wide 峰值 3001 MiB，无 OOM、CPU offload 或 Mock 音频。M5-A 双音色冒烟的约 5.5 RTF 与本次短句 E2E 的 2.85—3.65 RTF 都是各自实测，不据此承诺固定性能。
