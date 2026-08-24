# Third-party notices

Paper Crane Studio integrates with or documents the following third-party software, models, and services. This repository does not redistribute their model weights, full runtime checkouts, binaries, or service credentials.

| Component | Role | License or terms |
| --- | --- | --- |
| Qwen3-4B | Optional local text model served through llama.cpp | Apache-2.0 |
| Qwen3-TTS | Optional local speech synthesis model | Apache-2.0 |
| Animagine XL 4.0 | Optional image checkpoint used by ComfyUI | CreativeML Open RAIL++-M |
| llama.cpp | Optional local text-model runtime | MIT |
| ComfyUI | Optional local image runtime | GPL-3.0 |
| FFmpeg | Media composition and probing | LGPL-2.1-or-later by default. A particular build may be GPL-2.0-or-later when GPL components are enabled. |
| Alibaba Cloud Model Studio / Wan | Optional hosted Cloud Wan video service | Used under the applicable Alibaba Cloud Model Studio service terms. |

The MIT License in this repository applies only to Paper Crane Studio's own source code and documentation. Third-party models, model weights, runtimes, binaries, and hosted cloud services are not covered by the project's MIT License and are not distributed with this repository. Users must obtain them separately and comply with the applicable upstream license or service terms.

The adapter code in this repository interoperates with these components; it is not a relicensed copy of their source code. "ChatGPT Images" may be used as an external image source through the manual External Image Bridge, but there is no OpenAI Image API integration in this project.
