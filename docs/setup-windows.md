# Windows Setup

This guide covers the baseline environment and the optional runtimes used by Paper Crane Studio. The Mock providers are the supported starting point; model downloads are not required for the basic API and UI workflow.

## Baseline Tools

- Windows 10 or newer.
- Python 3.11 for the FastAPI backend and Worker.
- Node.js 20.19 or newer and npm for the Vite frontend.
- FFmpeg and `ffprobe` available on `PATH` for media composition and validation.
- Git and PowerShell 7 or newer are recommended.

Conda can create the application environment:

```powershell
conda create -n anime-platform python=3.11
conda activate anime-platform
python -m pip install -r backend/requirements.txt
```

Install frontend dependencies from `frontend/` with `npm install`. Copy `.env.example` to `.env` as a reference; the PowerShell launchers read process environment variables and do not parse `.env` automatically. Mock mode is enabled by default, so no environment import is needed for the basic workflow.

## FFmpeg

Install a Windows FFmpeg distribution that includes both `ffmpeg` and `ffprobe`. Verify:

```powershell
ffmpeg -version
ffprobe -version
```

If the binaries are not on `PATH`, set `FFMPEG_BIN` and `FFPROBE_BIN` to local executable paths in `.env`. Do not commit those paths.

## GPU and CUDA

GPU acceleration is optional for Mock mode and required only by selected local model runtimes. Use a driver and CUDA/PyTorch combination supported by the runtime version you install. Keep large model processes staged rather than resident together when GPU memory is limited.

## Optional Text Runtime

The text adapter targets a Qwen3-4B GGUF served by llama.cpp:

1. Download a compatible llama.cpp release and Qwen3 GGUF from their official sources.
2. Keep both outside Git-tracked source directories (the example uses `tools/llama.cpp/` and `models/text/`, which are ignored).
3. Set `SCRIPT_PROVIDER=llamacpp` and the `LLAMA_*` paths in `.env`.
4. Start the local server with `scripts/run_llm_server.ps1` before creating a real script job.

## Optional Image Runtime

The image adapter targets Animagine XL 4.0 through a separate ComfyUI installation:

1. Install ComfyUI in its own Python environment.
2. Download the checkpoint from an official model source and review its license.
3. Set `IMAGE_PROVIDER=comfyui-animagine-xl-4` and the `COMFYUI_*` paths.
4. Start ComfyUI on the local host and port configured in `.env`.

## Optional Speech Runtime

Qwen3-TTS runs in a dedicated environment because its dependency set can differ from the backend:

1. Create a separate Python environment compatible with the Qwen3-TTS release.
2. Download the Qwen3-TTS model outside the repository.
3. Set `AUDIO_PROVIDER=qwen3-tts-0.6b-customvoice`, `QWEN_TTS_PYTHON`, and the model path.

## Optional Cloud Wan Runtime

Cloud Wan 2.7 is accessed through DashScope. Set `DASHSCOPE_API_KEY` and, when required by the account, `DASHSCOPE_WORKSPACE_ID` only in a local `.env`. These values are placeholders in `.env.example`; use your own credentials and review current service pricing, quotas, regional availability, and terms before enabling the provider.

## Verification

With Mock providers selected, compile Python and run the backend tests from the repository root:

```powershell
python -m compileall -q backend scripts
python -m pytest -q
```

Frontend scripts are defined in `frontend/package.json`; run `npm run build` after installing dependencies.
