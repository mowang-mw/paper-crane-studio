# Product Requirements

## Product Goal

Paper Crane Workshop is a local, traceable workstation for turning a short story into an anime concept video. It coordinates structured script planning, visual asset generation, narration, subtitles, audio mixing, camera motion, and final MP4 export through stable Provider contracts.

## User Flow

1. Create a project and enter a short story.
2. Generate or edit a validated `ScriptV1` with characters, scenes, shots, visual prompts, and narration.
3. Review shot plans and choose Mock, local, external-image, or cloud Providers where configured.
4. Generate or import visual and audio assets, with source, revision, seed, hashes, and validation results recorded.
5. Review assets and selections, retry failed jobs explicitly, and reuse valid assets when possible.
6. Render subtitles, camera motion, background audio, and the final MP4 with FFmpeg.
7. Inspect the Manifest to trace the final output back to its inputs, Providers, jobs, and media files.

## Core Features

- Project, story, character, scene, shot, asset, and export management.
- Schema-validated ScriptV1 generation with Mock and local text Providers.
- Pluggable image, audio, video, and External Image Bridge Providers.
- SQLite-backed jobs with a single Worker, explicit states, retries, and failure boundaries.
- Deterministic offline Mock media for development and regression testing.
- Asset validation, SHA-256 integrity, provenance snapshots, and media reuse.
- GPU handoff controls for sequential local model execution.
- FFmpeg-based subtitles, camera motion, audio mixing, and MP4 export.
- Export Manifest with model, Provider, parameter, timing, source, and hash metadata.

## Non-Functional Requirements

- The offline Mock path must run without network access, API credentials, or model weights.
- Provider failures must be explicit and must not be silently converted into Mock success.
- Jobs and assets must remain inspectable after restart and must preserve their provenance.
- File inputs must be validated, confined to configured project storage, and protected from path traversal.
- The default local workflow should run on Windows with supported Python, Node.js, CUDA, GPU, and FFmpeg versions.
- Model execution must respect available VRAM through sequential loading and release checks.
- APIs, schemas, and media outputs should remain deterministic where the selected Provider supports determinism.

## Project Scope

The current scope is a single-user local workstation for short anime concept videos. It covers structured planning, a small number of shots, local or external Provider integration, reviewable intermediate assets, and reproducible MP4 export.

## Non-Goals

- Multi-user collaboration, accounts, permissions, or hosted multi-tenant deployment.
- A professional nonlinear editing timeline or character-level continuous video generation.
- Silent fallback from a failed real Provider to a different source.
- Bundling model weights, third-party runtimes, or generated media in the source repository.
- Treating a web chat product as a runtime Provider.
