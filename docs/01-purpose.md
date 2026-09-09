# 01. Purpose (项目目的)

## What we're building

AgentAssistant — an **open-source** personal desktop Agentic assistant for Windows. The assistant lives on the user's PC and helps with everyday tasks through natural conversation (text or voice). It is **agentic by design**: the model decides what to do and which tools to call; there are no hardcoded pipelines. Distributed as open-source for self-hosting (single-user per instance); a freemium cloud layer is a possible future path but not the current focus.

## Why (用户愿景)

The user wants a desktop companion that:

- Translates / summarizes / answers questions about anything selected (right-click toolbox)
- Opens apps and switches "scenes" (work mode, game mode) with one command
- Researches topics and produces real, source-annotated reports
- Briefs them every morning on newly coined AI technical terms and hot GitHub projects (not industry news)
- Watches PC performance and proactively suggests fixes
- Is interacted with via voice (push-to-talk for quick commands; continuous-call for long conversations)
- Presents itself as a fluid Dynamic-Island-style floating UI (liquid glass, light/dark, edge-collapse, drag-out)

## Core philosophy

**Model-heavy, flow-light (Agentic)** — let the agent autonomously decide tool calls and orchestration. The framework only "safely executes the model's decisions"; all flow orchestration is delegated to model reasoning. Inspired by Claude Code source.

See `02-philosophy.md` for the full principles.

## Target user

A single user (the developer/owner) on their personal Windows PC. Not a multi-tenant product. This shapes decisions: personal-scale cost is OK, no auth/multi-tenancy needed, preferences are personal.

## Success criteria

- The 10 features in `04-features.md` all work end-to-end
- The agent picks tools by reasoning, not by hardcoded if/else (verifiable: read the code, no flow-orchestration branches)
- Dangerous ops always confirm
- The Dynamic-Island UI feels fluid (liquid glass, smooth spring animations, edge-collapse works)
- Voice interaction is usable (push-to-talk low-latency; continuous-call acceptable latency)

## Out of scope (this phase)

- **WeChat / cross-app搬运** — deferred to a later phase; `save_note` is the local entry point
- **Personal knowledge base RAG** — later phase; `save_note` writes Markdown that future RAG will index
- **Native realtime voice model** — use STT+TTS pipeline first; switch to a realtime voice model later only if latency is unbearable (pipeline architecture stays the same)
- **Multi-user / auth / cloud sync** — personal single-user, all local

## Key decisions already made

| Decision | Choice | Rationale |
|---|---|---|
| Main LLM | deepseek-v4-pro | strong + Chinese-capable; test function-calling maturity first |
| Implementation language | Python | richest ecosystem, full Windows automation libs |
| Voice wake | push-to-talk (primary) + continuous-call (secondary) | push-to-talk is simple/low-latency; continuous-call for long hands-free interaction |
| Web search | SearXNG (self-hosted, primary) + Tavily (fallback) | free unlimited local; Tavily ~1000/month free fallback |
| Front-end UI | pywebview + WebView2 + HTML/CSS/JS | Python-native, lightest; Chromium supports backdrop-filter/SVG liquid glass |
| Sub-agent count | 1 (deep research) | morning briefing reuses it; don't over-engineer |

## Project metadata

- **Project name**: AgentAssistant
- **Positioning**: Open-source personal desktop Agentic assistant (self-hosted, single-user per instance)
- **Main LLM**: deepseek-v4-pro
- **Implementation language**: Python
- **Voice strategy**: push-to-talk (primary); continuous-call (5.10) secondary
- **Platform**: Windows
