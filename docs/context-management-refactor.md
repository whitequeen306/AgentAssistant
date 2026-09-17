# 上下文管理重构设计

> 状态：方案已定稿（2026-09-14），**阶段 1–3 已实施并通过全量测试**；阶段 4（事件流）待做。
> 动机：现有主 Agent 上下文管理被拆成约 8 个机制，概念重叠、命名混乱，且遗漏了最大的膨胀源（工具结果）。
> 参考：对标 `sst/opencode`（MIT）的 `session/compaction.ts` 与 `session/overflow.ts`。

---

## 0. 实施进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 黄金测试基线 | ✅ 现有 79 用例已覆盖（锚点/配对/快照 stub 等），新增 34 个用例 |
| 1 | 工具结果 + 参数统一截断（大类摘要 + ok + 失败分类） | ✅ `memory/tool_summary.py` + `_age_off_old_results` |
| 2a | 预算随模型上下文（85%） | ✅ `llm_context_window` / `compaction_ratio` / `effective_memory_budget` |
| 2b | 移除检查点机制，摘要改拼接 | ✅ `_absorb_dropped` 重写；进度摘要轮保留 |
| 3a | 摘要超限 → 归档最老层 + 指针行 | ✅ `memory/summary_archive.py` + `_fit_within_cap` |
| 3b | `recall_summary` 工具 | ✅ `tools/recall_summary.py`（已注册） |
| 3c | 旧归档数据清理 | ✅ 13 文件 / 517K 软删除至 `_old-archive-20260914/` |
| 4a | `events.jsonl` 事件流（11 类事件） | ✅ `memory/session_trace.py` |
| 4b | `tool_details.jsonl`（承接原 `tool_archive`，带 `T-xxxx` ID） | ✅ 同一模块；recall 读同一文件 |
| 4c | 埋点接入（loop / manager / recall） | ✅ turn·user·llm·tool·llm_error / compaction·pruning / recall |
| 4d | 真实端到端验证 | ✅ 见下方"通电验证" |
| 5 | 轮中不丢消息（原同时改的"保底按轮次"已删除，见第 7 行） | ✅ `_MIDTURN_HARD_RATIO` / `allow_dropping` |
| 6 | 预算扣掉**固定开销**（系统提示 + 工具 schema） | ✅ `memory_context_reserve` + `tests/test_context_budget.py` |
| 7 | **删除冗余的保底机制**（A/B 实验 10 场景 10 个无差异） | ✅ `_apply_turn_floor` / `soft_rounds` / `config.memory_soft_rounds` 全部移除 |
| 8 | Pruning 改按 **token**（35% 保护 / 49% 触发），不再是"保最近 4 组" | ✅ 含快照豁免与顺序修正，见下方 |
| 9 | **Pruning 统一为"老组整体降级"**：结果→一行摘要，参数→占位 | ✅ `_age_off_old_groups` 编排；`_arg_placeholder` 三字段；短参数不动 |
| 10 | **修复子 Agent 事件竞态**：`emit()` 的序号分配与推送不在同一临界区 | ✅ `_emit_lock`；新增并发顺序测试，A/B 验证（去锁必失败） |
| 11 | **子 Agent 复用主 Agent 摘要模板** | ✅ `runtime.py` 删自研 `_tool_summary`，改用 `summarize_tool_result`；工具分类表补到 0 未覆盖 |

**已验证**：全量 1168 用例通过（含新增 62 个）。

**通电验证**（`scratch/verify_trace_e2e.py`，真实模型 + 真实工具，非 mock）：

```
已注册工具数: 38
turn=1 tool  ok=true | 列出 36 项，27 目录 / 9 文件，.   [ref=T-0001]
turn=2 tool  ok=true | 读了 src/agent_assistant/memory/tool_summary.py，1 行  [ref=T-0002]
→ <data_dir>/sessions/verify-session/{events.jsonl, tool_details.jsonl} 均已落盘
```

⚠️ 首次验证踩到的坑（值得记住）：脚本只 import 了 `AgentLoop` 而**没调 `register_all_tools()`**，
`tool_registry.openai_schemas()` 返回 0 个 → 模型拿不到 tools → 它把工具调用**写成正文标记**，
于是既没有 `tool` 事件也没有 `tool_details.jsonl`。真实入口是 `ui/launch.py` 的
`register_all_tools()`——**脱离应用入口直接驱动 loop 时，必须自己先注册工具**。

同一次验证还暴露了摘要文案质量问题（`list_files` 只显示"检索完成"、长路径把"行数"挤掉），
已一并修掉（见 §6）。


---

## 1. 现状与问题

现有主 Agent 侧机制（`src/agent_assistant/memory/manager.py`、`agent/loop.py`）：

```
Pass 0   _stub_superseded_snapshots    快照类工具结果替换为 stub（每轮，无门槛）
Pass 0b  _age_off_old_arguments        老组参数截断到 200 字符（每轮，无门槛）
Pass 1   _find_split_point             按 token 预算从末尾切分（超预算 + 10% 缓冲）
         _absorb_dropped               丢弃部分吸收进 rolling_summary（含检查点原文合并）
         摘要上限                       rolling_summary ≤ 1200 token，超限整段再压
         锚点保护                       最后一条 user 消息永不丢
         安全边界                       切分点不能落在 tool 消息上
         tool_archive + recall_tool_result   完整结果归档 + 可召回
```

**四个真实问题**：

1. **最大膨胀源没人管**：工具**结果**在主 Agent 侧完全不截断（子 Agent 有 6000 字符上限，主 Agent 没有）。反而去截"参数"（通常只有几十字符）——**方向反了**。
2. **没有"行动门槛"**：Pass 0 / 0b 每轮无条件全量遍历，长会话下是白白的 O(n) 开销。
3. **概念混乱**：stub / stale / aging / absorb / checkpoint 各自为政，看不出主线。
4. **阈值不随模型走**：token 预算是配置死值，换模型不自动适配。

---

## 2. 目标架构：两层 + 归档

```
┌─ Pruning（低损 · 廉价 · 后台）───────────────────┐
│  只动工具结果，不动对话消息                        │
│  按工具大类生成固定摘要（零成本）+ 兜底 LLM        │
│  按体量阈值触发，turn 结束时后台执行               │
└──────────────────────────────────────────────┘
                    ↓ 压力继续增大
┌─ Compaction（有损 · 昂贵 · 同步阻塞）──────────────┐
│  整段历史（对话 + 工具）→ 模型生成摘要              │
│  摘要拼接（不二次压缩），超限则移入归档              │
└──────────────────────────────────────────────┘
                    ↓ 兜底
┌─ Archive + Recall（按需 · 保留我们相对 OpenCode 的优势）┐
│  events.jsonl / tool_details.jsonl / summaries/       │
│  recall_tool_result + recall_summary                  │
└──────────────────────────────────────────────┘
```

**与 OpenCode 的关键差异（要守住的）**：它清掉的内容**没有任何取回通道**；我们有。这是必需的，因为它的场景（代码）可重放，我们的场景（调研网页、UI 截图）不可复现。

---

## 3. 参数（定稿）

| 项 | 值 | 说明 |
|---|---|---|
| **输入触发线** | 模型上下文 **85%**（128K → 108,800） | 不能用 95%：压缩时需装下"待压缩历史 + 系统提示 + prompt + 摘要输出"，贴上限会导致摘要请求自身溢出 |
| **固定开销预留** | **15,000**（`memory_context_reserve`） | 实测：系统提示词 ~2.7K + 工具 schema ~8.7K（38 个工具）+ 动态画像/摘要余量。**2026-09-14 补**——见下方"预算没扣固定开销" |
| **消息预算**（manager 实际可用） | 触发线 − 固定预留 = **93,800** @128K | 这才是 memory manager 能花的钱 |
| **输出预留** | 剩余 ~15%（≈19.2K） | 输出 + 压缩时的摘要器调用 |
| **保留窗口** | **从尾部尽量多留，装不下就停**（唯一规则） | 见下方"保底机制已删除"——曾经还有一条"至少保 N 轮"的下限，A/B 实验证明它从不改变结果 |
| **轮中保护** | 用户轮进行中**不丢消息**（只做无损瘦身）；超过预算 **1.5×** 才允许丢 | 中途丢消息会让模型失去"自己正在做的事"的开头——它还能看到最新工具结果，却看不到产生这些结果的步骤 |
| **Pruning 保护线** | 可用预算 **35%** | 从最新往回累计**整组成本**（参数 + 结果），35% 以内的组原样不动 |
| **Pruning 行动门槛** | **保护线的 40%**（≈可用预算 14%） | 可清理量不足则不动手，避免为小事折腾 |
| **老组降级形态** | 结果 → 一行摘要；参数 → 占位 `{"_aged":true,"_call_id":…,"_hint":…}` | **没有"半截"这第四种形态** |
| **两条下限** | 参数 < 120 字符不动、结果 < 120 字符不动 | 它们本身就是一句话；摘要要加标记 + 召回指针，压了反而更长（实测结果 82→114 字符） |
| **摘要阈值** | ~8K token | 超阈值 → 保留最近 30%，多出的移入 `summaries/` |
| **兜底摘要长度** | ≤100 字（LLM 生成） | 仅用于大类模板覆盖不到的工具 |

**Pruning 按 token 而非按条数**（2026-09-14 修正）。实现里一度写成"保最近 4 组"——
这和"保底按 8 条消息"是同一类错误：**条数不反映真实体量**。实测
（`scratch/probe_prune_by_token.py`，budget 6,000 → 保护线 2,100 / 门槛 840）：

| 场景 | 工具结果累计 | 新规则（按 token） | 旧规则（保 4 组） |
|---|---|---|---|
| 8 条短结果 | 360 tok | **压 0 条** | 压 4 条（白丢信息） |
| 8 条大结果 | 6,296 tok | **压 6 条** → 1,964 | 压 4 条 |
| **3 条超大结果** | 7,616 tok | **压 3 条** → 200 | **压 0 条**（3 < 4 组 → 直接撑爆） |

最后一行是为什么必须改：只要"组数不够"但单条极大，旧规则一条都不压 —— 上下文就这么被撑爆。

**两个连带修正**（都是这次改动才暴露的）：
- **快照豁免**：`ui_inspect` 这类快照结果归 Pass 0 管（它有自己的"同窗口取新弃旧"规则），
  结果摘要**不碰它** —— 否则模型正在读的控件树会被压成一行。
- **执行顺序**：结果摘要必须在参数截断**之前**。摘要生成器要读参数才能写出
  "读了 `<path>`" / "来自 `<URL>`"；先截参数，摘要会全部退化成 `read_file(...) → ok`。

**预算没扣固定开销**（2026-09-14 修正）。`effective_memory_budget` 曾是
`llm_context_window × compaction_ratio` = 108,800，但那是**整个请求**的预算：窗口
还要装系统提示词（实测 ~2.7K）和 38 个工具的 schema（~8.7K）。实测最坏
`108,800 + 11,410 = 120,210`（93.9%），再加压缩的 10% 缓冲带 `= 131,090`（**102.4%**）
——从"该压缩了"变成"直接溢出"。现在扣掉 `memory_context_reserve`（15,000）：
消息预算 93,800，最坏水位 89.5%，留 13.4K 给输出。
（日志查证：旧预算 12K 时**从未**出现超限——12K + 11.4K 离窗口还远；这个缺陷是改成
85% 时引入的，且当时尚未真实运行过。回归锁见 `tests/test_context_budget.py`。）

**保底机制已删除**（2026-09-14）。这段历史值得完整记下：

1. 原实现是 `min_keep = soft_rounds × 2` 条消息。在 agentic 循环里是错的——一个用户轮次
   = 1 条 user + N 组 `assistant(tool_calls) + tool`，**随手超过 8 条**。实测当前轮 31 条消息时，
   "保 8 条"只覆盖最后 4 组工具调用 → 当前轮前半段被摘要掉。
2. 于是改成按 `user` 消息定位轮次（至少保最近 2 个 user 轮次），并加了"装不下就让位预算"的检查。
3. **然后 A/B 实验证明它从头到尾都没起作用**：同一批场景分别用 `soft_rounds=2` 与
   `soft_rounds=0` 跑，比较消息数 / token / 各轮存活 —— **10 个场景 10 个完全相同**
   （含专门构造的并行工具调用场景，那是最容易触发边界调整的形态）。
4. 原因：`_find_split_point` 的预算切分本身就是"从最新往回累加，加到不能再加"，
   **天然保留尽可能长的尾部连续段**。保底想要的"最近 N 轮"就在尾部——装得下时本来就在窗口里，
   装不下时保底自己也会放弃。**两种情况下都不产生任何效果。**
5. 已删除：`_apply_turn_floor()`、`soft_rounds` 构造参数、`config.memory_soft_rounds`。

⚠️ 这是本项目第 4 次遇到同族问题，且是最隐蔽的一种：**机制存在、每轮都在跑、有返回值，
但分支永远不会产生与"不做"不同的结果**。静态分析和覆盖率都查不出来（覆盖率只显示"这行执行过"），
只能靠 **A/B 对照实验**（关掉它，看结果是否变化）才能发现。

**删除后靠什么保护**（三者独立存在，与保底无关）：
| 机制 | 位置 | 保护什么 |
|---|---|---|
| **轮中不丢** | `allow_dropping=round_idx==0` | 当前轮进行中根本不丢消息——防"当前轮被切"的主力 |
| **锚点** | `maybe_compact` | 最后一条 user 消息永不留失（"任务失忆不可恢复"） |
| **最后完整组回退** | `maybe_compact` | 至少让模型看到自己最近的结果（防 API 400） |

**两个"百分比"要分清**：Pruning 的 35% 是"保留线"，Compaction 的 30% 是"摘要层归档时保留的比例"；
前者统计**工具调用组的成本**（参数 + 结果），后者是滚动摘要。

**保护线统计整组成本，不是只算结果**（2026-09-15 修正）。最初按"只统计工具输出"实现，实测翻车：
一次真实构造里参数占 9,510 tokens、结果只占 1,686——结果累计没过保护线，**Pruning 一次都没触发**，
而真正吃预算的参数完全不受约束。所以判定口径改为整组（参数 + 结果），与"整组降级"的动作口径一致。

---

## 4. 目录结构

```
<data_dir>/
├── sessions/
│   └── <session_id>/              # 一次会话一个目录（安全的目录名 + 冲突哈希后缀）
│       ├── events.jsonl           # 事件流：发生了什么（一句话摘要）
│       ├── tool_details.jsonl     # 完整参数 + 完整结果（不截断，带 T-xxxx ID）
│       ├── details/
│       │   └── T-0007.json        # 超大结果（>1MB）单独落盘，记录里只留指针
│       └── <stem>-<stamp>-N.jsonl # 轮转文件（超过 5MB 时）
├── summaries/                     # 超限摘要原文（永不删除）
└── research_runs/                 # 子 Agent 调研归档（保持现状，不动）
```

**为什么按会话分目录**（而不是 §4 早期草稿里的"一个 events.jsonl"）：两个文件都要按会话隔离
（`recall_tool_result` 的语义就是"仅当前会话"），分目录天然满足，且轮转、清理、审计都以会话为单位。
文件超过体积上限时重命名为带时间戳的文件并开新文件（保证读取有界，沿用原 `tool_archive` 的轮转做法）。

**分层原则**：`events.jsonl` 记**事件与一句话摘要**（小、可整读），`tool_details.jsonl` 存**正文**
（大、按 ID 取）。两者分工不重叠——这正是不把工具正文塞进事件流的原因（否则等于存了两份）。

---

## 5. 字段定义

### events.jsonl（每行一条）

```jsonc
{
  "v": 1,
  "ts": "2026-09-14T15:41:00+08:00",
  "session_id": "…",
  "turn": 12,                 // 轮次序号
  "event": "tool|user|llm|llm_error|compaction|pruning|recall|turn_start|turn_end",
  "ok": true,                 // 是否成功（必带）
  "error_category": null,     // 失败分类（见下）
  "summary": "读了 src/loop.py，412 行",   // 一句话摘要
  "detail_ref": "T-0007",     // 指向 tool_details 的 ID（工具事件才有）
  "tokens": { "total": 41200, "input": …, "output": … },
  "model": { "provider": "deepseek", "id": "deepseek-chat" }
}
```

**事件类型（11 类）**：

| 事件 | 记录时机 |
|---|---|
| `session_start` / `session_end` | 会话起止（带时间戳） |
| `turn_start` / `turn_end` | 每轮边界（便于按轮回溯） |
| `user` | 用户消息（记摘要，不记全文） |
| `llm` | 模型回复（记摘要） |
| `tool` | 工具调用（记大类摘要 + ok + 失败分类 + detail_ref） |
| `llm_error` | 模型调用失败（分类） |
| `compaction` | **触发时机、压缩范围（起止 turn）、生成摘要 ID、压缩前 token** |
| `pruning` | **哪些结果被截断、原始长度**（配合 recall 定位） |
| `summary_archive` | **哪些摘要层被移入 `summaries/`**（配合 `recall_summary` 定位） |
| `recall` | 模型召回了什么（审计用） |

`compaction`、`pruning`、`summary_archive` 三类**必须记**——否则事后无法解释"这段内容为什么没了"。

### tool_details.jsonl（每行一条）

```jsonc
{
  "id": "T-0007",
  "session_id": "…",
  "call_id": "…",
  "ts": "…",
  "tool": "read_file",
  "arguments": { "path": "src/loop.py" },   // 完整，不截断
  "ok": true,
  "result": { … }                            // 完整结果，不截断
}
```

超大结果（如超过 1MB）写入独立文件，此处只存指针。

---

## 6. 工具大类固定摘要模板

统一格式：`ok=<bool> | <摘要正文>`，失败时摘要位置放**原因分类**。

| 大类 | 成功摘要 | 失败摘要 |
|---|---|---|
| 文件读取 | `读了 <路径>，<N> 行 / <N> 字符` | `读 <路径> 失败：<分类>` |
| 文件写入 | `写入 <路径>，+N / −M 行` | `写 <路径> 失败：<分类>` |
| 搜索列举 | `命中 N 处，前三：<路径:行号>…` | `搜索失败：<分类>` |
| **列目录**（`list_files`） | `列出 N 项，X 目录 / Y 文件，<路径>` | `列出失败：<分类>` |
| 网络搜索 | 前 3 条，每行 `标题 — URL` | `搜索失败：<分类>` |
| 网页读取 | `<标题>，<N> 字符，来自 <URL>` | `读 <URL> 失败：<分类>` |
| 命令执行 | `执行 <命令前 N 字符>，退出码 X，输出 <N> 行` | `执行失败（退出码 X）：<末 N 行>` |
| **UI 快照** | `窗口「标题」，<N> 个可交互元素` | `快照失败：<分类>` |
| UI 操作 | `点击 <元素> / 输入 <前 N 字符>` | `操作失败：<分类>` |
| 知识库检索 | `命中 N 片段，来自 <文档名>…` | `检索失败：<分类>` |

**两条文案规则**（真实验证后补的，见 §0"通电验证"）：
1. **长路径保留尾部**：绝对路径动辄八九十字符，直接用 `_short(...,60)` 截断会变成
   `C:\Users\hp\Desktop\...\memor…` —— 既看不出文件名，又把后面的"N 行"挤掉。
   改为 `_display_path()` 只留 `…/src/agent_assistant/memory/tool_summary.py`。
2. **列目录不是"命中"**：`list_files` 报条数与目录/文件构成（`列出 36 项，27 目录 / 9 文件`），
   而不是笼统的"检索完成"——后者等于没给信息。

| 子 Agent | `调研完成，报告 <N> 字符，<M> 条来源` | `未完成：<分类>` |
| 兜底 | LLM 一句话（≤100 字） | 同上 |

**三条硬规则**：

1. **必带 `ok` 字段**
2. **失败写分类值，不写原始报错**——沿用现有 `tools/failure_feedback.py` 的分类（超时 / 反爬 / 读不出内容 / SSRF / 权限 / 配额 / 熔断）。原始报错（"HTTP 500"）模型看不出该怎么办；分类文案带行动指引。
3. **UI 快照走丢弃分支**：同一窗口出现新快照 → 旧的**连摘要都不留**（完全无用），这是比摘要更彻底的处理，要保留现有 Pass 0 的能力。

---

## 7. Recall 工具（两个，必须成对）

| 工具 | 用途 |
|---|---|
| `recall_tool_result`（已有） | 取回被截断/清空的**工具结果** |
| `recall_summary`（新增） | 取回被移入 `summaries/` 的**历史摘要** |

两者的描述里都要写明：
- 三种用法（按 ID 精确取 / 按关键词或工具名搜 / 列出最近）
- 长内容分页（offset）
- **"这是昂贵操作，仅在必要时使用"**（防止模型无脑召回，把省下的空间又吃回去）
- 仅限当前会话

**指针行（关键）**：摘要被移走后，保留在上下文里的那部分**末尾必须留一行**：

```
（更早的摘要已归档至 summaries/<id>.md，可用 recall_summary 查看；覆盖 turn 1–45）
```

没有这行，模型不知道归档存在 → 归档等于没有。

---

## 8. 迁移步骤（渐进式，不一次性重写）

**阶段 0：锁住现状（先做）**
- 为现有压缩行为写"黄金测试"（输入固定消息序列，断言压缩后的消息结构与摘要内容）
- 目的：后续每一步改动都能验证行为是否等价

**阶段 1：加工具结果截断（收益最大、改动最小）**
- 在 `agent/loop.py` 写回 tool 消息处加截断（对齐大类模板）
- 补齐 `ok` + 失败分类
- 保留现有 Pass 0（UI 快照）

**阶段 2：改触发方式**
- Pruning 从"每轮无条件扫描"改为"体量阈值触发 + turn 结束后台执行"
- Compaction 触发线改为按模型上下文百分比（85%），输出独立预留

**阶段 3：调归档与摘要策略**
- 引入 `events.jsonl` / `tool_details.jsonl` / `summaries/`
- 摘要改为拼接 + 超限归档（不再"整段再压"）
- 新增 `recall_summary`
- 移除/收敛旧的 checkpoint 机制（评估后再定，见 §9）

---

## 9. 风险与待定项

| 项 | 说明 |
|---|---|
| **检查点机制何去何从** | 现有"每 20 轮强制进度报告"OpenCode 没有。价值是"在完整上下文下写摘要"（质量高）；代价是多一套状态。建议先保留，观察阶段 3 之后是否仍有必要；若要简化，它是最先可砍的 |
| **黄金测试的成本** | 写测试本身要花时间，但不写等于盲改。这一步不能省 |
| **85% 是否仍偏激进** | 上线后应记录"压缩时的实际 token 占用"，若频繁出现压缩失败，降到 80% |
| **30% 保留比例** | 需实测召回频率；若模型频繁召回归档，说明保留太少，应上调 |
| **子 Agent 侧暂不动** | `research/` 的 L0/L1/L2 与 `subagents/archive.py` 保持现状，本次只改主 Agent 侧 |

---

## 10. 与现状的映射（便于实施时定位）

| 新机制 | 对应/替代现有 |
|---|---|
| Pruning（大类摘要 + 阈值触发） | 替代 `_age_off_old_arguments`，保留 `_stub_superseded_snapshots` 作为快照分支 |
| Compaction（85% + 30% 尾部 + 拼接） | 替代 `maybe_compact` 的预算逻辑与"整段再压" |
| 保留最近 2 个 user 轮次 | 替代/简化 `soft_rounds` 与锚点逻辑（保留锚点语义） |
| events.jsonl | 替代 `tool_archive` 的事件维度（扩展事件类型） |
| tool_details.jsonl | 承接 `tool_archive` 的完整结果存储 |
| summaries/ + recall_summary | 新增 |

---

## 11. 阶段 4（已完成）：events.jsonl 事件流

**目标**：把"发生过什么"完整落盘，使事后能解释「这段内容为什么没了」。

**实现**：`src/agent_assistant/memory/session_trace.py`（写入器 + 读接口），
`tools/tool_archive.py` 变为它的读写门面（recall 工具读同一份 `tool_details.jsonl`）。

**已接入的埋点**：

| 事件 | 接入点 | 携带 |
|---|---|---|
| `turn_start` / `turn_end` | `loop.achat`（含异常路径，`ok=false`） | turn 号、用户输入摘要 / 回复长度 |
| `user` | `loop.achat` | 消息摘要、字符数 |
| `llm` | `loop._run_rounds`（工具轮 + 最终回答各一处） | 轮号、工具名列表 / 最终答复摘要、`final_answer`、token 快照、模型 id |
| `tool` | `loop._run_rounds` 工具执行后 | **与上下文里完全相同的一行摘要**、ok、失败分类、`detail_ref`（指向 `tool_details.jsonl`）、轮号 |
| `llm_error` | `loop._run_rounds` 的 `achat_stream` 异常处 | 异常类型、模型 id |
| `compaction` | `manager.maybe_compact` 丢消息后 | 丢弃/保留条数、压缩前窗口 tokens、摘要 tokens |
| `pruning` | `manager.maybe_compact` 的 Pass 0b/0c 之后 | 结果改摘要条数、参数截断组数 |
| `summary_archive` | `manager._fit_within_cap` | 归档层数、文件名、压缩后摘要 tokens |
| `recall` | `recall_tool_result` / `recall_summary` | 模式（fetch/search/read/list）、命中与否、目标 call_id / 文件名 |

**未接入（有意为之）**：
- `session_start` / `session_end`：loop 的构造与销毁由 `agent/pool.py`、`ui/bridge.py` 掌管，
  没有单一生命周期钩子；用 `turn_start`/`turn_end` 已足以按轮回溯。
- `tool_details.jsonl` 不重复记正文到事件流——这是分层的关键（见 §4 末尾）。

**两个实现约定**（都来自踩坑）：
1. **写入永不抛异常**。`SessionTrace.event/tool_detail` 内部吞掉所有异常——日志/追踪失败绝不能影响
   正在跑的工具调用或回合（沿用原 `tool_archive.record` 的契约）。
2. **测试环境必须重定向**。`tests/conftest.py` 新增 autouse fixture，把 `session_trace` 指到 tmp，
   否则任何 `AgentLoop()` 的单测都会往真实 `~/AgentAssistant/sessions` 里写事件。

## 12. 实施后的行为变化（给使用者看）

| 变化 | 影响 |
|---|---|
| 预算从固定 12K → 模型上下文 × 85% | 128K 模型下预算 ≈ 108.8K，压缩触发大幅推迟；换更大模型自动受益 |
| 老工具结果换一行摘要 | 上下文里"调过什么、成没成"仍可见，正文改由 `recall_tool_result` 取回 |
| 摘要改为拼接，超限归档 | 旧摘要层永不被重写（防失真）；超限的部分移入 `summaries/`，留指针行 |
| 检查点机制移除 | 进度摘要轮（每 20 轮）保留——它负责打断工具乒乓；只是不再登记为检查点 |
| **保底按轮次** | 压缩时至少保住最近 2 个**用户轮次**（原为 8 条消息）；当前轮不再被切掉前半段 |
| **轮中不丢消息** | 用户轮进行中只做无损瘦身；要丢也等下一轮开始——模型不会中途失去自己的上下文 |
| **保底已删除** | 切分只剩一条规则："从尾部尽量多留，装不下就停"。少一层概念，行为不变（A/B 10/10 一致） |
| **预算扣掉固定开销** | 消息预算 108.8K → **93.8K**：系统提示 + 38 个工具 schema + 压缩缓冲带本来会把请求顶到 102% 窗口（溢出） |
| 旧 `tool_archive` 数据 | 软删除至 `_old-archive-20260914/`，可随时彻底清掉 |

**可调配置（.env）**：
```
LLM_CONTEXT_WINDOW=128000   # 按实际模型调整（DeepSeek 系列通常 64K~128K）
COMPACTION_RATIO=0.85       # 触发线占比；频繁出现压缩失败就降到 0.80
MEMORY_TOKEN_BUDGET=0       # 0 = 自动按上面的公式算；>0 则固定使用该值
```

