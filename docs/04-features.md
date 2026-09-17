# 04. Features (功能清单, 10 个)

## 4.1 Launch App (打开应用)

- **Goal**: user says "open 逆战 / Cursor / ..." → launch the corresponding app
- **Tool**: `launch_app(name)` — the agent decides UWP/exe/launcher itself and decides focus-or-launch
- **Implementation points**:
  - App registry (name → exe path / protocol URI / launcher path + window title pattern)
  - Before launch, detect whether the window already exists → if yes SetForegroundWindow to focus, if no launch
  - Registry manually configurable + first-run scan of Start Menu shortcuts to auto-populate
- **Pitfalls**:
  - UWP/Store apps need a protocol URI (`start whatsapp:`), not an exe path
  - Games with launchers (Tencent / Epic / Battle.net) must call the launcher; calling the game exe directly may trigger anti-cheat
  - Re-launching an already-running app opens a second instance or no-ops; window detection is mandatory
- **Agentic-ify**: don't hardcode "game mode = mute notifications + open 逆战 + adjust volume"; give a goal and let the agent call a bunch of tools itself

## 4.2 Right-Click Toolbox (右键大全: Translate / Long-text Summarize / Text Ask)

- **Goal**: add "交给助手" to the Windows right-click menu; on click pop a picker: Translate / Long-text Summarize / Text Ask (three-way choice)
- **Flow**: right-click → "交给助手" → grab selected text → picker picks one of three → main agent executes → show result → optionally call `save_note` to save a note (note is a wrap-up action, not a picker option)
- **Key implementation point (grabbing text)**:
  - After clicking the menu item, Windows only tells "user clicked menu"; it does NOT pass the selected text automatically
  - The assistant must actively grab the selected text, three ways:
    1. UI Automation interface querying the focused control (clean, no clipboard touch, but not all apps support it)
    2. Simulate Ctrl+C and read the clipboard (universal, but **overwrites the user's clipboard**)
    3. App-specific API (most accurate, but per-app adaptation, high workload)
  - **Recommended**: try UI Automation first, fall back to Ctrl+C + backup-restore; the side effect is handled inside the `get_selection()` tool, model unaware
- **Picker doesn't violate agentic**: the user states intent (decides what to do); the agent decides how (which source / where to display / whether to save a note / whether to follow up)
- **Agentic-ify**: after the picker, don't write a "grab → translate → display" pipeline; give `get_selection()` + three abilities, let the agent decide which to use based on the text, whether to chain

## 4.3 Local Note Storage (本地笔记存储)

- **Goal**: after selected content → translate/summarize/ask, optionally save a local note
- **Tool**: `save_note(content, title, tag)` — the unified wrap-up tool for the main agent
- **Saving notes is the host's (main agent's) responsibility**: translate / summarize / ask / deep-research / morning-briefing results can all call `save_note` to wrap up
- **Storage format**: local Markdown files + simple index (title / tag / time), for later RAG integration
- **Future extension**: cross-app搬运 is deferred to later; this version only does local notes

## 4.4 Desktop / Downloads Smart Classification (桌面/下载文件夹智能归类)

- **Goal**: auto-classify by filename semantics (work / screenshots / installers / materials); LLM is stronger than pure rules
- **Tools**: `list_files(path)` / `move_file(src, dst)` — the classification strategy itself is model reasoning, not a tool
- **Agentic-ify**: don't hardcode "classify by type"; the agent decides the strategy itself (by project / date / content)
- **Safety**: confirm before move; batch ops show a preview list for user confirmation

## 4.5 Scene-Mode One-Click Launch (情景模式一键启动)

- **Goal**: user says "starting work" / "game time" → one-click multi-step environment setup
- **Example**: game mode = open 逆战 + kill background processes to free memory + adjust volume + mute notifications
- **Dependencies**: launch_app tool + system control (kill process / adjust volume / mute notifications) — must be done after launch-app, and requires extending system control capabilities
- **Agentic-ify (core)**: don't hardcode "game mode = ..."; give a goal "I want to play a game, free up resources and open 逆战", let the agent call a bunch of tools itself

## 4.6 Deep-Research Sub-Agent (深度研究子智能体, Strengthened)

- **Goal**: produce a real, multi-source cross-verified, source-annotated report that distinguishes facts from inferences; the main agent only wants the final summary
- **Strengthening path: method-level, not multi-layer sub-agents**: don't build a research→writer→reviewer three-layer pipeline (enterprise-grade, over-engineering for personal use); instead, one research sub-agent + strong toolset + strong prompt + internal iterative loop
- **Sub-agent toolset**: `web_search` (AnySearch，带相关度闸门) / `read_page` / `read_document` (PDF 表格 + 扫描件 OCR) / `extract_content` (precise extraction by CSS selector) / `save_note` (store intermediate findings)
- **Strong prompt (describes goal + quality bar, not steps)**:
  - Goal: produce a real, multi-source cross-verified, source-annotated report distinguishing facts vs inferences for X
  - Quality bar: self-check before finalizing — any unverified claims? contradictory sources? obvious gaps? if yes, search more; only finalize when confident
- **Framework agent loop (infrastructure, not control flow)**: model thinks → calls tool → gets result → thinks again, looping until the model says "finalize"; the only hard control is the turn/token budget cap (prevents runaway cost)
- **Sub-agents must also be agentic**: the internal loop is prompt-driven, not code if/else; quality judgment is always the model's, budget judgment is the framework's
- **Main-agent routing**: `dispatch_research(goal, context)` dispatches the sub-agent; the main agent may use `web_search` / `read_page` for single-shot query/read, but deep research must go through the sub-agent (avoids the main agent being tempted to do it itself, which violates delegation)
- **Morning briefing reuses this sub-agent**: same engine, fixed goal prompt
- **Cost control**: the sub-agent has a token/turn budget; breaker trips on exceed

## 4.7 Morning Briefing (晨间播报: after-login delay + network-ready)

- **Goal**: after login, auto-search recently coined AI/agent technical terms (e.g. GraphEngineering / LoopEngineering) plus trending AI GitHub repos — not "what happened in AI yesterday" — then form a "briefing → preview / follow-up" closed loop
- **Trigger timing**: 5 minutes (configurable) after user login + network-ready, to avoid the boot-busy period (network not connected / system loading / services starting)
- **Architecture**: reuses the deep-research sub-agent (4.6) + a fixed goal prompt; no fixed flow template
- **Data-source strategy (no fixed source)**: don't hardcode a source list; the agent searches the whole web and judges "mention frequency" itself, fitting the agentic philosophy
- **Light guidance**: prefer GitHub Trending / Show HN / paperswithcode / technical blogs; skip general AI news recaps. The agent decides which sources to use; non-binding
- **Trade-off**: the agent self-judging "most-mentioned" has subjective bias (may surface what it thinks is important rather than the real trend); acceptable
- **Closed loop (core)**:
  1. The sub-agent produces a **full report** (Markdown file) + a **summary** (keywords + first few sentences)
  2. Pop a Windows toast notification bottom-right → shows only the summary (keywords + first few sentences) + two buttons: [Preview full report] / [Ask about this report]
  3a. Click "Preview full report" → call `launch_app` to open the .md report file with the system default app
  3b. Click "Ask about this report" → auto-create a new session → inject "scenario = morning briefing + full report text" as context → user converses directly with the main agent for follow-up
- **Wrap-up**: the full report is optionally saved as a note (call `save_note`); the summary is saved separately for popup reuse
- **Dependencies**: launch_app tool (preview report), session management (create new session + inject context), Windows toast notification API

## 4.8 Perf Anomaly Self-Diagnosis (性能异常自诊断)

- **Goal**: when the fan roars / CPU-memory is anomalous, proactively say "Chrome is using 4G, want to kill it?"
- **Architecture**:
  - Monitoring itself uses a lightweight script (psutil / perf counters) reading continuously, **not an LLM monitoring**
  - Over-threshold → pass a snapshot to wake the main agent → the agent explains in natural language + suggests killing processes
- **Safety**: killing a process **requires 二次确认**; killing a system process can blue-screen
- **Agentic-ify**: how to diagnose and what to suggest is delegated to the agent; the trigger and threshold are hardcoded

## 4.9 Voice Interaction — Push-to-Talk Mode (语音交互·按键说话模式)

- **Goal**: push-to-talk; release sends to STT → main agent → TTS playback
- **Pipeline**: microphone → STT (sherpa-onnx + SenseVoice) → text → main agent → text → TTS (Edge-TTS) → speaker
- **Key understanding**: voice is the agent's "ears and mouth"; the agent internally only ever handles text. STT/TTS are outer pipelines, not part of routing decisions. Voice is the last shell wrapped around the system; the agent internals don't change
- **Wake mode**: push-to-talk (simple, not tiring, lowest latency); continuous listening + VAD see 4.10 continuous-call mode
- **Interruption handling**: wanting to interject while the agent is speaking a long reply is awkward with a pure pipeline (either wait until done, or add "stop current playback" logic). This is the obvious shortcoming of the STT+TTS pipeline vs native voice models
- **Agentic-ify**: the pipeline is fixed, but "after speaking, whether to execute directly / whether to confirm / whether to follow up" is delegated to the model
- **Two modes coexist**: push-to-talk (this section, quick commands) + continuous-call (4.10, long interaction); global hotkey switches modes

## 4.10 Continuous-Call Mode (持续通话模式, Long-form Voice Interaction)

- **Goal**: continuous voice conversation; user says a sentence, agent replies, hands-free; suited for long interactions ("research X" / "organize the desktop then tell me what you did")
- **Coexists with 4.9**: push-to-talk (quick commands) + continuous-call (long interaction); global hotkey switches
- **Pipeline (STT+TTS, no voice model needed)**: microphone → VAD → STT (sherpa-onnx + SenseVoice streaming) → text + history → main agent (streaming) → TTS (Edge-TTS streaming) → speaker, looping
- **Components beyond push-to-talk**:
  - **VAD**: silero-vad (local, accurate), detects speech start/end
  - **turn-taking**: silence threshold (~1.2s configurable) → 认定 user finished → send to STT → send to agent
  - **barge-in**: while the agent is speaking TTS, VAD keeps listening; detecting user voice stops TTS immediately
  - **full-pipeline streaming**: STT streaming + LLM streaming (DeepSeek streaming) + TTS streaming → first word out within ~1s
- **Honest gap**: STT+TTS continuous-call feels worse than native voice models (latency 1-3s / occasional turn-taking awkwardness / loses prosody); acceptable for command-execution + Q&A scenarios; if latency becomes unbearable, switch to a realtime voice model — the pipeline architecture stays the same

### 4.10.1 Tiered Memory (分层记忆, Context Management)

Continuous-call sessions are long and history balloons; use memgpt-style tiered memory:

```
┌─ 1. Recent verbatim window (last N turns) ─── full detail
├─ 2. Rolling summary (mid-history, incremental) ── compressed
├─ 3. Important-event list (kept across the whole conversation) ── persistent
└─ 4. Archive (dropped from summary → save_note / RAG) ── searchable, not lost
```

- **Importance filtering must be model-judged, not code rules**: "what counts as important" goes in the summarizer prompt (decisions / tool results / tasks / user preferences / error fixes); code only calls the summarizer at trigger points; judgment is all the model's
- **Trigger timing (event-driven, not every turn)**: ① recent window overflows ② rolling summary exceeds threshold ③ overall token approaches the budget cap
- **Important-event list contains**: user decisions / tool calls + results / pending tasks / key facts / user preferences; once in the list **it does not get re-compressed**, as a safety net against summary-of-summary drift
- **Who summarizes**: a lightweight model (deepseek-chat or a local small model) runs in the background; the main LLM only does conversation; or the main agent does it async during conversation gaps
- **Three layers each have their role**: important-event list = long-term memory, rolling summary = mid-term memory, recent verbatim = short-term memory; losing any one layer isn't fatal
- **Demotion ≠ loss**: content dropped from the summary goes to `save_note` → RAG; "truncation" is "demotion to a searchable archive", connecting to the later personal knowledge base (RAG = unlimited-capacity cold storage)

### 4.10.2 Dependencies

- Voice pipeline (STT/TTS, reuse 4.9)
- VAD / turn-taking / barge-in components
- Session management (`create_session`, reuse)
- Lightweight summarization model (independent of the main LLM)
