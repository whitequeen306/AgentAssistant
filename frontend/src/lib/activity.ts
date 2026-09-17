/** Categorize agent tool calls into Cursor-style activity summaries. */

import type { ActivityStep, SubagentTaskView } from "@/types";
import { toolLabel } from "@/lib/toolLabels";

export type ActivityCategory =
  | "thought"
  | "search"
  | "read_page"
  | "read_file"
  | "edit_file"
  | "write_file"
  | "list_files"
  | "command"
  | "research"
  | "knowledge"
  | "memory"
  | "note"
  | "other";

export function formatThoughtLabel(startedAt: number, endedAt = Date.now()): string {
  const sec = Math.max(0, Math.round((endedAt - startedAt) / 1000));
  if (sec < 1) return "思考片刻";
  return `思考 ${sec} 秒`;
}

function parseArgs(raw?: string): Record<string, unknown> {
  if (!raw) return {};
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

function shortUrl(url: string, max = 48): string {
  try {
    const u = new URL(url);
    const path = u.pathname === "/" ? "" : u.pathname;
    const s = u.host + path;
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  } catch {
    return url.length > max ? url.slice(0, max - 1) + "…" : url;
  }
}

function shortPath(path: string, max = 40): string {
  const norm = path.replace(/\\/g, "/");
  const base = norm.split("/").filter(Boolean).slice(-2).join("/") || norm;
  return base.length > max ? "…" + base.slice(-(max - 1)) : base;
}

export function categorizeTool(name: string): ActivityCategory {
  switch (name) {
    case "web_search":
      return "search";
    case "read_page":
    case "extract_content":
      return "read_page";
    case "read_file":
    case "read_attached_source":
    case "read_local_source":
    case "get_selection":
      return "read_file";
    case "edit_file":
      return "edit_file";
    case "write_file":
    case "move_file":
      return "write_file";
    case "list_files":
      return "list_files";
    case "run_command":
      return "command";
    case "dispatch_research":
      return "research";
    case "search_knowledge":
      return "knowledge";
    case "recall_memory":
    case "save_memory":
    case "update_profile":
      return "memory";
    case "save_note":
      return "note";
    default:
      return "other";
  }
}

export function stepLabel(name: string, args?: string): string {
  const a = parseArgs(args);
  switch (name) {
    case "web_search": {
      const q = str(a.query || a.q || a.search);
      return q ? `搜索「${q.length > 40 ? q.slice(0, 39) + "…" : q}」` : "搜索网页";
    }
    case "read_page": {
      const url = str(a.url);
      return url ? `读取 ${shortUrl(url)}` : "读取页面";
    }
    case "extract_content": {
      const url = str(a.url);
      return url ? `提取 ${shortUrl(url)}` : "提取页面内容";
    }
    case "read_file":
    case "read_attached_source":
    case "read_local_source": {
      const path = str(a.path || a.filename);
      return path ? `读取 ${shortPath(path)}` : "读取文件";
    }
    case "edit_file": {
      const path = str(a.path);
      return path ? `修改 ${shortPath(path)}` : "修改文件";
    }
    case "write_file": {
      const path = str(a.path);
      return path ? `写入 ${shortPath(path)}` : "写入文件";
    }
    case "move_file": {
      const src = str(a.src || a.source || a.path);
      return src ? `移动 ${shortPath(src)}` : "移动文件";
    }
    case "list_files": {
      const path = str(a.path || a.directory || a.dir);
      return path ? `浏览 ${shortPath(path)}` : "浏览目录";
    }
    case "run_command": {
      const cmd = str(a.command || a.cmd);
      return cmd
        ? `执行 \`${cmd.length > 36 ? cmd.slice(0, 35) + "…" : cmd}\``
        : "执行命令";
    }
    case "dispatch_research": {
      const goal = str(a.goal);
      return goal
        ? `派出调研：${goal.length > 36 ? goal.slice(0, 35) + "…" : goal}`
        : "派出调研";
    }
    case "search_knowledge": {
      const q = str(a.query);
      return q ? `检索知识库「${q.length > 32 ? q.slice(0, 31) + "…" : q}」` : "检索知识库";
    }
    case "recall_memory":
      return "回忆记忆";
    case "save_memory":
      return "保存记忆";
    case "launch_app": {
      const app = str(a.name);
      return app ? `打开应用「${app}」` : "打开应用";
    }
    case "focus_window": {
      const app = str(a.name || a.title);
      return app ? `聚焦「${app}」` : "聚焦窗口";
    }
    case "set_volume":
      return "调节音量";
    case "toggle_notifications":
      return "切换通知勿扰";
    case "save_note": {
      const title = str(a.title);
      return title ? `保存笔记「${title}」` : "保存笔记";
    }
    default:
      return toolLabel(name);
  }
}

/** 折叠态的摘要行（中文，形如「已读取 2 个页面，搜索 3 次」）。 */
export function summarizeActivity(steps: ActivityStep[]): string {
  if (!steps.length) return "处理中…";

  const nonThought = steps.filter((s) => s.category !== "thought");
  const pendingThought = steps.find(
    (s) => s.category === "thought" && s.status === "pending",
  );
  if (!nonThought.length) {
    if (pendingThought) return "思考中…";
    const last = [...steps].reverse().find((s) => s.category === "thought");
    return last?.label || "思考";
  }

  const counts: Partial<Record<ActivityCategory, number>> = {};
  for (const s of nonThought) {
    const cat = s.category as ActivityCategory;
    counts[cat] = (counts[cat] || 0) + 1;
  }

  const parts: string[] = [];
  const n = (c: ActivityCategory) => counts[c] || 0;
  const push = (c: ActivityCategory, unit: string, verb: string) => {
    const k = n(c);
    if (k) parts.push(`${verb} ${k} ${unit}`);
  };

  push("search", "次", "搜索");
  push("read_page", "个页面", "读取");
  push("read_file", "个文件", "读取");
  push("list_files", "个目录", "浏览");
  push("edit_file", "个文件", "修改");
  push("write_file", "个文件", "写入");
  push("command", "条命令", "执行");
  push("research", "次", "派出调研");
  push("knowledge", "次", "检索知识库");
  push("memory", "次", "记忆调用");
  push("note", "条", "保存笔记");
  push("other", "个", "调用工具");

  if (!parts.length) return `调用了 ${nonThought.length} 个工具`;
  return `已${parts.join("，")}`;
}

/** dispatch_research 步骤里的目标文本（解析失败返回空串）。 */
export function researchGoalOf(step: ActivityStep): string {
  if (!step.args) return "";
  try {
    const g = JSON.parse(step.args)?.goal;
    return typeof g === "string" ? g : "";
  } catch {
    return "";
  }
}

/**
 * 该调研是否已被子任务卡片接管。
 *
 * 一次调研会同时产生两条 UI 线索：主 Agent 的 dispatch_research 工具步骤，
 * 和 SubagentManager 派出的子任务。两者渲染同一件事，必须只显示一张卡片。
 * 走 SubagentManager 时子任务存在 → 内联卡片让位；挂了本地附件走直连路径时
 * 没有子任务 → 内联卡片是唯一展示位置，必须保留。
 */
export function isResearchCovered(
  step: ActivityStep,
  subagents: Record<string, SubagentTaskView>,
): boolean {
  if (step.category !== "research") return false;
  const raw = researchGoalOf(step);
  if (!raw) return false;
  const norm = (s: string) => s.replace(/\s+/g, " ").trim();
  const target = norm(raw);
  const probe = target.slice(0, 40);
  return Object.values(subagents).some((t) => {
    if (t.role !== "researcher" || !t.goal) return false;
    const g = norm(t.goal);
    return g === target || g.startsWith(probe) || target.startsWith(g.slice(0, 40));
  });
}
