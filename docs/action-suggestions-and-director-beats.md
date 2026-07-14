# AIRPG 行动提案与 Director Beat 协议

状态：最小纵切已实现（2026-07-14）

相关实现：

- `server/engine/llm_protocol.py`
- `server/engine/suggestions.py`
- `server/engine/director.py`
- `server/engine/social.py`

## 1. 为什么需要这一层

自由输入是 AIRPG 的主交互，但空白输入框会带来表达负担。行动提案的职责是给玩家提供灵感，不是把底层 `intent` 或对象 ID 换一种方式重新暴露出来，也不是限制玩家只能从卡片中选择。

因此第一版同时保留两条等价入口：

- 玩家继续输入任意自然语言；
- 玩家从 3—5 张 LLM 根据当前感知生成的行动卡中选择一张。

卡片描述的是“玩家准备尝试什么”，不能承诺尚未裁决的结果。每张卡都必须先转成 `ActionPlan` 并通过确定性能力校验，才有资格展示和执行。

## 2. 玩家行动与导演节拍必须分离

两个协议拥有不同控制权：

| 协议 | 谁采取行动 | 可以引用什么 | 当前状态 |
|---|---|---|---|
| `SuggestedAction` | 玩家 | 当前感知中的实体与可用能力工具 | 已接入 mock、DeepSeek 和 CLI |
| `DirectorBeat` | 世界 / NPC | 作者预定义的角色、地点和目标 | 已接入 mock / DeepSeek、确定性复验与同回合原子提交 |

LLM 不能把“某个 NPC 突然进场”伪装成玩家行动卡，也不能借 Director Beat 临场创建重要人物。重要人物属于 Core Canon：作者定义身份、动机、关系和秘密，Director 只负责调度已有实体。

## 3. `SuggestedAction` 协议

每张草案包含：

| 字段 | 含义 |
|---|---|
| `suggestion_id` | 本批提案内唯一 ID |
| `perception_revision` | 生成时所依据的权威状态版本 |
| `title` | 简短侧重点，例如“直接询问”或“先说明用途” |
| `action_text` | 玩家看到并将要采取的第一人称行动 / 对话 |
| `focus` | 用于多样性和统计的稳定侧重点 ID |
| `rationale` | 为什么这张卡适合当前局势，不得包含隐藏事实 |
| `plan` | 与卡片文本一一绑定的 `ActionPlan` |

硬约束：

1. `action_text` 必须与 `plan.player_text` 完全一致。
2. 草案的 revision、plan 的 revision 和当前会话 revision 必须一致。
3. 每个 plan 必须只使用当前 `PlayerPerception.capability_tools` 中存在的能力。
4. 卡片只能引用玩家可感知的实体、公开状态和用途标签。
5. 卡片不能保证成功、泄露秘密或创建核心人物。
6. 生成后逐张经过现有 `CapabilityRouter` 校验；无效卡直接淘汰，不把错误选择交给玩家。
7. 对可执行卡按“能力 + 参数”去重，第一版最多展示 5 张。

玩家选择卡片时，引擎执行卡片中已经验证并冻结的 plan，不把展示文字再次发送给 LLM 理解。玩家如果编辑了文字，则视为一条新的自由输入，重新规划和验证。任何提交、撤回或分支切换导致 revision 改变后，整批旧卡立即失效。

## 4. 运行时流程

```text
PlayerPerception + 当前目标 + 近期公开事件 + capability_tools
→ LLM 生成 SuggestedActionDraft[]
→ 协议字段与 revision 校验
→ 对每张 ActionPlan 做确定性能力校验
→ 淘汰无效项并按能力 / 参数去重
→ 展示 SuggestedActionSet
→ 玩家选择一张
→ 执行冻结的 ValidationResult
→ 自动提交可逆结果，或仅对不可逆结果确认
```

CLI 当前提供：

- `ideas`：按当前状态生成并展示动态行动提案；
- `idea <n>`：选择第 n 张并执行其已验证 plan；
- 自然语言输入：始终保留；
- 旧 intent 菜单：暂时作为开发与回归入口保留，不是最终产品交互。

Trace 会保留原始草案、接受 / 淘汰原因和玩家最终选择，供后续统计提案采用率、编辑率、撤回率、校验淘汰率与能力多样性。

## 5. 第一个通用能力：`social.request_item`

该纵切验证“LLM 提议 + 引擎裁决”可以接住未为具体组合预写 storylet 的普通行动：

```text
玩家：问罗叔有没有能铺桌的干净东西
→ LLM 选择 social.request_item(owner_id=clerk_luo, purpose=table_cover)
→ 引擎检查人物是否在场、物品是否仍由该人物持有、用途是否开放、情境与关系是否满足
→ 同意：原子转移物品
→ 不同意：提交明确拒绝，不伪造软收益，也不把拒绝当系统错误
```

能力工具只向行动 LLM 暴露在场人物和作者定义的公开用途，例如“能铺桌的干净东西”；不会提前暴露隐藏物品 ID 或具体库存。实际物品、所有权、关系阈值和转移结果由引擎确定。

内容通过 `items.<id>.request_policy` 选择性接入。没有策略的物品不会因为模型随口提议而被借出；不可携带物品也不能接入。关键证据、唯一任务物品和其他 Canon 变化应继续使用更严格的专用能力或作者锚点。

## 6. `DirectorBeat` 协议

第一版支持三种节拍：

- `enter_scene`：一个作者预定义 NPC 从相邻地点进入玩家当前场景；
- `react`：已在场 NPC 对已发生的公开结果作出反应；
- `advance_plan`：已在场 NPC 推进自身已定义动机或计划。

确定性校验包括：

- revision 必须是当前版本；
- `actor_id` 必须存在于 `characters`，且不能是玩家；
- 目标地点必须存在于世界图；
- actor 与 target 引用必须有权威位置 / 实体记录；
- `enter_scene` 只能沿已声明的相邻路线进入当前玩家场景；
- 同一人物在一次提交中最多获得一个节拍；已经被 storylet 或 world rule 移动的人物不能再移动一次；
- `react` 与 `advance_plan` 要求 actor 已在当前场景；
- 协议没有 `create_character`、人物卡草稿或其他实体创建字段。

LLM 模式中的 Director 周期发生在玩家能力、storylet、world rules 和 after-world 事件已经确定之后，但在本回合 revision 增长与 checkpoint 写入之前：

```text
已裁决的玩家结果 + 当前地点
→ 引擎只列出在场或相邻的作者角色候选（含 role / public_profile / motivation）
→ LLM 返回 0—2 个 DirectorBeat；没有必要时允许为空
→ 引擎按最新的提交后状态逐个复验
→ 拒绝未知人物、不可达进场、重复人物、二次移动与过期 revision
→ 进场位置变化和表现层反应写入同一份 CommittedOutcome / checkpoint
```

这里使用即将成为事实的“提交后 revision”，不是玩家行动卡所依据的旧 perception revision。Director 服务不可用、返回空列表或所有候选被淘汰时，玩家行动仍照常提交，不会把模型故障变成世界代价。`director_cycle` trace 保存原始响应、模型、耗时、用量、接受项和淘汰原因。

`enter_scene` 的位置变化属于 `MECHANICAL` 权威；`react` / `advance_plan` 当前只允许产生表现层节拍，不直接修改关系、物品、任务或 Canon。完整人物日程与长期计划状态尚未结构化，当前模型只能依据作者 motivation、位置、角色资料、世界边界和本回合已提交结果决定是否介入。

## 7. 当前边界与下一步

本轮已经打通协议和首个能力纵切，但还不是完整产品形态：

- 提案由 CLI 按需生成，尚未在每轮 UI 中自动刷新；
- mock 的提案多样性较基础，真实模型质量需要基于 trace 评估；
- `social.request_item` 当前只有直接同意转移或明确拒绝，条件交换应由后续社会能力协议显式表达；
- Director 已能调度进场和表现层反应，但结构化日程、长期 NPC 计划状态与节拍冷却仍未实现；
- 正式可插拔 Capability Module Contract 与 Local Canon 仍未实现。

测试夹具 `tests/fixtures/open_neighbor_scene.yaml` 刻意不包含任何普通行动 storylet：`social.request_item` 仍可完成物品请求，Director 仍只能让相邻的作者角色进场。这证明 storylet 可以退回 Canon 锚点，而不是普通行动白名单。

下一步开始最小 Local Canon，优先处理局部地点与局势，同时为社会能力增加显式条件交换结果。核心人物继续禁止临场创建。
