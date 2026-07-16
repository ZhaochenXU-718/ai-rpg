# AIRPG 状态时间线

## 保存内容

每个 checkpoint 保存物理状态、完整 `MemoryState`、回合号、最后一次提交结果和随机数状态。当前物理状态只有人物位置与关键物品归属。

## revision

每次成功提交都增加 `state_revision`。感知、行动提案、事实抽取和 `FactBatch` 都绑定 revision；旧产物不能跨 revision 使用。

失败候选不会增加回合、revision 或 checkpoint。

## 撤回与分支

`undo` 恢复父 checkpoint，并从该点创建新分支。旧分支和提交仍保留，`timeline` 可以查看当前路径和各分支头。

恢复 checkpoint 本身也会增加 live revision，防止恢复前生成的抽取或行动卡误提交到新分支。

## 叙事记忆

每次成功提交都会创建一个 `MemoryEvent`，记录玩家原话、已提交散文、场景前后、参与人物、引用实体和实际物理变化。完整事件日志保存在当前分支的 `MemoryState` 中，最近 4 个事件构成短期感知窗口。

M2 在旧事件达到阈值时，尝试把一批旧事件整理成滚动小结。成功的小结或失败冷却元数据都附着到当前已提交 checkpoint，不创建新 checkpoint，也不增加物理 revision。完整事件不会因为压缩而删除。

`undo` 会同时恢复祖先 checkpoint 的原始事件、小结和失败冷却状态。被放弃分支的后续事件与小结只保留在该分支 checkpoint 中，不会进入新分支。小结仍是非权威上下文，不自动升级为承诺、关系或任务状态。

M3 的 `MemoryContext` 不单独保存，而是在生成前从恢复后的 `MemoryState` 派生。它绑定当前 live revision；因此撤回前生成的旁白/提案上下文不能跨分支继续使用。上下文只交给旁白与行动提案，事实抽取仍只读取当前回合散文和物理账本。
