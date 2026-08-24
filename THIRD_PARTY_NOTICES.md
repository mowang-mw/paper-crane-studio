# Third-party notices

Paper Crane Studio integrates with or documents the following third-party software, models, and services. This repository does not redistribute their model weights, full runtime checkouts, or service credentials. Before downloading, embedding, or distributing any component, review the current official license and terms for the exact version you use.

| Component | Role | Public-release note |
| --- | --- | --- |
| Qwen3 | Optional local text model served through llama.cpp | Use the official model license and distribution terms; weights are not included. |
| Qwen3-TTS | Optional local speech synthesis model | Review the official model and package licenses; weights are not included. |
| Animagine XL 4.0 | Optional image checkpoint used by ComfyUI | Review the checkpoint license and any base-model terms before redistribution. |
| llama.cpp | Optional local text-model runtime | Review the upstream source license and preserve required notices for any redistributed binaries. |
| ComfyUI | Optional local image runtime | Review the upstream source license and extension/model terms; the runtime is not vendored. |
| FFmpeg | Media composition and probing | Review the FFmpeg license and build configuration. Redistribution obligations depend on the exact build and enabled components. |
| Wan / DashScope | Optional Cloud Wan 2.7 API | This is a hosted service, not a bundled dependency. Follow the provider's SDK, API, pricing, and service terms. |

The adapter code in this repository is intended to call or interoperate with these components; it is not a relicensed copy of their source code. "ChatGPT Images" may be used as an external image source through the manual External Image Bridge, but there is no OpenAI Image API integration in this project.

License details are deliberately not guessed here. Confirm official notices for the versions and distributions you intend to publish, and add any required attribution or `NOTICE` text before redistributing third-party binaries, checkpoints, or generated assets.
