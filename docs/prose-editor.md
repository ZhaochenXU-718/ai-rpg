# 可选行编辑器

## 目标与边界

行编辑器处理旁白已经完成的初稿。它只移除或改写无功能表达，不续写剧情、
不修复事实、不改变人物立场，也不以缩短文本为目标。全局原则仍以
[语言与行文原则](language_style.md) 为准：删除一句话前，先判断是否会损失
人物、张力、节奏、视角、空间或信息。

编辑器不读取故事蓝图、人物私密卡或完整记忆小结。请求只包含：

- 本回合玩家行动；
- 上一回合已经公开的正文；
- 本回合初稿；
- 当前可见名称组成的保护词集合。

它是引擎运行时能力，不进入作品 YAML，也不要求创作者配置 `style_bible`、
`prose_profile` 或其他作品级编辑规则。

## 模式

CLI 和 Web 服务都接受：

```bash
--prose-editor off|shadow|on
```

- `off`（默认）：不调用编辑器，保持原有生成、流式输出和事实抽取路径。
- `shadow`：初稿照常流给玩家；初稿通过物理校验后，额外生成编辑候选并写入
  trace，但始终提交初稿。该模式会增加回合提交前的等待时间。
- `on`：先缓冲初稿，完成保护检查后一次性发送最终选中的正文。编辑器出错、
  候选被拒绝或物理事实发生变化时，回退到已经验证的初稿。

`on` 会增加一次行编辑调用；编辑稿确有变化时，还会增加一次事实抽取调用。
它不再保留逐 token 的旁白流式体验，而是在候选通过检查后发送完整正文。

## Web 开发对照面板

Web 服务可显式打开逐回合对照面板：

```bash
python server/web.py content/hogwarts_before_hogwarts.yaml \
  --llm kimi \
  --prose-editor shadow \
  --show-editor-comparison
```

`--show-editor-comparison` 只接受 `shadow` 或 `on` 编辑模式。它会在每段已提交
旁白后显示编辑前、编辑后、最终采用项、编辑状态、字符比例和 guard 结果；
桌面使用双栏，窄屏回落为单栏。`shadow` 中最终采用项始终是初稿，但仍可直接
审阅候选；`on` 中面板会标出实际进入历史记录的版本。

该开关默认关闭。关闭时，Web 状态响应和 SSE 都不暴露初稿或候选稿。打开后
的对照记录只保存在当前 Web 服务进程内，刷新页面可以恢复，重启服务后不会
保留；需要长期分析时仍以 trace 为准。这个面板是开发工具，不进入作品 YAML，
也不改变行编辑器的选择和回退逻辑。

## Prompt 结构

DeepSeek 和 Kimi 共享同一份中文 prompt，provider 只分别记录版本号。第一版按
以下优先级组织：

1. 行编辑身份与不得续写的职责边界；
2. 事实、知识边界、披露程度、人物立场和玩家能动性等不可变约束；
3. “经济性不等于极简”的功能判断；
4. 重复解释、情绪复述、空泛气氛和机械句式等候选问题；
5. 人物声音、心理运动、POV、潜台词、节奏和多功能细节的保护清单；
6. 过度压缩为动作简报的反例；
7. 只输出正文的输出契约。

“常见于 AI 文本”的模式只能触发检查，不能直接触发删除。如果初稿没有明确
问题，prompt 要求逐字返回原稿。

## 代码级保护与回退

`server/engine/prose_editor.py` 先做低成本候选检查：

- 空输出或泄漏“修改后”、Markdown 围栏等编辑说明；
- 正文压缩到初稿非空白字符数的 65% 以下；
- 正文扩张到初稿的 120% 以上；
- 初稿中出现的当前可见名称被删除，或未出现的可见名称被添加；
- 数字被增加、删除或改写。

这些阈值只拦截明显越界，不代表文学质量判断。候选通过后，`on` 模式会对
初稿和编辑稿分别运行事实抽取与铁律校验；只有两者产生完全相同的物理状态
变化，编辑稿才可提交。编辑稿的二次抽取失败或结果不一致时，直接提交已验证
的初稿，不让可选编辑器阻塞回合。

现有代码只能确定性保护人物位置和关键物品归属。人物态度、线索披露程度、
心理运动和节奏等软语义仍无法被上述检查证明完全等价，因此默认保持 `off`，
并先通过 `shadow` trace 建立人工审阅的回归样本。通用“AI 分数”不作为启用
`on` 的依据。

## Trace

编辑开启时新增：

- `prose_editor`：初稿、候选、provider、prompt 版本、用量、粗粒度 guard；
- `prose_editor_selection`：最终选择初稿还是编辑稿，以及选择原因；
- `prose_editor_fact_check_error`：编辑稿二次事实抽取失败的诊断。

原有 `fact_extraction` 与 `physical_fact_validation` 增加
`candidate_kind=draft|edited`，可区分两次检查。

## 开源先验与版本固定

prompt 是项目自己的中文规则合成，不在运行时加载或拼接外部 skill。以下版本
只作为原则、检查维度和正反例组织方式的来源；英文词频表、标点数量、博客
结构和“强行添加人味”等规则明确不采用。

- `haowjy/creative-writing-skills`，commit
  [`52e6adce`](https://github.com/haowjy/creative-writing-skills/tree/52e6adce0951b14894732d7759392347b35a856f)，
  Apache-2.0：相信读者、经济性不等于极简、阅读回报通道、行编辑边界。
- `conorbronsdon/avoid-ai-writing`，commit
  [`f9fef0ee`](https://github.com/conorbronsdon/avoid-ai-writing/tree/f9fef0ee35f45fb8b0b4681a3f7c3fc7d43788d8)，
  MIT：先识别再判断、局部修改、结构与节奏优先、避免过度编辑。
- `blader/humanizer`，commit
  [`523374de`](https://github.com/blader/humanizer/tree/523374dee72d67c7b2b5f858ea0094ffda49c3ac)，
  MIT：保留信息、不发明事实、匹配原有声音、避免无菌统一。
- `stephenturner/skill-deslop`，commit
  [`a906154b`](https://github.com/stephenturner/skill-deslop/tree/a906154bef375d9d49ed2ad7da13b2db16f0d3d2)，
  MIT：directness、rhythm、trust、authenticity、density 检查维度。

当前 prompt 为重新组织和中文化后的项目原创文本，没有复制外部词表或完整
段落。未来若直接引入、翻译或分发上游文本，须另行保留其许可证、版权与修改
说明。
