# Paper Crane Studio

Paper Crane Studio is a local, staged media pipeline for turning a story into a short animated sequence. It combines structured script generation, keyframe images, narration, optional video generation, and deterministic FFmpeg composition behind one job-oriented API and web UI.

The core flow is:

```text
Story -> ScriptV1 -> Keyframe -> Video / Audio -> Composition -> Final Media
```

## Features

- Project and story management with a versioned `ScriptV1` schema.
- Provider contracts with safe Mock implementations for offline development.
- Optional Qwen3 text generation through a local llama.cpp server.
- Optional Animagine XL 4.0 keyframes through ComfyUI.
- Optional Qwen3-TTS narration in a dedicated Python environment.
- Optional Cloud Wan 2.7 image-to-video jobs through DashScope.
- External Image Bridge for manually importing images created outside this repository.
- Single-Worker staged execution, job snapshots, retries, manifests, and media validation.
- FFmpeg composition with subtitles, camera motion, audio ducking, and final MP4 output.

## Architecture

The FastAPI backend owns projects, jobs, provider contracts, media metadata, and the SQLite database. A separate Worker polls the job queue and executes one stage at a time. The React frontend talks to the backend over the `/api` prefix. Model runtimes remain separate processes or environments and are only needed when a real provider is selected.

## Pipeline

```text
Web UI / API client
        -> FastAPI backend
        -> SQLite job queue
        -> single Worker
        -> Script provider (Mock or Qwen3/llama.cpp)
        -> Image provider (Mock or Animagine XL/ComfyUI)
        -> Audio provider (Mock or Qwen3-TTS)
        -> Video provider (Mock or Cloud Wan 2.7)
        -> FFmpeg composition
        -> media files and a traceable manifest
```

The Worker is staged and single-process. Providers are not advertised as concurrently resident; this makes local GPU hand-off explicit and keeps the Mock path usable on a clean checkout.

## Quick Start (Mock mode)

Requirements: Python 3.11+, Node.js 20.19+, npm, and `ffmpeg` plus `ffprobe` on `PATH`. Conda is recommended for the backend and Worker.

```powershell
conda create -n anime-platform python=3.11
conda activate anime-platform
python -m pip install -r backend/requirements.txt
Copy-Item .env.example .env
Set-Location frontend
npm install
Set-Location ..
```

Start all three local services in separate PowerShell windows:

```powershell
.\scripts\run_demo.ps1
```

Or start them individually:

```powershell
.\scripts\run_backend.ps1       # FastAPI at http://127.0.0.1:8000
.\scripts\run_worker.ps1        # queue worker
.\scripts\run_frontend.ps1      # Vite at http://127.0.0.1:5173
```

The launcher starts Backend, Worker, and Frontend only. It does not start llama.cpp, ComfyUI, Qwen3-TTS, or Cloud Wan. Mock providers let the basic workflow run without model weights or cloud credentials. The scripts read process environment variables; they do not automatically parse `.env`, so import any non-default settings into the PowerShell session before starting a provider. Use `.\scripts\stop_demo.ps1` to stop processes created by the launcher.

## Optional Real Providers

Set the provider variables in a local `.env` file; leave the other providers on `mock` when they are not installed.

- **Text:** set `SCRIPT_PROVIDER=llamacpp`, download a compatible Qwen3-4B GGUF, and start `.\scripts\run_llm_server.ps1`.
- **Image:** set `IMAGE_PROVIDER=comfyui-animagine-xl-4`, install ComfyUI in a separate environment, place the Animagine XL 4.0 checkpoint in a local model directory, and start ComfyUI on `127.0.0.1:8188`.
- **Audio:** set `AUDIO_PROVIDER=qwen3-tts-0.6b-customvoice`, create the dedicated Qwen3-TTS environment, and make the model available locally.
- **Video:** choose the Cloud Wan provider for a job and configure the DashScope variables. Cloud calls are opt-in and may incur charges.

Model weights and third-party runtimes are intentionally not distributed in this repository. Download them from their official sources and review their licenses and service terms.

## Model / Runtime Setup

See [docs/setup-windows.md](docs/setup-windows.md) for Python, Node.js, Conda, FFmpeg, CUDA, and provider-specific setup notes. The repository contains provider adapters and scripts, not model weights or a full ComfyUI/llama.cpp checkout.

## External Image Bridge

External Image Bridge is a manual import path. A user supplies an image file produced by an external tool, records its source metadata, and attaches it to a project or shot. The bridge does not call the OpenAI Image API. ChatGPT Images is only one possible external source; the imported file and its reuse rights remain the user's responsibility.

## Project Structure

```text
backend/      FastAPI application, providers, Worker, media services, and tests
docs/         Architecture, schemas, setup, limitations, and provider notes
fixtures/     Small deterministic inputs used by tests and Mock mode
frontend/     React + Vite web client
scripts/      PowerShell launchers and optional provider runners
```

Runtime directories such as `data/`, `models/`, `delivery/`, local environments, logs, and generated media are ignored and should stay outside source control.

## Configuration

Copy `.env.example` to `.env` and change only the providers you intend to run. The example file contains no credentials. In particular, `DASHSCOPE_API_KEY` and `DASHSCOPE_WORKSPACE_ID` are blank placeholders. Never commit `.env` or a real key. `ANIME_PLATFORM_DATA_DIR` controls the local SQLite database and generated runtime data.

## Known Limitations

- The current runtime is local, single-user, and single-Worker.
- Real providers require their own runtimes, model downloads, GPU capacity, and license review.
- Cloud Wan requires a DashScope account and network access; availability, pricing, and quotas are controlled by the service.
- FFmpeg is required for final media composition and validation.
- This project does not provide a production multi-user deployment, distributed queue, or continuous video generation model.

## Third-party Components

Supported integrations include Qwen3, Qwen3-TTS, Animagine XL 4.0, llama.cpp, ComfyUI, FFmpeg, and Wan/DashScope. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution and license-review guidance.

## Authors

- Zihan Wang ([@mowang-mw](https://github.com/mowang-mw))
- Lei Zhao
- Huaizhong Lin

## License

Paper Crane Studio's own source code and documentation are released under the [MIT License](LICENSE). Third-party models, weights, runtimes, binaries, and cloud services are not covered by the project license and are not distributed with this repository; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
