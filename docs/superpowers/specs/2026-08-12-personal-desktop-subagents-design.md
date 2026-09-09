# Cursor 式个人桌面子 Agent 设计

日期：2026-08-12  
状态：设计已确认，等待实施计划

## 1. 背景

AgentAssistant 的核心是运行在 Windows 上的个人桌面助手，不是代码 Agent。

本设计借鉴 Cursor 的以下体验：

- 主 Agent 能把一个复杂目标拆成多个具名子任务。
- 多个子 Agent 拥有独立上下文并真实并行执行。
- 会话中用紧凑的扁平列表展示每个子 Agent。
- 用户可展开活动记录，也可停止、重试或继续追问单个子 Agent。
- 子 Agent 失败时进度不会丢失，由主 Agent继续决策。

本设计不复制 Cursor 的代码角色。第一版仅提供面向个人桌面助手的四种固定角色：

- Explorer
- Researcher
- Operator
- Organizer

## 2. 目标

### 2.1 功能目标

1. 同一主 Agent 回合最多同时运行 4 个子任务。
2. 每个子 Agent 有独立任务 ID、消息上下文、工具集合、取消令牌、检查点和成果物。
3. Explorer 与 Researcher 可并行运行。
4. Operator 独占桌面交互资源，避免多个任务同时切换窗口或发送输入。
5. Organizer 采用“只读规划 → 用户一次确认 → 受控执行”的两阶段流程。
6. 主 Agent 通过事件等待接收结果，不轮询子 Agent 状态。
7. 应用退出或崩溃后，运行中任务可从已保存检查点恢复。
8. 前端按 `task_id` 精确更新任务状态，不再猜测“最近一个 pending 卡片”。

### 2.2 体验目标

1. 默认只显示一行任务标题、角色和当前状态。
2. 运行中的状态文字使用轻量 shimmer，完成后停止动画。
3. 点击任务行展开活动时间线、结果和控制按钮。
4. Operator 控制桌面时显示常驻控制条，可暂停或停止。
5. Organizer 在执行前显示完整变更清单和一次性批准入口。
6. 子 Agent 失败后，用户不需要复制进度文件或手动告诉主 Agent 如何恢复。

## 3. 非目标

第一版不包含：

- Coder 或代码工作流专用角色。
- 子 Agent 再派发子 Agent。
- 多个 Operator 同时操作桌面。
- 多个 Organizer 同时写入文件系统。
- 任意角色动态提升工具权限。
- 自动删除用户文件。
- 邮件、日历或即时通信专用 Agent；这些角色要等对应工具正式接入后再增加。
- 用 LLM 子 Agent 替代现有性能监控和晨间触发 daemon。
- 跨设备或远程 Agent 服务。

## 4. 核心设计原则

### 4.1 角色是权限模板

角色定义工具白名单、资源锁和输出契约，不定义人格。

主 Agent 动态决定：

- 任务标题
- 任务目标
- 传给子 Agent 的最小必要上下文
- 使用哪个固定角色

主 Agent不能为单个任务临时扩大角色权限。

### 4.2 主 Agent 保留个人关系上下文

以下能力不下放：

- 用户长期记忆的最终写入判断
- 用户画像更新
- 最终授权和风险判断
- 对多个子任务结果的综合决策
- 面向用户的最终回答

子 Agent 只获得完成当前任务所需的上下文，默认不复制整段主会话。

### 4.3 共享状态结构化

不使用任意可写的“全局变量”让多个 Agent 相互修改。

共享信息只通过以下结构传递：

- 持久化任务记录
- 带序号的任务事件
- 结构化子 Agent 结果
- 主 Agent 发出的续跑指令

这避免竞态、隐式依赖和上下文污染。

## 5. 角色定义

### 5.1 Explorer

用途：

- 按文件名、路径和安全文本内容搜索本地文件。
- 读取用户授权目录内的文件。
- 查询知识库和已附加资料。
- 汇总本地资料并返回路径证据。

允许的能力：

- `list_files`
- `read_file`
- `search_knowledge`
- `read_attached_source`
- 新增的只读 `search_local_files`

禁止的能力：

- 写入、编辑、移动文件
- 运行任意命令
- 操作桌面应用
- 写入长期记忆或用户画像

`search_local_files` 必须：

- 仅搜索文件 jail 内的规范化路径。
- 支持文件名、扩展名和可选安全文本类型。
- 限制递归深度、扫描文件数、单文件大小和总耗时。
- 跳过二进制文件、隐藏系统目录和敏感凭据目录。
- 返回命中路径、元数据和短片段，不返回无限量原文。

### 5.2 Researcher

用途：

- 联网搜索。
- 读取网页正文。
- 多来源交叉核验。
- 生成带来源的完整报告。

允许的能力：

- `web_search`
- `read_page`
- `extract_content`
- 用户明确要求保存调研结果时使用 `save_note`
- 用户明确附加本地资料时使用 `read_local_source`
- 用户明确启用知识库时使用 `search_knowledge`

Researcher 继续保留现有：

- L0 原始工具档案
- L1 活动摘要
- 来源对齐 findings
- 报告生成
- 超时保护
- 局部结果 salvage
- `resume_from` 恢复

### 5.3 Operator

用途：

- 启动或聚焦桌面应用。
- 读取 UI Automation 控件树。
- 点击、输入、发送快捷键和滚动。
- 根据状态变化完成应用内任务。

允许的能力：

- `launch_app`
- `focus_window`
- `ui_inspect`
- `ui_click`
- `ui_type`
- `ui_hotkey`
- `ui_scroll`

系统性能、进程、音量和通知工具继续由主 Agent 使用，不进入 Operator
第一版白名单。

资源规则：

- 所有桌面交互必须持有全局 `desktop_lock`。
- 同时最多一个 Operator 持有该锁。
- 主 Agent 直接调用桌面 UI 工具时也必须使用同一把锁。
- 等待锁的任务保持 `queued`，不占用运行 worker。

用户体验：

- Operator 开始操作时显示常驻控制条。
- 控制条显示目标应用和当前动作。
- 用户可以暂停或停止。
- 用户的鼠标或键盘操作可能改变界面，控制条必须明确提示这一点。
- 暂停后释放 worker 和 `desktop_lock`；继续时重新排队获取锁，并在执行任何
  动作前重新读取界面状态。

验证规则：

- 点击、输入或滚动后重新读取必要的界面状态。
- 使用 `state_hash`、目标控件和相邻上下文确认界面变化。
- 无法确认成功时不得编造完成。
- `ui_type` 的原始文本默认不进入可见活动日志；疑似密码、Token 或密钥时必须完全脱敏。

### 5.4 Organizer

用途：

- 归类和移动用户文件。
- 生成用户明确要求的笔记或整理成果。
- 提供可审核、可追踪的变更清单。

第一阶段：规划

- 只允许使用 Explorer 的只读能力。
- 生成包含源路径、目标路径、冲突处理和跳过原因的 manifest。
- 计算 manifest hash。
- 在 UI 中展示清单。
- 不执行任何写入。

第二阶段：执行

- 用户一次批准后生成短期、单次、与 manifest hash 绑定的 approval token。
- 只允许执行 manifest 中已批准的操作。
- 仍然执行文件 jail、系统目录和硬 guardrail 检查。
- 任何清单外操作都必须被拒绝。
- 文件写入阶段持有全局 `filesystem_write_lock`。

允许的写能力：

- `move_file`
- 用户明确要求保存笔记时使用 `save_note`

限制：

- 第一版不提供删除能力。
- 第一版 Organizer 不编辑已有文件，也不创建任意成果文件；生成的整理报告
  作为子 Agent 输出返回。
- approval token 不能绕过硬 guardrail。
- `save_note` 继续遵守“用户明确要求 + UI 确认”的既有规则。

回滚：

- 每次成功移动后记录源路径、目标路径、时间和文件指纹。
- 回滚前验证目标文件未被外部修改。
- 回滚不得覆盖现有文件。
- 无法安全回滚的条目必须报告给用户，不得强制覆盖。
- `save_note` 不属于批量移动事务，不包含在 rollback token 中。

## 6. 系统架构

### 6.1 新模块

新增 `src/agent_assistant/subagents/`：

- `models.py`
  - 任务、事件、结果和状态模型。
- `profiles.py`
  - 四种固定角色的提示词、工具白名单和预算配置。
- `manager.py`
  - 创建、排队、调度、暂停、取消、重试和恢复任务。
- `runtime.py`
  - 独立子 Agent 消息循环、工具执行和上下文折叠。
- `resources.py`
  - 并发槽位、`desktop_lock` 和 `filesystem_write_lock`。
- `store.py`
  - SQLite 任务状态和任务档案路径。
- `events.py`
  - 事件构造、序号分配和前端发布。

### 6.2 SubagentManager

`SubagentManager` 是进程内单例服务，但任务运行状态不依赖任意模块全局变量。

职责：

- 接收主 Agent 派发的任务。
- 为每个任务生成稳定 `task_id`。
- 持久化任务后再进入队列。
- 在最多 4 个 active slot 内调度任务。
- 在启动 worker 前获取对应资源锁。
- 管理取消令牌和暂停状态。
- 发布带任务 ID 的事件。
- 保存检查点和结果。
- 在应用启动时恢复中断任务元数据。

并发规则：

- 全局最大 active 子 Agent 数为 4。
- Explorer 与 Researcher 使用普通并发槽。
- Operator 必须额外获得 `desktop_lock`。
- Organizer 规划阶段使用普通并发槽。
- Organizer 执行阶段必须额外获得 `filesystem_write_lock`。
- 等待用户批准或等待资源锁时不占用 active slot。
- 排队默认采用 FIFO，用户手动继续的任务可进入同等级队列前部，但不能抢占正在操作桌面的任务。

### 6.3 SubagentRuntime

每个 Runtime 拥有：

- 独立 system prompt
- 独立 messages
- 独立 token 和工具轮次预算
- 独立工具 Registry 视图
- 独立取消令牌
- 独立活动序列
- 独立检查点

Runtime 不允许：

- 访问未分配的工具。
- 创建新的子 Agent。
- 直接修改主 Agent 的消息历史。
- 直接写用户画像或长期记忆。

现有 Researcher 循环首先通过适配器接入统一 Runtime/Manager，不要求一次性重写所有成熟逻辑。

## 7. 主 Agent 调度接口

### 7.1 `dispatch_subagents`

用途：

- 一次派发一个或多个可并行任务。

每个任务输入：

- `title`
- `role`
- `goal`
- 可选 `context`

约束：

- 单次最多创建 4 个任务。
- `role` 只能是固定枚举。
- 主 Agent 应只传最小必要上下文。
- 任务在持久化成功后才返回 accepted。

输出：

- `task_ids`
- 每个任务的角色和初始状态

### 7.2 `await_subagents`

用途：

- 主 Agent 对指定任务执行一次事件等待。

行为：

- 不循环调用状态工具。
- 使用 Manager 的 condition/event 唤醒。
- 全部指定任务完成时返回。
- 出现需要主 Agent 决策的不可恢复失败时提前返回。
- 用户批准、暂停或停止由 UI 直接通知 Manager，不消耗模型工具轮次。

### 7.3 `continue_subagent`

用途：

- 对已完成、失败或中断的任务进行追问或续跑。

行为：

- 保留原 `task_id`。
- `attempt` 递增。
- 注入原检查点、已确认成果和新的 instruction。
- 前端仍显示为同一任务行，并在活动时间线中分隔 attempt。

### 7.4 兼容接口

保留现有 `dispatch_research`：

- 内部创建一个 Researcher 任务。
- 等待该任务结束。
- 把统一结果转换为现有 ToolResult 字段。
- 保留 `run_id`、`resume_from`、L0/L1/L2 和报告字段。

旧提示词、自动晨间播报和现有测试在迁移期继续工作。

## 8. 数据模型

### 8.1 SubagentTask

必须包含：

- `task_id`
- `conversation_id`
- `parent_turn_id`
- `role`
- `title`
- `goal`
- `context_digest`
- `status`
- `attempt`
- `created_at`
- `started_at`
- `updated_at`
- `finished_at`
- `last_sequence`
- `checkpoint_path`
- `result_path`
- 可选 `error_category`

### 8.2 状态枚举

状态为：

- `created`
- `queued`
- `running`
- `waiting_user`
- `paused`
- `completed`
- `failed`
- `cancelled`
- `interrupted`

只有以下状态是终态：

- `completed`
- `failed`
- `cancelled`

以上“终态”针对单次 attempt；Retry 或 Continue 会创建新 attempt，并允许同一
`task_id` 重新进入 `queued`。`interrupted` 可恢复，不视为最终失败。

### 8.3 SubagentEvent

每条事件必须包含：

- `event_id`
- `conversation_id`
- `parent_turn_id`
- `task_id`
- `sequence`
- `role`
- `type`
- `timestamp`
- 已脱敏的 `payload`

`sequence` 在单个任务内严格递增。

前端忽略：

- 小于或等于已处理序号的重复/迟到事件。
- 不属于当前会话的事件。
- 缺少 `task_id` 的新协议事件。

迁移期内旧 `subagent_progress` 事件仍可映射到单个兼容 Researcher 卡片。

### 8.4 SubagentResult

统一结果包含：

- `task_id`
- `role`
- `status`
- `summary`
- `output`
- `artifacts`
- `evidence`
- `partial_result`
- `next_action`
- `stats`
- `l0_raw`
- `l1_trace`

角色扩展：

- Explorer：文件命中、路径、片段和知识库引用。
- Researcher：来源 URL、findings、完整报告和 L2 路径。
- Operator：动作记录、最终目标窗口、最后状态 hash 和验证结论。
- Organizer：manifest、manifest hash、执行结果和 rollback token。

主 Agent 默认只接收摘要、证据和档案路径，不自动拼入全部原始工具输出。

## 9. 持久化与上下文

### 9.1 存储位置

任务记录保存到 AgentAssistant 私有数据目录，不写项目根目录。

每个任务目录包含：

- `raw.jsonl`
  - L0：已脱敏的工具参数和完整结果。
- `trace.jsonl`
  - L1：每步活动摘要、状态和证据引用。
- `checkpoint.json`
  - 恢复所需的运行状态和上下文摘要。
- `output.md`
  - 最终输出。
- 可选角色档案
  - Researcher findings
  - Operator action ledger
  - Organizer manifest 和 rollback ledger

SQLite 保存：

- 任务元数据
- 当前状态
- 最后事件序号
- 档案路径
- attempt 信息

所有 SQL 必须使用参数化查询。

### 9.2 上下文管理

每个子 Agent 使用与现有 Researcher 相同的分层思路：

- 最近工具调用保留足够原文。
- 较旧工具结果折叠为 L1 摘要。
- L0 持久化保存脱敏后的完整参数和结果。
- 检查点保存当前目标、已完成步骤、未解决问题和下一步。

折叠只能减少模型上下文副本，不能删除任务档案。

## 10. 前端设计

### 10.1 状态结构

前端新增：

- `subagentsById`
- `subagentOrderByParentTurn`
- `activeOperatorTaskId`
- `pendingOrganizerApproval`

更新必须按 `task_id` 完成。

移除现有 `updateResearchProgress` 中“从后往前寻找最近 pending research step”的绑定方式。

### 10.2 扁平任务列表

每个任务行默认展示：

- 动态任务标题
- 角色名
- 当前状态
- 最新活动摘要
- 状态标签

运行状态使用 shimmer。

完成、失败、取消后停止动画并保留历史。

点击任务行展开：

- 任务目标
- 活动时间线
- 工具调用与结果摘要
- 最终结果或 partial result
- 成果物和证据
- 停止、重试、继续追问按钮

原始 L0 内容默认不展示；用户主动查看时再按页加载。

按钮按状态出现：

- `running`：停止；Operator 额外显示暂停。
- `paused`：继续、停止。
- `failed`、`cancelled`、`interrupted`：重试。
- `completed`、`failed`：继续追问。

### 10.3 Operator 控制条

当 Operator 持有 `desktop_lock` 时，在窗口顶部显示常驻控制条：

- 目标应用
- 当前动作
- 暂停
- 停止
- 用户输入可能干扰自动化的提示

控制条不得侵入其他会话的消息历史。

### 10.4 Organizer 批准卡

Organizer 进入 `waiting_user` 时显示：

- 变更数量
- 新建目录
- 源路径与目标路径
- 冲突和跳过项
- 展开后的完整清单
- 批准、拒绝按钮

用户点击批准后，前端把 `task_id`、manifest hash 和决定发送给 Manager。

点击页面其他位置或按 Esc 不得自动拒绝。

## 11. 错误与恢复

### 11.1 子 Agent 自恢复

子 Agent 可在预算内处理：

- 可重试网络错误
- LLM 单轮超时
- 临时网页读取失败
- 界面状态未变化
- 文件被临时占用

重试必须有上限，并记录原因。

不得无限重复同一动作。

### 11.2 上报主 Agent

无法自行恢复时返回：

- 已完成内容
- partial result
- 最后成功步骤
- 错误分类
- 建议的 next action
- 检查点路径

主 Agent 自主选择：

- 原任务续跑
- 调整 instruction 后续跑
- 改派另一角色
- 自己完成剩余小步骤
- 向用户询问真正缺失的授权或信息

主 Agent 不得要求用户复制任务进度文件。

### 11.3 应用重启

启动恢复流程：

1. 查询数据库中 `running`、`queued`、`waiting_user` 和 `paused` 的任务。
2. 将这些非终态任务统一标记为 `interrupted`，并保存 `previous_status`。
3. 保留原任务卡片、批准状态和检查点。
4. 向对应主会话发布恢复事件。
5. 由主 Agent 决定是否续跑；不自动恢复所有任务。

### 11.4 取消

取消流程：

- 设置任务取消令牌。
- 工具边界和 LLM 等待循环定期检查。
- 尽快释放并发槽和资源锁。
- 已产生的 partial result 和档案继续保留。
- 状态变为 `cancelled`。

Operator 取消后不承诺撤销第三方应用中已经完成的动作。

## 12. 安全要求

### 12.1 用户输入

- 角色、状态和事件类型使用枚举校验。
- 文件路径规范化后再执行 jail 检查。
- Organizer manifest 必须重新验证，不能信任前端传回路径。
- UI 自动化目标和坐标必须做类型及范围校验。

### 12.2 SQL

- 所有 SQLite 查询使用参数化语句。
- 不把 task ID、标题或搜索词拼接到 SQL 字符串。

### 12.3 密钥和 Token

- API key 只从既有安全配置读取。
- 不写入任务 prompt、活动事件、L0/L1、日志或错误消息。
- `ui_type` 内容按敏感规则脱敏。
- Organizer approval token 只保存哈希和有效期。

### 12.4 错误消息

- 继续使用统一错误脱敏。
- 用户可见错误不包含内部栈、绝对内部实现路径、数据库结构或组件版本。
- 私有诊断日志也不得记录密钥和密码。

### 12.5 文件

- Explorer 和 Organizer 均受 filesystem jail 限制。
- Organizer 批准不能绕过 jail。
- 第一版不支持删除。
- 新成果文件必须限制到用户批准目录。
- 若未来支持上传，必须单独加入类型、大小和路径限制；本设计不默认开放上传。

### 12.6 外部 URL

- Researcher 继续使用 URL guard 和 SSRF 防护。
- 不允许访问 loopback、私网和受限 scheme，除非是明确受信任的本地服务。
- 搜索结果 URL 在抓取前重新验证。
- 角色工具白名单不能绕过 URL guard。

## 13. 迁移策略

迁移分阶段完成：

1. 增加模型、Store、事件协议和前端按 ID 路由，不改变现有 Researcher 行为。
2. 用兼容适配器把 `dispatch_research` 接入 Manager。
3. 增加 Explorer。
4. 增加 Operator 与全局桌面锁。
5. 增加 Organizer 规划、审批和回滚。
6. 启用主 Agent 的多任务派发提示。
7. 删除确认无调用的旧“最近 pending research step”逻辑。

每一阶段保持现有功能可运行。

## 14. 主 Agent 调度策略

主 Agent 应派发子 Agent 的场景：

- 任务能拆成两个以上相对独立的长步骤。
- 搜索或操作会产生大量工具记录，适合隔离上下文。
- 多个只读任务可并行缩短总时间。
- 需要专业角色的权限边界。

主 Agent 不应派发的场景：

- 一次简单点击、一次文件读取或一次网页搜索即可完成。
- 模型原生能力能直接回答。
- 派发成本高于任务本身。
- 子任务必须严格依赖上一结果；此时应分阶段派发，而不是制造空等任务。

主 Agent 每次派发后：

- 用一次 `await_subagents` 等待目标集合。
- 不轮询。
- 收到结果后自行汇总和决定下一步。

## 15. 测试策略

### 15.1 单元测试

- 状态转换合法性。
- task ID 和事件 sequence。
- 事件去重。
- 角色工具白名单。
- 最大并发 4。
- `desktop_lock` 和 `filesystem_write_lock`。
- 取消令牌。
- approval token 与 manifest hash。
- 路径 jail 和规范化。
- 日志脱敏。
- 参数化数据库读写。

### 15.2 Runtime 测试

- 每个任务 messages 完全独立。
- 旧工具结果折叠但 L0 保留。
- LLM 超时和取消。
- partial result salvage。
- checkpoint 恢复。
- 子 Agent 无法调用未授权工具。
- 子 Agent 无法继续派发子 Agent。

### 15.3 角色测试

Explorer：

- 有界递归搜索。
- 跳过 jail 外路径和二进制文件。
- 返回可核对路径。

Researcher：

- 现有 robust、progress、resume 和报告测试继续通过。
- `dispatch_research` 兼容字段不变。

Operator：

- 两个 Operator 不会同时获得桌面锁。
- 暂停、继续和停止释放/恢复资源正确。
- 状态 hash 未变化时不会误报成功。
- 敏感输入不进入日志。

Organizer：

- 未批准前零写入。
- 批准后只执行 manifest 中操作。
- manifest 被修改时 token 失效。
- 回滚不覆盖已存在或已变化文件。

### 15.4 前端测试

- 四个任务事件按 task ID 更新，不串线。
- 迟到 sequence 被忽略。
- shimmer 仅在运行状态显示。
- 展开时间线和结果。
- Stop、Retry、Continue 行为。
- Operator 控制条。
- Organizer 清单批准与拒绝。
- 点击遮罩或按 Esc 不会隐式拒绝。
- 重启恢复后卡片正确显示 interrupted。

### 15.5 集成测试

代表性场景：

> 帮我找电脑里的考研资料，查今年政策，整理到一个文件夹，同时在网易云播放周杰伦的《夜曲》。

预期：

- Explorer、Researcher、Operator 和 Organizer 均有独立卡片。
- Explorer、Researcher 和 Organizer 规划阶段可并行。
- Operator 独占桌面控制。
- Organizer 在用户批准前不移动文件。
- 主 Agent 最终自动汇总，不要求用户复制进度。

### 15.6 回归测试

必须保持：

- 主 Agent 普通单任务工具调用。
- 现有 `dispatch_research`。
- 晨间播报和性能检测独立会话。
- 记忆和用户画像。
- 文件权限策略和 L2 jail。
- UI 自动化工具。
- 思考内容和活动卡片。
- 会话持久化。

## 16. 验收标准

设计完成的最低验收标准：

1. 主 Agent 能一次派发最多 4 个具名子任务。
2. 每个任务有真实独立上下文和工具集。
3. 四种角色严格遵守固定权限模板。
4. 前端精确显示和更新多个任务。
5. Operator 和 Organizer 的资源锁生效。
6. 用户可停止、重试和继续追问单个任务。
7. 子 Agent 失败返回 partial result，主 Agent 自动继续决策。
8. 应用重启后可以看到并恢复中断任务。
9. 现有 Researcher 和其他主流程回归测试通过。
10. 安全审查覆盖输入、SQL、密钥、错误、文件和 URL 六类硬要求。
