"""Configuration management via pydantic-settings + .env file."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def env_file_candidates() -> list[Path]:
    """`.env` 的候选位置，按优先级排列。

    ``env_file=".env"`` 是**相对进程 CWD** 解析的，从别处启动（计划任务、
    其他 IDE、`python -m` 在别的目录）就会静默读不到 —— 表现为"我明明填了 key
    却报 not set"。所以显式列出候选位置，谁存在用谁。
    """
    out: list[Path] = []
    for candidate in (
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",  # 源码运行时的项目根
        Path.home() / "AgentAssistant" / ".env",  # 数据目录（打包后放这儿也行）
    ):
        try:
            if candidate.is_file() and candidate.resolve() not in {p.resolve() for p in out}:
                out.append(candidate)
        except OSError:  # noqa: PERF203 — 路径不可访问时跳过
            continue
    return out


def resolve_env_file() -> str:
    """挑一个实际存在的 `.env`；都没有就退回默认相对路径。"""
    candidates = env_file_candidates()
    return str(candidates[0]) if candidates else ".env"


def env_or_dotenv(key: str, default: str = "") -> str:
    """读一个配置值：先看真实环境变量，没有则**即时**读一次 `.env`。

    存在的意义是「改完 .env 不用重启」。pydantic-settings 只在导入时读一次，
    用户加完 key 忘了重启就会一直看到 not set —— 这类问题排查成本极高。
    """
    value = os.environ.get(key, "")
    if value:
        return value
    for path in env_file_candidates():
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    v = v.strip().strip('"').strip("'")
                    if v:
                        logger.info("从 %s 即时读取 %s（进程启动后新增）", path, key)
                        return v
        except OSError:
            continue
    return default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # DeepSeek API
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # Web search（唯一搜索源；web_search 工具用 env_or_dotenv 读取，改完免重启）
    anysearch_api_key: str = ""

    # Storage paths
    data_dir: Path = Path.home() / "AgentAssistant"
    notes_dir: Path | None = None
    app_registry_path: Path | None = None

    # Agent behavior
    max_tool_rounds: int = 80  # hard safety cap per conversation turn (was 20)
    progress_summary_every: int = 20  # every N tool rounds → force no-tools summary
    # Sub-agent budget: 60 turns is safe because the loop ages off old tool
    # results to L1 summaries (context stays bounded); resume_from remains
    # the backstop for infra failures, not the normal path.
    subagent_max_turns: int = 60  # budget cap for sub-agent (was 30)
    subagent_max_concurrency: int = 4

    # DeepSeek thinking mode (reasoning_content stream → UI Thought panel)
    thinking_enabled: bool = True
    reasoning_effort: str = "low"  # off | low | high | max (mapped per model)

    # Hosted mode (LianYu MCP): forward one short user-facing narration line
    # per tool round as MCP progress (the chat shows it as a live bubble).
    hosted_narration: bool = False

    # Memory (A4). Budget is normally DERIVED from the model's context window
    # (see effective_memory_budget) so switching to a larger model
    # automatically uses the extra room. Set memory_token_budget > 0 to pin
    # an explicit value (Hosted mode does this from the request payload).
    memory_token_budget: int = 0  # 0 = derive from llm_context_window × compaction_ratio
    llm_context_window: int = 128_000  # model's context window — adjust per model
    # Trigger line as a fraction of the context window. 0.85 leaves ~15% for
    # the output plus the compaction summarizer call itself; going to 0.95
    # makes the summarizer overflow while it works (observed in the field).
    compaction_ratio: float = 0.85
    # The window also holds things the MESSAGE budget does not account for:
    # the system prompt (~2.7K) and the tool schemas (~8.7K with 38 tools),
    # plus the dynamic profile / rolling-summary sections. Measured with
    # scratch/probe_budget_account.py. Without subtracting this the worst case
    # was 108_800 + 11_410 = 120_210 (93.9% of the window), and 102.4% once the
    # 10% compaction hysteresis is included — i.e. an overflow instead of a
    # compaction. Conservative by design: an unused reserve only costs a
    # slightly earlier compaction, an insufficient one breaks the turn.
    memory_context_reserve: int = 15_000
    memory_summary_cap: int = 1200  # rolling summary token cap
    memory_profile_cap: int = 500  # stable profile token cap

    # Token estimation (DeepSeek). tiktoken's cl100k_base overcounts Chinese
    # (~0.7-1.0 tok/char) vs DeepSeek's documented ~0.6 tok/char — without
    # calibration, CJK-heavy conversations hit the compaction budget early
    # and throw away context they didn't need to. CJK chars are estimated as
    # chars × this factor; non-CJK text still goes through tiktoken.
    token_cjk_factor: float = 0.6

    # Voice — ASR via sherpa-onnx + SenseVoice (local), TTS via edge-tts
    # sherpa-onnx SenseVoice model.int8.onnx path (empty = data_dir/models/sense-voice/)
    sense_voice_model: str = ""
    sense_voice_tokens: str = ""  # SenseVoice tokens.txt path (empty = same dir as model)
    tts_voice: str = ""  # empty = auto-detect by language

    @property
    def resolved_notes_dir(self) -> Path:
        return self.notes_dir or (self.data_dir / "notes")

    @property
    def effective_memory_budget(self) -> int:
        """Short-term window token budget for the memory manager.

        ``llm_context_window × compaction_ratio`` is the budget for the WHOLE
        request (system prompt + tool schemas + messages); only the messages
        are the memory manager's to spend, so the fixed overhead is subtracted
        here. Derived rather than pinned so a larger model automatically gets
        more room; ``memory_token_budget > 0`` pins an explicit value instead
        (Hosted mode sets it per request).
        """
        if self.memory_token_budget > 0:
            return self.memory_token_budget
        envelope = int(self.llm_context_window * self.compaction_ratio)
        return max(2000, envelope - self.memory_context_reserve)

    @property
    def resolved_app_registry(self) -> Path:
        return self.app_registry_path or (self.data_dir / "apps.json")

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def research_runs_dir(self) -> Path:
        return self.data_dir / "research_runs"

    @property
    def subagent_runs_dir(self) -> Path:
        return self.data_dir / "subagent_runs"

    @property
    def subagent_db_path(self) -> Path:
        return self.data_dir / "subagents.db"

    @property
    def practice_db_path(self) -> Path:
        return self.data_dir / "practice.db"

    @property
    def goals_db_path(self) -> Path:
        return self.data_dir / "goals.db"

    @property
    def knowledge_dir(self) -> Path:
        return self.data_dir / "knowledge"

    def ensure_dirs(self) -> None:
        """Create necessary directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.resolved_notes_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.research_runs_dir.mkdir(parents=True, exist_ok=True)
        self.subagent_runs_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        (self.knowledge_dir / "files").mkdir(parents=True, exist_ok=True)
        # P2-A: optional user-editable SOUL.md / USER.md / AGENTS.md stubs
        from agent_assistant.agent.instructions import ensure_instruction_stubs

        ensure_instruction_stubs(self.data_dir)


settings = Settings()

REASONING_EFFORT_LEVELS = frozenset({"off", "low", "high", "max"})


def apply_reasoning_effort(value: str) -> bool:
    """Update thinking_enabled / reasoning_effort. False when value is invalid."""
    raw = (value or "").strip().lower()
    if raw not in REASONING_EFFORT_LEVELS:
        return False
    settings.reasoning_effort = raw
    settings.thinking_enabled = raw != "off"
    return True


# Keep the thinking flag in sync with .env REASONING_EFFORT (off disables CoT).
apply_reasoning_effort(settings.reasoning_effort)
