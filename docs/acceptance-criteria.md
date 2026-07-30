# 验收标准

## 1. 验收分层

验收分为三层：

- **工程保底 P0**：全 Mock + FFmpeg，在断网、无 API Key、无真实模型时必须通过；Job 只要求 `QUEUED/RUNNING/SUCCEEDED/FAILED` 与手动重试。
- **真实 Provider 完成目标**：P0 通过后，至少一个真实文本 Provider 和一个真实图像 Provider 分别通过第 8、9 节。真实模型安装/下载仍需许可和许可证复核；这些前置条件不是把目标永久降为可选的理由。
- **稳定后增强**：自动重试/`RETRY_WAIT`、取消、租约/心跳与崩溃恢复、客户端幂等键、STAGING 两阶段恢复、复杂 CAS、Provider 多次调用历史和完整 Export 失效竞争。它们不属于 P0。

工程保底要求全部 P0 项为 `PASS`，没有未解释的 `FAIL`。真实 Provider 完成还要求真实文本与真实图像均通过；若图像因目标机实测最终失败，必须保留证据、Provider 接口和替代演示，并把结果明确记为 `FAIL/NOT READY`，不能声明真实 Provider 目标已完全完成。每项保存测试日期、代码版本、环境、输入、关键日志或截图以及输出文件哈希。

## 2. 测试环境与固定输入

- 主验收平台：`ENVIRONMENT.md` 所述 Windows、RTX 4060 8GB、31.6GB RAM、Python 3.11、Node 24、FFmpeg 8.0。
- 测试使用隔离的数据根目录和数据库，禁止用清理命令触碰真实用户素材。
- P0 验收先断开网络或用测试配置阻断外网，清除所有模型 API Key，并禁用真实 Provider。
- 固定输入采用 [《纸鹤的夜航》](mvp-scope.md#61-故事纸鹤的夜航)，期望结构为 4 镜头、每镜头 7 秒、计划总长 28 秒。
- 所有时间以 ffprobe 实测为媒体验收依据；数据库计划值只作对照。

## 3. 核心业务功能

| ID | 验收操作 | 预期结果 | 证据 |
|---|---|---|---|
| AC-CORE-01 | 新建项目，填写标题、故事、目标 28 秒 | 返回 Project ID；刷新和重启 API 后数据仍存在 | API 响应、数据库查询、页面截图 |
| AC-CORE-02 | 提交 Script Job | HTTP 快速返回 `202` 和 Job ID，不等待生成完成 | 响应时间记录、Job 行 |
| AC-CORE-03 | 审核并保存固定剧本 | 生成 Character、Scene、4 个 Shot；序号唯一，总时长 28 秒 | API/数据库快照 |
| AC-CORE-04 | 尝试保存 2 个或 6 个镜头、19 秒或 41 秒计划，或镜头总和不等于 Project 目标值 | 返回稳定校验错误，不污染已审核业务数据 | 负例响应、前后数据库对比 |
| AC-CORE-05 | 修改角色视觉描述或一个 Shot 的时长 | P0 采用保守失效：相关选择被清除或 Asset 标为 STALE，旧素材不能用于新导出；按 role fingerprint 精确保留无关素材属于增强 | 变更前后状态、UI 提示 |
| AC-CORE-06 | 每镜头先生成一个 Mock 关键帧；再对其中一个镜头执行一次新的单候选生成并切换选择 | 两次 Job/候选均保留；Shot 只有一个选择指针；切换选择不删除旧文件 | Asset 列表、Shot 外键 |
| AC-CORE-07 | 重新生成已选镜头关键帧 | 创建新 Asset 和文件，不覆盖旧 SHA-256 或路径 | 文件/数据库差异 |
| AC-CORE-08 | 从关键帧生成并选择镜头/音频，预览字幕时间轴 | 每镜头存在可探测且已选择的片段和音频；时间轴合法；此时尚无正式 SUBTITLE Asset | ffprobe JSON、时间轴响应、Asset 元数据 |
| AC-CORE-08A | 未先选择 clip 或 audio 就请求导出 | 导出门禁拒绝并列出具体 Shot；Export 不隐式创建 Speech/Clip Job | API 响应、Job 列表 |
| AC-CORE-09 | 缺少某镜头选择或使用 STALE Asset 时导出 | Export Job 不启动或明确失败，返回具体 Shot 和缺项 | 错误响应/Job error code |
| AC-CORE-10 | 完整项目导出 | Export Job 自动产生 MP4、正式 SUBTITLE 边车 Asset 和 manifest，无需手改媒体文件 | Export/Asset 记录和文件 |

## 4. 全 Mock 完整链路验收

| ID | 条件与步骤 | 通过标准 |
|---|---|---|
| AC-MOCK-01 | 空测试数据库、无网络、无 Key、所有真实 Provider disabled，从新建项目开始 | 完整到达成功 Export，不访问外部模型服务 |
| AC-MOCK-02 | 固定故事生成结构化剧本 | 输出通过 `script.v1` Schema，含 1 个主角、最多 2 个场景、4 个镜头和 28 秒计划 |
| AC-MOCK-03 | Text/Image/Video/TTS Mock 输出 | 与真实 Provider 使用相同 DTO；文件不是空文件或假 URL，能够被后续模块读取 |
| AC-MOCK-04 | 运行两次相同输入与 seed | Mock 业务结构、颜色/文件内容在设计允许范围内确定；每次 Job/Asset 历史仍独立可追溯 |
| AC-MOCK-05 | 检查界面和 manifest | 每个 Mock 资产标记 `MOCK`；FFmpegMotion 标记 `DETERMINISTIC_FALLBACK`，绝无“AI 视频生成成功”的误报 |
| AC-MOCK-06 | 从头运行到导出 | 产生 3—5 镜头、20—40 秒、带音视频流与可见中文字幕的 MP4 |
| AC-MOCK-07 | 对四类 Mock 调用 descriptor/capabilities/health 和生成接口 | 公共契约字段完整；health 可注入 healthy/unavailable；与真实 Adapter 使用同一 DTO |

P0 演示必须至少执行一次 AC-MOCK-01，而不能只播放预先手工制作的文件。

## 5. P0 Job 与手动重试

| ID | 注入或操作 | 通过标准 |
|---|---|---|
| AC-JOB-01 | 连续提交两个 Job，生产配置保持单 Worker | Job 依次经过 `QUEUED -> RUNNING`；同一时刻最多一个 RUNNING |
| AC-JOB-02 | 正常运行生成或导出 Job | 成功进入 SUCCEEDED；输出可读取且与 Asset 记录对应 |
| AC-JOB-03 | 注入 `INVALID_REQUEST`、超时或 `RESOURCE_EXHAUSTED` | 进入 FAILED，错误码与建议正确，不产生可选 READY 假资产；Worker 仍可处理下一个 Job |
| AC-JOB-04 | 对 FAILED Job 手动重试 | 创建新的 QUEUED Job并写 `retry_of_job_id`；旧 Job 保持 FAILED；成功后新旧记录均可查 |
| AC-JOB-05 | 检查 API、数据库和 UI 枚举 | 第一版只出现 QUEUED、RUNNING、SUCCEEDED、FAILED，不依赖取消或自动重试状态 |
| AC-JOB-06 | EXPORT_VIDEO 成功与失败各一次 | Export 与 Job 使用相同四态；失败时没有 READY final output |

### 5.1 稳定后增强验收（非 P0）

以下机制可在 M6 实现后再启用相应测试，不得作为第一段 MP4或 P0 发布门禁：

- 自动重试及 `RUNNING -> RETRY_WAIT -> QUEUED`、attempt 历史。
- 排队/运行/等待重试取消与复杂成功竞争。
- Worker 崩溃注入、租约/心跳和启动恢复。
- 客户端幂等键相同/冲突请求，以及多领取者复杂 CAS。
- STAGING 登记、原子移动各崩溃点和 `(job, attempt, output_slot)` 恢复。
- 运行中上游编辑的两种事务排序。
- Provider 单次 attempt 内多次调用/自动 fallback 历史。
- Export 的 RETRY_WAIT、取消及全套失效竞争映射。

## 6. 数据持久化与素材完整性

| ID | 验收操作 | 通过标准 |
|---|---|---|
| AC-DATA-01 | 完成到关键帧选择后重启 API 和 Worker | Project、角色、场景、Shot、选择、Job 和 Asset 全部保留 |
| AC-DATA-02 | 对 READY Asset 计算实际 SHA-256 并与数据库比较 | 完全一致；relative_path 能解析到 data 根目录内 |
| AC-DATA-03 | 注入半写文件或错误 MIME | 非法输出不进入 READY，Job 为 FAILED；STAGING 崩溃点恢复属于增强 |
| AC-DATA-04 | 提交 `../`、绝对路径、UNC/设备路径和非法扩展名 | 全部拒绝，日志不泄露任意文件内容 |
| AC-DATA-05 | 访问其他 Project 的 Asset ID | 归属校验拒绝，不能通过静态目录绕过 |
| AC-DATA-06 | 修改配置文件/环境变量中的 Provider 设置后查看旧 Job | 旧 Job 的 Provider/model/settings 快照不改变；第一版不要求 ProviderConfig 表 |
| AC-DATA-07 | 归档项目 | `archived_at` 被设置、项目从活动列表移除，workflow_status 保留归档前阶段，素材不被物理误删 |
| AC-DATA-08 | 尝试选择其他 Project、其他 Shot、错误 kind、INVALID 或 STALE Asset 作为当前有效输入 | API 稳定拒绝；旧 STALE 指针只可由系统失效传播暂留供审计，不能由选择接口新设 |
| AC-DATA-09 | 检查 SQLite 连接并制造外键违规 | 每个连接实际启用 foreign_keys；违规写入失败 |

## 7. FFmpeg 镜头与导出验收

| ID | 验收对象 | 通过标准 |
|---|---|---|
| AC-FF-01 | D1 能力预检 | 在 `anime-platform` 中复现 FFmpeg/ffprobe 8.0；记录已列 H.264/AAC、drawtext/zoompan/concat/xfade，明确 libass/subtitles 不可用；固定微软雅黑路径且不把基础 PATH 误当可用环境 |
| AC-FF-02 | 四种静帧模板 | STATIC、PUSH_IN、PULL_OUT、至少一个 PAN 均产生无明显抖动的固定规格片段 |
| AC-FF-03 | 片段规范化与 concat 路径 | 每段为 1280×720、24fps、方形像素、兼容像素格式；demuxer 路径验证 codec/流布局/time base 等完全同构；filter 路径验证对应流数量与参数一致、每段音视频 PTS 从 0 开始且缺失音轨已补齐 |
| AC-FF-04 | 固定故事最终 MP4 | ffprobe 实测 `20.0 <= duration <= 40.0`，目标约 28 秒；有至少一个视频流和一个音频流 |
| AC-FF-05 | 编码与播放 | 目标 H.264/AAC、`yuv420p`；Edge/Chrome 或 Windows 常用播放器至少一个从头播放到尾 |
| AC-FF-06 | drawtext 字幕 | 使用 UTF-8 textfile、固定 fontfile 和 cue enable 区间；四条中文字幕在画面中可见、换行/标点正确且不越界；同时存在 UTF-8 SRT/ASS 边车 |
| AC-FF-07 | manifest | 列出 4 个有序 Shot、所有 Asset ID/SHA-256/来源、字幕区间、FFmpeg/ffprobe 版本、脱敏参数和验证结果 |
| AC-FF-08 | 注入 FFmpeg 非零退出 | Export 不标 SUCCEEDED，不创建成功 output Asset；Job 保存 `FFMPEG_ERROR` 和可操作提示 |
| AC-FF-09 | 验证总时长计算 | `abs(ffprobe_duration_ms - planned_duration_ms) <= 250 ms`，且实测仍在 20—40 秒；若用 xfade，计划时间线已明确扣除重叠时长 |
| AC-FF-10 | API/Worker 在成功导出后重启 | Export、输出 Asset、字幕、manifest 仍能查询和读取，哈希不变 |
| AC-FF-11 | 分别保存 3 镜头/20 秒与 5 镜头/40 秒合法项目并导出 | 两个边界项目都通过业务门禁与 ffprobe；不存在把固定 28 秒写死的逻辑 |

如果 `libx264` 不存在，可使用预检确认的其他 H.264 编码器，但 manifest 必须记录实际值；不能悄悄改为另一格式并继续宣称通过 H.264 目标。

## 8. 真实文本模型验收（真实 Provider 目标）

前置条件：已获得依赖安装与权重下载许可；模型 ID/revision、许可证和文件哈希已记录；工程保底 P0 全绿；本机资源监控可用。

| ID | 验收操作 | 通过标准 |
|---|---|---|
| AC-TEXT-01 | 启动唯一真实文本服务并查询健康状态 | UI 显示 `LOCAL_MODEL`、准确模型 ID/量化，不显示为外部聊天产品 |
| AC-TEXT-02 | 固定故事连续运行 3 次 | 三次均在配置 timeout 内结束；均可经最多一次受控修复通过 Schema，且为 3—5 镜头、20—40 秒 |
| AC-TEXT-03 | 人工审核固定输出 | 角色/场景引用存在、镜头顺序可理解、时长合法、旁白与画面不明显矛盾；质量判断人和日期有记录 |
| AC-TEXT-04 | 检查追溯 | 保存模板 ID/version/hash、最终 prompt、非敏感参数、请求/实际 seed、模型/revision、耗时和资源峰值 |
| AC-TEXT-05 | 关闭服务、制造超时或返回非法 JSON | 项目不损坏，真实 Job 明确失败；用户可显式切换 Mock 并继续，不隐藏 fallback |
| AC-TEXT-06 | 重启文本服务 | 不需要改变核心业务表或前端 DTO 即可继续创建新 Job |

未达到 AC-TEXT-02 时，该 Provider 标记 `NOT READY`，不能在演示中声称已稳定支持；工程保底仍可通过，但真实 Provider 完成目标未通过。

## 9. 真实图像模型验收（真实 Provider 目标）

前置条件：P0 全绿；模型和依赖安装/下载获准；许可证、模型 revision、精确文件与哈希已复核；磁盘预检通过。

| ID | 验收操作 | 通过标准 |
|---|---|---|
| AC-IMG-01 | 用固定镜头生成一张关键帧 | 文件可解码，MIME、宽高、byte size 和 SHA-256 正确；来源 `LOCAL_MODEL` |
| AC-IMG-02 | 顺序生成固定故事四张关键帧 | 无 OOM、驱动重置或系统失稳；每张在配置 timeout 内；峰值显存/RAM、耗时和磁盘有记录 |
| AC-IMG-03 | 检查参数与选择 | 保存英文标签提示、负面提示、steps、CFG、尺寸、请求/实际 seed；用户可从候选选择 |
| AC-IMG-04 | 人工质量审核 | 主角核心特征、纸鹤/银杏和场景至少可识别；不要求商业级一致性，记录失败样本和限制 |
| AC-IMG-05 | 注入 OOM、超时或服务不可用 | 真实 Job 不产生 READY 假资产；切换 Mock 后所有镜头仍可导出 |
| AC-IMG-06 | 检查来源透明度 | 真实、Mock、用户 fixture 不混写；manifest 明确每个镜头最终使用哪种来源 |

如果只能成功生成一张，可以作为技术实验展示，但不满足“真实图像 Provider 可用于完整固定故事”的 AC-IMG-02，不应默认启用。若目标机最终无法通过，必须附准确环境、模型版本、参数、资源峰值、耗时与错误日志，保留 ImageProvider 接口和 Mock/自制素材替代演示，并明确真实 Provider 目标未完全达成。

## 10. 可选 TTS 验收（不属于 P0）

- 输入四条中文旁白，输出可由 ffprobe 读取的音频，采样率、声道和 duration 被记录。
- 人工确认中文内容和专名大致可辨，不出现明显截断。
- Windows 原生运行，不为它引入项目未批准的 Docker 依赖。
- 关闭 TTS 后 Mock WAV 或获授权固定旁白仍可导出。
- 若使用参考声音，Asset 记录授权来源；无授权样本不得进入演示。

## 11. 配置、安全与追溯验收

| ID | 检查 | 通过标准 |
|---|---|---|
| AC-SEC-01 | 搜索 Git 工作树和日志中的测试密钥 | 没有明文 Key；`.env` 被忽略，`.env.example` 可提交且无值 |
| AC-SEC-02 | 查询 ProviderConfig API | 只返回 `configured`、能力和非敏感设置，不返回 secret |
| AC-SEC-03 | 让 Provider 返回包含 token/绝对路径的错误 | API、UI 和结构化日志已脱敏 |
| AC-SEC-04 | 在普通 Job/故事请求中注入 base_url，并加载一个不在 allowlist 的测试启动配置 | 请求字段被拒绝；非法配置不能启用 Provider；故事文本不能触发 SSRF；P0 API 始终只读 ProviderConfig |
| AC-TRACE-01 | 从 final.mp4 反查 | P0 可追到 Export、Shot、所选 Asset、Job、executor、模型/prompt/参数和文件哈希；dependency fingerprint 与完整调用链为增强，EXPORT_VIDEO 的 Provider/model 为 null/N/A |
| AC-TRACE-02 | 检查 seed | requested_seed 与 actual_seed 分开；未知为 null/unknown，不写“保证复现” |
| AC-TRACE-03 | 检查来源或手动 fallback | 失败原因与最终 Provider/来源在 UI 和 manifest 可见；多次自动调用链为增强 |

## 12. 最终现场演示验收

| ID | 演示步骤 | 通过标准 |
|---|---|---|
| AC-DEMO-01 | 按固定故事从新建项目开始 | 展示故事、剧本审核、4 个分镜、候选选择、镜头、音频字幕和导出 |
| AC-DEMO-02 | 展示一次失败与恢复 | 观众看到 FAILED 和可操作错误，再通过手动重试或显式切换 Mock 继续 |
| AC-DEMO-04 | 播放最终视频 | 20—40 秒、4 镜头、音视频完整、中文字幕可见 |
| AC-DEMO-05 | 打开追溯清单 | 能解释任一镜头的输入、Provider、模型/Mock、prompt、seed、参数和文件哈希 |
| AC-DEMO-06 | 断开网络或禁用真实 Provider | 全 Mock 仍可完成；真实模型失败不阻断平台 |
| AC-DEMO-07 | 主动说明限制 | 清楚区分 Mock、真实 Provider 和外部视频边界；说明 Mock TTS、角色一致性和真实视频边界 |
| AC-DEMO-08 | 重复性 | 演示冻结里程碑至少两次演练从预定起点完成，记录用时和异常；第二次无阻断缺陷 |

## 13. 验收结果模板

```text
测试 ID：
结果：PASS / FAIL / NOT RUN / NOT APPLICABLE
日期与执行人：
代码版本：commit 或 working-tree/unknown
环境：
输入与配置：
实际结果：
证据路径/Asset ID/SHA-256：
未解决问题：
```

## 14. 发布门禁

完成声明分开判断：

1. 宣布“工程保底完成”：AC-CORE、AC-MOCK、本节四态 AC-JOB、基础 AC-DATA、AC-FF、AC-SEC、基础 AC-TRACE 和 AC-DEMO 的所有 P0 项通过；第 5.1 节增强测试不在门禁内。
2. 没有会导致数据损坏、密钥泄漏、错误来源标记或无法导出的已知高影响缺陷。
3. 宣布“真实 Provider 完成”：在工程保底之上，AC-TEXT 与 AC-IMG 均通过。图像硬件例外只能形成诚实的未达成说明，不能算通过。
4. README、架构、模型评估、风险和实际演示一致。
5. 已准备无网络、无 Key、无真实模型的恢复演示路径。
