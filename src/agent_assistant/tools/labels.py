"""Chinese display labels for tools (scene editor / UI).

Internal tool ``name`` stays English for the model & registry; UI shows ``label``.
"""

from __future__ import annotations

# Scene-mode action builder: only lifestyle shortcuts, not agent-internal tools.
SCENE_TOOL_WHITELIST: frozenset[str] = frozenset({
    "launch_app",
    "focus_window",
    "set_volume",
    "toggle_notifications",
    "kill_process",
    "notify",
    "create_session",
})

# Keep in sync with registered main-agent tools (register.py).
TOOL_LABELS_ZH: dict[str, str] = {
    "launch_app": "打开应用",
    "focus_window": "聚焦窗口",
    "ui_inspect": "查看界面控件",
    "ui_click": "点击控件",
    "ui_type": "输入文字",
    "ui_hotkey": "发送快捷键",
    "ui_scroll": "滚动界面",
    "get_selection": "获取选中文本",
    "save_note": "保存笔记",
    "run_command": "运行命令",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "编辑文件",
    "list_files": "列出文件",
    "move_file": "移动文件",
    "search_local_files": "搜索本地文件",
    "kill_process": "结束进程",
    "set_volume": "调节音量",
    "toggle_notifications": "通知勿扰",
    "perf_snapshot": "性能快照",
    "web_search": "网页搜索",
    "read_page": "阅读网页",
    "create_session": "创建会话提醒",
    "notify": "发送通知",
    "dispatch_research": "深度调研",
    "dispatch_subagents": "派发子任务",
    "await_subagents": "等待子任务",
    "control_subagent": "控制子任务",
    "recall_memory": "回忆记忆",
    "save_memory": "保存记忆",
    "update_profile": "更新用户档案",
    "search_knowledge": "搜索知识库",
    "read_attached_source": "读取附件",
}

# Parameter field labels shown in the scene action builder.
PARAM_LABELS_ZH: dict[str, str] = {
    "name": "名称",
    "path": "路径",
    "src": "源路径",
    "dst": "目标路径",
    "command": "命令",
    "purpose": "目的",
    "workdir": "工作目录",
    "timeout": "超时(ms)",
    "query": "查询",
    "url": "网址",
    "content": "内容",
    "title": "标题",
    "tag": "标签",
    "pid": "进程 PID",
    "level": "音量",
    "enabled": "启用",
    "goal": "调研目标",
    "key": "键",
    "value": "值",
    "text": "文本",
    "pattern": "匹配模式",
    "offset": "起始行",
    "limit": "行数",
    "title_pattern": "窗口标题",
    "target": "目标控件",
    "button": "鼠标按键",
    "clear": "先清空",
    "keys": "快捷键",
    "max_depth": "控件树深度",
    "max_nodes": "控件数量上限",
    "control_type": "控件类型",
    "name_contains": "名称包含",
    "x": "X 坐标",
    "y": "Y 坐标",
    "dx": "X 偏移",
    "dy": "Y 偏移",
    "direction": "滚动方向",
    "amount": "滚动行数",
    "tasks": "子任务列表",
    "task_id": "任务 ID",
    "task_ids": "任务 ID 列表",
    "action": "操作",
    "instruction": "补充指令",
    "timeout_s": "超时(秒)",
}


def tool_label(name: str) -> str:
    return TOOL_LABELS_ZH.get(name, name)


def param_label(name: str) -> str:
    return PARAM_LABELS_ZH.get(name, name)
