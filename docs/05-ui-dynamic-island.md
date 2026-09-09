# 05. Front-end UI / Dynamic Island (前端 UI / 灵动岛)

> 视觉语言: 灵动岛 + 液态玻璃(iOS 18 Liquid Glass)
> 强调色: `#007AFF`(iOS 系统蓝,Settings 可改)
> Chat 区密度: 极简
> 结构: **多页面 app**(非简单 3 态),灵动岛液态玻璃只是视觉语言
> 实现栈: pywebview + WebView2 + HTML/CSS/JS(见 `03-architecture.md`)

---

## 5.1 状态模型 State Model

四态 + 转换。默认启动 = **minimized**(最不扰民)。

| 状态 | 形态 | 触发 |
|---|---|---|
| **main**(主页) | 完整 app:左侧栏 + 内容区(§5.2) | 默认启动?否,启动即 minimized;从 minimized/docked 点开/拖出进入 |
| **docked-search**(贴上下边) | 搜索栏:"让我来帮你全网搜索一些事情?" + 输入框 + 发送 + 语音(点击 PTT) | main 拖到屏幕**上下**边界 |
| **docked-sliver**(贴左右边) | 一条细缝拖柄(无输入) | main 拖到**左右**边界 |
| **minimized**(最小框) | 小输入框:"随便问我一些事情?" + 发送 | 点最小化;启动默认态 |

**转换**:
- `main` ↔ `docked-search`(拖到上下边 / 拖出)
- `main` → `docked-sliver`(拖到左右边)→ 拖回 main
- `main` → `minimized`(点最小化按钮)
- `minimized` / `docked-search` / `docked-sliver` → `main`(点击 / 拖出)

**用户权力**(Settings 可配):
- 默认启动态: minimized / main / docked-search
- 最小化行为: 保留小框 / 隐藏到托盘
- 边缘吸附开关: 开 / 关(关了就不自动收缩)

---

## 5.2 主页结构 Main View

```
┌─ 左侧栏 (240px) ──┬─ 内容区 (flex 1) ─────────┐
│ + 新建会话       │                            │
│ 🔍 搜索会话(按标题)│   当前页:                  │
│ ─────────       │   Chat / Scene Modes /      │
│ 会话历史列表     │   Library / Settings /      │
│ · 会话A(第一句话)│   About                    │
│ · 会话B         │                            │
│ · 会话C         │                            │
│ ...             │                            │
│ ─────────       │                            │
│ [💬][🎯][📚][⚙] │ ← 底部 nav 图标(48px)      │
└─────────────────┴────────────────────────────┘
```

- **左侧栏**: 新建会话 / 搜索会话(按会话标题) / 会话历史列表(标题 = 该会话用户第一句话,超长截断) / 底部 nav 图标(Chat / Scene Modes / Library / Settings)
- **内容区**: 显示当前选中页
- 会话历史持久化到 SQLite;点击切换会话;右键可重命名/删除/固定

---

## 5.3 页面集 Pages

| 页 | 内容 | 必要 |
|---|---|---|
| **Chat**(默认) | 当前会话对话气泡 + 输入框 + 发送 + 语音(点击 PTT) + 工具状态条 | 核心 |
| **Scene Modes** | 已添加场景列表(名称+触发词预览) + 添加/编辑/删除/测试 | §5.4 |
| **Library** | 已存笔记 + 历史研究报告 + 历史晨间播报;可重看/重问/导出 | save_note 产物的家 |
| **Settings** | §5.5 | 必要 |
| **About** | 版本 / 开源链接 / 致谢 | 小 |

**Chat 页布局**(极简):
```
┌─ header (40px) ──────────────────────┐
│ 会话标题          [◐主题][🎤语音][─最小] │  ← header 控件
├──────────────────────────────────────┤
│ chat-area (flex, scroll)             │
│ · user bubble                        │
│ · assistant bubble (markdown 渲染)   │
│ · tool status (⚡ 调用中 / ✓ 完成)    │
├──────────────────────────────────────┤
│ [input]........................[↑][🎤]│  ← 输入区:输入 + 发送 + 语音
└──────────────────────────────────────┘
```

**Chat 页控件清单**(全部要实现,Qoder 别漏):
- header: 会话标题 / 主题切换 / 语音模式切换 / 最小化
- chat-area: user/assistant/system/tool 四类气泡,assistant 走 markdown 渲染(§5.8)
- input-area: 输入框(Enter 发送,Shift+Enter 换行) / 发送按钮 / 语音按钮(点击 PTT,§5.7)

---

## 5.4 Scene Mode 实现

**数据结构**(存 SQLite `scenes` 表):
```json
{
  "id": "scene-001",
  "name": "游戏模式",
  "trigger_phrases": ["游戏时间", "开逆战"],
  "actions": [
    {"tool": "launch_app", "args": {"name": "逆战"}},
    {"tool": "kill_process", "args": {"name": "chrome.exe"}, "confirm": true},
    {"tool": "set_volume", "args": {"level": 80}},
    {"tool": "toggle_notifications", "args": {"state": "on"}}
  ],
  "created_at": "2026-07-26T..."
}
```

**执行流(agentic,符合 `02-philosophy`)**:
- 用户输入匹配某场景的 `trigger_phrases` → 框架**不直接执行**,注入给 agent:
  > "User triggered scene '游戏模式'. Scene definition: [actions]. Execute it, respecting safety confirm rules (kill_process 等需 confirm)。"
- agent 调对应工具执行(可适应上下文,如某应用没开就跳过 kill)——场景只是"用户预设快捷指令",不是硬编码流程
- 危险动作仍走 confirm 框架(B1/B2 修后)
- 执行结果回到当前会话 chat-area

**Scene Modes 页交互**:
- 列表:每个场景卡片(名称 + 触发词预览 + 动作数) + 编辑/删除/测试按钮
- 添加场景:名称 + 多个触发词 + 动作序列(每条:下拉选 tool → 填 args,可视化构造,不写 JSON)
- 测试运行:直接注入 agent 跑一次,结果回 Chat 页

---

## 5.5 Settings 页面内容

| 区 | 配置项 | 默认 |
|---|---|---|
| **Provider** | DeepSeek API key / base_url / 模型(pro/flash) | key 空 / `https://api.deepseek.com` / pro |
| **功能开关** | 10 个功能各一 toggle(右键大全/晨间播报/perf 监控/语音 PTT/持续通话/深度研究/...) | 全开 |
| **语音** | PTT 热键 / STT 模型(base/small/medium) / TTS 语音 | Ctrl+Shift+Space / base / zh-CN-XiaoxiaoNeural |
| **主题** | 浅 / 深 / 跟随系统 + 强调色 | 跟随系统 / `#007AFF` |
| **最小化行为** | 保留小框 / 隐藏到托盘 | 保留小框 |
| **启动态** | minimized / main / docked-search | minimized |
| **边缘吸附** | 开 / 关(关了不自动收缩) | 开 |
| **数据** | 数据目录 / 笔记目录 / 导出 / 清空 | ~/AgentAssistant / 同左 |
| **关于** | 版本 / 开源链接 | — |

Settings 持久化到 SQLite `settings` 表(key-value)。改了即时生效,不重启。

---

## 5.6 边缘收缩与最小化 Edge & Minimize

**docked-search**(贴上下边):
- 宽度:屏宽 60%,居中,贴边
- 内容:占位文字"让我来帮你全网搜索一些事情?" + 输入框 + 发送 + 语音(点击 PTT)
- 用户点输入框 → 激活打字 → 发送 = 走 web_search 或 dispatch_research(由 agent 判)
- 拖出搜索栏 → 回 main

**docked-sliver**(贴左右边):
- 宽度 8px,贴边,无输入,只作拖柄
- 拖出 → 回 main

**minimized**(最小框):
- 小输入框:"随便问我一些事情?" + 发送
- 无语音按钮(保持最小)
- 设置"隐藏到托盘"开 → 最小化只留托盘图标,无小框

**边缘检测**:`main` 状态监听窗口位置,接近屏上下左右阈值(~20px)→ 触发吸附动画 → 切到对应 docked 态。吸附有边缘记忆(下次展开沿同方向)。

---

## 5.7 语音交互(Voice)

**Chat 页语音按钮**:点击式 PTT(暂时,**不做长对话流**——长对话流 G2 推迟)
- 点下 → 录音(状态:语音按钮变红 + 灵动岛显示"录音中")
- 松开 → STT → 文本进输入框(用户可编辑后发送)或直接发送(设置可配)
- PTT 热键(默认 Ctrl+Shift+Space)同效

**docked-search 语音按钮**:同 Chat 页,点击式 PTT

**渲染**:录音时主区显示音量波形(Canvas,§5.10),松开转写后波形消失

---

## 5.8 渲染规则 Render Rules(防重复渲染 S3)

- **每条消息带唯一 `id`**(UUID 或递增),append 前**按 id 去重**,已存在不追加
- assistant 气泡**渲染 markdown**(`marked.js` 或 `markdown-it`),**必须 sanitize**(DOMPurify)防 XSS——用户输入和 agent 输出都过
- tool_call/tool_result 各显示一行(不重复追加):tool_call 显示"⚡ name(args)";tool_result 显示"✓/✗ name"(+ 可展开看 data)
- response 事件:只在最终回复时追加一条 assistant 气泡(中间 thinking 不追加气泡,只更新状态点)
- 状态点(idle/listening/thinking/speaking)只更新 className,不追加气泡

---

## 5.9 液态玻璃实现 Liquid Glass

- **玻璃模糊**: `backdrop-filter: blur(24px) saturate(1.6)`(所有玻璃面)
- **高光/折射**(补 S1/J9): SVG `filter` 用 `feSpecularLighting`(镜面高光)+ `feGaussianBlur`(边缘光),叠在玻璃面上方
- **自适应 tint**(补 S1): 玻璃背景用 `backdrop-filter` + 半透明 bg,自动取背后内容色调;浅/深模式各一套 bg opacity(见 tokens,浅模式更不透防文字不可读)
- **边缘光**: 1px border + `inset 0 1px 0 rgba(255,255,255,0.2)`(高光边)
- **阴影**: `0 8px 32px var(--glass-shadow)`(深模式更重)
- **圆角**: 连续圆角(`border-radius` + 必要时 SVG,非简单 px)
- **动画**: spring `cubic-bezier(0.34, 1.56, 0.64, 1)`,状态切换 0.3-0.4s

---

## 5.10 设计 Tokens Design Tokens

**全部主题变量化**(修 S1/S2:不许硬编码 `rgba(255,255,255,...)`,浅深各一套)。

### Colors — Light(`:root`)
```yaml
glass-bg: rgba(255, 255, 255, 0.65)        # 更不透,防深色背景下文字不可读(S2)
glass-border: rgba(0, 0, 0, 0.08)
glass-shadow: rgba(0, 0, 0, 0.12)
text-primary: "#1a1a2e"
text-secondary: "#555555"
text-tertiary: "#999999"
accent: "#007AFF"
accent-glow: rgba(0, 122, 255, 0.30)
bubble-assistant-bg: rgba(0, 0, 0, 0.04)   # 非 white-alpha,浅模式可见(S1)
bubble-assistant-border: rgba(0, 0, 0, 0.06)
bubble-user-bg: "#007AFF"
bubble-user-text: "#ffffff"
bubble-system-text: "#555555"
input-bg: rgba(0, 0, 0, 0.04)
input-border: rgba(0, 0, 0, 0.10)
hover-bg: rgba(0, 0, 0, 0.05)
scrollbar: rgba(0, 0, 0, 0.15)
dot-idle: "#888888"
dot-thinking: "#f5a623"
dot-listening: "#4cd964"
```

### Colors — Dark(`[data-theme="dark"]`)
```yaml
glass-bg: rgba(30, 30, 40, 0.72)
glass-border: rgba(255, 255, 255, 0.10)
glass-shadow: rgba(0, 0, 0, 0.40)
text-primary: "#f0f0f5"
text-secondary: "#aaaaaa"
text-tertiary: "#666666"
accent: "#0A84FF"                           # iOS 暗模式系统蓝(更亮)
accent-glow: rgba(10, 132, 255, 0.35)
bubble-assistant-bg: rgba(255, 255, 255, 0.06)
bubble-assistant-border: rgba(255, 255, 255, 0.08)
bubble-user-bg: "#0A84FF"
bubble-user-text: "#ffffff"
bubble-system-text: "#aaaaaa"
input-bg: rgba(255, 255, 255, 0.06)
input-border: rgba(255, 255, 255, 0.12)
hover-bg: rgba(255, 255, 255, 0.08)
scrollbar: rgba(255, 255, 255, 0.15)
dot-idle: "#666666"
dot-thinking: "#f5a623"
dot-listening: "#4cd964"
```

### Typography
```yaml
font-family: "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif"
font-mono: "'Cascadia Code', 'JetBrains Mono', monospace"
size-xs: 11px
size-sm: 12px
size-base: 13px    # 气泡正文
size-md: 14px
size-lg: 16px
size-xl: 20px      # 标题
size-xxl: 24px
weight-regular: 400
weight-medium: 500
weight-semibold: 600
weight-bold: 700
line-height-body: 1.5
line-height-heading: 1.2
```

### Spacing(4px 网格)
```yaml
space-1: 4px
space-2: 8px
space-3: 12px
space-4: 16px
space-5: 20px
space-6: 24px
space-8: 32px
space-12: 48px
```

### Radius
```yaml
radius-pill: 9999px    # 药丸/搜索栏
radius-panel: 20px      # 主面板
radius-button: 10px
radius-input: 12px
radius-small: 8px
radius-bubble: 14px
```

### Shadow
```yaml
shadow-ambient: "0 4px 16px var(--glass-shadow)"
shadow-drop: "0 8px 32px var(--glass-shadow)"
shadow-glow: "0 0 12px var(--accent-glow)"
shadow-inset-highlight: "inset 0 1px 0 rgba(255,255,255,0.2)"
```

### Motion
```yaml
easing-spring: cubic-bezier(0.34, 1.56, 0.64, 1)
easing-ease: cubic-bezier(0.4, 0, 0.2, 1)
duration-fast: 0.2s
duration-normal: 0.3s
duration-slow: 0.4s
```

### Layout sizes
```yaml
sidebar-width: 240px
nav-icon-size: 48px
header-height: 40px
input-height: 44px
pill-height: 48px
pill-width: 220px
panel-width: 420px
panel-height: 600px
sliver-width: 8px
```

---

## 5.11 依赖 Dependencies

- pywebview + WebView2 runtime(Win11 自带;Win10 可能要装)
- DWM 透明合成(Win11 最佳;Win10 部分)— 风险见 `08` J6/J8
- `marked.js` / `markdown-it` + `DOMPurify`(markdown 渲染 + XSS 防护,§5.8)
- Canvas API(语音波形,§5.7)
- 后端:agent loop + 工具执行 + WebSocket/bridge

---

## 5.12 已知前端问题清单(从 `08` J 节 + 本次新发现)

Qoder 上一版前端问题,本次重写要一并修:

| ID | 问题 | 本 spec 对应修法 |
|---|---|---|
| J6 | `-webkit-app-region: drag` 在 pywebview 不工作 | 用 pywebview `window.events.drag` 或 Win32 WM_NCLBUTTONDOWN,不用 CSS |
| J7 | collapsed 三态全缺 | 新 4 态模型(§5.1)替换,实现要全 |
| J8 | 点击穿透 JS 没做 | docked-sliver/minimized 用 `pointer-events` + WS_EX_TRANSPARENT,点击穿透到下层 |
| J9 | 液态玻璃只 backdrop-blur | 补 SVG 滤镜 + 自适应 tint(§5.9) |
| J10 | 主题不持久化 | localStorage 存 theme + accent,启动读回 |
| J11 | Markdown 不渲染 | marked.js + DOMPurify(§5.8) |
| J12 | 无流式 token | 推迟(随 A2 流式一起) |
| J13 | PTT 录音状态不推 UI | 录音 start/stop 调 bridge.push_voice_state(§5.7) |
| J15 | tool_result 不显内容 | tool_result 行可展开看 data(§5.8) |
| J16 | input 单行无历史 | textarea 多行 + 上下键命令历史 |
| **S1** | 硬编码 `rgba(255,255,255,...)` 浅模式不可见 | 全 tokens 主题变量化(§5.10) |
| **S2** | 浅模式玻璃太透文字不可读 | 浅模式 glass-bg opacity 0.65(§5.10) |
| **S3** | 重复渲染 | 每消息带 id 去重(§5.8) |
| **S4** | 语音按钮没做 | Chat/docked-search 都加语音按钮(§5.3/5.6) |
