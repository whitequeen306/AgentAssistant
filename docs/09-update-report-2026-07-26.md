# 09. 更新报告 Update Report (Round 2, 2026-07-26)

> Qoder 第二轮产出后的修复报告。**给 Qoder 的 handoff 入口**:先读本报告 → 再读 `05-ui-dynamic-island.md`(UI 硬 spec)→ 需要细节查 `08-engineering-details.md`(完整修复清单)。
> 规则: **只改文档不改代码**。

---

## 1. 本轮新发现

### 1.1 UI 需要重建(根因:结构 + tokens 未定 → Qoder 自由发挥 → 丑)
- 之前 `05-ui` 只写高层概念,没给组件结构/tokens → Qoder 自由发挥,做出来丑
- **已修**:`05-ui` 全文重写,钉死 4 态模型 + 多页面 + tokens + 渲染规则 + 液态玻璃 + §5.12 问题清单

### 1.2 前端样式 bug(S 系列,新发现)
| ID | 问题 | 修法 |
|---|---|---|
| S1 | 硬编码 `rgba(255,255,255,...)` 浅模式不可见(气泡/边框/滚动条) | 全 tokens 主题变量化(`05-ui §5.10`) |
| S2 | 浅模式玻璃太透(0.12)→ 深色背景下文字不可读 | opacity 提到 0.65(`05-ui §5.10`) |
| S3 | 重复渲染 | 每消息带 id 去重(`05-ui §5.8`) |
| S4 | 语音按钮根本没做(index.html 无麦元素) | Chat/docked-search 都加语音按钮(`05-ui §5.3/5.6`) |

### 1.3 二次审计 J6-J22(前端 + 守护进程 + 语音)
见 `08 §3 J 节`。要点:J6 拖不动 / J7 collapsed 缺 / J8 点击穿透缺 / J9 液态玻璃只 backdrop-blur / J10 主题不持久 / J11 markdown 不渲染 / J12 无流式 / J13 PTT 录音不推 UI / J17 event_bus 同步阻塞 / J18 perf 阈值过高 / J19 TTS 非流式 / J20 STT base+CPU。**全部在 `05-ui §5.12` 列了对应修法**。

### 1.4 J23 文件/文件夹右键召唤流程(扩展 J1)
- 用户右键文件夹 → "交给助手" 应:**新会话 → "正在读取..." → agent 读 → "我已读取完成,可以进行我们的工作"**
- **当前断**:`main.py` 不处理 `--right-click` / 无单实例 IPC / 无召唤处理器
- spec 见 `08 §4.6`

### 1.5 Edge 选中文字右键不出现"交给助手" = J1 预测确认
- shell 文件菜单只在资源管理器出现;Edge 选中文本右键是 Edge 自画,永远不出现
- 走**浏览器扩展**(`chrome.contextMenus`),`08 §4.5` 已决

---

## 2. 已锁定决策(本轮)

| 决策 | 内容 | 位置 |
|---|---|---|
| Premise | 开源个人助手(单用户自托管) | `08 §1` |
| 05-ui 重写 | 4 态 + 多页面 + tokens + 渲染规则 + 液态玻璃 | `05-ui` 全文 |
| J1 召唤路径 | shell 文件菜单(保留+修)/ 浏览器扩展 / 全局热键;撤销 Path B | `08 §4.5` |
| J23 文件召唤流程 | main.py `--right-click` + 单实例 IPC + `invoke_on_file` 处理器 | `08 §4.6` |
| A4 分层记忆 | token 短期窗 + 滚动摘要 1200 + 重要性过滤 + 稳定画像/事件拆分 + Chroma + Agentic RAG + 语义 512+overlap + 混合检索+rerank+query 改写 + 进程内缓存 + SQLite | `08 §4.3` |
| I1 工具披露 | 19 全塞,~40-50 切渐进式,~100+ 上 RAG | `08 §4.2` |
| 文件问答 | 读全文不走 RAG(单会话临时);RAG 限持久跨会话 KB | `08 §4.5` |
| 安全红线 | B8 SSRF / B1·B2 confirm / B9 错误脱敏 发布前必修 | `08 §4.4` |

---

## 3. Qoder 下一步(按顺序)

### 3.1 前端重建(主)— 照 `05-ui` 实现,不许自由发挥
1. **4 态模型**(main / docked-search 上下边 / docked-sliver 左右边 / minimized)+ 转换 + 边缘检测 + 边缘记忆(`05-ui §5.1`)
2. **main 结构**:左侧栏(新建/搜索/历史/底部 nav)+ 内容区(`§5.2`)
3. **Chat 页**:header + chat-area(markdown+DOMPurify)+ input-area(含语音 PTT)(`§5.3`)
4. **Scene Modes 页**:列表 + 添加(可视化构造动作序列)+ 编辑/删除/测试;数据结构 `§5.4`;执行走注入 agent(agentic)
5. **Settings 页**:Provider/功能开关(10 个)/语音/主题/最小化行为/启动态/边缘吸附/数据(`§5.5`)
6. **Library 页**:笔记 + 报告 + 晨间播报历史
7. **tokens**:全主题变量化(`§5.10`),浅深两套,**禁硬编码 `rgba(255,255,255,...)`**
8. **液态玻璃**:backdrop-filter + SVG 滤镜 + 自适应 tint(`§5.9`)
9. **渲染规则**:每消息 id 去重,markdown+DOMPurify,tool 行可展开(`§5.8`)
10. **拖动**:pywebview `window.events.drag` 或 Win32 WM_NCLBUTTONDOWN,**不用 `-webkit-app-region`**(修 J6)

### 3.2 J23 后端(连带做,和前端 bridge 同区域)
- `main.py` 处理 `--right-click <path>` 参数
- **单实例 IPC**(socket/命名管道):助手已跑→转发路径;没跑→启动 UI
- **`invoke_on_file(path)` 处理器**(launch/bridge 层):建新会话 + push"正在读取文件夹/文件中..."状态 + 注入 agent
- agent 用现有 `read_file`/`list_files` 读,**不新增工具**;读后报告"我已读取完成,可以进行我们的工作"

### 3.3 §5.12 前端问题清单全修
J6/J7/J8/J9/J10/J11/J13/J15/J16 + S1/S2/S3/S4,每个在 `05-ui §5.12` 列了对应修法。

---

## 4. 仍待决(`08 §5`,Qoder 做到时停下来问用户)
- B1/B2 confirm 机制三选一(框架拦截 / 工具加 confirmed / UI 确认)
- B8 SSRF 范围(白名单域名?还是只禁私网+localhost)
- F1/F2 边缘收缩放 Python 还是 JS / 点击穿透 WS_EX_TRANSPARENT 可行性
- H2 何时切 asyncio
- A1 主 agent 模型 pro 还是 flash;是否分层(主 pro / 摘要 flash)
- A4 数值(token 预算 / 稳定画像字段表 / 轻量摘要模型 / query 改写模型)

---

## 5. 红线
- **Agentic 不许破坏**:工具调用顺序由模型推理;框架只跑 agent loop + confirm + 预算上限;不写流程编排 if/else
- **tokens 严格照 `05-ui §5.10`**,不许自由发挥颜色/字号/间距/圆角
- **用户权力**:启动态/最小化行为/边缘吸附/功能开关都给 Settings 控制权,别替用户决定
- **TDD**:每模块先写测试(状态转换 / 去重 / 边缘检测 / IPC)
- **安全**:markdown 必须 DOMPurify sanitize;B8/B1/B2/B9 发布前必修

---

## 6. 读文档顺序(给 Qoder)
1. 本报告(`09`)— 当前态 + 下一步
2. `05-ui-dynamic-island.md` — UI 硬 spec(4 态/页面/tokens/渲染规则/液态玻璃/§5.12 问题清单)
3. `08-engineering-details.md` — 完整修复清单(A-J 全项)+ 决策细节(§4)
4. `02-philosophy.md` — Agentic 红线
5. `01/03/04/06/07` — spec 参考
