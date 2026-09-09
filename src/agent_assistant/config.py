"""Configuration management via pydantic-settings + .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # DeepSeek API
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

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

    # Memory (A4). 12000 avoids mid-UI-turn compaction churn: control trees
    # from ui_inspect sit just over 6000 and used to summarizer-pause every round.
    memory_token_budget: int = 12000  # short-term window token cap
    memory_summary_cap: int = 1200  # rolling summary token cap
    memory_profile_cap: int = 500  # stable profile token cap
    memory_soft_rounds: int = 4  # soft round limit before compaction

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
