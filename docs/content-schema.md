# AIRPG 内容格式协议 v1

日期：2026-07-08

本文档定义 AIRPG 故事 YAML 的通用格式。后续新故事应优先遵守这份协议，而不是为每个故事修改引擎。

## 1. 设计目标

内容格式的目标是把“故事内容”和“导演引擎”分开：

```text
故事 YAML 负责描述世界、角色、场景、意图、事件卡和结局。
核心引擎负责读取 schema、执行判定、更新状态、触发事件卡和渲染反馈。
```

原则：

1. 所有故事共享同一个通用 schema。
2. 不同类型可以通过 `genre` 和 `genre_system` 扩展。
3. 引擎只依赖通用字段，不依赖某个具体故事的字段名。
4. 状态变化必须通过结构化路径表达，避免把事实藏在叙事文本里。
5. LLM 可以帮助理解和渲染，但不能替代状态协议。

## 2. 顶层结构

推荐顶层结构：

```yaml
id: midnight_archive
title: "午夜前的档案室"
version: 0.1.0
schema_version: 1
language: zh-CN
genre: mystery_infiltration
target_duration_minutes: 20-30

design_goal: |
  ...

premise: |
  ...

player_role:
  ...

style_bible:
  ...

global_rules:
  ...

initial_state:
  ...

characters:
  ...

intents:
  ...

scenes:
  ...

storylets:
  ...

endings:
  ...

genre_system:
  ...

authoring_notes:
  ...
```

### 2.1 必填字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 故事唯一 ID，建议 snake_case |
| `title` | string | 故事标题 |
| `version` | string | 内容版本 |
| `schema_version` | number | 内容协议版本，当前为 `1` |
| `language` | string | 内容语言，例如 `zh-CN` |
| `genre` | string | 类型标识，例如 `mystery_infiltration` |
| `premise` | string | 故事前提 |
| `player_role` | map | 玩家身份、目标与限制 |
| `style_bible` | map | 文风和表现约束 |
| `global_rules` | map | 全局规则和边界 |
| `initial_state` | map | 初始世界状态 |
| `characters` | map | 角色定义 |
| `intents` | map | 玩家意图空间 |
| `scenes` | map | 场景定义 |
| `storylets` | list | 事件卡 |
| `endings` | map | 结局条件 |

### 2.2 可选字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `design_goal` | string | 本故事用于验证什么体验 |
| `target_duration_minutes` | string / number | 目标体验时长 |
| `genre_system` | map | 类型专属扩展 |
| `authoring_notes` | map | 创作备注、生产记录 |

## 3. `player_role`

`player_role` 描述玩家在故事中的公共身份、隐藏目标和行动边界。

```yaml
player_role:
  id: player
  name: "见证人"
  public_identity: "受邀到场的遗产见证人"
  private_goal: "午夜前进入档案室，找到证据"
  constraints:
    - "不能凭空制造不存在的科技、武器或超自然能力。"
```

规则：

- `id` 必须能在 `characters` 中找到。
- `private_goal` 会用于导演目标和 UI 当前目标。
- `constraints` 会用于判断越界输入。

## 4. `initial_state`

`initial_state` 是运行时状态的初始值。第一版约定四个核心命名空间：

```yaml
initial_state:
  world:
    scene: great_hall
    time_left: 6
  player:
    has_key: false
  scene:
    fire_risk: 0
  flags:
    opening_delivered: false
```

命名空间：

| 命名空间 | 用途 |
|---|---|
| `world` | 全局剧情状态，例如章节、当前场景、证据状态 |
| `player` | 玩家状态，例如伤势、物品、暴露程度 |
| `scene` | 当前场景状态，例如火势、噪音、人群注意力 |
| `flags` | 布尔或轻量剧情标记 |

NPC 初始状态写在 `characters.<id>.initial_state` 中，不放在 `initial_state` 里。

## 5. `characters`

角色以 map 形式定义，key 是角色 ID。

```yaml
characters:
  maid:
    name: "艾拉侍女"
    role: reluctant_helper
    public_profile: |
      年轻侍女，害怕管家，却试图保护真相。
    secret: "她看见管家进入档案室。"
    motivation: "想提醒玩家，但不能让管家发现。"
    initial_state:
      suspicion: 1
      trust: 2
      location: great_hall
```

必填：

- `name`
- `role`
- `public_profile`

推荐填写：

- `secret`
- `motivation`
- `initial_state`

## 6. `intents`

意图定义玩家的受控行动空间。

```yaml
intents:
  create_distraction:
    label: "制造混乱"
    description: "转移注意力、制造声响、引发争执或局部事故。"
    base_risk: medium
    typical_cost:
      time_left: -1
```

必填：

- `label`
- `description`

推荐填写：

- `base_risk`: `low`、`medium`、`high`、`variable`
- `typical_cost`: 常见代价，通常包含 `time_left: -1`

规则：

- UI 的意图按钮来自这里。
- 场景的 `suggested_intents` 必须引用这里已有的 intent ID。
- 玩家自由输入最终也应映射到某个 intent，`custom` 是兜底意图。

## 7. `scenes`

场景定义当前目标、可操作对象和建议意图。

```yaml
scenes:
  great_hall:
    name: "大理石大厅"
    purpose: "建立目标、展示人物关系。"
    goal: "找到进入二楼档案室的第一条机会。"
    entry_text: |
      暴风雪把窗玻璃拍得发白。
    available_characters:
      - butler
      - maid
    available_objects:
      people:
        - butler
        - maid
      items:
        - oil_lamp
      environment:
        - fireplace
      states:
        - time_pressure
    suggested_intents:
      - observe
      - negotiate
    exit_conditions:
      - "side_door_found == true"
```

必填：

- `name`
- `purpose`
- `goal`
- `entry_text`
- `available_characters`
- `available_objects`
- `suggested_intents`

规则：

- `available_characters` 必须引用 `characters` 中已有 ID。
- `suggested_intents` 必须引用 `intents` 中已有 ID。
- `available_objects` 可包含 `people`、`items`、`environment`、`states`。
- `exit_conditions` 第一版可作为作者备注，后续可接入正式条件解析。

## 8. 状态路径

事件卡和结局条件通过状态路径读写运行时状态。

推荐路径：

```text
world.time_left
world.scene
player.has_key
scene.fire_risk
flags.maid_warned_player
characters.maid.trust
```

为了降低创作成本，storylet 内允许 NPC 状态简写：

```text
maid.trust
butler.suspicion
guard.alertness
```

简写规则：

```text
<character_id>.<state_key>
```

等价于：

```text
characters.<character_id>.<state_key>
```

注意：

- 通用 schema 推荐使用完整路径。
- 当前 MVP 内容允许简写，validator 会识别角色 ID。
- 后续如果进入创作者工具阶段，应在保存时自动规范化为完整路径。

## 9. 条件语法

条件默认是 AND 关系。

### 9.1 精确匹配

```yaml
world_state:
  evidence_status: intact

player_state:
  has_key: true

flags:
  maid_warned_player: true
```

也可以使用完整路径：

```yaml
conditions:
  player.has_evidence: true
  world.evidence_status: secured
```

### 9.2 比较条件

支持后缀：

| 后缀 | 含义 |
|---|---|
| `_gte` | 大于等于 |
| `_lte` | 小于等于 |
| `_gt` | 大于 |
| `_lt` | 小于 |
| `_ne` | 不等于 |

示例：

```yaml
npc_state:
  maid.trust_gte: 2

state_gte:
  scene.fire_risk: 2

conditions:
  heir.trust_gte: 2
```

### 9.3 OR 条件

字段名带 `_any` 表示列表内 OR。

```yaml
scene_any:
  - great_hall
  - servant_corridor

intent_any:
  - negotiate
  - observe

object_any:
  - oil_lamp
  - curtains
```

## 10. `storylets`

Storylet 是导演引擎调度的事件卡。

```yaml
storylets:
  - id: maid_warning
    title: "侍女的低声提醒"
    type: reveal
    once: true
    trigger:
      scene_any:
        - great_hall
        - servant_corridor
      npc_state:
        maid.trust_gte: 2
      world_state:
        evidence_status: intact
    effect:
      set_flags:
        maid_warned_player: true
      state_patch:
        player.has_maid_note: true
      add_clues:
        - "艾拉看见管家进入档案室。"
    narrative_hint: "侍女避开管家的视线，把便签塞给玩家。"
```

必填：

- `id`
- `title`
- `type`
- `once`
- `trigger`
- `effect`
- `narrative_hint`

### 10.1 `trigger`

推荐字段：

| 字段 | 说明 |
|---|---|
| `scene` | 当前场景必须等于某 scene ID |
| `scene_any` | 当前场景在列表中任一匹配 |
| `intent` | 当前玩家意图必须等于某 intent ID |
| `intent_any` | 当前玩家意图在列表中任一匹配 |
| `object_any` | 玩家方案或目标对象命中列表中任一对象 |
| `world_state` | 匹配 `world` 命名空间状态 |
| `player_state` | 匹配 `player` 命名空间状态 |
| `npc_state` | 匹配角色状态，允许角色简写 |
| `flags` | 匹配 `flags` |
| `state_gte` | 状态路径大于等于某值 |
| `state_lte` | 状态路径小于等于某值 |

### 10.2 `effect`

推荐字段：

| 字段 | 说明 |
|---|---|
| `set_flags` | 设置 `flags` |
| `set_world` | 设置 `world` 命名空间状态 |
| `state_patch` | 通用状态变化 |
| `add_clues` | 添加玩家已知线索 |
| `duration_turns` | 临时效果持续回合数 |

`state_patch` 语义：

- 数字表示对已有数值做增量，例如 `butler.suspicion: +1`。
- 布尔、字符串、列表、map 表示直接赋值。
- 如果路径不存在，数字 patch 第一版可视为从 `0` 开始增量。

示例：

```yaml
state_patch:
  scene.fire_risk: +1
  player.has_key: true
  world.evidence_status: secured
```

## 11. `endings`

结局以 map 形式定义，key 是结局 ID。

```yaml
endings:
  truth_exposed:
    title: "真相曝光"
    priority: 1
    conditions:
      player.has_evidence: true
      player.exposed: false
      heir.trust_gte: 2
    outcome: |
      薇拉公开承认证据与父亲留下的旧物一致。
```

必填：

- `title`
- `priority`
- `conditions`
- `outcome`

规则：

- `priority` 数字越小，优先级越高。
- 多个结局同时满足时，取 `priority` 最小者。
- `conditions` 使用第 9 节条件语法。

## 12. `genre_system`

`genre_system` 是类型扩展，不应破坏核心 schema。

示例：悬疑类型

```yaml
genre: mystery
genre_system:
  suspects:
    - butler
    - heir
  clues:
    - id: wax_seal
      truth_weight: high
  red_herrings:
    - drunk_guest_argument
```

示例：恐怖生存

```yaml
genre: horror_survival
genre_system:
  resources:
    sanity: 5
    matches: 3
  safe_rooms:
    - chapel
  threat_clock:
    max: 6
```

规则：

- 核心引擎可以忽略未知 `genre_system` 字段。
- 类型模块可以读取自己的扩展字段。
- 不允许把核心状态变化只写在 `genre_system` 中，核心因果仍要通过 `initial_state`、`storylets` 和 `endings` 表达。

## 13. 文件组织

推荐：

```text
content/
  midnight_archive.yaml
  another_story.yaml

docs/
  content-schema.md

tools/
  validate_content.py
```

校验命令：

```bash
python3 tools/validate_content.py content/midnight_archive.yaml
```

## 14. 版本策略

当前版本：

```yaml
schema_version: 1
```

如果未来发生不兼容变更，例如：

- `storylets` trigger 语法变化。
- `state_patch` 语义变化。
- `endings` 条件语法变化。

应升级 `schema_version`，并在 validator 中同时支持旧版本或提供迁移脚本。

兼容性原则：

> 新故事可以使用新字段，但旧故事不能因为引擎升级而无法运行。

## 15. 内容创作检查清单

新增故事前检查：

1. 是否有唯一 `id`。
2. 是否填写 `schema_version: 1`。
3. `player_role.id` 是否存在于 `characters`。
4. 每个场景的角色是否都存在。
5. 每个场景的意图是否都存在。
6. 每个 storylet 的 `id` 是否唯一。
7. 每个 storylet 是否有 `trigger`、`effect` 和 `narrative_hint`。
8. 每个结局是否有 `priority` 和 `conditions`。
9. 重要剧情事实是否写入状态，而不是只写在文本里。
10. 是否能在不接 LLM 的情况下人工跑通主线。

一句话总结：

> 故事可以自由创作，但必须用稳定的状态、意图、事件卡和结局协议与导演引擎对话。
