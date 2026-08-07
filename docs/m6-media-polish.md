# M6-2 轻量媒体与成片体验优化

## 三种运动模式

新创建的成片 Job 在请求快照和 Manifest 中记录 `motion_preset`：

- `static`：中心裁切后的固定画面，不使用平移或缩放，仅保留 0.35 秒淡入淡出。
- `gentle_zoom`：中心固定、连续确定性的缓慢缩放，范围限制为 `1.000 -> 1.018`。
- `cinematic_pan`：固定 `1.018` 轻微缩放，在可用裁切范围的 42% 到 58% 之间横向移动。

默认值为 `gentle_zoom`。它保留静态关键帧的呼吸感，同时明显降低大幅缩放和平移造成的边缘跳动。Mock 与真实 PNG 共用同一个媒体函数和 preset 实现，帧率保持 24fps。

旧 Job 没有 `motion_preset` 字段时继续使用原有逐镜头 motion，不静默重写历史行为。手动重试复制原 Job 的完整请求快照，因此沿用原 preset。

## 抖动改善方案

原实现按镜头使用最高约 1.06 的缩放和全范围横向移动。`gentle_zoom` 改为只使用中心缩放，按 `on / (frame_count - 1)` 计算连续进度，缩放上限降为 1.018，不再改变平移方向。`cinematic_pan` 的位移只覆盖可用裁切范围中较小的一段，并且不作为默认值。

`static` 不包含 `zoompan`，因此可用于需要完全稳定构图的镜头。三种模式都不改变镜头帧数、字幕时间或 TimingPlan。

## 背景音乐上传与版权提示

项目级背景音接口接受 WAV、MP3、M4A 和 OGG，最大 20MB。上传使用原始二进制请求体，不新增 multipart 依赖。服务端依次执行：

1. 文件名、扩展名和 MIME 白名单校验。
2. 流式大小限制，超限立即拒绝。
3. ffprobe 检查音轨、格式和有限正时长。
4. FFmpeg 完整音轨解码，拒绝中途损坏的文件。
5. 计算 SHA256，写入项目受控目录并原子替换元数据。

保存字段包括原始文件名、MIME、格式、时长、大小、codec、采样率、声道、SHA256、存储路径和 `USER_UPLOAD` 来源。界面明确要求用户只上传拥有使用权的音频；Manifest 只记录用户上传来源，不声明版权归属。

## 混音与 ducking

未启用背景音时保持原有音频与封装路径。启用后，FFmpeg 执行稳定的两阶段处理：

- 背景音使用 `-stream_loop -1` 循环，并精确裁剪至最终渲染时长。
- 默认音量为 0.12，界面允许在 0.02 到 0.35 之间调整。
- 开头 0.6 秒淡入，结尾 0.8 秒淡出。
- 使用旁白轨作为 sidechain，对背景轨执行 `sidechaincompress`。
- attack 为 20ms、release 为 500ms，旁白开始时快速压低，结束后平滑恢复。
- 最后使用 `amix` 和 limiter；旁白轨自身不进入压缩器，始终作为清晰的主轨保留。

Mock 音频和真实 Qwen3-TTS 音频共用该混音实现。背景音只影响最终音频合成，不改变字幕、镜头或 TimingPlan。

## Poster 生成

最终 MP4 验证通过后，从第一镜头淡入结束后的合适时间点抽取一帧，统一缩放裁切为 1280x720 JPEG。Manifest 记录 poster 路径、SHA256、尺寸和抽帧时间；导出 API 提供公开 poster URL，前端 `<video>` 使用该 URL。旧导出没有 poster 时接口返回 404，不阻止 MP4 加载和播放。

## 已知限制

- ducking 基于整条最终旁白轨的电平，不做语义级语音活动检测。
- 极低电平或已被重度压缩的用户音频可能需要人工调节音量。
- 背景音属于项目级当前素材；Job 会冻结文件哈希和设置，但用户删除文件后，尚未执行的启用背景音 Job 会明确失败。
- 不提供音乐版权识别、响度标准化或音乐生成能力。
- 旧成片不会被后台重新编码，也不会自动补 poster。

## 人工验收步骤

1. 创建或选择项目，在“配音与成片”区切换三种运动模式。
2. 使用同一组关键帧分别生成 `static` 与 `gentle_zoom` 成片，观察中心构图和边缘稳定性。
3. 上传合法 WAV/MP3/M4A/OGG，确认显示文件名、时长和用户上传来源。
4. 尝试超 20MB、错误扩展名和损坏音频，确认界面显示可理解的拒绝原因。
5. 开启背景音并使用默认 12% 音量生成短片，确认开头/结尾无突变。
6. 重点试听旁白开始和结束位置，确认背景音压低与恢复平滑、旁白完整清晰。
7. 关闭背景音重新生成，确认与旧流程兼容。
8. 检查最终播放器加载前显示 poster，poster 失败时视频仍可播放。
9. 下载 Manifest，核对 motion、背景音来源/哈希/音量、ducking、poster 和 TimingPlan 字段。
10. 确认中文字幕仍烧录在画面中，MP4 总时长符合渲染计划。

## 提交前阻塞修复：逐镜头旁白

成功真实旁白 Job 的 `audio_shots` 通过数据库 `NARRATION_AUDIO` 资产记录生成公共 `audio_url`，不向前端暴露 `audio_path` 的绝对路径。Job 单项查询和项目详情查询使用同一序列化逻辑，因此已有成功 Job 无需重新生成 WAV 即可播放。

公共资产路由只接受属于当前项目的 `KEYFRAME_IMAGE` 或 `NARRATION_AUDIO`，路径必须保持在项目数据目录内，扩展名分别限制为 PNG 和 WAV。播放器使用 `preload="metadata"` 获取实际时长；URL 缌失显示 `AUDIO_ASSET_URL_MISSING`，请求或解码失败显示 `AUDIO_DECODE_FAILED`，不再静默停留在 `0:00 / 0:00`。

## Media-only rerender

`MEDIA_RERENDER` Job 只接受同项目中成功的真实 Qwen3-TTS Job 作为入口，并继续校验其 ScriptV1、真实 Animagine 图片 Job 和逐镜头 WAV 来源。Job 快照冻结：

- `parent_job_id`、`source_script_job_id`、`source_image_job_id`、`source_audio_job_id`
- `motion_preset`、背景音资产引用和 `background_volume`
- 三个 Provider 均为 `reused`，预期调用次数均为 0
- `media_only = true`

Worker 的专用分支不会创建 GenerationService、ImageProvider 或 AudioProvider。它在 FFmpeg 合成前校验来源 Job 归属和状态、PNG/WAV 数量、受控路径、SHA256，并完整解码每张 PNG 和每段 PCM16 WAV；任何缺失或损坏都会明确失败，不回退 Mock，也不重新生成模型资产。

通过校验后复用原 TimingPlan 调用现有真实旁白媒体 renderer，生成新的 MP4、Manifest 和 Poster。Manifest 记录 `media_only`、来源 Job、`reused_providers` 和三个 Provider 的零调用追溯。手动重试复制原 Job 快照，因此沿用相同来源、运动模式、背景音和音量。新 Export 写入后自动成为项目最新成片。

Poster 抽帧失败时 MP4 和 Manifest 仍保留成功结果，Manifest 与 Job warnings 记录 `POSTER_GENERATION_FAILED`；正常情况下三种结果均通过项目媒体 API 提供公共 URL。

人工验收时，在已有成功真实旁白 Job 的“配音与成片”区域分别选择三种 motion preset，点击“仅重新合成成片”，确认界面显示媒体进度并自动切换到新视频。整个过程中不应出现 Qwen、ComfyUI 或 Qwen3-TTS 进程与请求。
