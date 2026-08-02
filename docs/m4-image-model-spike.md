# M4-A 本地动漫图像模型可行性冒烟记录

## 1. 结论

在 Windows、RTX 4060 Laptop 8GB 显存环境中，Animagine XL 4.0 Opt 已通过 ComfyUI 本地 API 使用内置节点完成单张真实动漫关键帧生成。最终测试使用 `--lowvram` 和 DynamicVRAM，保持 1024×576、24 步，没有发生 OOM，也没有降低分辨率或步数。

本结论只证明“单请求、单张关键帧可运行”。它不证明五镜头吞吐、角色一致性、复杂构图可靠性或生产级常驻服务稳定性。

## 2. 选型与官方来源

- 运行后端：[Comfy-Org/ComfyUI](https://github.com/comfy-org/ComfyUI)，只克隆官方仓库，不安装自定义节点。
- 模型仓库：[cagliostrolab/animagine-xl-4.0](https://huggingface.co/cagliostrolab/animagine-xl-4.0)。
- 模型文件：[animagine-xl-4.0-opt.safetensors](https://huggingface.co/cagliostrolab/animagine-xl-4.0/blob/main/animagine-xl-4.0-opt.safetensors)。
- 许可证：CreativeML Open RAIL++-M。
- 官方页面标示大小：约 6.94GB；本地实际大小为 6,938,350,040 字节，约 6.462GiB。
- 官方及本地核对后的 SHA256：`6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac`。

Animagine XL 4.0 Opt 是面向动漫图像的 SDXL 微调模型，官方模型卡明确支持 ComfyUI，并建议标签式英文提示、CFG 5 左右及质量标签。它符合本轮“动漫单帧、官方来源、内置节点”的固定范围。

## 3. 准入检查与磁盘预算

开始时检查结果：

- Git 分支：`feat/m4-real-image-provider`。
- Git 工作区：干净。
- GPU：NVIDIA GeForce RTX 4060 Laptop GPU，8188MiB。
- 驱动：576.88；`nvidia-smi` 报告最高 CUDA 12.9。
- 8081 无监听；没有 llama-server、Python 推理进程或 ComfyUI 进程需要停止。

安装前估计峰值磁盘占用为 25—35GB：模型约 6.94GB，独立 Python/CUDA/PyTorch 环境约 12—20GB，其余为下载缓存、ComfyUI 源码和输出。实际空间足以继续。

## 4. 独立安装方式

没有修改 `anime-platform` Conda 环境。ComfyUI 使用项目内、已被 Git 忽略的 `.venv-comfyui` 独立环境，源码位于已忽略的 `tools/ComfyUI`。

关键命令：

```powershell
git clone --depth 1 https://github.com/comfy-org/ComfyUI.git tools/ComfyUI
<PYTHON_ROOT>\python.exe -m venv .venv-comfyui
.\.venv-comfyui\Scripts\python.exe -m pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
.\.venv-comfyui\Scripts\python.exe -m pip install -r tools\ComfyUI\requirements.txt
.\.venv-comfyui\Scripts\hf.exe download cagliostrolab/animagine-xl-4.0 animagine-xl-4.0-opt.safetensors --local-dir models\image
Get-FileHash -Algorithm SHA256 models\image\animagine-xl-4.0-opt.safetensors
```

选择 CUDA 12.8 轮子是因为当前驱动报告最高 CUDA 12.9；没有尝试 CUDA 13.0，也没有升级驱动、系统 CUDA 或全局 Python。最初使用支持续传的 `curl --continue-at -` 访问同一官方 URL，但无法连接 `huggingface.co:443`；随后改用 Hugging Face 官方 CLI/Xet 下载后端，同样保持官方仓库来源和续传能力，没有使用镜像。

## 5. 环境版本

- ComfyUI project version：0.29.0。
- ComfyUI Git commit：`f06a187f50f896e4a0ba5be1ce1f2d2dcd13b77b`。
- ComfyUI frontend package：1.47.11。
- Python：3.13.3。
- PyTorch：2.11.0+cu128。
- CUDA runtime：12.8。
- NVIDIA 驱动：576.88。
- GPU：NVIDIA GeForce RTX 4060 Laptop GPU，计算能力 8.9。

`torch.cuda.is_available()` 为真，独立环境正确识别 GPU。没有安装 ComfyUI Manager 或任何自定义节点。

## 6. 有界测试脚本

脚本为 `scripts/m4_image_smoke_test.py`，只依赖 Python 标准库和 ComfyUI 环境已有的 Pillow。它负责：

1. 在启动前完整计算模型 SHA256，并在不匹配时立即失败；
2. 使用 `subprocess.Popen` 参数列表启动 ComfyUI，不使用 `shell=True` 或 `Start-Process`；
3. 在 240 秒启动超时内轮询 `/system_stats`；
4. 提交只含内置节点的 API 工作流；
5. 在 1200 秒生成总超时内轮询 `/history/{prompt_id}`；
6. 通过 `/view` 获取图片，使用 Pillow 完整解码并核对 PNG 分辨率；
7. 每秒通过 `nvidia-smi` 记录一次 GPU 全局显存；
8. 在 `finally` 中终止 ComfyUI 进程组，并验证进程退出和 8188 端口释放。

为避免新版 ComfyUI 默认数据库写入被忽略的源码目录，最终脚本显式使用 `--database-url sqlite:///:memory:`。日志末尾的 `forrtl: error (200)` 是 Windows 收到 `CTRL_BREAK_EVENT` 后的进程退出信息；工作流此前已经完成，脚本确认进程退出，且未调用强制 `taskkill`。

## 7. 工作流与最终参数

工作流只包含：

- `CheckpointLoaderSimple`
- `CLIPTextEncode` 正向提示
- `CLIPTextEncode` 负向提示
- `EmptyLatentImage`
- `KSampler`
- `VAEDecode`
- `SaveImage`

最终参数：

| 参数 | 实际值 |
|---|---:|
| batch size | 1 |
| width × height | 1024 × 576 |
| seed | 20260802 |
| steps | 24 |
| cfg | 5.0 |
| sampler | euler_ancestral |
| scheduler | normal |
| denoise | 1.0 |
| lowvram | 是 |
| OOM 重试 | 否 |
| 分辨率或步数降级 | 否 |

原始中文描述、最终英文标签提示和负向提示分别保存在 `request.json`、`positive_prompt.txt` 和 `negative_prompt.txt`。测试主体只描述原创少女，没有使用真实动漫角色名。

## 8. 实际结果


- ComfyUI API 计时：29.802 秒；ComfyUI 日志内部采样为 29.51 秒。
- 输出：`data/generated/m4/smoke/20260802T071111Z/generated.png`。
- PNG SHA256：`69e040782c598567345b406face4239d34a4435b0d2f5eb1cf33586ce6691725`。
- PNG：1024×576、RGB，可完整解码。
- GPU 全局基线：2751MiB；峰值：7842MiB；观测增量：5091MiB。
- OOM：未发生。
- 输出与前一轮相同 seed、相同最终提示的图片 SHA256 完全一致。

人工查看确认画面包含原创少女、旧书店、打开并发光的画册、清晰蓝色鲸鱼、蓝紫色夜间光线和横向动漫关键帧，未见明显文字或水印。鲸鱼位于书本上方并向画面游出，但单张扩散模型并不能严格保证“从某一页精确穿出”的几何关系。

## 9. 追溯文件

最终运行目录 `data/generated/m4/smoke/20260802T071111Z/` 包含：

- `generated.png`
- `workflow_api.json`
- `request.json`
- `result.json`
- `positive_prompt.txt`
- `negative_prompt.txt`
- `environment.json`
- `comfyui.stdout.log`
- `comfyui.stderr.log`

此外还保留了本轮使用的 `extra_model_paths.yaml` 以及隔离的 ComfyUI output/temp/user 子目录。模型、环境、源码、缓存和所有生成结果均由 `.gitignore` 排除。

## 10. 已知限制与 M4-B 建议

- 只生成了一张最终关键帧，没有测试并发、连续五镜头或长时间服务稳定性。
- `--lowvram` 是当前 8GB 显存设备的必要保守配置；峰值已接近显存上限。
- ComfyUI/Animagine 不应与本地 Qwen 同时占用 GPU，后续必须通过互斥运行或显式资源调度串行化。
- 标签提示能增强主体，但不能替代独立语义评分，也不能保证复杂空间关系。
- 没有测试角色一致性、ControlNet、IP-Adapter、LoRA、TTS 或视频生成。
- 许可证允许范围不等于项目可忽略合规义务；分发模型或派生服务前仍需保留许可证与通知并复核具体使用场景。

建议进入 M4-B，但应是“受控单并发 ImageProvider 集成”：复用本轮工作流和有界生命周期，默认 `lowvram`，与 Qwen GPU 互斥，保留 Mock 回退，并记录真实提示、seed、参数、模型 SHA 和显存/耗时。该建议不包含角色一致性或多模型扩展。
