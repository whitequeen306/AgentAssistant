# 02. Design Philosophy (设计理念)

> Read this before writing any code. The agentic mindset is the difference between this project working or failing.

## Agentic 原则 Agentic Principles

- Flow orchestration delegated to the model: which tool, how many times, what order — all decided by model reasoning
- Tool-call order is **NOT** code if/else; it's the product of model thinking
- Sub-agents also have no fixed flows — give them a goal + toolset, let them explore
- System-level triggers (hotkeys / right-click / perf thresholds) are 固化 as lightweight scripts; they don't depend on an always-on LLM
- Sub-agents must also be agentic: internal loops (search → verify → fill-gaps → finalize) are prompt-driven, not code if/else; the framework only runs the agent loop + a budget cap

## 边界:什么不交给模型 Boundaries: What NOT to Delegate to the Model

| Type | Description | Example |
|---|---|---|
| Safety boundary | Hard confirm before dangerous ops | kill process, delete file, batch move — require 二次确认 |
| System triggers | 固化 as lightweight scripts | global hotkey listener, right-click menu hook, perf counter thresholds |
| Tool side effects | Hard-guaranteed inside the tool, model unaware | "backup original clipboard → grab → restore" when grabbing selection |
| Cost budget | Hard-capped at the framework layer | token / call-count limit, breaker trips on exceed |
| Error bubbling | Don't swallow errors inside tools | tool failure returns a structured error; model decides retry / switch / ask user |

## 工具设计黄金原则 Golden Rules of Tool Design

1. **Appropriate granularity**: one tool = one clear capability = agent understands at a glance. Don't wrap a whole feature into one tool (too coarse); don't make every API a tool (too granular)
2. **Clear descriptions**: include "what it does / when to use / params / side effects / failure behavior" — the agent picks tools entirely from descriptions
3. **Side-effect encapsulation**: the selection-grabbing tool internally backs up and restores the clipboard; the model doesn't know and doesn't decide
4. **Error bubbling**: tool failure returns a structured error; let the agent decide retry / switch / ask user
5. **Idempotency preferred**: launch app (focus if already open), file classification (no-op if already in target folder) — let the agent retry without risk

## Agentic 判断标准 Agentic Judgment Standard

> Look at the code: tool-call order is **hardcoded if/else → pipeline**; is **model-reasoned → agentic**.

## 模型能力 vs 工具能力 Model Ability vs Tool Ability

What the model can do is not tool-ified; tools only cover what the model can't (touch files / system / network):

| Model-native ability (not tool-ified) | Ability needing a tool |
|---|---|
| Translate / summarize / Q&A | Read / write / modify files |
| Writing / rewrite / explain | Run terminal commands |
| Reasoning / planning / naming | Launch app / grab selection |
| Code generation | Web search / read pages |
| | Save note / file classification / system control |

**Key clarification**: web search is **NOT** a model-native ability. The model can "decide to search", but "actually fetching pages" requires a tool — the "search" in ChatGPT/Claude web versions is a vendor-attached tool; raw API calls have no search capability at all; you must build your own `web_search` tool.

---

## Agentic 设计速查 Quick Reference

**Delegate to the model**: which tool, how many times, what order; translate vs summarize (judge from text); how many sources to query; which search path the sub-agent takes; whether to follow up / confirm

**Don't delegate to the model**: confirm before kill/delete; hotkey / right-click / perf-threshold triggers; tool side effects (clipboard protection during selection); token budget; error bubbling (don't swallow)

**Judgment standard**: tool-call order is hardcoded if/else → pipeline; is model-reasoned → agentic

**Model ability vs tool**: translate/summarize/Q&A/writing/reasoning = model-native, not tool-ified; web search / read page / read-write files / launch app / system control = must be tools (the model can't do them)

**Sub-agents must also be agentic**: the internal loop is prompt-driven (goal + quality bar + self-check), not code if/else; the framework only runs the agent loop + a budget cap
