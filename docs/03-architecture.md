# 03. Architecture (整体架构) & Tech Stack (技术栈)

## 两层架构 Two-Layer Architecture

```
┌─ Background daemon (runs when main agent is not in conversation) ─┐
│  · Perf anomaly monitor (continuous; over-threshold → wake main agent) │
│  · Morning briefing (after-login delay + network-ready)           │
│  · Right-click toolbox menu registration (system-level hook)       │
│  · Voice hotkey listener (press → record; release → STT → main agent) │
└────────────────────────────────────────────────────────────────────┘
               ↓ trigger (passes snapshot / text / event)
┌─ Main agent (conversation / tool execution) ──────────────────────┐
│  · Translate / summarize / ask · launch app · scene mode          │
│  · Save note · file classification · deep research               │
└───────────────────────────────────────────────────────────────────┘
```

The background layer doesn't depend on an always-on LLM (expensive + slow); it's just a "trigger" that wakes the main agent when fired.

**Front-end UI**: the Dynamic-Island UI (see `05-ui-dynamic-island.md`) sits on top of the main agent as the human-interaction layer (conversation, voice waveform, tool results, briefing popups).

## 路由模式 Router Pattern (Dispatcher)

- The main agent does routing + lightweight tasks inline (translate, launch app, single `web_search` / `read_page`)
- Heavy work is delegated to sub-agents (deep research, morning briefing)
- Routing judgment: needs "multi-round search + synthesis + report" → dispatch the deep-research sub-agent; single-shot query / read → main agent inline (the model decides, no hardcoded rules)
- **Tool call ≠ sub-agent**: a sub-agent's essence is "independent context + multi-step reasoning researcher". Second-level tasks like translate / launch-app shouldn't be wrapped as sub-agents — avoid over-engineering
- Sub-agent results are **summarized** before returning to the main agent, avoiding context pollution

## 工具清单总览 Tool List Overview

**Main agent tools (19)**

| Domain | Tools | Use |
|---|---|---|
| File | `read_file` / `write_file` / `edit_file` / `list_files` / `move_file` | read/write/modify + classify |
| Terminal | `run_command` | run shell (whitelist / confirm) |
| App | `launch_app` / `focus_window` | launch app + focus |
| Right-click / note | `get_selection` / `save_note` | grab selection + save note |
| System control | `kill_process` / `set_volume` / `toggle_notifications` | scene mode + free memory |
| Perf | `perf_snapshot` | take snapshot during diagnosis |
| Session / notify | `create_session` / `notify` | morning briefing closed loop |
| Dispatch | `dispatch_research` | pull deep-research sub-agent |
| Web | `web_search` / `read_page` | single-shot query / single-page read |

**Deep-research sub-agent tools (5)**: `web_search` / `read_page` / `read_document` / `extract_content` / `save_note` + strong prompt + agent loop (with budget cap)

`read_document` 是 `read_page` 的另一半：研招目录、分数线这类数据几乎只存在于
PDF 附件与图片表格里，HTML 正文拿不到。它走「文本层 → 表格 → 扫描件 OCR」三级，
并把表格排在正文之前输出，保证截断时先砍正文。

**Shared tools**: `web_search` / `read_page` / `save_note` (main agent: single-shot; sub-agent: iterative)

**Sub-agent**: only 1 (deep research); reused by morning briefing

**Non-tools**: model-native abilities (translate / summarize / Q&A / writing / reasoning) · pipelines (STT / TTS / VAD / turn-taking / barge-in) · background triggers (hotkey / right-click hook / perf monitor / login delay)

> Per-tool detailed spec: see `06-tool-spec.md`.

---

## 技术栈 Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Main LLM | deepseek-v4-pro | must test function-calling maturity first; if weak, fallback to structured output |
| Language | Python | richest ecosystem, full Windows automation libs |
| STT | sherpa-onnx + SenseVoice-Small (local) | Chinese-strong, multilingual, zero cost, private |
| TTS | Edge-TTS | free, good quality, Chinese neural voices |
| VAD | silero-vad (local) | for continuous-call mode, detects speech start/end; lightweight alt: webrtc-vad |
| Vector DB (later) | Chroma / Qdrant | personal knowledge base RAG; not implemented in this plan |
| UI automation | pywinauto / pyautogui | grab selected text, UI automation |
| Global hotkey | keyboard / pynput | push-to-talk |
| Right-click menu | Registry / Shell extension | register "交给助手" menu item |
| Perf monitoring | psutil / Windows perf counters | read by background daemon |
| Web search | SearXNG (self-hosted, primary) + Tavily (fallback) | local Docker, aggregates multiple sources, free unlimited; Tavily free tier ~1000/month fallback |
| Front-end UI | pywebview + WebView2 | Python-native, lightest; Chromium core supports backdrop-filter/SVG liquid glass; borderless/transparent/top-most OK |
| UI rendering | HTML/CSS/JS (Svelte or Vue optional) | liquid glass via backdrop-filter + SVG filters; spring animations via CSS / Web Animations API |
