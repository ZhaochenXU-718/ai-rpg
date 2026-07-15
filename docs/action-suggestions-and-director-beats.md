# AIRPG 行动提案、DirectorPlan 与 Local Canon

状态：协议 0.3 已实现（2026-07-15）

相关实现：`suggestions.py`、`director.py`、`local_canon.py`、`llm_protocol.py`

## 1. 两种玩家输入

自由输入和行动卡完全等价。行动卡解决空白输入框的表达负担，不是行为白名单。

卡片只能描述“我准备怎么做”，不能保证“世界已经怎样”。玩家可直接采用，也可以任意改写；两者都会进入同一条叙事生成与后验事实管线。

## 2. 行动卡协议

`SuggestedAction` 包含 ID、生成时 revision、标题、第一人称行动文本、侧重点、理由和预期铁律触点。不存在 plan、机械步骤、状态 patch 或预验证结果。

生成流程：

```text
玩家 PerceptionSnapshot + 世界边界
→ provider 生成轻量行动卡
→ 引擎清洗空项、按 action_text 去重
→ 根据可见实体标记可能触及的位置 / 物品 / 披露 / 承诺
→ 展示最多 5 张
```

provider 失败时使用感知快照生成保守兜底卡。任何提交、撤回或分支恢复都会改变 revision，旧卡立即失效。

CLI：`ideas` 生成；`idea <n>` 采用；自由文本始终可用。

## 3. 玩家行动与世界行动分离

行动卡只代表玩家。人物进场、反应和自身计划由 DirectorPlan 提议；未来 NPC agent 的具体对白和行动使用 NpcTurn。模型不能把“某人突然出现并答应了”塞进玩家卡片绕过人物权限。

## 4. DirectorPlan

Director 获得本回合已提交结果、当前地点、目标、世界边界、生成预算，以及在场/相邻的作者人物候选。返回：

- 0—2 个 `DirectorBeat`；
- 0—1 条 Local Canon 提议。

节拍类型：

- `enter_scene`：相邻的作者人物进入玩家当前场景；
- `react`：在场人物作克制反应；
- `advance_plan`：在场人物推进作者声明的动机。

引擎复验 revision、actor、target、人物权威位置、目标场景、邻接路线、同回合二次移动和重复人物。只有通过的进场变化才能写入位置账本；玩家看到的进场提示由引擎根据已提交人物与地点确定性生成。当前提交后 Director 的 `react` / `advance_plan` 摘要没有经过 Phase 2 抽取，因此只留在 trace，不进入玩家输出或 recent memory，不能自行改变物品、秘密、承诺或任务。

Director 服务失败只写 trace 和提示，不取消玩家回合。

## 5. Local Canon

Local Canon 只能通过 DirectorPlan 提议。作者在 `generation` 中声明原型与硬预算；没有声明即禁止。

准入顺序：

1. revision 当前；
2. kind 与 archetype 在作者清单内；
3. `gen_` ID 全新，名称不与作者/已生成实体冲突；
4. 父地点是作者地点且在原型允许范围；
5. situation 生命周期不超过上限；
6. 分支累计预算仍有剩余；
7. 每回合硬上限 1 条。

生成地点自动派生父地点双向出口；生成局势在父地点可见，到期后转为不活跃但保留 provenance。所有生成记录随 checkpoint 撤回和分支。

v1 不生成人物、不允许生成实体嵌套、不处理承诺/任务/关系事实。

## 6. 当前边界

Director Beat 仍是保守调度骨架，不是 Director v2 的完整场景编排。NpcTurn 只有协议，尚未接 agent。行动卡和自由输入现在都进入 Phase 2 后验管线；只有抽取并通过铁律检查的位置、物品、披露或承诺变化才能提交。
