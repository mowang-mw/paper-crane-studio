# M2 最小全栈纵向链路实施记录

> 阶段状态：M2 已不再是当前开发阶段，本报告保留为当时的验收记录。M3 继续复用 M2 的 API、SQLite、四态 Job、独立 Worker、前端和 Mock + FFmpeg 兜底，并在其上新增本地文本 Provider；这不会追溯改写 M2 的实测结果，也不改变 M2 原验收结论。

> 字幕历史更正：M0 的 UTF-8 LF 字幕当时实际可见；M1/M2 共享动态路径在 Windows 下写出了 CRLF 文本，导致 FFmpeg `drawtext=textfile` 没有渲染旁白，成片只有左上角镜头标签。旧报告中的“命令含 drawtext/字幕文件存在”只证明配置存在，不能证明画面已出现旁白。本轮 M3 可用性修复后，公共媒体管线才通过 UTF-8 LF、完整解码与中点抽帧真正验证动态 `shot.narration` 烧录。

## 1. 阶段结论

M2 的代码实现与自动化纵向链路已通过：React 页面、FastAPI、SQLite、独立单 Worker、Mock Provider、M1 复用媒体函数和 FFmpeg 导出已经连成一条可实际运行的链路。最近一次黑盒 E2E 创建真实项目和 `QUEUED` Job，再由独立 Worker 子进程生成 4 镜头 MP4；下载后的文件通过 ffprobe 与 SHA-256 校验。

本轮执行环境没有可用浏览器绑定，可用浏览器列表为空。因此已验证 Vite 页面 HTTP 200、生产构建、CORS、前端 API 契约、媒体 HTTP 200 和下载文件，但没有把“浏览器实际点击、`<video>` 解码播放、点击下载”伪报为已验证。这三项保留为现场人工 smoke test。

| 项目 | 结论 |
|---|---|
| 后端单元/集成测试 | 通过，8/8 |
| 前端生产构建 | 通过，退出码 0 |
| API → SQLite → 独立 Worker → FFmpeg → Export | 通过 |
| M1 回归 | 通过 |
| 失败与显式手动重试 API | 通过 |
| 浏览器人工播放与下载 | 待现场确认 |
| 真实模型 | 未进入，符合 M2 边界 |

## 2. 实际实现结构

| 路径 | 职责 |
|---|---|
| `backend/app/main.py` | FastAPI 应用工厂、CORS、路由装配 |
| `backend/app/config.py` | 根目录、数据目录、SQLite URL、Worker 轮询间隔配置 |
| `backend/app/database.py` | SQLAlchemy Engine/Session；SQLite 外键、5 秒 busy timeout 与 WAL |
| `backend/app/models.py` | Project、Shot、Asset、GenerationJob、Export；Job 严格四态 |
| `backend/app/api/` | health、项目、任务、重试、安全媒体读取 API |
| `backend/app/providers/` | Script、Image、Audio Provider 最小接口及 Mock 实现 |
| `backend/app/services/generation.py` | Provider 编排、结构化脚本与镜头落库 |
| `backend/app/worker.py` | 独立单 Worker；领取、生成、失败落库、Export/Asset 持久化 |
| `backend/app/media/mock_pipeline.py` | M0/M1/M2 共用的确定性 FFmpeg 媒体链路 |
| `frontend/src/` | 单页项目工作区、轮询、四态展示、失败重试、播放与下载入口 |
| `backend/tests/` | API、持久化、失败重试、真实 FFmpeg Worker、媒体归属测试 |
| `scripts/*.ps1` | API、Worker、Vite 的 Windows 启动入口 |
| `scripts/m2_e2e_test.py` | 针对运行中 API 的标准库黑盒 E2E 与下载媒体校验 |

M1 的 `generate_m1_short()` 与 M2 Worker 都直接调用 `render_mock_project_short()`；Worker 没有通过 subprocess 启动 M1 脚本，也不存在第二套不兼容的 FFmpeg 流程。

## 3. 实际数据与状态链路

```text
POST /api/projects
  -> POST /api/projects/{id}/generate
  -> GenerationJob(QUEUED, progress=0)
  -> HTTP 立即返回 202，不生成媒体
  -> 独立 Worker 领取并提交 RUNNING
  -> Mock Script/Image/Audio Provider 形成 4 镜头及确定性参数
  -> M1 共用媒体函数在 data/projects/<project>/exports/<job>/ 生成文件
  -> ffprobe + SHA-256 成功
  -> 同一完成事务写入 2 个 Asset、1 个 Export 和 Job.result_json
  -> GenerationJob(SUCCEEDED, progress=100)
  -> 前端轮询详情并展示镜头、播放器和下载入口
```

失败路径已由自动测试验证：渲染异常使原 Job 变为 `FAILED` 并保留错误；只有 `POST /api/jobs/{id}/retry` 可以创建新的 `QUEUED` Job，原失败记录不被覆盖。第一版没有自动重试、取消、租约、心跳或崩溃恢复。

## 4. 实际依赖与版本

以下版本来自本机 package metadata、`package-lock.json` 与真实命令输出：

| 类别 | 包或工具 | 实际版本 |
|---|---|---:|
| 运行时 | Python | 3.11.15 |
| 后端 | FastAPI | 0.116.1 |
| 后端 | SQLAlchemy | 2.0.43 |
| 后端 | Pydantic | 2.13.4 |
| 后端 | Uvicorn | 0.35.0 |
| 测试 | pytest / httpx | 8.4.1 / 0.28.1 |
| 兼容依赖 | typing-extensions | 4.15.0 |
| 前端 | React / React DOM | 19.2.8 / 19.2.8 |
| 前端 | Vite / TypeScript | 7.3.6 / 5.9.3 |
| 系统 | Node.js / npm | 24.15.0 / 11.12.1 |
| 媒体 | FFmpeg / FFprobe | 8.0 / 8.0 |

`backend/requirements.txt` 固定本轮验证过的直接 Python 依赖，`frontend/package-lock.json` 固定完整 Node 依赖树。`python -m pip check` 返回 `No broken requirements found`，npm 审计返回 0 个已知漏洞。

## 5. 实际执行命令

本轮实际执行过的关键命令如下；没有执行 Git commit、模型下载、付费 API 或 M3 操作。

```powershell
conda run -n anime-platform python -m pip install fastapi==0.116.1 sqlalchemy==2.0.43 uvicorn==0.35.0 pytest==8.4.1 httpx==0.28.1
conda run -n anime-platform python -m pip install --ignore-installed --no-deps typing-extensions==4.15.0
npm install                                      # 工作目录 frontend

conda run -n anime-platform python -m compileall -q backend scripts
conda run -n anime-platform python -m pytest backend/tests -q
conda run -n anime-platform python -m pip check
npm run build                                    # 工作目录 frontend

conda run -n anime-platform python scripts/generate_m1_short.py
conda run -n anime-platform python scripts/verify_m1_output.py

conda run --no-capture-output -n anime-platform python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
npm run dev -- --host 127.0.0.1 --port 5173      # 工作目录 frontend
conda run --no-capture-output -n anime-platform python scripts/m2_e2e_test.py --api-base http://127.0.0.1:8000/api --worker-once
```

还执行了 PowerShell 脚本 AST 解析、前端与 health HTTP/CORS 请求、SQLite `PRAGMA journal_mode`、ffprobe、SHA-256、Git 状态与差异检查。最初的 pip/npm 网络请求被沙箱网络阻止，按明确授权在外部网络重试；TypeScript/Vite 在沙箱内写构建缓存时报 Windows `EPERM`，在相同工作区以获批权限执行后构建成功。

## 6. 自动化验证结果

- 后端：人工可用性修复后的最终结果为 `8 passed in 8.79s`，退出码 0。
- 前端：TypeScript 独立检查退出码 0；Vite 7.3.6 production build 转换 30 个模块，CSS 16.48 kB（gzip 4.55 kB），JS 213.34 kB（gzip 67.48 kB），退出码 0。
- 黑盒 E2E：`M2 E2E PASSED`，退出码 0。
- HTTP 请求线程边界：提交后立即查询仍为 `QUEUED`、progress 0，shots/export 为空。
- Worker：单次处理后 Job 为 `SUCCEEDED`、progress 100，数据库保存 4 Shot、2 Asset、1 Export。
- 失败重试：非 FAILED 重试返回 409；FAILED 重试创建不同 ID 的新 `QUEUED` Job。
- 持久化：使用指向同一文件的新 Database/Application 实例仍能查询项目。
- SQLite：应用重启后实测 `PRAGMA journal_mode` 为 `wal`。
- 前端服务：`GET http://127.0.0.1:5173/` 为 200 且包含 `#root`；health 为 200，CORS 返回 `http://127.0.0.1:5173`。
- M1：当时的独立验证只确认生成、时长和编码规格未回退，不能证明动态旁白字幕可见；字幕结论以上方历史更正为准。

## 7. 本轮 E2E 成片与追溯结果

| 字段 | 实际值 |
|---|---|
| 项目 ID | `5c45bfeb-69bf-4a19-9582-77e76fc269e2` |
| Job ID | `064f46fb-be5d-48cf-a394-60d798a6d66a` |
| Export ID | `e9646d0d-365b-47ac-9e60-5c5010db050d` |
| MP4 | `data/projects/5c45bfeb-69bf-4a19-9582-77e76fc269e2/exports/064f46fb-be5d-48cf-a394-60d798a6d66a/mock_short_064f46fb-be5d-48cf-a394-60d798a6d66a.mp4` |
| Manifest | `data/projects/5c45bfeb-69bf-4a19-9582-77e76fc269e2/exports/064f46fb-be5d-48cf-a394-60d798a6d66a/manifest.json` |
| 文件大小 | 777,947 bytes |
| 时长 | 28.021333 秒 |
| 视频 | H.264，1280×720，24 fps，yuv420p |
| 音频 | AAC，48 kHz，双声道 |
| 中文字体 | `C:\Windows\Fonts\msyh.ttc` |
| SHA-256 | `0d8f5b1a5315e2ee1f44a81a2e699769e3efd69f79b336c3e949dcd3b80265d8` |

Manifest 明确记录 Job ID、Job 请求快照、脚本 schema/fixture/provider 摘要、4 个镜头的参数与种子、每段视频和音频 SHA-256、字体、Python/FFmpeg/FFprobe 版本、安全命令展示、最终 ffprobe 摘要与 SHA-256；不包含虚构的模型、显存或 API 信息。

## 8. Windows、SQLite 与媒体处理

- API、Worker 与脚本都从激活的 `anime-platform` 环境运行；媒体模块通过环境变量或 `shutil.which` 解析 FFmpeg/FFprobe，并在缺失时给出明确错误。
- 当时的 drawtext 命令确实引用独立 UTF-8 textfile，媒体工具也处理了 Windows 路径转义；但动态文件采用 CRLF，实际旁白未渲染。修复后的公共模块显式以 UTF-8 LF 写入，并由真实中点帧确认。
- SQLite 打开外键、WAL 与 5 秒 busy timeout；HTTP 入队和 Worker 领取均使用短事务，FFmpeg 不占用数据库事务。
- 每个 Job 使用 `data/projects/<project-id>/exports/<job-id>/`，重复生成不会覆盖其他项目或旧 Job。
- 媒体 API 只读取数据库已登记的 Export，并再次校验解析后路径仍位于对应项目目录；错误项目 ID 和越界路径返回 404。
- 所有 subprocess 调用均使用参数列表、`shell=False`、返回码检查、stdout/stderr 捕获与超时。

## 9. 已知限制与进入 M3 前置项

1. 当前只有 Mock Provider，画面与声音仅用于验证工作流，不代表真实模型质量。
2. 当前 Image/Audio Provider 返回确定性的视觉参数和音频参数，实际几何画面与 WAV 由媒体服务生成。M3 接入前应冻结统一的 `VisualInput/MediaArtifact` 契约，使真实图像文件及其元数据可由 Provider 产出、由媒体服务消费；API、Job 和页面无需重写。
3. GenerationService 当前在数据库 Session 内调用极快的 Mock Provider。接入慢速真实 Provider 前必须改成“短事务读取快照 → 事务外调用 → 短事务校验并落库”，避免长期持有事务。FFmpeg 已经在事务外执行。
4. 仅允许一个 Worker；没有租约、心跳、崩溃恢复、自动重试、取消、复杂幂等、两阶段归档或 Export 失效传播。
5. 没有账号、多租户、权限、Redis、Celery 或分布式调度。
6. 当前会话无法进行真实浏览器自动化；开始 M3 前应人工完成 Demo 创建、轮询、视频播放、MP4/manifest 下载及刷新后 FAILED 重试。
7. 数据库保存最终视频和 manifest 两个 Asset；逐镜头片段/音频虽有 manifest 哈希，但未逐一登记 Asset ID，后续按实际编辑需求再增强。

## 10. 下一阶段可复用模块与判断

可直接复用的基线包括：应用工厂与可注入测试数据库、四态 Job、独立 Worker、Provider 抽象、项目隔离媒体函数、Windows 安全路径处理、ffprobe/SHA 验证、前端轮询与手动重试、黑盒 E2E。

从工程自动化结果看，M2 最小纵向链路已经具备进入 M3 的代码条件；正式开始真实模型接入前还必须完成一次现场浏览器人工 smoke，并先解决上节第 2、3 项的 Provider 产物契约和长事务边界。M3 只能替换 Provider，不应改变已经验证的 API、Job 四态、页面和 Mock + FFmpeg 兜底。

## 11. 人工可用性测试修复

本轮只调整 M2 交互和增加项目删除，没有修改媒体算法、视频参数、Mock Provider 或 Job 四态。

### 11.1 已修复行为

- Demo 按钮不再调用创建 API，只填充《纸鹤的夜航》标题和故事，并通过 `aria-live="polite"` 提示用户确认后创建。
- 创建使用同步 in-flight ref 和禁用按钮双重防重；成功后选中新项目，由 React effect 等待详情渲染，再滚动并聚焦项目标题，不使用盲目 `setTimeout`。
- 新增 `DELETE /api/projects/{project_id}`。不存在返回 404，存在 `QUEUED`/`RUNNING` Job 返回 409；其他项目会级联删除 Shot、Asset、GenerationJob、Export、Project 和受控项目目录。
- 删除路径必须是 `DATA_ROOT/projects` 的直接子目录，同时拒绝绝对路径、`..`、嵌套路径、projects 根目录、DATA_ROOT 和解析到其他项目/外部位置的链接。
- 项目列表的选择与删除是两个独立按钮；删除使用 `stopPropagation`、项目标题、不可恢复警告和二次确认。
- 页面增加 01—04 阶段导航，已完成、当前和未完成阶段有明确状态；只有已有内容的镜头/结果阶段可点击。
- 轮询检测到某个 Job 首次转为 `SUCCEEDED` 时，把 Job ID 写入本次页面生命周期内的已处理集合，显示成功提示并在结果 DOM 出现后只滚动一次。历史成功 Job 仅来自详情读取，不进入该集合触发路径，所以刷新不会强制滚动。
- 如果完成瞬间焦点仍在输入框、文本框、选择框或可编辑元素中，会保留成功提示和“查看成片”按钮，但跳过自动抢夺滚动。
- 镜头区域底部按 Job 状态显示下一步；成功时提供“前往播放成片”，失败时返回任务区域。
- 结果区域统一使用 `result-section`，显示“最终成片”、已生成、时长和镜头数；所有入口均调用同一个滚动函数。
- 右下角轻量提示在运行时显示进度，成功后保留“查看成片”快捷入口，小屏幕下收缩到可用宽度。
- 滚动尊重 `prefers-reduced-motion`；程序化聚焦目标设置 `tabindex="-1"`。

### 11.2 人工回归步骤

1. 连续点击 Demo，确认表单被填充且 Network 中没有创建 POST；快速双击创建，确认只有一个 POST。
2. 确认创建成功后新项目被选中、页面定位到项目区域并聚焦标题。
3. 验证删除按钮不会选中项目；取消确认不删除，确认删除只移除目标项目和目录。
4. 对等待/运行中项目删除，确认显示“当前项目仍有任务正在等待或生成，请等待任务结束后再删除。”
5. 运行一个新 Job，确认首次成功后出现提示并自动到达“最终成片”；刷新后不再次自动滚动。
6. 分别点击阶段导航、镜头区“前往播放成片”和固定快捷入口，确认都到达同一结果区。
7. 实际播放视频并下载 MP4、manifest；在窄屏及减少动态效果模式下检查焦点、滚动和按钮布局。

自动测试覆盖删除状态、数据库级联、项目目录清理、其他项目隔离和路径安全；前端不新增测试框架，以 TypeScript、production build 和上述人工清单验收。

本轮实际 API smoke 另创建并立即删除一个专用测试项目：DELETE 返回 204，随后 GET 返回 404，删除前后项目总数一致，未删除任何既有开发项目。DELETE 的 CORS 预检返回 200，允许方法为 `GET, POST, DELETE`。Vite 页面返回 200 且包含 React 根节点。

浏览器连接再次检查时可用浏览器列表仍为空，因此本节 11.2 的真实点击、焦点位置、滚动观感、视频播放和窄屏布局仍需人工执行；报告没有把 HTTP 或源码检查冒充为浏览器交互通过。

### 11.3 动态旁白字幕修复回归

修复后的 M2 黑盒 E2E 创建 Project `0c8b4d51-a0d6-4c34-994b-51ba9f76c872`、Job `c22a7d1e-69fc-44bf-ab34-c027cb9b1125`，生成 4 镜头、28.021333 秒、H.264/AAC、1280×720、24 fps 成片，SHA-256 为 `639b3864867c5bd494504ec069d1bc907ddeae32a7f9fc99f5c800203f8d065e`。完整音视频解码通过，四个镜头的独立字幕文件均为 UTF-8 LF；中点帧经人工查看分别显示各自旁白，不再只有左上角镜头标签。

证据目录为 `data/generated/subtitle-check/m2/short_c22a7d1e-69fc-44bf-ab34-c027cb9b1125/`，其中 `subtitle_check.report.json` 记录 ffprobe、完整解码、字幕文件和抽帧路径。该目录为本地生成证据并由 Git 忽略。
