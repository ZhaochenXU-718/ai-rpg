# AIRPG 内容格式协议 v2

日期：2026-07-10

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
version: 0.2.0
schema_version: 2
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

world_board:
  ...

world_rules:
  ...

items:
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
| `schema_version` | number | 内容协议版本，当前为 `2`；引擎仍兼容读取 v1 |
| `language` | string | 内容语言，例如 `zh-CN` |
| `genre` | string | 类型标识，例如 `mystery_infiltration` |
| `premise` | string | 故事前提 |
| `player_role` | map | 玩家身份、目标与限制 |
| `style_bible` | map | 文风和表现约束 |
| `global_rules` | map | 全局规则和边界 |
| `world_board` | map | v2 抽象空间图，定义节点与连接 |
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
| `perception` | map | 玩家可感知的公开状态键与展示标签（感知墙），见 6.1 |
| `quote_warnings` | list | 报价卡上的世界内风险提示（条件 + 文案），见 6.1 |
| `world_rules` | list | 每个 world step 执行的通用实体移动规则，缺省为空 |
| `items` | map | 可携带物品定义；使用时必须同时提供 `initial_state.item_locations` |
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

`initial_state` 是运行时状态的初始值。v2 约定六个核心命名空间：

```yaml
initial_state:
  world:
    step: 0
    time_left: 6
  positions:
    player: great_hall
    butler: great_hall
    guard: archive_door
  item_locations:
    servant_key: { type: carried_by, id: maid }
    old_badge: { type: carried_by, id: heir }
  player:
    injury: 0
  scene:
    fire_risk: 0
  flags:
    opening_delivered: false
```

命名空间：

| 命名空间 | 用途 |
|---|---|
| `world` | 全局剧情状态，例如章节、world step、证据状态 |
| `positions` | 所有角色当前所在的世界图节点，是物理位置的唯一事实来源 |
| `item_locations` | 所有可携带物品的位置，是场景物品、容器和背包所有权的唯一事实来源 |
| `player` | 玩家状态，例如伤势、物品、暴露程度 |
| `scene` | 当前场景状态，例如火势、噪音、人群注意力 |
| `flags` | 布尔或轻量剧情标记 |

NPC 初始状态写在 `characters.<id>.initial_state` 中，不放在 `initial_state` 里。

### 4.1 `world_board`：抽象世界图

世界棋盘是有向图，不要求是二维方格。节点可以表示房间、走廊、区域或叙事遭遇；边表示实体可以在一个 world step 内通过的连接。

```yaml
world_board:
  nodes:
    great_hall: { name: "大理石大厅", kind: room }
    servant_corridor: { name: "仆役走廊", kind: corridor }
    archive_door: { name: "档案室门口", kind: area }
  edges:
    - { from: great_hall, to: servant_corridor, bidirectional: true }
    - { from: servant_corridor, to: archive_door, bidirectional: true }
```

规则：

- `scenes` 中每个玩家可进入的场景必须有同 ID 的棋盘节点；棋盘可以额外包含只供 NPC 使用的节点。
- 每个 `characters` 实体必须在 `initial_state.positions` 中恰有一个节点。
- v2 禁止同时填写 `initial_state.world.scene`；玩家位置读取 `positions.<player_role.id>`。
- 位置不能通过 `state_patch` 或 LLM 兜底判定修改，只能通过 `move_entities` 或 world step 移动系统改变。

### 4.2 `world_rules` 与 world step

```yaml
world_rules:
  - id: butler_pursues_intruder
    actor: butler
    priority: 100
    when:
      scene_any: [archive_door, archive_room]
      state_gte:
        butler.suspicion: 5
    move:
      toward: player
```

- `move.to` 移动到相邻的固定节点。
- `move.toward` 沿世界图最短路径向目标角色移动一格。
- 同一角色同一 step 最多采用一个命中规则，`priority` 高者优先；同优先级按内容顺序优先。
- 所有角色的规则都读取 step 开始时的同一状态快照，再原子应用移动，禁止因规则执行顺序产生二次移动。
- 无规则命中时角色保持原位。报价、帮助、查看对象和放弃行动不推进 world step；确认执行的行动推进一次。

### 4.3 `items`、物品位置与背包

```yaml
items:
  servant_key:
    name: "仆役侧门钥匙"
    description: "可以打开仆役窄门。"
    portable: true
    consumable: false

initial_state:
  item_locations:
    servant_key: { type: carried_by, id: maid }
```

每个物品同一时刻必须且只能有一个位置：

| `type` | `id` 含义 |
|---|---|
| `board` | 世界图节点，物品显示为当前地点的可拾取物品 |
| `carried_by` | 角色 ID；`id` 等于玩家 ID 时显示在口袋中 |
| `container` | 场景对象 ID，例如 `hidden_compartment`；默认不直接显示 |
| `removed` | 已消耗或离开世界，不填写 `id` |

物品所有权只能通过受信任的 `move_items` 效果改变。v2 禁止同时用 `player.has_key` 之类布尔值复制同一个所有权事实，也禁止在 `available_objects` 中重复声明受跟踪物品。

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

- 物理在场由 `positions.<character_id> == positions.<player_role.id>` 唯一推导。
- UI 的人物面板、`who`、状态栏和人物目标词都读取这个派生结果，场景内容不能再声明静态人物名单。
- 不在场人物不能成为行动目标；请求会在扣除时间和推进 world step 之前被拒绝。
- “相邻、逼近、隐藏、可听见”等感知关系应在位置之上继续派生，不能通过把同一角色重复塞进多个场景来模拟。

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
  use:
    label: "使用"
    description: "使用口袋物品作用于当前目标。"
    base_risk: variable
    quote_required: true
    requires_storylet_match: true
    min_objects: 2
    typical_cost:
      time_left: -1
  move:
    label: "移动"
    description: "沿当前可用出口移动。"
    base_risk: low
    quote_required: false
    engine_action: move
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
- `min_objects` / `max_objects`: 行动需要的目标数量边界
- `requires_storylet_match`: `true` 时，本次行动必须能命中一张显式声明该意图的 action storylet；否则在报价、扣时和 world step 前拒绝。**该严格模式只约束结构化（无 LLM）路径**：LLM 计划路径上，目标合法但未预写的尝试按 fail-forward 执行（扣时、软代价过白名单、无 canon 效果、报价明示"没有把握"），防止零成本探测作者预写面
- `engine_action`: 通用内建动作；当前仅支持 `move`
- `fallback_proposal`: 无 LLM 模式下，行动未命中事件卡时的默认软状态提议。`npc_state: {key, step}` 作用于目标中的第一个人物；或直接给 `state_patch`。提议仍要过 `resolution_limits` 裁剪。阶段 3 由 LLM 提议替代
- `fallback_narrative`: 未命中事件卡但有状态变化时的反馈文案；不填时引擎用无类型色彩的通用句

规则：

- UI 的意图按钮来自这里。
- 场景的 `suggested_intents` 必须引用这里已有的 intent ID。
- 玩家自由输入最终也应映射到某个 intent，`custom` 是兜底意图。

`quote_required` 语义（分层报价，避免每回合五步交互的节奏损耗）：

- `false`：低风险意图直接执行，系统在结果叙事前用一句话复述理解即可。
- `true`：必须先返回报价卡（理解、收益、风险、代价），玩家确认后才执行。
- 缺省按 `base_risk` 推导：`low` 为 `false`，其余为 `true`。
- 报价对判定有约束力：执行结果的恶化程度不得超出报价列出的风险与代价范围。
- 规则引擎会在私有状态副本上预演确定性结果；预演不得修改真实状态。
- **报价披露受感知墙约束（见 6.1）**：预演的全量结果只用于约束力校验和日志；报价卡上只允许出现玩家已可感知的公开状态变化。揭示、获得物品、位置变化和结局一律不上卡——报价是风险估计，不是预言机。
- 重新报价免费且不消耗 `time_left`，但同一回合最多 3 次，次数计入日志用于公平感分析。

### 6.1 `perception`：感知墙

`perception` 声明玩家（以及替玩家理解输入的 LLM）可以看到哪些状态键，及其展示标签。状态栏、报价卡"预计影响"、`PlayerPerception` 快照都以它为唯一依据；未声明的状态（flags、他人位置、结局条件、未发现的物品）在结构上不可见。

```yaml
perception:
  facts_label: "线索"       # 玩家知识日志的故事内称呼，缺省为"发现"
  world_state:
    time_left: "剩余时间"
  scene_state:
    fire_risk: "火势风险"
  character_state:        # 只对与玩家同一位置的角色生效
    suspicion: "疑"
    trust: "信"
```

规则：

- 引擎只认识 `world_state` / `scene_state` / `character_state` 三个组，键名和标签全部由故事声明——引擎代码不包含任何故事词汇（如 trust、fire_risk）。
- `facts_label` 是 `add_facts` 累积的玩家知识日志的展示称呼：悬疑故事叫"线索"，情感剧可以叫"回忆"。引擎概念只有 facts，词汇属于故事。
- `character_state` 只披露**在场**角色的对应键；角色离开玩家所在节点后即不可见。
- schema v2 故事缺少 `perception` 时校验器给警告：状态栏和报价卡将没有任何数值可显示。

`quote_warnings` 是报价卡上的世界内风险提示，`when` 使用第 9 节条件语法：

```yaml
quote_warnings:
  - when:
      state_gte: { butler.suspicion: 4 }
    text: "洛维尔管家已经高度怀疑你。"
```

这是确定性引擎下"风险感"的合法来源：告诉玩家局势的紧张程度（他已经在怀疑你），而不是预告行动的精确后果。

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
    exits:
      - to: servant_corridor
        label: "前往仆役走廊"
        narrative_hint: "你沿侧梯进入仆役走廊。"
    available_objects:
      items:
        oil_lamp: "油灯"
        hidden_compartment:
          label: "暗格"
          visible_when:
            flags:
              compartment_found: true
          actionable_when:
            flags:
              compartment_unlocked: true
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
- `available_objects`
- `suggested_intents`

规则：

- `suggested_intents` 必须引用 `intents` 中已有 ID。
- `exits` 是玩家可主动选择的相邻移动出口；`to` 必须有世界图边和场景定义，可选 `when` 使用统一条件语法。
- `engine_action: move` 只允许移动到当前场景经过条件过滤后仍可用的出口；非法出口在扣时和 world step 前拒绝。
- v2 的 `available_objects` 可包含 `items`、`environment`、`states`；禁止 `people`，人物由 `positions` 动态派生。
- 每个对象组可以是 ID 列表，也可以是 `ID: 显示名` 映射，一律推荐映射形式：对象面板是玩家的合法目标词表，没有显示名的裸 ID（`oil_lamp`）会让玩家无法判断哪些是可操作的"叙事积木"。
- 需要动态感知的对象使用对象规格映射：`label` 必填，`visible_when` 和 `actionable_when` 使用第 9 节统一条件块。缺省条件为真；不可见对象一定不可交互，可见但暂不可交互的对象可以显示在面板但不能成为行动目标。
- 引擎匹配（`object_any`）永远使用 ID；显示名只用于展示和输入别名。
- `exit_conditions` 使用第 9 节的结构化条件语法，不允许使用表达式字符串——引擎只实现一种条件求值器，trigger、exit_conditions 和 endings 共用。
- `exit_conditions.any` 是条件块列表，任一块满足即视为场景目标达成（块之间 OR，块内 AND）。
- 场景目标达成本身不自动移动玩家；位置变化必须由 storylet 的 `move_entities` 显式表达。`exit_conditions` 是给导演层的信号：目标已达成，应调度收束或转场事件。

## 8. 状态路径

事件卡和结局条件通过状态路径读写运行时状态。

推荐路径：

```text
world.time_left
positions.player
item_locations.servant_key.id
player.injury
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
  exposed: false

flags:
  maid_warned_player: true
```

也可以使用完整路径：

```yaml
conditions:
  item_locations.forgery_evidence.id: player
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

inventory_all:
  - servant_key

inventory_none:
  - old_badge
```

### 9.4 条件块组合

当需要"多组条件之一成立"时（目前用于 `exit_conditions`），使用 `any` 包裹条件块列表：

```yaml
exit_conditions:
  any:
    - inventory_all:
        - servant_key
    - flags:
        guard_pattern_known: true
      world_state:
        archive_door_unwatched: true
```

规则：

- `any` 的每个元素是一个条件块，块内所有条件组是 AND 关系。
- 块之间是 OR 关系。
- 条件块内允许的字段与 storylet trigger 的状态类字段一致（`world_state`、`player_state`、`npc_state`、`flags`、`state_gte`、`state_lte`、`positions`、`same_location`、`inventory_all`、`inventory_any`、`inventory_none`）。

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
      move_items:
        maid_note: { type: carried_by, id: player }
      add_facts:
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

可选的 `phase` 取 `action`（默认）或 `after_world`。只有需要读取本回合 NPC 移动结果的到达、相遇、追捕事件才使用 `after_world`。

可选的 `director_hint` 取布尔值或提示标题字符串。场景切换事件默认会成为导演提示；非转场但必须向玩家显式暴露的关键交互使用 `director_hint: true`。导演只在状态前置满足且所需对象当前可交互时展示它。

### 10.1 `trigger`

推荐字段：

| 字段 | 说明 |
|---|---|
| `scene` | 当前场景必须等于某 scene ID |
| `scene_any` | 当前场景在列表中任一匹配 |
| `intent` | 当前玩家意图必须等于某 intent ID |
| `intent_any` | 当前玩家意图在列表中任一匹配 |
| `object_any` | 玩家方案或目标对象命中列表中任一对象 |
| `object_all` | 玩家方案必须同时包含列表中的全部对象，适合“物品 + 使用目标” |
| `positions` | 指定实体必须位于指定棋盘节点，例如 `{ butler: archive_door }` |
| `same_location` | 列表中的两个或多个实体必须位于同一节点 |
| `inventory_all` | 玩家必须持有列表中的全部物品 |
| `inventory_any` | 玩家必须持有列表中的至少一个物品 |
| `inventory_none` | 玩家不能持有列表中的任何物品 |
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
| `move_entities` | 原子设置一个或多个实体的位置 |
| `move_items` | 原子设置一个或多个物品的位置/所有者 |
| `add_facts` | 追加玩家知识日志条目（展示称呼由 `perception.facts_label` 声明） |
| `temporary` | 临时效果块，到期自动回滚，见下 |

`state_patch` 语义：

- 数字表示对已有数值做增量，例如 `butler.suspicion: +1`。
- 布尔、字符串、列表、map 表示直接赋值。
- 如果路径不存在，数字 patch 第一版可视为从 `0` 开始增量。

示例：

```yaml
state_patch:
  scene.fire_risk: +1
  player.exposed: true
  world.evidence_status: secured
```

移动示例：

```yaml
effect:
  move_entities:
    player: archive_room
```

`move_entities` 是 storylet 的显式、受信任移动效果，可用于玩家转场或剧情集合；普通 world step 的 NPC 移动应优先使用 `world_rules`。v2 禁止 `set_world.scene` 和 `state_patch.positions.*`。

物品转移示例：

```yaml
effect:
  move_items:
    servant_key: { type: carried_by, id: player }
```

`move_items` 是获得、交付、丢弃和消耗物品的唯一修改入口；v2 禁止 `state_patch.item_locations.*`。

`temporary` 语义：

`effect` 顶层的 `set_flags` / `set_world` / `state_patch` / `move_entities` / `move_items` 都是永久变化。会自动过期的变化必须写进 `temporary` 块，避免"哪些部分会回滚"的歧义：

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
2. 应用兜底判定的受限 patch（如有，见第 11 节）。
3. 若 intent 声明 `engine_action: move`，先沿已校验的 `scenes.<id>.exits` 移动玩家；随后对默认 `phase: action` 的 storylet 做一次有序单遍扫描。
4. 推进一个 world step：递增 `world.step`，从同一快照计算 NPC 移动提案并原子应用。
5. 对显式 `phase: after_world` 的 storylet 做一次单遍扫描，用于到达、相遇和追捕反应。
6. 处理临时效果过期。
7. 求值 `endings`，任一结局条件成立则本局结束。

两条附加规则：

- **每回合最多一次玩家位置变化。** 本回合已有 storylet 通过 `move_entities` 移动玩家时，后续会再次移动玩家的 storylet 一律跳过（不消耗 `once`，下回合仍可触发）。NPC 的 world step 移动不计入这条限制。
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
    - positions.*
    - item_locations.*
    - world.evidence_status
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

- 关键剧情事实（证据、钥匙、位置、结局条件涉及的状态）必须列入 `protected`，只能由 storylet 或 world step 改变。schema v2 强制要求 `positions.*` 受保护；声明了 `items` 时也强制要求 `item_locations.*` 受保护。
- `requires_storylet_match: true` 的意图不走“空动作”兜底：若当前意图、对象与状态不能命中作者交互，整次操作原子拒绝，不消耗时间、不增加回合数、不推进 world step。
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
      positions.player: final_confrontation
      item_locations.forgery_evidence.id: player
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
- 结局条件建议显式包含 `positions.<player_role.id>`（通常是收束场景），避免玩家在中途场景意外触发结局、跳过收束演出。
- 持有关键物品的结局条件应检查 `item_locations.<item_id>.id: <player_role.id>`，不再维护重复的 `has_*` 布尔值。

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
| `steps[].expect_world_rules` | 可选；该回合应实际移动实体的 world rule 及顺序 |
| `steps[].note` | 人类可读说明 |

校验器 `tools/check_walkthroughs.py` 按第 10.3 节的执行语义逐回合模拟：应用意图代价和 `generic_patch`、扫描 action storylet、推进 world step、扫描 after-world storylet、处理临时效果并求值结局。任何一步与 `expect_storylets` 或 `target_ending` 不符即失败。

规则：

- 每个结局至少要有一条 walkthrough 覆盖。
- 修改故事内容后必须重跑 walkthrough 校验，两者一起提交。
- walkthrough 同时是阶段 2 规则引擎的验收用例：引擎实现后跑同一批用例，结果必须一致。
- 对允许玩家提前进入关键区域的故事，必须增加“缺少首选道具后的补救路线”或明确失败路线，防止只覆盖最优解而漏掉胜利软锁。

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
schema_version: 2
```

v2 是位置模型的不兼容升级：

- 新增 `world_board`、`world_rules` 和 `initial_state.positions`。
- 删除场景中的静态 `available_characters` / `available_objects.people`。
- 玩家场景从 `positions.<player_role.id>` 派生，转场使用 `move_entities`。
- 新增 `phase: after_world`、`positions` 和 `same_location` 条件。
- 新增唯一 `item_locations`、派生背包、`move_items`、`inventory_*` 条件和显式 `use` 内容协议。
- 新增 `scenes.<id>.exits` 与 `engine_action: move`，玩家可以沿已声明的世界图出口返回。

如果未来继续发生不兼容变更，例如：

- `storylets` trigger 语法变化。
- `state_patch` 语义变化。
- `endings` 条件语法变化。

应升级 `schema_version`，并在 validator 中同时支持旧版本或提供迁移脚本。

已发生的 v1 内部修订：

- `exit_conditions` 从表达式字符串改为结构化 `any` 条件块（2026-07-09）。
- 临时效果从 `effect.duration_turns` 改为 `effect.temporary` 块（2026-07-09）。
- 新增 `resolution_limits`、`intents.<id>.quote_required`（2026-07-09）。

兼容性原则：

> 新故事可以使用新字段，但旧故事不能因为引擎升级而无法运行。

## 17. 内容创作检查清单

新增故事前检查：

1. 是否有唯一 `id`。
2. 是否填写 `schema_version: 2`。
3. `player_role.id` 是否存在于 `characters`。
4. 每个角色是否在 `initial_state.positions` 中有且只有一个合法节点。
5. 每个物品是否在 `initial_state.item_locations` 中有且只有一个合法位置，且未在场景对象里重复声明。
6. 每个场景的意图是否都存在，出口是否连接合法相邻节点。
7. 每个 storylet 的 `id` 是否唯一。
8. 每个 storylet 是否有 `trigger`、`effect` 和 `narrative_hint`。
9. 每个结局是否有 `priority` 和 `conditions`。
10. 重要剧情事实是否写入状态，而不是只写在文本里。
11. 世界图节点和边是否合法；初始场景之外的每个场景，是否有 storylet 通过 `move_entities` 将玩家移入。
12. 每个 `flags` 是否至少被一个 storylet 设置（没有就是死变量或缺事件卡）。
13. `resolution_limits` 是否覆盖软状态，`positions.*` 与 `item_locations.*` 是否列入 `protected`。
14. 每个结局是否有 walkthrough 覆盖，关键区域是否覆盖“缺少首选物品”的补救/失败路线。
15. world rule 是否只引用通用实体/节点、从同一快照可确定地得到唯一移动提案。
16. 关键路径回合数加上合理冗余是否在 `time_left` 预算内。

一句话总结：

> 故事可以自由创作，但必须用稳定的状态、意图、事件卡和结局协议与导演引擎对话。
