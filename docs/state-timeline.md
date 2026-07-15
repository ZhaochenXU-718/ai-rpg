# 状态时间线与分支协议

状态：内存版本已实现
最近修订：2026-07-15

## 1. 核心语义

撤回不是删除聊天文本，而是把权威世界恢复到旧 checkpoint，并从那里建立一条新历史。原分支继续保留，用于审计、回放和未来消息树 UI。

## 2. Checkpoint

`GameSession` 从 `cp_0` 开始。每次成功原子提交后写入：

- 完整权威 state；
- 已消费的 once 锚点；
- 分支内 `turn_no`；
- ending 与上一回合 TurnResult；
- 近期叙事窗口；
- 随机源状态；
- 父 checkpoint、branch 与创建时 revision。

快照深拷贝保存，对外读取也返回隔离副本。

## 3. Revision

- `turn_no` 是当前分支内的回合序号，恢复旧节点时回退。
- `state_revision` 是 session 的权威版本，永不回退；成功提交和恢复旧节点都会递增。
- PerceptionSnapshot、行动卡、FactExtraction、FactBatch、DirectorPlan 和 NpcTurn 都绑定 revision。
- revision 不匹配的 FactBatch 必须在复制/修改 live state 之前拒绝。

因此撤回后不能复用旧生成结果。需要重新基于恢复后的快照生成。

## 4. 分支

- `undo()` 恢复当前节点的父节点并创建 `branch_N`；
- `restore_checkpoint(id)` 从任意保留节点建立新分支；
- 原 branch 的 head 不变；
- 新分支首次提交以恢复目标为 parent；
- 所有分支共享不可变祖先，不覆写旧节点。

## 5. 原子性

候选散文和 FactExtraction 不进入时间线。检查通过后的 FactBatch、事实锚点、Director 和 Local Canon 先应用到工作副本；全部成功后，session 才替换 live state、生成 `CommittedTurn`、增加 turn/revision 并写 checkpoint。抽取失败、铁律冲突或异常时 state、consumed、turn、revision 和当前 checkpoint 均保持不变。

Director provider 自身故障被降级为可记录的可选层错误，不使已完成的事实批次失败。

## 6. 接口

| 接口 | 作用 |
|---|---|
| `current_checkpoint_id` | 当前提交节点 |
| `current_branch_id` | 当前活动分支 |
| `get_checkpoint(id)` | 读取隔离快照 |
| `checkpoint_history()` | 当前路径的根到 head |
| `branches()` | 全部分支的 fork / head |
| `undo()` | 恢复父节点并新建分支 |
| `restore_checkpoint(id)` | 从指定节点新建分支 |

CLI 使用 `undo` 和 `timeline`。日志记录 checkpoint、parent、branch、revision 与恢复事件。

## 7. 尚未实现

- 跨进程持久化；
- 完整消息树 UI 与分支选择；
- 对同一 CommittedTurn 只重生成散文；
- 已结束故事的结算页分支切换；
- 手动选择旧候选、重生成次数策略与随机种子展示；
- Chapter / Arc 长期记忆随分支的增量索引。

## 8. 验收不变量

- revision 在提交和恢复后单调递增；
- 失败批次零状态变化、零回合、零 checkpoint；
- 旧 revision 生成物不能提交；
- 新提交 parent 是恢复目标，旧 branch head 不变；
- 根节点不可撤回；
- 修改外部 checkpoint 副本不影响 live state 或历史。
