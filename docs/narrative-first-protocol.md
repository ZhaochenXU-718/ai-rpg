# AIRPG 叙事优先协议 0.5

## 1. 权限链

```text
PerceptionSnapshot + MemoryContext
→ Narrative / SuggestedAction
→ FactExtraction
→ 代码级物理检查
→ FactBatch
→ CommittedTurn
```

只有 `FactBatch` 能进入提交缝。provider 生成的散文、提案与抽取都没有状态 patch 权限。

## 2. PerceptionSnapshot

快照绑定 `session_id`、`turn_no` 和 `state_revision`，包含当前场景、可见实体、随身关键物品、作者目标、最近叙事及主体私有上下文。

玩家快照不包含远处人物或 NPC 的 `secret`、`motivation` 等私有卡片。NPC 快照接口保留，但当前没有自动 NPC 回合。

## 3. SuggestedAction

行动提案只有标题、可编辑自然语言、侧重点与理由。它绑定感知 revision，但没有计划、能力、预期状态变化或执行权限。

## 4. FactExtraction

当前只允许两类抽取：

- `character_move(actor_id, destination_id, evidence)`；
- `item_transfer(item_id, from_placement, to_placement, evidence)`。

`evidence` 是审计元数据，不是脆弱的字符串相等门槛。普通对话、承诺、秘密、关系、情绪、任务进度和结局不得被抽成物理事实。

## 5. 代码级检查

人物移动检查人物与目标场景是否为作者实体、人物是否在当前感知范围、同批是否重复移动。关键物品检查物品可见性、可携带性、当前归属、接收者和共处位置。

任一事实冲突会拒绝整批候选。可重试冲突默认最多重写一次；失败不增加回合、revision 或 checkpoint。

## 6. FactBatch 与 CommittedTurn

检查器将合法变化写入 revision-bound `FactBatch`。session 在工作副本上原子应用后生成 `CommittedTurn`，记录 revision 前后、场景前后、散文与实际物理变化。

`CommittedTurn` 不再包含 Director、Local Canon、锚点、动态事实或结局字段。

## 7. Trace

trace 记录候选生成、事实抽取、物理检查、重写、最终提交及其 `MemoryEvent`。trace 是审计数据，不是权威状态。

## 8. MemoryState（M1/M2）

成功提交后，引擎确定性生成 `MemoryEvent`：玩家原话、散文、场景前后、参与者、引用实体和物理变化。事件不需要 LLM 抽取，也不写回物理状态。

`MemoryState` 保存完整分支事件、`rolling_summary`、`open_loops`、人物/场景小结和最近解决事项。最近 4 回合保留原文；更老的未压缩事件达到窗口或字符预算后，M2 才调用 provider 更新这些软字段。

小结发生在回合提交之后，没有状态 patch 权限。失败不回滚回合、不删除事件，并使用跨回合冷却避免重试风暴。

## 9. MemoryContext（M3）

`MemoryContext` 是从当前分支 `MemoryState` 派生的、带字符预算的生成视图。它绑定 `subject_id`、`turn_no` 与 `state_revision`，组合滚动小结、结构化软笔记和压缩边界之后的原始事件。

旁白与 `SuggestedAction` 请求使用同一份上下文。`FactExtractionRequest` 没有该字段，避免旧小结影响物理事实抽取。当前感知和事实清单始终覆盖软记忆；跨主体、跨回合或跨 revision 的上下文在发给 provider 前被拒绝。
