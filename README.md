# AgentAssistant · 大学生学习科研智能体

> 面向大学生学习科研场景、运行在本机 Windows 上的桌面智能体。
> 右键任何文件交给它精读，深度调研自动写带引用的报告，学习资料沉淀入库，
> 自动出题自测，并按遗忘曲线把知识点push回来复习。
>
> **不是又一个聊天窗口，而是"读过的东西 → 记得住的东西"的学习闭环。**
>
> **Agentic by design** —— 模型自主决定工具调用与编排，框架只负责安全执行。
> **Local-first** —— 学习数据不出本机。

![logo](assets/logo.png)

## 为什么做这个

一个考研学生要对比 5 所院校 × 3 个专业，得逐个打开研招网，在异步加载的树形目录里层层点开，
下载多份 PDF 招生目录（科目代码常是乱码，无法直接复制），手工对齐招生人数、考试科目、
参考书目、近三年分数线——**每年 9 月目录更新后整套流程还要重做一遍**。

这件事我们最初是用 23 个抓取脚本、1384 行代码硬扛下来的。它本该由 Agent 完成。

而"读完就忘、资料散落、数据不敢上云"，是同样的道理。

详细定位见 [docs/01-purpose.md](docs/01-purpose.md)。

## 它能做什么

| 功能 | 说明 |
|---|---|
| 📖 **交互式精读** | 资源管理器右键「Learn with Assistant」→ 助手边读边讲解 PDF / Word / Excel / PPT / 代码 / 文本，读完后主动给出后续学习选项 |
| 🔍 **深度调研** | `dispatch_research` 派出独立调研子 Agent：多轮搜索（BoCha/Bing/arXiv/Semantic Scholar）→ 交叉核验 → 生成带来源链接的报告，报告一键存入资料库 |
| 🗂 **学习库** | 资料笔记（Markdown）+ 向量知识库（RAG）+ **练习室**三合一 |
| 🎓 **练习室** | 选任意一份学习材料，AI 自动出题（选择/简答/混合），本地判分 + 逐题解析 |
| 💬 **三形态窗口** | 常驻搜索胶囊 → 展开小窗对话 → 完整主界面，边缘吸附、拖拽变形 |
| 🏎 **桌面自动化** | UIA 驱动：一句话「打开网易云播放水手」级别的真实桌面操作 |
| 🔐 **安全围栏** | 危险操作强制确认、文件操作围栏、工具权限白名单、子 Agent 预算/熔断 |

## 快速开始（3 步）

### 1. 安装依赖

需要 **Python 3.11+**（建议 Windows）：

```powershell
git clone https://github.com/<你的用户名>/AgentAssistant.git
cd AgentAssistant
pip install -e ".[voice]"
```

> `.[voice]` 是可选的语音依赖（SenseVoice 本地 STT + Edge-TTS）。不需要语音可只装 `pip install -e .`

### 2. 配置 `.env`

在项目根目录创建 `.env`（参考 `.env.example`）：

```ini
DEEPSEEK_API_KEY=sk-你的key          # https://platform.deepseek.com
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
# 可选：博查搜索（国内可直连，无需梯子）
BOCHA_API_KEY=sk-你的key              # https://open.bochaai.com
```

### 3. 启动（无黑窗口）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
```

这会在**桌面创建带 Logo 图标的快捷方式**——双击即启动，全程无控制台窗口。
应用默认以顶部搜索胶囊形态启动，输入问题回车即可对话；把胶囊往下拖或点展开按钮进入完整主界面。

> 也可以直接 `python -m agent_assistant.main`（会带控制台窗口，方便看实时日志）。

## 右键「Learn with Assistant」

启动一次后自动注册到 Windows 右键菜单（HKCU，无需管理员）：

- 右键**任意文件** → `Learn with Assistant` → 助手交互式精读该文件
- 叱键**任意文件夹** → 同上，助手先列结构再挑关键文件读
- 点击后若有运行中的实例会自动转发到它（单实例 IPC），否则冷启动

卸载菜单：

```powershell
python -c "import sys; sys.path.insert(0,'src'); from agent_assistant.daemon.context_menu import unregister_learn_context_menu; unregister_learn_context_menu()"
```

## 使用姿势（推荐动线）

```
右键论文 PDF → 精读对话（边读边讲）→ 追问/继续调研 → 报告存入资料库
→ 学习库「练习室」出题自测 → 知识库随时问答复习
```

**典型场景 · 考研择校与备考**

```
剪藏各校研招页面 → 一句话下达「对比 A/B/C/D 四校计算机专硕，看初试科目与近三年分数线」
→ 结构化对比报告 + 一页纸决策卡（每格带来源链接，可回溯核验）
→ 参考书目 PDF 存入学习库 → 练习室出题自测 → 错题按 Leitner 五盒到期回炉
```

> 场景设计与验证方案见 [competition/](competition/)。

- **空会话引导**：新会话顶部会出现来自你学习库真实内容的快捷 chips（精读某笔记 / 考考我某材料），点击即用
- **深度调研**：对它说「深度调研对比 A 和 B，写份带来源的报告」——调研进度会以卡片形式实时滚动，报告完成后可一键存库
- **晨间简报**：登录后自动生成（可在设置中开关）

## 可选：本地语音

`pip install -e ".[voice]"` 后，到 [sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases) 下载
SenseVoice-Small 的 `model.int8.onnx` 放入 `%USERPROFILE%\AgentAssistant\models\sense-voice\`，
设置里开启「语音输入（PTT）」即可按住麦克风键说话。

## 目录结构

```
src/agent_assistant/
├── agent/            # agentic 主循环（模型决策驱动）
├── tools/            # 30+ 工具：文件/桌面/搜索/学术/知识库/子Agent调度…
├── subagents/        # 多子Agent框架（researcher/explorer/operator/organizer）
│   └── runners/      #   深度调研 = 独立 agentic loop + L0/L1/L2 工件
├── research/         # 调研工件存储（trace/findings/report）
├── knowledge/        # 向量知识库（ChromaDB 混合检索）
├── daemon/           # 常驻服务（简报/右键菜单注册）
├── ui/               # pywebview 桥 + 窗口管理 + 内置 Web 产物
└── memory/           # 长期记忆（profile + 滚动摘要）
frontend/             # React 18 + TS + Tailwind v4 + GSAP 前端（单文件构建）
docs/                 # 设计文档（philosophy / architecture / tool-spec…）
```

## 开发

```powershell
cd frontend
npm install
npm run dev                 # Vite 热更新开发
npm run build               # 单文件构建 → src/agent_assistant/ui/web/
npx tsc -b --force          # 类型检查
node e2e/verify.mjs         # Playwright 门禁（13 项）

# 仓库根目录
python -m pytest tests -q   # 后端测试（800+ 用例）
```

构建产物 `src/agent_assistant/ui/web/index.html` 是单文件（JS/CSS 全内联），
**已提交到仓库**——不装 Node 也能直接运行 Python 端。改了前端才需要重新 build。

## 架构要点

- **Model-heavy, flow-light**：不做硬编码流水线，工具编排全部交给模型推理
- **子 Agent 预算管理**：轮次封顶 + 收尾模式 + 同主机熔断 + 断点续查（resume_from）
- **上下文防膨胀**：旧工具结果折叠为 L1 摘要，原文进 L0 归档可随时召回
- **安全**：SSRF 校验、危险命令确认、文件围栏、错误净化（模型只见安全信息）

详见 [docs/](docs/)。

## License

MIT
