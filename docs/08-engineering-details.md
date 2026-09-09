# 08. 更新修复文档 Update & Fix Document

> 一份汇总:Qoder 实现后我查出的问题 + 今下午讨论结论 + 修复清单。
> 规则: **只商讨技术细节,只改本文档,不改代码**。代码修改由你(Qoder/其他智能体)执行。
> 状态图例: ✅ 已决 / 🔴 必修(发布前) / 🟠 高 / 🟡 中 / 🔵 低 / ⏸ 延后

---

## 1. 前提 Premise(2026-07-26 锁定)

**开源个人助手**(open-source personal assistant)。

**变更历史**:
- 初版:个人助手(单用户)
- 中途考虑:多租户付费产品——经市场评估,通用 Windows AI 桌面助手大众市场小且残酷(免费竞品是微软/OpenAI/Anthropic,分发打不赢),**撤回**
- **现锁定:开源个人助手**——打强项(本地优先 + 隐私 + 可定制),绕开分发仗;freemium 付费层留作后路

**对栈影响**: 单用户/易自托管 → 简化栈(SQLite + Chroma + 进程内 LRU);MySQL/Milvus/Redis 撤回为"可选后端",不默认。

**对安全影响**: 代码公开 → B8(SSRF)/B1·B2(confirm)/B9(错误脱敏)**发布前必修**。

---

## 2. 实现现状速览

Qoder 按 `docs/01-07` 搭出骨架,结构对得上文档(`agent/` `daemon/` `llm/` `tools/` `ui/` `voice/`),19 个主 agent 工具 + 1 个子 agent 工具(`extract_content`)注册,**agentic loop 正确**(无 if/else 编排,模型自驱)。

但有:占位实现、坏掉的 confirm 流程、安全坑、未接的闭环、未实现的模块。详见下。

---

## 3. 修复清单 Fix List(按区)

### A. LLM & Agent 核心(agent/loop.py, llm/client.py, config.py, agent/prompt.py)

| ID | 严重度 | 问题 | 动作/状态 |
|---|---|---|---|
| A1 | 🟠 | `config.py` 默认 `deepseek-v4-flash`,文档定 `deepseek-v4-pro` | 待定:主 agent 用 pro?子/摘要用 flash 分层? |
| A2 | 🟠 | 无 streaming(`stream=False`),UI/持续通话拿不到中间 token | 待定:何时切流式 |
| A3 | 🟡 | `temperature=0.7` 硬编码,工具调用偏发散 | 降到 0.0-0.3?或分场景 |
| A4 | ✅ | 主 agent `_messages` 扁平无限增长,无分层记忆 | **已决**(见 §4) |
| A5 | 🟡 | LLM API 无重试,网络/限流一次就废 | 加指数退避?或交模型靠错误冒泡 |
| A6 | 🔵 | `tool_choice="auto"` 永远,无法强制首调 | 右键路径要不要 force?待定 |

### B. 工具安全与正确性(tools/*.py)

| ID | 严重度 | 问题 | 动作/状态 |
|---|---|---|---|
| B1 | 🔴 | `kill_process` confirm 流程坏:提示"retry with confirmed=true"但工具无 `confirmed` 参数,永远杀不了 | 必修;confirm 机制方案待定(框架层拦截 / 工具加 confirmed / UI 确认) |
| B2 | 🔴 | `run_command` 危险命令同样无 confirm 通道;黑名单不覆盖 `iex`/`Invoke-Expression`/`Start-Process` 等绕过 | 同 B1;补黑名单 |
| B3 | 🔴 | `set_volume` 是占位假实现,只 SendKeys 静音键,没真设音量,假 success 骗模型 | 加 pycaw?或退化为只静音 |
| B4 | 🟠 | `toggle_notifications` 改 Win10 注册表,Win11 可能无效 | 验证 Win11 路径 |
| B5 | 🟠 | `get_selection` 剪贴板备份只覆盖文本(`CF_UNICODETEXT`),图片/文件被覆盖丢 | 接受只保文本?或全格式备份 |
| B6 | 🟡 | UIA 取词用 `uiautomation` 未在 pyproject deps,静默退到 Ctrl+C | 加进 deps?或接受剪贴板法 |
| B7 | 🟡 | `extract_content` 用 lxml 未在 deps,退到 regex 严重降级 | 加 lxml?或换 BeautifulSoup4 |
| B8 | 🔴 | 无 SSRF 防护,`read_page`/`web_search` 可抓 localhost/内网 IP/`file://` | **发布前必修**;URL 校验禁私网/localhost/file 协议 |
| B9 | 🔴 | 错误消息 `{e}`/stderr 直接回模型,泄露路径/栈/版本 | **发布前必修**;对外脱敏,详细落本地日志 |

### C. 网络搜索(tools/web_tools.py)

| ID | 严重度 | 问题 | 动作/状态 |
|---|---|---|---|
| C1 | 🟠 | SearXNG URL 硬编码 `localhost:8888`(官方默认 8080),不在 config | 加 `searxng_url` 到 settings |
| C2 | 🟡 | Tavily key 走 `os.environ`,绕过 pydantic-settings | 加 `tavily_api_key` 到 settings |
| C3 | 🟡 | `read_page` 无 JS 渲染,SPA 页抓空壳 | 加 playwright(重)/requests-html?或靠 snippet 兜底 |

### D. 子 agent / 深度研究(tools/research.py)

| ID | 严重度 | 问题 | 动作/状态 |
|---|---|---|---|
| D1 | 🟠 | 子 agent 只数 turn 预算,不数 token | 加 tiktoken 计 token?或限单工具输出大小 |
| D2 | 🟡 | 子 agent 用主 LLM 单例,没分层模型 | 研究用主模型 OK;摘要器该用轻量(见 A4) |
| D3 | 🟠 | 子 agent 非流式,UI 看不到进度,几分钟像卡死 | 进度推 UI(需 bridge 接通 F2) |
| D4 | 🟠 | `dispatch_research` 把**完整报告**当 tool result 回传,未摘要化(违反 `03-architecture` spec) | 回传前摘要;A4 决策已含,落实待 |

### E. 守护进程 / 触发器(daemon/*.py)

| ID | 严重度 | 问题 | 动作/状态 |
|---|---|---|---|
| E1 | 🔴 | 晨间播报复用没接闭环:只弹通知,[预览]/[询问] 按钮+回调全空 | 接 notify actions + 框架预设回调(launch_app/create_session) |
| E2 | 🔴 | 右键大全走错路:让 LLM 自然语言问意图,非文档定的 UI 三选一 picker | 改 UI picker(灵动岛弹三按钮),不走 LLM 问 |
| E3 | 🟡 | daemon 在 event_bus 线程直接 `agent.chat()`,与 UI 线程并发污染 `_messages` | 加锁?或事件队列单线程消费 |
| E4 | 🟠 | 持续通话分层记忆(4.10.1)零实现 | A4 决策已定方案,实现待 |
| E5 | 🟡 | perf→kill 闭环断在 B1 | 随 B1 修通 |
| E6 | 🟡 | `context_menu` 注册表写入要可逆/安全 | 审注册/反注册;加卸载命令 |

### F. UI / 灵动岛(ui/*.py, ui/web/*)

| ID | 严重度 | 问题 | 动作/状态 |
|---|---|---|---|
| F1 | 🔴 | 边缘收缩未实现,只有 `set_state` 改尺寸,无边缘检测/拖拽/动画 | Python 侧还是 JS 侧?待定 |
| F2 | 🔴 | 点击穿透未实现,`transparent=True` 但窗口仍捕获所有点击 | WS_EX_TRANSPARENT 可行性待验 |
| F3 | 🟠 | UI 未接 AgentLoop,bridge 有接口但 launch.py 接线待确认 | 审 launch.py;UI↔agent↔daemon 三方接线画清 |
| F4 | 🟠 | `notify` 的 action 回调未接(晨间播报闭环 E1 依赖它) | callback 注册机制:Python 预设表,JS 点击→bridge 调 |
| F5 | 🟡 | 状态切换只 resize,无 spring 动画 | CSS cubic-bezier 还是 Web Animations API |
| F6 | 🔵 | `style.css` 液态玻璃完整度待审 | 后续单独审 |

### G. 语音(voice/*.py)

| ID | 严重度 | 问题 | 动作/状态 |
|---|---|---|---|
| G1 | 🔴 | PTT 热键释放坏:`event.name=="space"`,用户打字空格误停录音 | 用 `trigger_on_release=True` 或跟踪 ctrl+shift 状态 |
| G2 | 🟠 | 持续通话模式(4.10)整节没实现(无 VAD/turn-taking/barge-in) | phase 10,先 PTT 上线再补 |
| G3 | 🟡 | STT/TTS 非流式,PTT 延迟=录完+转写+LLM+合成+播 | PTT 接受慢?流式留给持续通话? |

### H. 横切 / 架构债

| ID | 严重度 | 问题 | 动作/状态 |
|---|---|---|---|
| H1 | 🔴 | 零测试(pyproject 配 pytest 但无 tests/ 目录) | 先补安全相关工具测(kill/run/move) |
| H2 | 🔴 | 全栈同步,流式+持续通话+UI 并发需要异步 | **架构级决策**:何时切 asyncio?趁代码量小早定 |
| H3 | 🟡 | 无 token 计数,预算只数轮 | 引入 tiktoken |
| H4 | 🟡 | 日志只 basicConfig,排障难 + B9 脱敏后要有地方落 | 上结构化日志,json formatter,落 data_dir/logs/ |
| H5 | 🔵 | 配置不统一(Tavily 走 os.environ、SearXNG 硬编码、briefing delay 不在 .env) | 全收 pydantic-settings |

### I. 工具存储与检索

| ID | 严重度 | 问题 | 动作/状态 |
|---|---|---|---|
| I1 | ✅ | 全塞 19 工具每轮重发,不 scale | **已决**:19 全塞够用,~40-50 切渐进式(方案 A),~100+ 上 RAG(方案 B) |
| I2 | ⏸ | 存储:塞上下文 vs RAG | 延后,随 I1 阈值 |

### J. 二次审计补充(前端/流程/语音/守护进程)

**流程级断裂(整个功能 end-to-end 跑不通)**

| ID | 严重度 | 问题 | 状态 |
|---|---|---|---|
| J1 | ✅ | ~~右键大全整条走错路~~ 已决(见 §4.5):浏览器扩展 + 全局热键混合;**保留** shell 文件菜单(改 framing 为"文件→助手",修 `main.py --right-click <path>`) | ✅ 已决 |
| J2 | 🔴 | **TTS 播放 100% 失败**:`tts.py` 用 `Media.SoundPlayer.PlaySync()` 放 `.mp3`,SoundPlayer 只认 `.wav`;edge-tts 输出 mp3 → 必失败 → 退找 mpv(无则静默) | 新 |
| J3 | 🔴 | **晨间播报闭环断在 notify**:NotifyTool 只有 title/body,**spec 1.16 actions 参数未实现**;PowerShell toast XML 也只两行 text 无 button binding → [预览]/[询问] 按钮做不了 | 补 F4 |
| J4 | 🔴 | **create_session 建了不切换**:SessionManager 只存 session,无"主 agent 切到新 session"逻辑 → "询问该报告"建 session 后 agent 不切过去续聊 | 新 |
| J5 | 🔴 | **UI 模式默认无 daemon/PTT**:`main.py` UI 模式只 `launch_ui()` 不带 daemon → 无晨间播报/无 perf/无语音热键;`launch.py` 也只在 `--daemon` 才启 | 新 |
| J23 | ✅ | **文件/文件夹右键召唤流程**(扩展 J1):右键文件→新会话→"正在读取..."状态→agent 用 `read_file`/`list_files` 读→报告"我已读取完成,可以进行我们的工作"。需建:① `main.py` 处理 `--right-click <path>` ② 单实例 IPC(转发路径到运行中实例)③ `invoke_on_file(path)` 框架级处理器(建会话+push 状态+注入 agent)。读取能力已有(`read_file`/`list_files`),不是新工具 | ✅ 已决(spec 见 §4.6) |

**前端样式/交互(CSS/JS/HTML)**

| ID | 严重度 | 问题 | 状态 |
|---|---|---|---|
| J6 | 🔴 | **`-webkit-app-region: drag` 在 pywebview 不工作**:Electron 专有 CSS,pywebview/WebView2 不认 → 无边框窗口**根本拖不动**。用 `window.events.drag` 或 Win32 WM_NCLBUTTONDOWN | 新 |
| J7 | 🔴 | **collapsed 状态三层全缺**:CSS 只 idle/active,JS setState 无 collapsed 分支,HTML 无 collapsed 视图 → 边缘收缩全栈空 | 补 F1 |
| J8 | 🔴 | **点击穿透 JS 侧没做**:app.js 无 pointer-events 切换,idle/collapsed 都收点击 → 配合 F2 全栈空 | 补 F2 |
| J9 | 🟠 | **液态玻璃只 backdrop-blur**:无 SVG 滤镜高光/折射/边缘光,无自适应 tint → 离 iOS 18 液态玻璃差一截 | 补 F6 |
| J10 | 🟠 | **主题不持久化**:isDark 每次重载读系统,btn-theme 切换不写 localStorage → 重载丢手动选择 | 新 |
| J11 | 🟠 | **Markdown 不渲染**:`bubble.textContent = text` 纯文本 → 研究报告/笔记显示原始 `#` `**` 符号。需 marked.js + XSS sanitize | 新 |
| J12 | 🟠 | **无流式 token 显示**:app.js 收 'response' 事件才一次性 addMessage,无逐 token | 补 A2 |
| J13 | 🟠 | **PTT 录音状态不推 UI**:hotkey 录音时不发 voice_state 给 bridge,app.js 有 voice_state 分支但无人触发 → 录音时灵动岛无"录音中"反馈 | 新 |
| J14 | 🟡 | **无语音波形可视化**:仅 status dot pulse,无 canvas 波形(spec 5.3/8.6) | 新 |
| J15 | 🟡 | **tool_result 不显示结果内容**:app.js 只显 ok/fail,不显 data → 用户看不到抓到啥/快照看到啥 | 新 |
| J16 | 🔵 | **input 单行无历史**:单行 `<input type="text">`,不支持多行/命令历史(上下键) | 新 |

**守护进程/语音**

| ID | 严重度 | 问题 | 状态 |
|---|---|---|---|
| J17 | 🟡 | **event_bus handler 同步阻塞**:`_process_loop` 单线程,handler 同步 `agent.chat()` → agent 跑 30s 期间所有触发事件排队等 | 补 E3 |
| J18 | 🟡 | **perf 阈值过高**:cpu/mem 90% 持续 10s 才触发 → 大多数异常漏(瞬时 95% 几秒就过)。降到 80%/5s? | 新 |
| J19 | 🟡 | **TTS 非流式**:整段 mp3 生成完才放,延迟=整段合成时间 | 补 G3 |
| J20 | 🟡 | **STT base+CPU**:base 模型中文一般,CPU 慢。上 small/medium?或 GPU(cuda) | 新 |
| J21 | 🟡 | **speak() asyncio.run 阻塞**:tts.py speak() 同步卡调用线程;speak_async 才起线程 | 新 |
| J22 | 🟠 | **UI 接线漏 thinking/error 转发**:launch.py on_agent_event 只转 tool_call/tool_result,不转 thinking/error(response 经 bridge._process_message 推,OK;thinking/error UI 收不到) | 新 |

---

## 4. 已锁定决策汇总

### 4.1 Premise(§1)
开源个人助手。栈简化(SQLite + Chroma + 进程内 LRU),MySQL/Milvus/Redis 为可选后端。安全发布前必修。

### 4.2 I1 工具披露
19 个暂不切渐进式,全塞够用(DeepSeek 64k+,占比 <6%)。阈值:工具数到 ~40-50 切方案 A(索引卡塞 system prompt + `get_tool_detail` 元工具),~100+ 上 RAG(方案 B,复用 Chroma)。I2 存储选型延后。

### 4.3 A4 主 agent 分层记忆(完整方案)

- **短期窗**:token 预算阈值截断(非轮数;轮子大小方差大),保句子完整,4 轮作软上限;**丢老轮前先摘要进滚动摘要再丢**(不裸丢)
- **滚动摘要**:增量追加 + 1200 token 上限 + 超阈值全量重摘要(compaction 修漂移)
- **重要性过滤**:模型判(不写规则),重要事件长期保存
- **存储拆两层**(替代"全量注入"):稳定画像(<500 token,每轮注入)+ 情节事件(向量库按需召回);**不**全量注入事件
- **向量库**:Chroma(非 Milvus);**Agentic RAG**(模型判上下文+画像够不够,不够才检索)
- **切块(MVP)**:语义 512 + overlap 64;contextual retrieval(每块 LLM 写上下文前缀)推迟到接大量外部代词重文档时
- **检索**:混合(dense+sparse BM25)+ cross-encoder rerank + query 改写(HyDE/扩写,对付含糊提问)
- **缓存**:进程内 LRU + TTL(非 Redis,单用户无并发)
- **结构化存储**:SQLite(非 MySQL),用户+会话隔离(单用户也预留)
- **子 agent 结果摘要化**(D4 落实):回传主 agent 前先摘要,不全量灌

### 4.4 安全红线(发布前必修)
- B8 SSRF:URL 校验禁 localhost/私网 IP/file 协议
- B1/B2 confirm:统一 confirm 框架(机制方案待定,见 §5)
- B9 错误脱敏:对外只返类别码,详细落本地日志

### 4.5 J1 召唤助手(三种互补路径)+ 文件问答策略(2026-07-26)

**三种"召唤助手"路径(互补,不冲突)**:
1. **Shell 文件右键菜单**(保留 Qoder 的 `context_menu.py`,改 framing):右键**文件**→"交给助手"→ `main.py` 处理 `--right-click <path>` → agent 拿到文件路径 → 可 `read_file`/摘要/翻译。**修 `main.py` 的 `--right-click` 参数处理**(当前无)
2. **浏览器扩展**(Chrome/Edge `chrome.contextMenus` API):右键**选中文字**→"交给助手"→扩展抓选中文本→发助手。覆盖浏览器主战场
3. **全局热键**(Ctrl+Shift+T 类):选中文字 + 热键 → UIA `GetSelection` / SendInput Ctrl+C 抓文字 → 助手。非浏览器兜底
- **撤销 Path B**(全局鼠标钩子拦截,侵入式)

**文件问答策略(读全文 vs RAG)**:
- 右键文件召唤 = **单会话临时性**,**不走 RAG**;agent 直接 `read_file` 把全文读进上下文,然后 Q&A
- RAG(Chroma + Agentic RAG)角色明确限定为 **持久跨会话知识库**(笔记/画像/跨会话回忆),不是一次性文件问答
- **大小护栏**:小/中文件(<~50k token)直接读全文;大文件 agent 用 `read_file` 的 `offset`/`limit` 分页,或先摘要再 Q&A;只有"用户要把文件长期存进知识库"才进 RAG

### 4.6 J23 文件/文件夹右键召唤流程(2026-07-26)

**用户意图流程**:右键文件/文件夹 → "交给助手" → **新开会话** → UI 显示"正在读取文件夹/文件中..." → agent 读 → 报告"我已读取完成,可以进行我们的工作"。

**当前断点**:`context_menu.py` 注册命令 `main.py --right-click "%1"`,但 `main.py` 不处理 `--right-click` → 点了进默认 CLI 无反应;无单实例 IPC;无召唤处理器。

**要建(3 块,均框架级,非 agent 工具)**:
1. **`main.py` 处理 `--right-click <path>` 参数**:不走默认 CLI,调 `invoke_on_file(path)`
2. **单实例 IPC**:助手已在跑(UI 开)→ 新 `--right-click` 进程把路径转发给运行中实例(socket / 命名管道 / 文件锁+信号);没在跑→启动 UI 再处理
3. **`invoke_on_file(path)` 处理器**(在 launch/bridge 层):
   - 建新会话(用 `create_session` 工具或框架直接建)
   - UI push 状态"正在读取文件夹/文件中..."(bridge.push_status)
   - 注入 agent:"User invoked you on `<path>` via right-click. Read it (file → `read_file`; folder → `list_files` then `read_file` key files). After reading, tell the user: '我已读取完成,可以进行我们的工作'."
   - agent 调 `read_file`/`list_files` 读 → 按指令报告
   - UI 显示工具调用进度(⚡ read_file / ⚡ list_files)

**读取能力复用现有工具**(`read_file`/`list_files`),**不新增 agent 工具**。新的是框架级处理器 + IPC + main.py 参数处理。

**Edge 选中文字右键**(J1 另一路径):走浏览器扩展(`chrome.contextMenus`),不是 shell 菜单——这条 J23 只管"文件/文件夹右键"(shell 菜单能覆盖的)。

---

## 5. 待最终锁定的细节

- **A4**:token 预算具体数值 / 稳定画像字段表 / 轻量摘要模型(deepseek-chat 还是本地小模型)/ query 改写模型选型
- **B1/B2 confirm 机制**:方案 1 工具加 `confirmed` 参数 / 方案 2 框架层拦截(推荐)/ 方案 3 UI 确认 —— 三选一
- **B8 SSRF 范围**:白名单域名?还是只禁私网+localhost
- **F1/F2**:边缘收缩放 Python 侧还是 JS 侧;点击穿透 WS_EX_TRANSPARENT 在 pywebview 可行性
- **H2**:何时切 asyncio(现在 / PTT 上线后)
- **A1**:主 agent 模型 pro 还是 flash;是否分层(主 pro / 摘要 flash)

---

## 6. 推荐修复顺序

1. **安全 B8/B1/B2/B9**(发布红线)
2. **架构 H2**(异步,趁代码量小先定)
3. **记忆 A4**(按 §4.3 落实)+ D4(子 agent 摘要化)
4. **流程级断裂 J1-J5**(右键改路 / TTS 播放 / notify actions / session 切换 / UI 默认带 daemon)+ 功能闭环 E1/E2/F4
5. **UI 交互**:J6(拖动)+ F1/J7(边缘收缩)+ F2/J8(点击穿透)+ J9(液态玻璃补完)
6. **语音**:G1(PTT 热键)+ J13(PTT 录音反馈)+ J2(TTS 播放)+ G2(持续通话)+ J19/J20(流式/模型)
7. 其余 Medium/Low 滚动清(J10-J12/J14-J18/J21-J22 等)

---

## 7. 约定

- 每次讨论聚焦 1-3 个 ID,定下后我在 §3 该条下追加 **决策:** 子项,并把决策汇总到 §4
- 我只改本文档(及相关 spec 文档 02/04/06 保持一致),**不改代码**;代码由你(Qoder/其他智能体)执行
- 新发现的工程细节随时追加新 ID(下一空号)

---

## 8. 实现状态核实（2026-07-28 grep + 代码核查）

> 用 grep + 代码读取核实 §3 各项的实际实现状态，区分"原评估"与"代码实状"。
> 图例: ✅ 已实现 / 🟡 部分·待验 / 🔴 未实现·占位 / ⏸ 待用户决策

| ID | 实状 | 证据 |
|---|---|---|
| A4 | ✅ | `memory/manager.py` rolling_summary + `episodic.py` Chroma+importance + `retrieval.py` dense+BM25+rerank+query_rewrite + `service.py` 接线 |
| B1/B2 | ✅ | `tools/confirm.py` ConfirmGate+UIConfirmProvider + `registry.py:67` 框架层拦截；kill_process/run_command/move_file 均 gated |
| B3 | 🔴 仍占位 | `system_tools.py:137-143` set_volume 仍 SendKeys 静音键 + warning 要 pycaw，**未真设音量** |
| B4 | 🟡 部分 | toggle_notifications 用注册表法（Win10 路径），Win11 可能无效，未验证 |
| B8 | ✅ | `tools/url_guard.py` validate_url + web_tools 已接 |
| B9 | ✅ | `tools/sanitize.py` sanitize_error 全工具覆盖（含 run_command stderr） |
| F1 | ✅ | `window.py` detect_edge_snap + JS 指针追踪拖动 morph（本次重做） |
| H1 | ✅ | `tests/` 190 测试通过 |
| I1 | ✅ | `tools/register.py` 注册 22 工具 |
| J6 | ✅ | `app.js` bindDrag JS 指针追踪（替代 -webkit-app-region / Win32 模态拖动） |
| J11 | ✅ | `app.js` marked.js + DOMPurify 渲染 |
| J15 | ✅ | `app.js` addToolRow/finishToolRow 显 tool_result |
| J16 | ✅ | `app.js` cmdHistory + ArrowUp 召回 |
| J23 | ✅ | `context_menu.py` + `main.py --right-click` + `ipc.py` + `bridge.invoke_on_file` |
| J1 浏览器扩展 | 🔴 未实现 | 无 manifest.json / 扩展目录 / native messaging — **本次将建（Edge 优先）** |

**待验（本次未核查代码，后续补）**: A1/A2/A3/A5/A6, C1-C3, D1-D4, E1-E6, F2-F6, G1-G3, J2-J5, J7-J10, J12-J14, J17-J22, H2-H5

**本次将做**: J1 浏览器扩展（Edge 优先）→ 见下方设计。B3 修复（pycaw 真音量）留作后续单独任务。
