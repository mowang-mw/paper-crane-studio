# ScriptV1 结构化剧本契约

## 1. 权威来源与用途

`backend/app/script_schema.py` 中的 Pydantic `ScriptV1` 是当前结构化剧本的唯一权威模型。Mock 文本 Provider 与 llama-server 文本 Provider 都必须先产生合法 `ScriptV1`，再进入数据库和媒体流水线。页面、Worker、测试或后续 Provider 不应各自定义另一套相似 JSON。

代码通过 `script_v1_json_schema(desired_shot_count)` 导出 JSON Schema，并将它作为 llama-server `/v1/chat/completions` 的严格 `response_format.json_schema.schema`。自动模式保持 `shots.minItems=3`、`maxItems=5`；固定模式把二者同时设为 3、4 或 5。JSON Schema 负责约束可表达的字段、类型、数量和长度；跨实体引用、连续序号、总时长、动态旁白长度和固定镜头数仍由应用层再次校验。

所有对象统一启用：

- `extra="forbid"`：任何未声明字段都视为错误。
- `strict=True`：不把字符串数字等错误类型自动转换为目标类型。
- `str_strip_whitespace=True`：字符串校验前移除首尾空白。
- 必填文本除 `negative_prompt` 外均至少包含一个字符，不能只填空白。

字段名没有兼容别名。例如，应使用 `synopsis`，不能使用 `summary`；实体主键应使用 `id`，不能使用 `character_id`、`scene_id` 作为实体自身主键，也不能使用 `shot_id` 代替镜头自身的 `id`。

## 2. 公共 ID 规则

角色、场景、镜头及引用字段使用同一个 `EntityId` 规则：

- 长度为 1—64 个字符。
- 必须以英文字母开头。
- 后续只能包含英文字母、数字、下划线和连字符。
- 正则表达式为 `^[A-Za-z][A-Za-z0-9_-]*$`。

因此 `character_01`、`scene-night` 和 `shot_03` 合法，中文 ID、以数字开头的 ID 和含空格的 ID 不合法。ID 用于稳定引用，不承担展示文案职责。

## 3. 顶层 ScriptV1

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `schema_version` | 字符串常量 | 必须精确为 `script.v1` |
| `title` | 字符串 | 1—200 字符，剧本标题 |
| `synopsis` | 字符串 | 1—1000 字符，完整故事梗概 |
| `characters` | `Character[]` | 1—8 个角色 |
| `scenes` | `Scene[]` | 1—8 个场景 |
| `shots` | `Shot[]` | 3—5 个镜头 |

## 4. Character

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | `EntityId` | 角色稳定 ID，在同一剧本内唯一 |
| `name` | 字符串 | 1—80 字符，展示名称 |
| `role` | 字符串 | 1—200 字符，角色在故事中的作用 |
| `appearance` | 字符串 | 1—500 字符，稳定外观特征 |
| `personality` | 字符串 | 1—300 字符，性格特征 |
| `costume` | 字符串 | 1—300 字符，服装描述 |
| `consistency_prompt` | 字符串 | 1—800 字符，后续图像生成使用的角色一致性提示词 |

## 5. Scene

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | `EntityId` | 场景稳定 ID，在同一剧本内唯一 |
| `name` | 字符串 | 1—120 字符，展示名称 |
| `description` | 字符串 | 1—500 字符，空间与内容描述 |
| `time` | 字符串 | 1—120 字符，时间或时段 |
| `lighting` | 字符串 | 1—300 字符，光照和色彩信息 |
| `consistency_prompt` | 字符串 | 1—800 字符，后续图像生成使用的场景一致性提示词 |

## 6. Shot

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | `EntityId` | 镜头稳定 ID，在同一剧本内唯一 |
| `index` | 整数 | 1—5；还必须按数组顺序从 1 连续递增 |
| `title` | 字符串 | 1—120 字符，镜头标题 |
| `scene_id` | `EntityId` | 必须引用 `scenes[].id` 中存在的场景 |
| `character_ids` | `EntityId[]` | 1—8 项；同一镜头内不得重复，且每项必须引用已声明角色 |
| `visual_description` | 字符串 | 1—800 字符，画面和动作描述 |
| `camera` | 字符串 | 1—120 字符，简洁、可执行的运镜描述 |
| `image_prompt` | 字符串 | 1—1200 字符，关键帧图像提示词 |
| `negative_prompt` | 字符串或 `null` | 可省略；字符串最长 800 字符，允许空字符串 |
| `narration` | 字符串 | 1—200 字符，并受镜头时长相关规则限制 |
| `duration_seconds` | 浮点数 | 4.0—10.0 秒 |

## 7. 跨字段和时间轴规则

结构满足字段类型后，还必须同时满足以下运行时规则：

1. `characters[].id`、`scenes[].id`、`shots[].id` 在各自集合内唯一。
2. `shots[].index` 必须与数组顺序一致，并精确等于 `[1, 2, ..., N]`；缺号、重复、乱序都无效。
3. 每个 `Shot.scene_id` 必须引用一个已声明场景。
4. 每个 `Shot.character_ids` 中的 ID 必须引用已声明角色，且同一镜头内不能重复。
5. 已声明但未被镜头使用的角色或场景允许保留，不影响 ScriptV1 合法性；系统通过 `unused_character_ids` 和 `unused_scene_ids` 给出非阻断警告。
6. 所有镜头 `duration_seconds` 之和必须在 20.0—40.0 秒之间，端点包含在内。
7. 中文旁白按每秒最多 5 个非空白字符计算。实现会移除所有空白字符后计数，标点仍计入字符数；允许的最大字符数为 `int(duration_seconds * 5)`。例如，7.0 秒镜头最多 35 个非空白字符。

旁白规则是一个可解释且偏保守的工程上限，用于避免字幕和 Mock/后续 TTS 时间轴明显超载；它不是语音学质量评估，也不能替代真人试听。

### 7.1 生成参数与故事输入边界

`desired_shot_count` 是 GenerationJob 请求参数，不是 ScriptV1 字段：

- `null` 表示自动规划，3、4、5 个镜头均合法。
- `3`、`4`、`5` 表示最终必须恰好为对应数量。
- 参数、`story_char_count` 和 Provider 选择均冻结在 `GenerationJob.request_json`；手动重试复制原任务快照。
- 固定模式数量不符属于可修复错误，系统不得静默复制、删除、合并或重排模型镜头。

项目故事去除首尾空白后硬限制为 10—3000 个 Unicode 字符，界面建议约 50—1000 个中文字符。10—3000 字符的输入不得仅因长度被判失败；模型输出的 JSON、Schema、引用、镜头数或时长错误必须另行诊断。

### 7.2 纯时长问题的确定性规范化

严格持久化契约仍要求单镜头 4—10 秒、总时长 20—40 秒。Provider 首次不合法时仍只发起一次模型修复；只有修复输出的字段、类型、未知字段、索引、引用和镜头数全部合法，且剩余错误仅与时长分配有关时，才允许调用 `normalize_script_durations()`：

- 不增删、复制、合并或重排镜头。
- 按原始时长比例缩放，并以 0.1 秒确定性取整。
- 3/4/5 镜头默认目标总时长分别为 24/28/35 秒。
- 同时满足旁白相对时长上限；无法在每镜头 4—10 秒内容纳时仍失败。
- 返回并追溯原始/最终逐镜头时长、原始/最终总时长和原因。

结构、引用或固定镜头数错误绝不使用时长规范化掩盖。

### 7.3 计划时长与编码时长

ScriptV1 的 `planned_duration_seconds` 是所有 `shot.duration_seconds` 之和，业务边界仍严格为 20—40 秒，端点包含在内；计划时长 40.1 秒不会因媒体容差而合法。最终 MP4 的 `encoded_duration_seconds` 来自 ffprobe，允许视频帧、AAC 采样帧和封装舍入带来的极小偏差：

```text
duration_tolerance_seconds =
    max(1 / video_fps, audio_frame_samples / audio_sample_rate)
    + small_epsilon
```

当前 24 fps、AAC 1024 samples、48 kHz、`small_epsilon=0.010` 时，容差为约 0.051667 秒。验收结果记录 `planned_duration_seconds`、`encoded_duration_seconds`、`duration_delta_seconds`、`duration_tolerance_seconds` 和 `duration_validation`；后者只允许 `passed_exactly` 或 `passed_with_media_tolerance`。这不会把 40 秒业务上限改成 41 秒。

## 8. 持久化职责

### 8.1 `Project.script_json`

文本 Provider 成功后，`GenerationService` 使用 `ScriptV1.model_dump(mode="json")` 得到完整快照，并在短事务中写入 `Project.script_json`。该字段：

- 保存完整且已校验的 `ScriptV1`，包括角色、场景和镜头。
- 是后续展示和再生成所依赖的剧本权威记录。
- 不混入 Provider、模型文件、调用耗时、HTTP 响应或 FFmpeg 元数据。
- 在生成成功前可以为 `null`；非法模型输出不得写入。

Provider 追溯信息单独保存在 `GenerationJob.result_json.script_trace`，并进入导出 manifest。当前追溯包括 Provider、模型、参数、提示词版本、Schema 摘要、请求耗时、原始响应路径、校验结果和未使用实体警告。未使用角色或场景不会被静默裁剪，原始数组仍完整保存在 `Project.script_json`。这样既保持剧本 JSON 稳定，也保留真实模型调用证据。

### 8.2 `Shot` 表

`Shot` 表是供查询、任务处理和媒体合成使用的派生投影，不是第二套剧本 Schema。每次成功持久化剧本时，服务从 `ScriptV1.shots` 重建项目的 Shot 记录：

- 一等列保存 `shot_index`、`title`、`visual_description`、`narration`、`duration_seconds` 和 `provider_id`。
- `parameters_json` 保存 `provider_shot_id`、`scene_id`、`character_ids`、`camera`、`image_prompt`、`negative_prompt`，以及视觉/音频 Mock Provider 和媒体参数。
- 数据库 `Shot.id` 是本地记录 ID；结构化剧本中的 `Shot.id` 以 `parameters_json.provider_shot_id` 保存，二者不能混为一谈。
- 角色和场景当前不拆为独立业务表，其完整定义保留在 `Project.script_json`。

若投影与 `Project.script_json` 出现矛盾，应以通过 `ScriptV1` 校验的 `Project.script_json` 为剧本语义依据，并把投影视为需要重新生成或修复的派生数据。

## 9. 严格解析、一次修复和失败行为

llama-server Provider 只读取 OpenAI-compatible 响应中的 `choices[0].message.content` 字符串，并按以下顺序处理：

1. 只允许 JSON 外围存在空白；空响应无效。
2. 出现 Markdown 代码围栏、`<think>` 或 `</think>` 时直接判定无效。
3. 使用严格 JSON 解析；解释前缀或后缀、重复键、`NaN`/`Infinity`、非对象顶层都无效。
4. 使用 `analyze_script_candidate()` 分离 Schema、引用、镜头数和时长诊断；未使用场景/角色仍只是警告。
5. 严格校验通过后才形成 `ScriptV1`；固定模式还必须恰好满足 `desired_shot_count`。
6. `finish_reason` 只有 `stop` 或 `null` 可继续校验，其他值视为不完整输出。

首次模型正文不合法时，Provider 最多发送一次带结构化错误的修复请求。首次与修复请求都把 `desired_shot_count` 作为独立、高优先级结构化参数；固定数量不符时，修复消息明确写出“要求 N、当前 M”。修复输出仍执行完全相同的纯 JSON 与候选校验。第二次仍失败时，只有满足第 7.2 节的纯时长问题可以被透明规范化；其他情况：

- 抛出 `LlamaCppOutputError`，不静默改用 Mock Provider。
- 不把非法剧本写入 `Project.script_json` 或 Shot 表。
- Worker 将任务标记为 `FAILED`，把中文 `generation_error` 写入 `result_json`，用户可显式手动重试。

首次生成与唯一修复 Prompt 都要求镜头覆盖故事开端、主要发展和明确结局，最后一镜表现原故事最终事件或结局状态；节点多于镜头时应合并相邻节点而不是删除末尾。自动模式仍接受 3—5 镜头。当前只做模型提示增强，尚未实现独立语义覆盖评分，因此不得把它描述为严格语义完整性校验。

若失败阶段明确为 `MEDIA_RENDER`，手动重试会创建一个新的恢复 Job，从来源 Job 受控追溯目录中的严格 ScriptV1 继续；已有 MP4 完整解码和新容差验收通过时直接复用字节，不重新调用 ScriptProvider，也不重新编码。追溯缺失或损坏时恢复 Job 明确失败，禁止静默重生成剧本。
- 错误阶段区分 `INPUT_VALIDATION`、`PROVIDER_UNAVAILABLE`、`MODEL_REQUEST`、`MODEL_JSON_PARSE`、`SCRIPT_SCHEMA_VALIDATION`、`SCRIPT_REFERENCE_VALIDATION`、`SHOT_COUNT_VALIDATION`、`DURATION_VALIDATION`、`REPAIR_FAILED` 和 `MEDIA_RENDER`。
- 受控 Job 目录保存 `first_raw_response.json|txt`、可选 `repair_raw_response.json|txt`、`validation_report.json`、请求快照和 `trace.json`。
- 数据库只保存响应路径、哈希、摘要、错误数组和规范化信息，不内嵌完整模型原始响应。

连接失败、超时、非 2xx HTTP 状态和 OpenAI-compatible 响应信封缺失属于传输或协议错误，不消耗“输出修复”机会，也不会自动回退 Mock。

## 10. 合法精简示例

下面示例含 3 个镜头，每个 7.0 秒，总时长 21.0 秒；所有角色和场景均被引用。

```json
{
  "schema_version": "script.v1",
  "title": "夜灯归途",
  "synopsis": "停电后，小澄点亮纸灯；纸灯引导她穿过屋顶，在黎明找到回家的方向。",
  "characters": [
    {
      "id": "character_01",
      "name": "小澄",
      "role": "跟随纸灯寻找归途的主角",
      "appearance": "原创少女，深色短发，神情温和。",
      "personality": "安静、好奇、坚定",
      "costume": "浅色外套与深色长裤",
      "consistency_prompt": "同一原创少女，深色短发，浅色外套，二维动漫造型一致。"
    }
  ],
  "scenes": [
    {
      "id": "scene_01",
      "name": "停电房间",
      "description": "窗边桌面摆着一盏未点亮的纸灯。",
      "time": "雨夜",
      "lighting": "冷色窗光与微弱烛光",
      "consistency_prompt": "原创雨夜房间，木桌靠窗，冷暖光对比，16:9。"
    },
    {
      "id": "scene_02",
      "name": "城市屋顶",
      "description": "纸灯的微光越过安静屋顶。",
      "time": "深夜",
      "lighting": "深蓝夜色与暖黄灯光",
      "consistency_prompt": "原创城市屋顶，深蓝夜空，暖黄点状灯火，16:9。"
    },
    {
      "id": "scene_03",
      "name": "黎明坡道",
      "description": "坡道尽头出现晨光与家的轮廓。",
      "time": "黎明",
      "lighting": "柔和金色晨光",
      "consistency_prompt": "原创黎明坡道，金色地平线，清晰远景，16:9。"
    }
  ],
  "shots": [
    {
      "id": "shot_01",
      "index": 1,
      "title": "点亮纸灯",
      "scene_id": "scene_01",
      "character_ids": ["character_01"],
      "visual_description": "小澄在窗边点亮纸灯，暖光映在脸上。",
      "camera": "从房间远景缓慢推近桌面",
      "image_prompt": "原创二维动漫，小澄在雨夜窗边点亮纸灯，冷暖光对比，角色造型一致。",
      "negative_prompt": "文字，水印，品牌标志，角色畸变",
      "narration": "停电后，小澄点亮桌边的纸灯。",
      "duration_seconds": 7.0
    },
    {
      "id": "shot_02",
      "index": 2,
      "title": "越过屋顶",
      "scene_id": "scene_02",
      "character_ids": ["character_01"],
      "visual_description": "小澄追随纸灯微光穿过安静的城市屋顶。",
      "camera": "低机位向右平移",
      "image_prompt": "原创二维动漫，小澄追随微光经过深蓝屋顶，暖黄灯火，造型一致。",
      "negative_prompt": "文字，水印，品牌标志，角色畸变",
      "narration": "纸灯化作微光，穿过安静的屋顶。",
      "duration_seconds": 7.0
    },
    {
      "id": "shot_03",
      "index": 3,
      "title": "看见归途",
      "scene_id": "scene_03",
      "character_ids": ["character_01"],
      "visual_description": "晨光照亮坡道，小澄望见远处家的轮廓。",
      "camera": "缓慢拉远至黎明全景",
      "image_prompt": "原创二维动漫，小澄站在黎明坡道，金色地平线与远方房屋，造型一致。",
      "negative_prompt": null,
      "narration": "黎明升起，她终于看见回家的路。",
      "duration_seconds": 7.0
    }
  ]
}
```
