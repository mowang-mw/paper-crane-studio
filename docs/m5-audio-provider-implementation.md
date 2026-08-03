# M5-B Qwen3-TTS AudioProvider 实施记录

## 1. 本轮目标与边界

M5-B 把 M5-A 已验证的本地 `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` 接入平台正式 `AudioProvider`，形成以下受控纵向链路：

```text
已有成功 ScriptV1
  -> 已有成功 M4-B 真实动漫 PNG
  -> Qwen3-TTS 逐镜头真实中文旁白
  -> MediaTimingPlan
  -> FFmpeg 真实画面 + 真实语音 + 烧录中文字幕
  -> MP4 + Manifest
```

本轮不调用文本 Qwen、不重新生成图像、不启动常驻 TTS 服务，不使用云 API、声音克隆、真人参考声音、VoiceDesign、背景音乐模型或多角色对白分配，也不进入 M6 视频生成。

M5-B 已完成一次真实 Serena 三镜头 E2E；本文后半部分的 Job、WAV、TimingPlan、MP4、耗时、显存和清理数值均来自该次实际运行及文件证据，不使用估算值。

## 2. 实际代码结构

| 文件 | 职责 |
|---|---|
| `backend/app/providers/base.py` | 冻结 `AudioGenerationOptions`、`AudioGenerationRequest`、`GeneratedAudioAsset` 与批量 Provider 契约 |
| `backend/app/providers/mock.py` | 保留显式 `MockAudioProvider` 工程保底 |
| `backend/app/providers/qwen3_tts.py` | 正式 `Qwen3TTSAudioProvider`；模型校验、一次性子进程、进度、WAV 验收、复用和清理 |
| `backend/app/services/audio_jobs.py` | GPU 交接、来源快照、可复用 WAV 校验与 `MediaTimingPlan` |
| `scripts/qwen3_tts_job_runner.py` | 只在独立 Python 3.12 环境运行的离线推理入口 |
| `backend/app/api/projects.py` | 创建真实音频子 Job，校验成功 M4-B 来源并冻结请求快照 |
| `backend/app/worker.py` | 按 Job 类型编排来源复用、真实 TTS、TimingPlan、FFmpeg 和结果持久化 |
| `backend/app/media/mock_pipeline.py` | 扩展公共 FFmpeg 层，使用真实 PNG/WAV、渲染计划与动态中文字幕导出 |
| `frontend/src/types.ts`、`api.ts`、`App.tsx` | AudioProvider 状态、Serena/Vivian、创建入口、逐段进度、时长延长与结果标识 |
| `backend/tests/test_m5_real_audio_media.py` 及 M5-B 工作流测试 | 不加载真实 GPU 模型的契约、时序、媒体和失败恢复测试 |

## 3. AudioProvider 契约

真实 Provider ID 固定为：

```text
qwen3-tts-0.6b-customvoice
```

单镜头请求包含当前 Project/Job、权威 ScriptV1、对应 Shot、受控输出目录和不可变选项。上游来源通过 Job `request_json` 与独立来源快照冻结：

- `parent_job_id`
- `source_script_job_id`
- `source_image_job_id`
- `source_script_provider`
- `source_image_provider`
- ScriptV1 快照路径与 SHA256
- 每张真实 PNG 的受控路径、SHA256、尺寸和 Provider
- `audio_provider`
- `speaker`
- `language`
- seed、模型 revision、超时和媒体参数

`GeneratedAudioAsset` 至少记录 Provider/model/revision、shot、speaker/language、原文、WAV 路径与格式、时长、生成耗时、RTF、峰值、RMS、WAV SHA256、关键模型 SHA、trace、警告和是否复用。

Provider 不读取前端当前状态覆盖 Job 快照。真实 TTS 失败时不调用 `MockAudioProvider`；Mock 只作为用户明确选择的另一条工程保底存在。

## 4. 音色产品策略

| 音色 | 界面说明 | 策略 |
|---|---|---|
| Serena | 年轻、温暖、节奏较快 | 默认选择；真实 E2E 使用该音色 |
| Vivian | 稳重、明亮、节奏较慢 | 创建 Job 前可选；M5-A 已真实冒烟，M5-B 用单元测试验证快照与传递 |

语言固定为 `Chinese`。一个 Job 的 3—5 个镜头使用同一音色，不支持逐镜头换声线或角色对白分配。手动重试复制原 Job 的 `speaker` 与 `language`，不会采用用户重试时在页面上新选择的值。

音色描述只是产品定位，不是自动音质结论；发音、断句、自然度、情绪与 Serena/Vivian 主观偏好仍需人工试听。

## 5. 来源 ScriptV1 与真实 PNG 复用

前端操作为“为当前真实动漫画面生成AI旁白”。API：

```text
POST /api/projects/{project_id}/render-real-audio
```

请求体只允许：

```json
{
  "source_image_job_id": "<成功的 M4-B Job ID>",
  "audio_provider": "qwen3-tts-0.6b-customvoice",
  "speaker": "Serena",
  "language": "Chinese"
}
```

服务端要求来源 Job：属于同一 Project、类型为 `GENERATE_REAL_IMAGE_VIDEO`、状态为 `SUCCEEDED`、Provider 为 `comfyui-animagine-xl-4`，并具有严格 ScriptV1 快照与 3—5 张验收通过的真实 PNG。`source_script_job_id` 和来源 Provider 由服务端从 M4-B Job 派生，不由客户端自行声明。

音频子 Job 明确保存：

```text
script_provider = reused
image_provider = reused
audio_provider = qwen3-tts-0.6b-customvoice
script_provider_calls_expected = 0
image_provider_calls_expected = 0
```

Worker 只读取来源快照，不创建或调用 ScriptProvider/ImageProvider，不启动 llama-server 或 ComfyUI，也不修改项目当前 ScriptV1 与来源 PNG。

## 6. Python 环境隔离与一次性 runner

`anime-platform` 使用 Python 3.11，不能直接导入 `qwen_tts`、PyTorch 或 CUDA 依赖。真实 Provider 只启动：

```text
.venv-qwen3-tts\python.exe scripts\qwen3_tts_job_runner.py
```

子进程特征：

- 使用参数数组和 `shell=False`，stdin 关闭，stdout/stderr 写入 Job 日志，避免 PIPE 堵塞。
- 设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1` 和禁用遥测，只允许固定本地模型目录。
- 协议要求 `cloud_api_used=false`、`voice_cloning_used=false`、`attention_implementation=sdpa`、`dtype=bfloat16`、`device_map=cuda:0`。
- 一个 3—5 镜头 Job 只启动一个 runner，模型只加载一次，随后按 Shot index 单并发顺序生成全部缺失 WAV。
- 模型加载、单镜头生成和 Job 总时长分别设置超时；进度通过原子 JSON 文件可观测，不做无限轮询。
- 完成、错误和超时路径都等待并回收自己启动的子进程，不扫描或终止无关 Python 进程。
- 子进程退出后执行必要的 Python/CUDA 清理；父进程保存进程退出与 GPU 回落摘要。

固定模型事实沿用 M5-A：

| 项目 | 固定值 |
|---|---|
| 模型 | `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` |
| revision | `85e237c12c027371202489a0ec509ded67b5e4b5` |
| 根权重 SHA256 | `bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb` |
| speech tokenizer SHA256 | `836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258` |
| qwen-tts | 0.1.1 |
| 许可证 | Apache-2.0 |

Provider 启动前核对环境、下载 metadata revision 和两个关键 SHA256；不匹配立即失败，不自动下载、换模或改用云服务。

## 7. GPU 分阶段流程

8GB 显存下三个真实模型阶段严格互斥：

```text
llama.cpp / Qwen 文本
  -> 停止并释放 8081/GPU
ComfyUI / Animagine 图像
  -> 有界退出并释放 8188/GPU
Qwen3-TTS 音频
  -> 一次性子进程退出并释放 GPU
FFmpeg 媒体合成
```

API 入队与 Worker 执行前都检查 8081、8188、已知模型进程与 `nvidia-smi` 整卡占用，覆盖排队期间外部模型重新启动的竞争窗口。默认前置占用高于可配置的 2048 MiB 即视为 GPU 尚未交接；发现冲突返回 `GPU_HANDOFF_REQUIRED`，同时记录是否需要关闭 Qwen、ComfyUI 或其他高显存程序。平台只读检测，不主动结束用户进程。

## 8. 逐镜头 WAV 生成与验收

每个镜头直接传入 `shot.narration`，不切片、不改写、不添加笑声、喘息或控制标记。受控输出包括：

```text
jobs/<audio-job-id>/
  audio/
    shot-01.wav
    shot-01.request.json
    shot-01.result.json
    shot-01.text.txt
  tts.stdout.log
  tts.stderr.log
  audio-runner-progress.json
  audio-runner-summary.json
  audio_generation_report.json
  timing_plan.json
```

每段 WAV 进入媒体层前必须满足：

- 文件存在且非空，能够完整解码。
- 采样率、声道、sample width、帧数和时长已记录。
- 不包含 NaN/Inf；PCM 非全静音且没有明显数字削波。
- 结果中的原文与当前 `shot.narration` 完全一致。
- WAV SHA256 与 trace/报告一致。
- Provider、模型、revision、speaker、language 和 seed 与请求快照一致。

输入 WAV 采样率不硬编码；FFmpeg 先探测，再统一重采样为最终 48kHz AAC。

## 9. MediaTimingPlan

源 ScriptV1 保持不可变，原 `shot.duration_seconds` 不被真实语音覆盖。每镜头独立记录：

- `source_shot_duration`
- `audio_duration`
- `lead_in_seconds`
- `lead_out_seconds`
- `rendered_shot_duration`
- `extended_by_seconds`
- `extension_reason`

默认值与公式：

```text
lead_in_seconds = 0.20
lead_out_seconds = 0.35

raw_rendered_duration = max(
  source_shot_duration,
  audio_duration + lead_in_seconds + lead_out_seconds
)

rendered_shot_duration = ceil(raw_rendered_duration * 24) / 24
```

短 WAV 以尾部静音补足镜头；长 WAV 延长静态 PNG 运镜镜头。M5-B 不截断尾音、不循环旁白、不自动变速，也不为了适配时长静默改写 ScriptV1。

源 ScriptV1 总时长仍须为 20—40 秒。最终渲染计划可以透明延长，但默认硬上限为 60 秒；超过时返回 `AUDIO_TIMING_EXCEEDS_LIMIT`。最终 FFmpeg/ffprobe 容差比较 `encoded_duration_seconds` 与 `rendered_planned_duration_seconds`，不再错误地与源计划总时长直接比较。

## 10. FFmpeg 合成与 Manifest

每个镜头复用已有真实 PNG，按 `rendered_shot_duration` 应用轻微、确定性的 Ken Burns 运镜；旁白在 lead-in 后开始，尾部补静音。中文字幕继续从独立 UTF-8 LF 文件经 drawtext 烧录，并覆盖旁白主要播放区间。没有 Mock 测试音或背景音乐。

最终规格：

- 1280×720
- 24 fps
- H.264
- AAC 48kHz
- `yuv420p`
- 可由 ffprobe 读取并由 FFmpeg 完整解码

Manifest 至少记录：

- `script_provider=reused` 与来源 Script Job
- `image_provider=reused`、实际来源 Animagine Provider 与来源 Image Job
- `audio_provider=qwen3-tts-0.6b-customvoice`
- speaker、language、模型 revision 与关键 SHA256
- 每镜头 WAV 路径、SHA256、时长、生成耗时和 RTF
- 每张来源 PNG 路径与 SHA256
- `source_planned_duration_seconds`
- `rendered_planned_duration_seconds`
- `encoded_duration_seconds`
- 延长总量与 `timing_plan_path`
- `subtitle_rendering=burned_in`
- FFmpeg/ffprobe 与媒体容差验证摘要

## 11. 失败处理与显式重试

主要错误码：

- `GPU_HANDOFF_REQUIRED`
- `TTS_ENV_NOT_FOUND`
- `TTS_MODEL_NOT_FOUND`
- `TTS_MODEL_HASH_MISMATCH`
- `TTS_PROCESS_START_FAILED`
- `TTS_MODEL_LOAD_TIMEOUT`
- `TTS_GENERATION_TIMEOUT`
- `TTS_GENERATION_FAILED`
- `AUDIO_OUTPUT_MISSING`
- `AUDIO_DECODE_FAILED`
- `AUDIO_SILENT`
- `AUDIO_TIMING_EXCEEDS_LIMIT`
- `MEDIA_RENDER`

失败结果记录阶段、失败 shot、已完成数量、speaker、是否可重试、GPU 交接、OOM、日志路径和可复用 WAV。手动重试创建新的 QUEUED Job，保留旧 FAILED Job，并沿用原来源、Provider、speaker、language、seed 与时序配置。

旧 WAV 只有在来源剧本/图像 Job、Provider/model/revision、shot、原文、speaker/language、seed、哈希、解码、非静音和 trace 全部匹配时才复用；损坏或缺失镜头继续生成。如果 WAV 已齐全但 FFmpeg 失败，恢复不应重新调用 TTS。任何真实 TTS 失败都不会静默回退 Mock。

## 12. 前端交互

现有视觉体系下增加：

- AudioProvider ID、模型与配置/GPU 状态。
- Serena/Vivian 单选，默认 Serena，并显示两者简短说明。
- 成功 M4-B 来源 Job 与“为当前真实动漫画面生成AI旁白”按钮。
- 逐镜头音频进度、WAV 时长、生成耗时/RTF、源时长到渲染时长及延长量。
- Job 级总 TTS 耗时、音色、语言、源计划/渲染计划/编码时长。
- TTS/AUDIO/GPU/MEDIA 失败诊断、日志与显式手动重试。
- 最终真实旁白徽标、视频播放、MP4 与 Manifest 下载。

真实音频标识只在成功 Export 的 `audio_provider` 精确等于真实 Provider ID 时显示。已有 M4-B Mock 音轨以及真实音频 Job 完成前的上一版成片继续明确标为 Mock 音频，不会因为画面真实就冒充真实配音。

## 13. 测试与回归边界

单元测试和伪 Provider 测试不得加载真实 GPU 模型。它们覆盖 Provider 注册、默认/可选音色、快照与重试、来源复用、零 Script/Image 调用、GPU 冲突不杀进程、3/4/5 镜头顺序、一次加载、单并发、逐段失败与复用、WAV 技术验收、TimingPlan、60 秒上限、FFmpeg 真实音频、字幕和 Manifest。

完成检查要求：backend compileall、完整 pytest、TypeScript 严格检查、production build、M0—M4 回归、M5-B 真实三镜头 E2E、ffprobe、完整解码、字幕抽帧、Git 边界与受保护文件检查。

本文档创建时完成状态：

| 检查项 | 状态 |
|---|---|
| 后端 compileall / pytest | 通过；compileall 无错误，完整平台测试 115 passed |
| TypeScript / production build | 通过；严格 `tsc` 检查与 Vite production build 均成功 |
| M0—M4 回归 | 通过；M0/M1 实际生成、M2 黑盒 E2E、M3 71 tests、M4 17 tests 均成功 |
| M5-B 真实三镜头 E2E | 通过；Job `511262cc-ccf3-4038-878d-2b0037d737ee` |
| ffprobe / 完整解码 / 字幕抽帧 | 通过；H.264/AAC、1280×720、24 fps、20.021333 秒；完整解码及三镜头中点抽帧成功，另核验第一镜旁白中点字幕 |
| 进程、端口与显存清理 | 通过；自有子进程退出，GPU 675→3001→674 MiB，8000/8081/8188 均释放 |

## 14. 真实三镜头 E2E 取证

优先来源 M4-B Job：`11c1b83a-f5b7-4511-b7db-2e1056ef2160`；若本机数据库中不存在，只允许选择另一条已有成功的真实三镜头图像 Job，不为测试重新调用 Qwen 或 Animagine。

| 指标 | 最终实测 |
|---|---|
| M5-B Job ID | `511262cc-ccf3-4038-878d-2b0037d737ee` |
| Serena shot-01 WAV 路径/时长/耗时/RTF/SHA256 | `data/projects/36d4bdd5-0e88-4509-a2f5-eba7727fd38b/jobs/511262cc-ccf3-4038-878d-2b0037d737ee/audio/shot-01.wav`；2.960 秒；8.953 秒；3.024662；`3b398e6ca8fabfaa3f37d18d544052510ca6099600cb26aea6ad2323063f68e0` |
| Serena shot-02 WAV 路径/时长/耗时/RTF/SHA256 | `data/projects/36d4bdd5-0e88-4509-a2f5-eba7727fd38b/jobs/511262cc-ccf3-4038-878d-2b0037d737ee/audio/shot-02.wav`；4.000 秒；11.391 秒；2.847750；`c82f729a871f952e7e54d8fa06ff25f7e0033a2332369cf2da99765c10e66658` |
| Serena shot-03 WAV 路径/时长/耗时/RTF/SHA256 | `data/projects/36d4bdd5-0e88-4509-a2f5-eba7727fd38b/jobs/511262cc-ccf3-4038-878d-2b0037d737ee/audio/shot-03.wav`；3.920 秒；14.312 秒；3.651020；`d246dbc4e0251c7900cfa0751b8732441ba8af90dee735a1b777b8f2a1cc8113` |
| 源 ScriptV1 总时长 | 20.000 秒；原 8 / 6 / 6 秒镜头保持不变 |
| 渲染计划总时长与延长量 | 20.000 秒；三段旁白均在原镜头内完整容纳，延长 0.000 秒 |
| MP4 路径/编码时长/SHA256 | `data/projects/36d4bdd5-0e88-4509-a2f5-eba7727fd38b/exports/511262cc-ccf3-4038-878d-2b0037d737ee/short_511262cc-ccf3-4038-878d-2b0037d737ee.mp4`；20.021333 秒；`f55e6ea61fe6638a40ce9ab4950a6e15618b4d2b617bbf680b35adc17f6eb911` |
| 模型加载次数 | 1；三镜头顺序单并发生成 |
| 总耗时与 GPU-wide 峰值 | 总墙钟 88.235 秒；音频阶段 80.688 秒；模型加载 2.812 秒；基线 675 MiB、峰值 3001 MiB、观测增量 2326 MiB |
| OOM / CPU offload / Mock 音频 | 均为否；没有云 API 或声音克隆 |
| 8000 / 8081 / 8188 与 TTS 进程清理 | 三端口运行前后均空闲；runner 子进程返回并退出；显存回落到 674 MiB |

E2E 汇总位于同一 Export 目录的 `m5b-e2e-summary.json`。Vivian 未重复执行完整视频 E2E；其可选与透传由测试覆盖，最终音质仍依赖人工试听。

## 15. 已知限制与人工确认

- Qwen3-TTS 在 M5-A 的当前 Windows/SDPA 路径 RTF 约 5.5，适合离线后台 Job，不适合宣传为实时语音。
- MediaTimingPlan 解决“完整播放”与镜头时长的工程冲突，不自动保证旁白节奏、美术节奏或情绪匹配。
- 没有 ASR 回读；程序验证原文未被改写与 WAV 技术完整性，但不声称自动证明每个中文字发音正确。
- Serena/Vivian 的音质、断句、情绪、底噪和选择偏好必须人工试听。
- 一个 Job 只支持一个预置旁白音色；没有多角色对白或逐镜头换音色。
- 没有声音克隆、参考音频、VoiceDesign、背景音乐模型或云 API。
- 真实 TTS 不改变“真实视频模型不在成功关键路径”的既有范围。

## 16. 阶段结论

M5-B 已以真实三镜头 E2E 证明正式 AudioProvider、来源复用、一次性隔离推理、真实语音时序与 FFmpeg 成片能够形成可追溯纵向链路。完成后停止在 M5-B，不进入声音克隆、背景音乐、M5-C 或 M6。
