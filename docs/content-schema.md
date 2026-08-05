# AIRPG 活动内容 schema

活动内容使用 `schema_version: 2` 与 `content_profile: narrative_first`。

## 顶层

必填：`id`、`title`、`version`、`schema_version`、`content_profile`、`language`、`genre`、`premise`、`player_role`、`style_bible`、`global_rules`、`initial_state`、`characters`、`scenes`。

`items` 可选。`genre_tags` 是建议使用的结构化题材标签列表，用于故事库筛选和给创作辅助模型提供背景；标签不触发引擎题材分支。`genre` 保留为向后兼容的可读摘要。`design_goal`、`target_duration_minutes`、`authoring_notes` 等说明字段可保留。

## 作者叙事分层（可选）

以下可选顶层字段把"玩家文案"和"AI 指令"正式分开：

```yaml
player_facing_summary: "给玩家看的一句话简介，不进入任何模型调用"

emotional_contract: "希望玩家持续获得的感受；旁白与行动提案都会读取"

opening_narration: "人写的开场正文：启动时展示一次，并每回合作为文风与事实锚进入旁白"

ai_plot:
  player_role: "玩家在故事中的角色"
  main_goal: "主要目标和核心矛盾"
  opposition: "对抗力量或阻力来源"
  world_rules: "世界规律"
  hidden_truth: "作者知道、玩家开场不应知道的真相"

narrative_guidelines:
  - "视角、语气、节奏等叙述规则，只进旁白"

critical_reminders:
  - "最多 4 条最高优先级防漂移规则，旁白每回合必须遵守"
```

路由规则（有 prompt 快照测试守护）：

- `player_facing_summary` 不进入任何模型调用；
- `ai_plot` 完整进入旁白的作者私有上下文；去掉 `hidden_truth` 后作为"故事方向"进入行动提案；
- `narrative_guidelines` 与 `critical_reminders` 只进旁白；
- `emotional_contract` 同时进旁白与行动提案；
- `opening_narration` 玩家可见（开场渲染一次）且只进旁白——它是给模型续写的示范散文，不是秘密。

`ai_plot` 只接受上述五个键，拼错的键会被校验器拒绝——这防止本应保密的字段因键名笔误而流入行动提案。

## modules（可选）

模块是作者预写的剧情素材——"发生什么"加"何时适合出现"——不是分支脚本、任务或效果规则。编排器按类别策略、冷却与在场实体做确定性筛选，把少量候选交给旁白；自然语言触发语义由旁白判断。

```yaml
modules:
  zhou_hesitation:
    category: character        # main | character | pressure | aftermath | side
    title: "周师傅的迟疑"
    purpose: "这段戏在故事中的作用"          # 必填
    hook: "抛给玩家的钩子怎么写"              # 必填
    trigger: "什么时候适合出现（自然语言）"    # 必填
    involves: [keeper_zhou, rain_canvas]     # 可选；引用人物/物品/场景 ID，在场才候选
    escalation: "玩家接住钩子后往哪升级"       # 可选
    resolution: "怎样算收尾"                  # 可选
    fallback: "玩家忽略时怎样消退"            # 可选
    repeatable: false                        # 可选，默认 false
    cooldown_turns: 4                        # 可选，默认由类别策略决定
    min_turn: 6                              # 可选，最早出现回合
    priority: normal                         # 可选：low | normal | high
    tags: [neighborly]                       # 可选自由标签，引擎不解析
    requires:                                # 可选结构性依赖
      - module: other_module
        status: resolved                     # offered | engaged | resolved | dropped
```

类别是功能枚举（引擎按它区分编排策略），题材化分类用 `tags` 表达。模块**不允许** `effects`、`when`、`state_patch`、`conditions`——它没有任何状态权限。

模块软状态 `unseen → offered → engaged → resolved / dropped` 存在叙事记忆中，随 checkpoint 和分支恢复：候选进入已提交回合记 `offered`；被忽略 3 次自动消退为 `dropped`；`engaged`/`resolved` 由 M2 小结标注，`resolved` 终态，`dropped` 可被晚接的钩子复活为 `engaged`。

`requires` 的满足是**进度包含**而非精确匹配：`offered` 表示"钩子抛出过即可"（offered/engaged/resolved/dropped 都满足，零时延，由代码即时标记）；`engaged` 被 engaged/resolved 满足；`resolved` 与 `dropped` 精确匹配。选用原则：错序代价低的顺承用 `trigger` 语义或 `requires: offered`；只有"绝不能提前出现"的硬门槛才用 `engaged`/`resolved`——它们依赖 M2 记账，有批处理时延（引擎会在有模块被卡住时按需加速一次小结，但保护窗与冷却不变）。

## openings（可选）

```yaml
openings:
  courtyard_start:
    title: "从院子开始"
    intro: "可选的开场引导文字"
    positions:
      player: courtyard   # 覆盖 initial_state.positions 中的对应条目
```

开场只覆盖初始位置并附加引导文字；进入游戏后所有开场走同一运行管线。CLI 用 `--opening <id>` 选择。

活动内容不再支持：`intents`、`world_board`、`generation`、`storylets`、`endings`、`genre_system`、`perception`、`world_rules`、`resolution_limits`、`quote_warnings`。

## player_role

```yaml
player_role:
  id: player
  name: "住户"
  public_identity: "正在帮忙的普通住户"
  private_goal: "把眼前的事情妥善处理"
  constraints:
    - "不能凭空获得关键物品。"
```

约束只影响生成 prompt，不会自动变成代码规则。

## characters

玩家需要 `name`、`role`、`public_profile`。NPC 还需要 `motivation`、`voice`、`initial_relationship`，可选 `secret`、`pressure`、`behavior`、`mannerisms`、`narration_notes` 和 `dialogue_examples`（字符串列表）。人物卡不再包含 `initial_state`。

人物卡私有字段（`public_profile` 之外的全部字段）只在该人物在场时进入旁白的作者私有上下文，用于扮演；它们不进入玩家感知、行动提案和事实抽取，也不代表玩家已知。

## scenes

```yaml
scenes:
  workshop:
    name: "修理铺"
    purpose: "说明此场景在故事中的作用"
    goal: "给当前叙事一个开放方向"
    entry_text: "卷帘门开着一半。"
    exits:
      - to: courtyard
        label: "去公共院子"
    available_objects:
      environment:
        tool_wall: "挂满工具的墙"
```

出口是模型可见的导航提示，不带 `when`、`narrative_hint` 或 `exit_conditions`。场景对象是静态标签，不带条件可见性与可行动性 DSL。

## items 与 initial_state

```yaml
items:
  rain_canvas:
    name: "旧防雨布"
    description: "洗净后叠在架子上。"
    portable: true
    consumable: false
    visible_when_carried: true

initial_state:
  positions:
    player: workshop
    keeper_zhou: workshop
  item_locations:
    rain_canvas: {type: carried_by, id: keeper_zhou}
```

`initial_state` 只允许 `positions` 与 `item_locations`。物品 placement 只有 `{type: carried_by, id: <character>}` 或 `{type: board, id: <scene>}`。

## 校验

```bash
python tools/validate_content.py tests/fixtures/open_neighbor_scene.yaml
```

校验器检查 ID、必填人物卡、出口引用、人物初始位置、关键物品定义与归属。`pre_pivot_archive` 只做归档身份识别，不再深度维护旧 DSL。

当前仓库没有正式活动展示故事；`open_neighbor_scene` 是测试夹具。`rooftop_supper` 已归档，不能用作新内容模板。
