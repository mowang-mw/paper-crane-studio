# M0 + M1 实施记录

## 1. 实施结论

M0 与 M1 已在 Windows 的 `anime-platform` Conda 环境中实际运行并通过。M1 输出为《纸鹤的夜航》四镜头 Mock 短片，实测 28.021333 秒，包含 H.264 视频流、AAC 双声道音频流和已经烧录到画面中的中文字幕。

本阶段没有安装依赖、下载权重、联网调用 API，也没有创建 React、FastAPI、数据库或后台任务系统。

## 2. 实际实现结构

```text
backend/
  __init__.py
  app/
    __init__.py
    media/
      __init__.py
      ffmpeg.py          # 工具解析、安全 subprocess、ffprobe 验证、SHA-256
      mock_pipeline.py   # Mock WAV、构图、运镜、字幕、拼接和 manifest
fixtures/
  paper-crane/
    script.v1.json
    subtitles/
      m0.txt
      shot_01.txt
      shot_02.txt
      shot_03.txt
      shot_04.txt
scripts/
  media_smoke_test.py
  generate_m1_short.py
  verify_m1_output.py
data/generated/          # 运行产物，已被 .gitignore 忽略
```

代码只使用 Python 标准库。所有 FFmpeg/ffprobe 调用均传递参数列表，设置 `shell=False`，捕获 stdout/stderr，检查退出码并记录可安全展示的命令；输出先写本任务专用 `.part.mp4`，验证成功后才替换固定生成目标。

## 3. 实际环境与可执行文件

初始工具进程没有继承 Conda 激活状态：`python --version` 返回 3.13.3，基础 PATH 找不到 FFmpeg 和 ffprobe。未修改全局 PATH 或 Git 配置，而是通过 `conda run -n anime-platform` 运行所有阶段。

环境内实际解析结果：

- Python：3.11.15，`<CONDA_ROOT>\envs\anime-platform\python.exe`
- FFmpeg：8.0，`<CONDA_ROOT>\envs\anime-platform\Library\bin\ffmpeg.exe`
- ffprobe：8.0，`<CONDA_ROOT>\envs\anime-platform\Library\bin\ffprobe.exe`

运行时代码优先读取 `FFMPEG_BIN`/`FFPROBE_BIN` 的显式路径，否则使用 `shutil.which()`；找不到或路径不是文件时会立即给出清晰错误。

## 4. Windows 路径与字幕方案

### 4.1 遇到的问题

- Windows 字体和字幕文件路径包含盘符冒号与反斜杠，不能原样塞入 FFmpeg filter 字符串。
- 当前 FFmpeg 构建没有 libass/subtitles，不能依赖 ASS/SRT 字幕滤镜。
- 直接把中文和标点嵌入命令行会增加 PowerShell、Python 与 FFmpeg 多层转义风险。

### 4.2 最终方案

- 按顺序探测 `msyh.ttc`、`msyhbd.ttc`、`simhei.ttf`，本机最终使用 `C:\Windows\Fonts\msyh.ttc`。
- filter 内路径统一转成正斜杠，将盘符冒号转义为 `\:`，并以单引号包围。例如 `C\:/Windows/Fonts/msyh.ttc`。
- 每条字幕保存为独立 UTF-8 文本文件，通过 `drawtext=textfile=...` 读取。
- M0 与 M1 抽取中间帧进行目视检查，中文字符、标点、字幕底框和位置均正常。
- 同时生成 UTF-8 `data/generated/m1/subtitles.srt` 边车文件；正式 MP4 中的可见字幕仍来自 drawtext 烧录。

## 5. Mock 媒体实现

- 四镜头分别使用不同背景色、几何构图、场景标签和字幕，不是重复纯黑画面。
- `zoompan` 分别实现推近、拉远、横向平移和黎明推近；每镜头带简单淡入淡出。
- Python 标准库 `wave` 生成 48 kHz、双声道、确定性的低音量提示音；它不包含语义旁白，不能宣称为 TTS。
- 每镜头编码为 1280×720、24 fps、H.264/yuv420p + AAC，再以 concat demuxer 无重叠直接拼接。
- fixture 和 manifest 明确记录 `provider_id = mock`、`source_type = DETERMINISTIC_FALLBACK`，没有模型名称、显存或 API 信息。

## 6. 实际执行命令

环境检查：

```powershell
python --version
where.exe python
ffmpeg -version
ffprobe -version
where.exe ffmpeg
where.exe ffprobe

conda run -n anime-platform python --version
conda run -n anime-platform where.exe python
conda run -n anime-platform ffmpeg -version
conda run -n anime-platform ffprobe -version
conda run -n anime-platform where.exe ffmpeg
conda run -n anime-platform where.exe ffprobe
```

语法、M0、M1 和自动验证：

```powershell
conda run -n anime-platform python -m compileall -q backend scripts
conda run -n anime-platform python -m json.tool fixtures\paper-crane\script.v1.json
conda run -n anime-platform python scripts\media_smoke_test.py
conda run -n anime-platform python scripts\generate_m1_short.py
conda run -n anime-platform python scripts\verify_m1_output.py
```

M1 生成脚本和验证脚本均重复执行一次；两次生成得到相同 MP4 SHA-256，证明当前环境与参数下输出具有确定性。另使用 FFmpeg 在 2.5 秒抽取 M0 帧，并在 3.5、10.5、17.5、24.5 秒抽取 M1 四镜头检查帧进行目视核对。

## 7. 验证结果

### M0

| 项目 | 结果 |
|---|---|
| 输出 | `data/generated/m0/smoke_test.mp4` |
| 时长 | 5.000000 秒 |
| 视频 | H.264，1280×720，24 fps，yuv420p |
| 音频 | AAC，48 kHz，双声道 |
| 中文字幕 | “纸鹤飞向灯火之外”，已抽帧目视确认 |
| 字体 | `C:\Windows\Fonts\msyh.ttc` |
| SHA-256 | `499c322cf2612341db523a98c428b3810e8d5a855a3d84a6477e77016a9d2c04` |

### M1

| 项目 | 结果 |
|---|---|
| 输出 | `data/generated/m1/paper_crane_night_flight.mp4` |
| manifest | `data/generated/m1/manifest.json` |
| 镜头 | 4 个，每个计划 7 秒 |
| 实测总时长 | 28.021333 秒 |
| 视频 | H.264，1280×720，24 fps，yuv420p |
| 音频 | AAC，48 kHz，双声道 |
| 中文字幕 | 四条独立 UTF-8 textfile，四个镜头均已抽帧目视确认 |
| 字体 | `C:\Windows\Fonts\msyh.ttc` |
| 文件大小 | 817935 bytes |
| SHA-256 | `39cf73884f57589994fefa46f34a480e621abe2ad5aec9b3cd0cb87a4d529751` |

独立验证脚本检查 fixture、镜头数、Provider/来源标签、字体、文件非空、SHA-256、ffprobe 流、编码、分辨率、帧率、像素格式和时长；成功退出码为 0，任一条件失败返回非零退出码。

## 8. 已知限制

- 视觉是用于验证流水线的几何 Mock，不是动漫图像模型生成结果，也不代表角色一致性能力。
- 音频是低音量确定性提示音，不包含真实旁白或台词，不代表 TTS 能力。
- ffprobe 能验证流和媒体参数，不能识别字幕文字；本阶段额外采用四个时间点抽帧目视确认烧录结果。
- 当前字体来自 Windows 系统，不随仓库分发；其他机器若三个候选字体均不存在会明确失败。
- 当前使用四段同规格 MP4 直接拼接，没有复杂转场、混音、响度标准化或背景音乐。
- 本阶段没有 UI、API、数据库、Job/Worker、真实 Provider 或完整素材管理；这些属于后续里程碑。

## 9. 下一阶段可复用模块

- `ffmpeg.py`：可执行文件解析、字体检测、安全 subprocess、路径转义、ffprobe JSON 验证和 SHA-256。
- `mock_pipeline.py`：fixture 校验、确定性 WAV、镜头滤镜构建、片段规范化、拼接、SRT 与 manifest。
- `script.v1.json`：后续 TextProvider 和数据库导入可以共享的固定验收输入。
- 三个脚本：可直接作为 M2 媒体服务的烟测、离线兜底和回归验证入口。

M1 已满足进入 M2 的媒体前置条件；本报告不表示 M2 已开始。
