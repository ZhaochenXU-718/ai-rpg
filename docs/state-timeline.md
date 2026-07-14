# 状态时间线与分支协议

状态：最小内存版本已实现
日期：2026-07-14

## 1. 目的

撤回不是删除一条聊天消息，而是把权威世界恢复到旧提交点，并从那里建立一条新历史。旧分支继续保留，便于审计、回放和以后接入消息树 UI。

该协议只处理已经提交的权威状态。重新生成同一结果的叙事不创建状态分支；使用新随机结果重新检定属于玩法政策，也不等同于撤回。

## 2. 时间线对象

每个 `GameSession` 从 `cp_0` 根节点开始。每次成功提交行动后创建一个 checkpoint，并记录父节点和创建它的 branch。当前最小快照包含：

- 完整权威 `state`；
- 已消费的 once storylet；
- 待到期的 temporary effects；
- 分支内逻辑 `turn_no`；
- ending 与上一回合 `TurnResult`；
- 近期事件窗口，即当前叙事记忆；
- session 随机源状态。

Checkpoint 内容以深拷贝保存，对外读取也返回隔离副本，调用方不能修改历史节点。

## 3. Revision 与分支语义

- `turn_no` 是分支内的逻辑世界步。恢复旧节点时随快照回退，使临时效果、日程和世界步进继续使用正确的分支时间。
- `state_revision` 是当前 session 的权威版本，永不回退。普通提交递增一次；恢复旧节点也递增一次。
- 恢复节点后，所有未决报价、已验证 plan、requote 计数和澄清上下文立即清空。旧 `ValidationResult.state_revision` 与新 revision 不同，不能提交。
- `undo()` 恢复当前 checkpoint 的父节点，并创建新的 `branch_N`。原 branch 的 head 不变。
- `restore_checkpoint(id)` 可以从任意保留节点建立新分支，同样不覆盖原历史。
- 新分支的第一次提交以恢复目标为父节点；因此所有分支共享不可变祖先，而不会复制或改写旧链路。

## 4. 当前接口

`GameSession` 暴露以下最小接口：

| 接口 | 作用 |
|---|---|
| `current_checkpoint_id` | 当前所处的已提交节点 |
| `current_branch_id` | 当前活动分支 |
| `get_checkpoint(id)` | 读取一个隔离的历史快照 |
| `checkpoint_history()` | 获取当前路径的根到 head 祖先链 |
| `branches()` | 获取所有保留分支及其 fork / head |
| `undo()` | 撤回上一次提交并新建分支 |
| `restore_checkpoint(id)` | 从指定历史节点新建分支 |

CLI 提供：

- `undo` / `撤回`：撤回上次行动；
- `timeline` / `branches`：查看保留分支、head 与当前 revision。

Session 日志记录 `checkpoint_id`、`parent_checkpoint_id`、`branch_id` 和 `checkpoint_restored`；CLI trace 额外记录 `state_branch_created`。

## 5. 尚未包含

当前版本刻意保持为内存级运行时核心，尚未实现：

- checkpoint / branch 的跨进程持久化；
- 完整消息树与分支选择 UI；
- 对同一个 `CommittedOutcome` 重新生成叙事；
- 在已结束故事的 CLI 结算页直接切换分支；
- 重新检定权限、随机种子展示和硬核模式限制；
- Local Canon 实体随 checkpoint 的专门索引（在 Local Canon 落地前，完整 state 快照已经能覆盖其未来状态）。

这些产品层能力应建立在本协议上，不得通过删除日志、覆写 checkpoint 或复用旧验证结果实现。

## 6. 验收不变量

- 撤回后权威 state、consumed、temporaries、近期事件和随机源与目标 checkpoint 一致；
- revision 在提交、撤回和跨分支恢复后始终单调递增；
- 撤回前的 plan 与 quote 无法在新 revision 提交；
- 新提交的 parent 是恢复目标，旧 branch head 保持不变；
- 根节点不可继续撤回；
- 外部读取 checkpoint 后修改副本，不影响 live state 或历史。
