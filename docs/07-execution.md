# 07. Execution (开发顺序 / 风险 / 下一步)

## Development Order (开发顺序, dependency-driven)

| Phase | Feature | Rationale |
|---|---|---|
| 1 | Launch-app tool | simplest, validates overall architecture |
| 2 | Right-click toolbox (grab + three functions) | core interaction; main-agent capabilities come online |
| 3 | Save-note tool | follows right-click toolbox |
| 4 | Folder smart classification | independent; validates LLM dispatch |
| 5 | Scene-mode one-click launch | depends on launch-app + system control |
| 6 | Deep-research sub-agent | most complex research capability |
| 7 | Morning briefing | reuses deep research |
| 8 | Perf anomaly self-diagnosis | independent background daemon |
| 9 | Voice interaction (STT+TTS) | final shell; internals unchanged |
| 10 | Continuous-call mode (VAD + streaming + tiered memory) | depends on voice pipeline; advanced |
| 11 | Dynamic-Island UI (liquid glass + edge-collapse + state model) | final integration shell; after voice pipeline |

**MVP ordering logic**: first get the "main agent + text interaction" chain working end-to-end → translate / search / launch-app all usable → then wrap STT/TTS at the input/output ends. Voice is the last shell. UI is the very last integration layer.

## Open Questions / Risks (待定问题 / 风险)

| # | Issue | Impact | Mitigation |
|---|---|---|---|
| 1 | DeepSeek function-calling maturity untested | agentic depends on it; if weak, need structured-output fallback | write test cases to verify before starting |
| 2 | Morning-briefing "mention frequency" subjective bias | agent may surface what it thinks is important rather than the real trend | prompt light-guidance for cross-verification; accept subjectivity |
| 3 | Selection-grabbing cross-app consistency | UI Automation not supported by all apps | Ctrl+C + backup-restore fallback |
| 4 | Sub-agent cost runaway | deep research may burn tokens | hard cap on sub-agent budget + breaker |
| 5 | Perf-monitor kills wrong process | killing a system process blue-screens | killing a process requires 二次确认 |
| 6 | Continuous-call latency / turn-taking | full-pipeline 1-3s, silence threshold hard to tune | full-pipeline streaming + tunable threshold + barge-in; switch to realtime voice model if feel is bad |
| 7 | DWM transparency / Win10 compat | liquid-glass transparency quirks; Win10 partial support | test on Win10/11; fallback to opaque if DWM issues |

## Next Steps (下一步)

1. **Detailed tool-spec design** — name / description / parameters / side effects / failure behavior / idempotency for every tool — the first real artifact of agentic design. ✅ **Done**: see `06-tool-spec.md`.
2. Enter the `writing-plans` skill to make an implementation plan (split tasks by development order above)
3. **TDD**: write tests before implementation for every tool / feature (include side effects / failure behavior / idempotency verification cases)
4. **Code review**: run `requesting-code-review` after each module

## Handoff note (交接说明)

This docs/ folder is the spec for a development agent (e.g. Qoder). Start at `README.md` in the project root, read `02-philosophy.md` before any code, follow the dev order above, and use `06-tool-spec.md` as the per-tool contract. The plan's detail level is intentionally sufficient-but-not-excessive — don't over-plan further; build phase 1 first and learn from it.
