"""Tool registration — imports and registers all available tools.

Call `register_all_tools()` at startup to populate the global registry.
"""

from agent_assistant.tools.academic import SearchPapersTool
from agent_assistant.tools.file_tools import (
    EditFileTool,
    ListFilesTool,
    MoveFileTool,
    ReadFileTool,
    WriteFileTool,
)
from agent_assistant.tools.focus_window import FocusWindowTool
from agent_assistant.tools.get_selection import GetSelectionTool
from agent_assistant.tools.knowledge_tools import (
    ReadAttachedSourceTool,
    SearchKnowledgeTool,
)
from agent_assistant.tools.launch_app import LaunchAppTool
from agent_assistant.tools.memory_tools import (
    RecallMemoryTool,
    SaveMemoryTool,
    UpdateProfileTool,
)
from agent_assistant.tools.registry import tool_registry
from agent_assistant.tools.research import DispatchResearchTool
from agent_assistant.tools.run_command import RunCommandTool
from agent_assistant.tools.save_note import ReadNoteTool, SaveNoteTool
from agent_assistant.tools.search_local_files import SearchLocalFilesTool
from agent_assistant.tools.session_tools import CreateSessionTool, NotifyTool
from agent_assistant.tools.subagent_tools import (
    AwaitSubagentsTool,
    ControlSubagentTool,
    DispatchSubagentsTool,
)
from agent_assistant.tools.system_tools import (
    KillProcessTool,
    PerfSnapshotTool,
    SetVolumeTool,
    ToggleNotificationsTool,
)
from agent_assistant.tools.recall_summary import RecallSummaryTool
from agent_assistant.tools.tool_archive import RecallToolResultTool
from agent_assistant.tools.ui_automation import (
    UiClickTool,
    UiHotkeyTool,
    UiInspectTool,
    UiScrollTool,
    UiTypeTool,
)
from agent_assistant.tools.web_tools import (
    ReadDocumentTool,
    ReadPageTool,
    WebSearchTool,
)

# Tools that exist in the desktop app but make no sense (or are unwanted)
# when the engine runs embedded under LianYu:
# - dispatch_research: deep research belongs to the LianYu side; a research
#   run takes minutes and would blow the bridge's tool-call timeout. The
#   researcher SUB-AGENT role (via dispatch_subagents) still covers mixed
#   tasks that need some web work.
# - save_note: writes into AgentAssistant's own notes library, which LianYu
#   users never see — an approved save would just vanish. write_file (to a
#   user-visible path) covers explicit saves.
# - create_session: switches the desktop app's chat session; there is no
#   such UI in hosted mode.
# - get_selection / notify: desktop-companion features. Under LianYu the
#   character IS the chat UI — replies go to the conversation, not a Windows
#   toast; reading a selection in some other app is a niche the product
#   doesn't advertise.
_HOSTED_EXCLUDED_TOOLS = (
    "dispatch_research",
    "save_note",
    "create_session",
    "get_selection",
    "notify",
)


def register_hosted_tools() -> None:
    """Register the tool set for hosted (LianYu-embedded) mode.

    Same as :func:`register_all_tools` but WITHOUT the ChromaDB-backed tools
    (``search_knowledge``, ``read_attached_source``, ``recall_memory``,
    ``save_memory``) — the lean hosted package ships without chromadb — and
    WITHOUT desktop-app-only tools (see ``_HOSTED_EXCLUDED_TOOLS``).
    ``update_profile`` stays (SQLite-only).

    Sub-agent dispatch tools remain registered; if an Explorer sub-agent
    happens to call ``search_knowledge``, that single tool degrades to a
    graceful error and the sub-agent falls back to file search.
    """
    _register_common_tools()
    tool_registry.register(UpdateProfileTool())
    for name in _HOSTED_EXCLUDED_TOOLS:
        tool_registry.unregister(name)


def register_all_tools() -> None:
    """Register all main-agent tools into the global registry (standalone)."""
    _register_common_tools()
    # Memory (A4 — Agentic RAG)
    tool_registry.register(RecallMemoryTool())
    tool_registry.register(SaveMemoryTool())
    tool_registry.register(UpdateProfileTool())
    # Knowledge base (user uploads) + attached notes/files
    tool_registry.register(SearchKnowledgeTool())
    tool_registry.register(ReadAttachedSourceTool())


def _register_common_tools() -> None:
    """Tools shared by standalone and hosted mode (no ChromaDB dependency)."""
    # App / Window
    tool_registry.register(LaunchAppTool())
    tool_registry.register(FocusWindowTool())
    # Desktop UI automation (UIA)
    tool_registry.register(UiInspectTool())
    tool_registry.register(UiClickTool())
    tool_registry.register(UiTypeTool())
    tool_registry.register(UiHotkeyTool())
    tool_registry.register(UiScrollTool())
    # Selection / Note
    tool_registry.register(GetSelectionTool())
    tool_registry.register(SaveNoteTool())
    # Terminal
    tool_registry.register(RunCommandTool())
    # File operations
    tool_registry.register(ReadFileTool())
    tool_registry.register(WriteFileTool())
    tool_registry.register(EditFileTool())
    tool_registry.register(ListFilesTool())
    tool_registry.register(MoveFileTool())
    tool_registry.register(SearchLocalFilesTool())
    # System control
    tool_registry.register(KillProcessTool())
    tool_registry.register(SetVolumeTool())
    tool_registry.register(ToggleNotificationsTool())
    tool_registry.register(PerfSnapshotTool())
    # Web + academic
    tool_registry.register(WebSearchTool())
    tool_registry.register(ReadPageTool())
    tool_registry.register(ReadDocumentTool())
    tool_registry.register(SearchPapersTool())
    # Tool-result archive recall (compaction/stubbing pointers point here)
    tool_registry.register(RecallToolResultTool())
    # Archived rolling-summary layers (pointer lines in the context point here)
    tool_registry.register(RecallSummaryTool())
    # Session / Notify
    tool_registry.register(CreateSessionTool())
    tool_registry.register(NotifyTool())
    # Notes library read-back (practice / review flows)
    tool_registry.register(ReadNoteTool())
    # Dispatch (sub-agent)
    tool_registry.register(DispatchResearchTool())
    tool_registry.register(DispatchSubagentsTool())
    tool_registry.register(AwaitSubagentsTool())
    tool_registry.register(ControlSubagentTool())
