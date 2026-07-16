# AIRPG 活动内容 schema

活动内容使用 `schema_version: 2` 与 `content_profile: narrative_first`。

## 顶层

必填：`id`、`title`、`version`、`schema_version`、`content_profile`、`language`、`genre`、`premise`、`player_role`、`style_bible`、`global_rules`、`initial_state`、`characters`、`scenes`。

`items` 可选。`design_goal`、`target_duration_minutes`、`authoring_notes` 等说明字段可保留。

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

玩家需要 `name`、`role`、`public_profile`。NPC 还需要 `motivation`、`voice`、`initial_relationship`，可选 `secret`。人物卡不再包含 `initial_state`。

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
