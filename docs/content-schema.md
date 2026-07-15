# AIRPG 叙事优先内容 Schema

日期：2026-07-15
适用：`schema_version: 2`、`content_profile: narrative_first`

校验命令：

```bash
python tools/validate_content.py content/rooftop_supper.yaml
```

## 1. 顶层结构

必填：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` / `title` / `version` | string | 稳定 ID、显示标题、内容版本 |
| `schema_version` | int | 当前活动内容使用 2 |
| `content_profile` | string | 活动内容必须是 `narrative_first` |
| `language` / `genre` | string | 语言与类型标签 |
| `premise` | string | 玩家可见前提 |
| `player_role` | map | 玩家身份、目标与边界 |
| `style_bible` | map | 文风与禁区 |
| `global_rules` | map | 世界边界与可选时间政策 |
| `initial_state` | map | 初始权威状态 |
| `characters` | map | 作者人物池 |
| `scenes` | map | 场景与可见对象 |
| `storylets` | list | 提交后事实锚点 |
| `endings` | map | 结局条件与文本 |

常用可选：`design_goal`、`target_duration_minutes`、`world_board`、`items`、`perception`、`generation`。

叙事优先内容禁止顶层 `intents`、`quote_warnings`、`resolution_limits`、`world_rules`。

## 2. 玩家角色、风格与边界

```yaml
player_role:
  id: player
  name: "住户"
  public_identity: "正在帮忙整理院子的邻居"
  private_goal: "把公共长桌遮好"
  constraints:
    - "不能凭空获得物品。"

style_bible:
  tone: "克制、日常"
  camera: "只写当前可感知场景"
  dialogue: "简短自然"
  avoid:
    - "不要制造新核心人物。"

global_rules:
  time:
    countdown_enabled: false
  boundaries:
    - "故事只发生在院子和相邻店铺。"
```

`constraints` 与 `boundaries` 会进入叙事、提案与 Director prompt，但 prompt 不是铁律检查的替代品；Phase 2 必须把边界冲突结构化验证。

## 3. 世界图

```yaml
world_board:
  nodes:
    workshop: {name: "修理铺", kind: shop}
    courtyard: {name: "公共院子", kind: common_area}
  edges:
    - {from: workshop, to: courtyard, bidirectional: true}
```

schema v2 的每个 scene 必须有同 ID node。scene 的 exit 必须对应一条有向边；`bidirectional: true` 同时建立反向边。

世界图声明可达性，不自动移动人物。玩家/NPC 位置只来自 `initial_state.positions` 及后续通过校验的事实提交或锚点效果。

## 4. 关键物品与初始状态

```yaml
items:
  rain_canvas:
    name: "旧防雨布"
    description: "洗净后叠在架子上。"
    portable: true
    consumable: false

initial_state:
  world:
    step: 0
    current_goal: "找到能遮住长桌的东西"
  positions:
    player: workshop
    keeper: workshop
  item_locations:
    rain_canvas: {type: carried_by, id: keeper}
  player: {}
  scene: {}
  flags: {}
```

每个作者人物必须在 `positions` 中有且只有一个场景位置；每个关键物品必须在 `item_locations` 中有唯一放置。常用放置：`carried_by` 指向人物，`board` 指向地点；容器放置可用于隐藏物品，但披露仍受感知墙与事实协议约束。

## 5. 人物卡

```yaml
characters:
  player:
    name: "住户"
    role: player
    public_profile: "正在整理公共院子。"
  keeper:
    name: "周师傅"
    role: practical_keeper
    public_profile: "修理铺店主。"
    motivation: "不耽误营业地解决邻里小问题。"
    voice: "说话直接，先问用途和归还时间。"
    initial_relationship: "认识玩家这个邻居。"
    secret: "可选；只进入该 NPC 的私有上下文。"
```

玩家只要求 `name`、`role`、`public_profile`。每个非玩家核心人物还必须有 `motivation`、`voice`、`initial_relationship`。不要用 rapport/trust 数值替代人物关系记忆。

## 6. 感知声明

```yaml
perception:
  facts_label: "留意到"
  world_state:
    weather: "天气"
  scene_state: {}
  character_state: {}
```

该块声明允许进入玩家公开状态面的键与显示名。人物秘密、flags、远处位置和未发现物品不会因为存在于完整 state 就自动进入 player snapshot。

NPC 私有感知由运行时按 `subject_id` 构造；人物自己的 motivation、voice、relationship、secret 可进入其 `subject_context`，不会进入玩家快照。

## 7. 场景

```yaml
scenes:
  workshop:
    name: "修理铺"
    purpose: "让玩家了解可用遮盖物"
    goal: "说明用途并与周师傅商量"
    entry_text: "卷帘门开着一半。"
    exits:
      - to: courtyard
        label: "去公共院子"
        narrative_hint: "你从门口走进相邻院子。"
        when: {}
    available_objects:
      environment:
        tool_wall: "挂满常用工具的墙"
      states:
        shop_open: "修理铺正在营业"
    exit_conditions:
      any:
        - inventory_all: [rain_canvas]
```

`available_objects` 使用 `group → id: label`。对象也可写成：

```yaml
hidden_panel:
  label: "松动的墙板"
  visible_when: {flags: {panel_found: true}}
  actionable_when: {flags: {panel_open: true}}
```

schema v2 禁止 `available_characters` 和 `available_objects.people`；在场人物由 positions 派生。已跟踪物品不能在场景对象中重复声明。

`suggested_intents`、物品 `request_policy` 与普通行动路由字段已经退役。

## 8. 条件语法

锚点、exit `when` 与 `exit_conditions.any` 共用确定性条件：

- `scene` / `scene_any`；
- `positions`；
- `same_location`；
- `inventory_all` / `inventory_any` / `inventory_none`；
- `world_state` / `player_state` / `flags` / `npc_state`；
- `state_gte` / `state_lte`；
- 键后缀 `_gte`、`_lte`、`_gt`、`_lt`、`_ne`。

一个 block 内全部为 AND。`exit_conditions.any` 中任一 block 命中即可；`ending_reached: true` 可用于场景退出提示。

## 9. 事实锚点

```yaml
storylets:
  - id: opening_offer
    title: "临时没有去处的一篮饭"
    type: opportunity
    attribution: world_beat
    phase: after_world
    once: true
    trigger:
      scene: building_lobby
      flags: {opening_delivered: false}
    effect:
      set_flags: {opening_delivered: true}
      add_facts:
        - "陈阿姨有一篮临时没人吃的热饭"
    narrative_hint: "陈阿姨把女儿加班的消息收起。"
```

必填：`id`、`title`、`type`、`once`、`trigger`、`effect`、`narrative_hint`。叙事优先锚点还必须是 `attribution: world_beat`、`phase: after_world`。

禁止在 trigger 中使用 `intent`、`intent_any`、`object_any`、`object_all`。锚点只读取已提交事实，不能决定玩家某句话是否“命中正确解法”。

当前永久效果：

- `set_flags`；
- `set_world`；
- `state_patch`（数字为增量，其他值为赋值）；
- `move_entities`；
- `move_items`；
- `add_facts`。

作者锚点是受信任 Canon 来源。任何移动 ID、物品放置、状态路径和场景引用仍必须通过内容校验。

## 10. 结局

```yaml
endings:
  canvas_borrowed:
    title: "先把桌面护住"
    priority: 1
    conditions:
      item_locations.rain_canvas.id: player
    outcome: "周师傅把防雨布借给了你。"
```

条件是 dotted path 等值比较；多个条件为 AND。多个结局同时满足时，较小 priority 优先。

## 11. Local Canon 生成边界

```yaml
generation:
  budgets:
    locations: 1
    situations: 1
  location_archetypes:
    storage_nook:
      label: "临时收纳角"
      description_hint: "院子边的小空间"
      allowed_parents: [courtyard]
  situation_archetypes:
    neighbor_gathering:
      label: "邻里小聚"
      description_hint: "几位邻居短暂围拢"
      allowed_locations: [courtyard, workshop]
      max_duration_turns: 3
      expiry_narrative: "邻居各自散开。"
```

预算必须为非负整数；v1 只支持 locations/situations。原型 ID 是小写机器 ID，允许父地点必须来自作者世界图。无 generation 即无生成权限。

## 12. Retired 内容与归档

`pre_pivot_archive` 只用于读取历史机械样本。它可以保留旧字段并由兼容校验分支检查，但：

- CLI 不发现它；
- `GameSession` 拒绝启动；
- 新功能和新测试不得依赖它；
- 新故事不得复制其 intent/quote/walkthrough 结构。

## 13. 回归替代 walkthrough

旧 intent/object walkthrough 已退役。当前内容验收由四层组成：

1. `validate_content.py`：schema、引用、图、条件、锚点和生成边界；
2. 场景 fixture 测试：从明确 FactBatch 验证锚点、结局、Local Canon 与分支；
3. provider/协议测试：感知隔离、轻量行动卡和 Director 复验；
4. CLI mock 冒烟：自然语言、ideas、facts、undo、timeline。

Phase 2 接入 extractor 后，应增加“玩家文本 + 模型散文 + 预期抽取/冲突/提交”的叙事回归；不能恢复意图菜单模拟器。

## 14. 提交前检查

- 活动内容是否显式 `narrative_first`？
- 每个人物和物品是否有唯一初始位置？
- NPC 是否有动机、声音与定性初始关系？
- scene exit 是否与世界图一致？
- 玩家不可见事实是否留在感知墙后？
- storylet 是否只订阅已提交事实？
- 锚点效果的 ID、路径、物品放置是否有效？
- generation 是否有明确原型与硬预算？
- 内容校验、场景回归和 CLI 冒烟是否全绿？
