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

resolution_limits:
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
| `resolution_limits` | map | 兜底判定允许改写的状态白名单与边界，见第 11 节 |
| `genre_system` | map | 类型专属扩展 |
| `authoring_notes` | map | 创作备注、生产记录 |

`resolution_limits` 虽然是可选字段，但任何允许 `custom` 意图的故事都强烈建议填写，否则引擎会把所有未命中事件卡的方案按"无状态影响"处理。

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
```

必填：

- `name`
- `role`
- `public_profile`

推荐填写：

- `secret`
- `motivation`
- `initial_state`

在场模型（presence）：

- MVP 中"角色在不在场"由**场景的 `available_characters`** 声明：它表示"玩家在这个场景里能接触到谁"，同一角色可以出现在多个场景（例如侍女既在大厅侍应，也会回到仆役走廊）。
- **不要**在 `initial_state` 里写 `location` 之类没有任何事件卡会更新的字段——死数据会误导创作者和后续的 LLM 渲染。若未来需要动态位置（NPC 被调走、被引开），应设计成由 storylet 效果显式改变的状态，并让引擎据此过滤在场角色。
- UI 的在场人物栏、`who` 面板、状态栏都以当前场景的 `available_characters` 为准。

## 6. `intents`

意图定义玩家的受控行动空间。

```yaml
intents:
  create_distraction:
    label: "制造混乱"
    description: "转移注意力、制造声响、引发争执或局部事故。"
    base_risk: medium
    quote_required: true
    typical_cost:
      time_left: -1
```

必填：

- `label`
- `description`

推荐填写：

- `base_risk`: `low`、`medium`、`high`、`variable`
- `quote_required`: 是否必须先报价再执行，见下方规则
- `typical_cost`: 常见代价，通常包含 `time_left: -1`

规则：

- UI 的意图按钮来自这里。
- 场景的 `suggested_intents` 必须引用这里已有的 intent ID。
- 玩家自由输入最终也应映射到某个 intent，`custom` 是兜底意图。

`quote_required` 语义（分层报价，避免每回合五步交互的节奏损耗）：

- `false`：低风险意图直接执行，系统在结果叙事前用一句话复述理解即可。
- `true`：必须先返回报价卡（理解、收益、风险、代价），玩家确认后才执行。
- 缺省按 `base_risk` 推导：`low` 为 `false`，其余为 `true`。
- 报价对判定有约束力：执行结果的恶化程度不得超出报价列出的风险与代价范围。
- 重新报价免费且不消耗 `time_left`，但同一回合最多 3 次，次数计入日志用于公平感分析。

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
        oil_lamp: "油灯"
      environment:
        fireplace: "壁炉"
      states:
        time_pressure: "时间紧迫"
    suggested_intents:
      - observe
      - negotiate
    exit_conditions:
      any:
        - flags:
            side_door_found: true
        - world_state:
            archive_door_unwatched: true
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
- 每个对象组可以是 ID 列表，也可以是 `ID: 显示名` 映射。**除 `people` 外（人物显示名取自 `characters.<id>.name`），一律推荐映射形式**：对象面板是玩家的合法目标词表，没有显示名的裸 ID（`oil_lamp`）会让玩家无法判断哪些是可操作的"叙事积木"。
- 引擎匹配（`object_any`）永远使用 ID；显示名只用于展示和输入别名。
- `exit_conditions` 使用第 9 节的结构化条件语法，不允许使用表达式字符串——引擎只实现一种条件求值器，trigger、exit_conditions 和 endings 共用。
- `exit_conditions.any` 是条件块列表，任一块满足即视为场景目标达成（块之间 OR，块内 AND）。
- 场景目标达成本身不自动切换场景；场景切换必须由 storylet 的 `set_world.scene` 显式表达。`exit_conditions` 是给导演层的信号：目标已达成，应调度收束或转场事件。

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

### 9.4 条件块组合

当需要"多组条件之一成立"时（目前用于 `exit_conditions`），使用 `any` 包裹条件块列表：

```yaml
exit_conditions:
  any:
    - player_state:
        has_key: true
    - flags:
        guard_pattern_known: true
      world_state:
        archive_door_unwatched: true
```

规则：

- `any` 的每个元素是一个条件块，块内所有条件组是 AND 关系。
- 块之间是 OR 关系。
- 条件块内允许的字段与 storylet trigger 的状态类字段一致（`world_state`、`player_state`、`npc_state`、`flags`、`state_gte`、`state_lte`）。

### 9.5 派生条件

`ending_reached: true` 是引擎派生条件：任一结局的 `conditions` 成立即为真。只允许出现在 `exit_conditions` 中，用于收束场景。

```yaml
exit_conditions:
  any:
    - ending_reached: true
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
| `temporary` | 临时效果块，到期自动回滚，见下 |

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

`temporary` 语义：

`effect` 顶层的 `set_flags` / `set_world` / `state_patch` 都是永久变化。会自动过期的变化必须写进 `temporary` 块，避免"哪些部分会回滚"的歧义：

```yaml
effect:
  state_patch:
    butler.suspicion: +1        # 永久：怀疑不会自己消失
  temporary:
    duration_turns: 1
    set_world:
      archive_door_unwatched: true   # 临时：机会窗口只开一回合
```

- `duration_turns: N` 表示该临时变化在触发回合之后再持续 N 个回合，第 N 个回合结束时恢复为触发前的值。
- 例：第 5 回合触发 `duration_turns: 1`，则第 6 回合内条件仍成立，第 6 回合结束时回滚。
- 引擎为每个临时效果记录原值；回滚是恢复原值，不是取反。
- 不允许把 `duration_turns` 直接写在 `effect` 顶层。

### 10.3 触发与执行语义

引擎每回合按以下顺序处理，内容创作时按此推演因果：

1. 应用玩家行动的意图代价（`typical_cost`）。
2. 应用判定产生的状态变化（storylet 命中效果，或兜底判定的受限 patch，见第 11 节）。
3. 按 `storylets` 列表顺序做一次单遍扫描：逐个检查 trigger，命中即立刻应用 effect，列表中靠后的 storylet 能看到靠前 storylet 刚产生的状态变化（单遍级联，不做多轮回扫）。
4. 处理临时效果过期。
5. 求值 `endings`，任一结局条件成立则本局结束。

两条附加规则：

- **每回合最多一次场景切换。** 本回合已有 storylet 改变了 `world.scene` 时，后续会再次改变 `world.scene` 的 storylet 一律跳过（不消耗 `once`，下回合仍可触发）。否则两张都由"潜入"触发的转场卡会级联，让玩家一个行动连穿两个场景。
- storylet 在列表中的顺序是有语义的：转场类 storylet（如"拿到证据后进入终幕"）应放在产出其前置状态的 storylet 之后。

### 10.4 `type` 词表

`type` 用于导演层理解事件卡的节奏作用，推荐从以下词表选取：

`pressure`、`reveal`、`opportunity`、`opportunity_with_cost`、`consequence`、`reward`、`progress`、`partial_progress`、`failure_pressure`、`ending_route`

validator 对词表外的 type 只给 warning，不报错。

## 11. `resolution_limits`：兜底判定白名单

这是 MVP 核心命题（"玩家的具体想法被认真接住"）的关键协议。当玩家方案没有命中任何 storylet 时，引擎走兜底判定路径：

```text
LLM 从玩家方案提议一组状态变化（Plan）
-> 引擎按 resolution_limits 白名单过滤与裁剪（Validate）
-> 只应用通过校验的部分（Apply）
-> 渲染叙事时如实反映实际生效的变化
```

```yaml
resolution_limits:
  max_paths_per_action: 3
  patchable:
    butler.suspicion: { min: 0, max: 6, max_step: 2 }
    maid.trust: { min: 0, max: 5, max_step: 1 }
    scene.noise_level: { min: 0, max: 5, max_step: 2 }
    scene.crowd_attention: { values: [low, medium, high] }
  protected:
    - world.scene
    - world.evidence_status
    - player.has_evidence
    - flags.*
```

字段说明：

| 字段 | 说明 |
|---|---|
| `max_paths_per_action` | 单次兜底判定最多允许改动的状态路径数 |
| `patchable` | 允许 LLM 提议改动的路径及边界 |
| `patchable.<path>.min` / `max` | 数值路径的取值范围，越界时裁剪到边界 |
| `patchable.<path>.max_step` | 单次改动的最大步长，超出时裁剪 |
| `patchable.<path>.values` | 枚举路径的合法值列表 |
| `protected` | 禁止兜底判定触碰的路径，支持 `flags.*` 通配整个命名空间 |

规则：

- 关键剧情事实（证据、钥匙、场景切换、结局条件涉及的状态）必须列入 `protected`，只能由 storylet 改变。这保证玩家创意可以影响局面（信任、怀疑、噪音、机会），但不能绕过作者设计的因果关卡。
- 白名单之外、未列入 `protected` 的路径默认拒绝，并记入日志——高频被拒绝的路径是内容迭代信号（说明玩家普遍想影响某个作者没想到的维度）。
- 违反 `player_role.constraints` 或 `global_rules.boundaries` 的方案不进入兜底判定，报价阶段直接返回 `can_execute: false`，并附用世界内语言表述的原因（例如"暴风雪封死了山路"，而不是"系统不允许"）。

## 12. `endings`

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
- 结局条件建议显式包含 `world.scene`（通常是收束场景），避免玩家在中途场景意外触发结局、跳过收束演出。

## 13. `genre_system`

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

## 14. Walkthrough 用例：可解性回归

每个故事必须附带 walkthrough 文件，作为内容的可解性回归用例。它回答两个问题：

1. 每个结局是否真的可达（不是假分支）。
2. 时间预算是否够（关键路径的回合数不超过 `time_left`）。

文件位置：`content/walkthroughs/<story_id>.yaml`。

```yaml
story: midnight_archive
walkthroughs:
  - id: truth_exposed_fast
    title: "钥匙路线，无暴露"
    target_ending: truth_exposed
    steps:
      - note: "观察全家画像，建立与薇拉的第一层信任"
        intent: observe
        objects: [family_portrait]
        expect_storylets: [opening_pressure, observe_family_portrait, maid_warning]
      - note: "与薇拉交涉，触发旧徽章"
        intent: negotiate
        objects: [heir]
        generic_patch: { heir.trust: 1 }
        expect_storylets: [heir_badge]
```

字段说明：

| 字段 | 说明 |
|---|---|
| `target_ending` | 本条路线预期到达的结局 ID |
| `steps[].intent` | 该回合玩家意图 |
| `steps[].objects` | 玩家方案涉及的对象，用于 `object_any` 匹配 |
| `steps[].generic_patch` | 该回合兜底判定授予的状态变化（模拟"交涉成功 +1 信任"这类引擎行为），必须符合 `resolution_limits` |
| `steps[].expect_storylets` | 该回合应触发的 storylet 及顺序 |
| `steps[].note` | 人类可读说明 |

校验器 `tools/check_walkthroughs.py` 按第 10.3 节的执行语义逐回合模拟：应用意图代价和 `generic_patch`、扫描 storylet、处理临时效果、求值结局，任何一步与 `expect_storylets` 或 `target_ending` 不符即失败。

规则：

- 每个结局至少要有一条 walkthrough 覆盖。
- 修改故事内容后必须重跑 walkthrough 校验，两者一起提交。
- walkthrough 同时是阶段 2 规则引擎的验收用例：引擎实现后跑同一批用例，结果必须一致。

## 15. 文件组织

推荐：

```text
content/
  midnight_archive.yaml
  another_story.yaml
  walkthroughs/
    midnight_archive.yaml

docs/
  content-schema.md

tools/
  validate_content.py
  check_walkthroughs.py
```

校验命令：

```bash
python3 tools/validate_content.py content/midnight_archive.yaml
python3 tools/check_walkthroughs.py content/walkthroughs/midnight_archive.yaml
```

## 16. 版本策略

当前版本：

```yaml
schema_version: 1
```

如果未来发生不兼容变更，例如：

- `storylets` trigger 语法变化。
- `state_patch` 语义变化。
- `endings` 条件语法变化。

应升级 `schema_version`，并在 validator 中同时支持旧版本或提供迁移脚本。

已发生的 v1 内部修订（引擎开工前，不另升版本）：

- `exit_conditions` 从表达式字符串改为结构化 `any` 条件块（2026-07-09）。
- 临时效果从 `effect.duration_turns` 改为 `effect.temporary` 块（2026-07-09）。
- 新增 `resolution_limits`、`intents.<id>.quote_required`（2026-07-09）。

兼容性原则：

> 新故事可以使用新字段，但旧故事不能因为引擎升级而无法运行。

## 17. 内容创作检查清单

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
10. 场景图是否连通：初始场景之外的每个场景，都有 storylet 通过 `set_world.scene` 通向它。
11. 每个 `flags` 是否至少被一个 storylet 设置（没有就是死变量或缺事件卡）。
12. `resolution_limits` 是否覆盖了 NPC 信任/怀疑等兜底判定需要的路径，关键剧情事实是否列入 `protected`。
13. 每个结局是否有 walkthrough 覆盖，且 `check_walkthroughs.py` 通过。
14. 关键路径回合数加上合理冗余是否在 `time_left` 预算内。

一句话总结：

> 故事可以自由创作，但必须用稳定的状态、意图、事件卡和结局协议与导演引擎对话。
