/** Chinese UI labels for tools / params (scene editor). Fallback: English name. */

const TOOL_LABELS: Record<string, string> = {
  launch_app: "打开应用",
  focus_window: "聚焦窗口",
  get_selection: "获取选中文本",
  save_note: "保存笔记",
  run_command: "运行命令",
  read_file: "读取文件",
  write_file: "写入文件",
  edit_file: "编辑文件",
  list_files: "列出文件",
  move_file: "移动文件",
  kill_process: "结束进程",
  set_volume: "调节音量",
  toggle_notifications: "通知勿扰",
  perf_snapshot: "性能快照",
  web_search: "网页搜索",
  read_page: "阅读网页",
  create_session: "创建会话提醒",
  notify: "发送通知",
  dispatch_research: "深度调研",
  ui_inspect: "查看界面控件",
  ui_click: "点击控件",
  ui_type: "输入文字",
  ui_hotkey: "发送快捷键",
  ui_scroll: "滚动界面",
  recall_memory: "回忆记忆",
  save_memory: "保存记忆",
  update_profile: "更新用户档案",
  search_knowledge: "搜索知识库",
  read_attached_source: "读取附件",
};

const PARAM_LABELS: Record<string, string> = {
  name: "名称",
  path: "路径",
  src: "源路径",
  dst: "目标路径",
  command: "命令",
  workdir: "工作目录",
  timeout: "超时(ms)",
  query: "查询",
  url: "网址",
  content: "内容",
  title: "标题",
  tag: "标签",
  pid: "进程 PID",
  level: "音量",
  enabled: "启用",
  goal: "调研目标",
  key: "键",
  value: "值",
  text: "文本",
  pattern: "匹配模式",
  offset: "起始行",
  limit: "行数",
};

export function toolLabel(name: string, backendLabel?: string): string {
  return backendLabel || TOOL_LABELS[name] || name;
}

export function paramLabel(name: string, backendLabel?: string): string {
  return backendLabel || PARAM_LABELS[name] || name;
}
