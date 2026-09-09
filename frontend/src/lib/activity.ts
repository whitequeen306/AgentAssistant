/** Categorize agent tool calls into Cursor-style activity summaries. */

import type { ActivityStep } from "@/types";
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
  if (sec < 1) return "Thought briefly";
  return `Thought for ${sec}s`;
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
      return q ? `Searched "${q.length > 40 ? q.slice(0, 39) + "…" : q}"` : "Searched the web";
    }
    case "read_page": {
      const url = str(a.url);
      return url ? `Read ${shortUrl(url)}` : "Read a page";
    }
    case "extract_content": {
      const url = str(a.url);
      return url ? `Extracted from ${shortUrl(url)}` : "Extracted page content";
    }
    case "read_file":
    case "read_attached_source":
    case "read_local_source": {
      const path = str(a.path || a.filename);
      return path ? `Read ${shortPath(path)}` : "Read a file";
    }
    case "edit_file": {
      const path = str(a.path);
      return path ? `Edited ${shortPath(path)}` : "Edited a file";
    }
    case "write_file": {
      const path = str(a.path);
      return path ? `Wrote ${shortPath(path)}` : "Wrote a file";
    }
    case "move_file": {
      const src = str(a.src || a.source || a.path);
      return src ? `Moved ${shortPath(src)}` : "Moved a file";
    }
    case "list_files": {
      const path = str(a.path || a.directory || a.dir);
      return path ? `Listed ${shortPath(path)}` : "Listed files";
    }
    case "run_command": {
      const cmd = str(a.command || a.cmd);
      return cmd
        ? `Ran \`${cmd.length > 36 ? cmd.slice(0, 35) + "…" : cmd}\``
        : "Ran a command";
    }
    case "dispatch_research": {
      const goal = str(a.goal);
      return goal
        ? `Dispatched research: ${goal.length > 36 ? goal.slice(0, 35) + "…" : goal}`
        : "Dispatched research";
    }
    case "search_knowledge": {
      const q = str(a.query);
      return q ? `Queried knowledge: "${q.length > 32 ? q.slice(0, 31) + "…" : q}"` : "Queried knowledge";
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

/** Collapsed summary in English, Cursor-style. */
export function summarizeActivity(steps: ActivityStep[]): string {
  if (!steps.length) return "Working…";

  const nonThought = steps.filter((s) => s.category !== "thought");
  const pendingThought = steps.find(
    (s) => s.category === "thought" && s.status === "pending",
  );
  if (!nonThought.length) {
    if (pendingThought) return "Thinking…";
    const last = [...steps].reverse().find((s) => s.category === "thought");
    return last?.label || "Thought";
  }

  const counts: Partial<Record<ActivityCategory, number>> = {};
  for (const s of nonThought) {
    const cat = s.category as ActivityCategory;
    counts[cat] = (counts[cat] || 0) + 1;
  }

  const parts: string[] = [];
  const n = (c: ActivityCategory) => counts[c] || 0;

  if (n("edit_file")) {
    parts.push(`Edited ${n("edit_file")} file${n("edit_file") === 1 ? "" : "s"}`);
  }
  if (n("write_file")) {
    parts.push(`Wrote ${n("write_file")} file${n("write_file") === 1 ? "" : "s"}`);
  }
  if (n("read_file")) {
    parts.push(`Read ${n("read_file")} file${n("read_file") === 1 ? "" : "s"}`);
  }
  if (n("list_files")) {
    parts.push(`Explored ${n("list_files")} path${n("list_files") === 1 ? "" : "s"}`);
  }
  if (n("read_page")) {
    parts.push(`Read ${n("read_page")} page${n("read_page") === 1 ? "" : "s"}`);
  }
  if (n("search")) {
    parts.push(`Searched ${n("search")} time${n("search") === 1 ? "" : "s"}`);
  }
  if (n("command")) {
    parts.push(`Ran ${n("command")} command${n("command") === 1 ? "" : "s"}`);
  }
  if (n("research")) {
    parts.push(
      n("research") === 1 ? "Dispatched research" : `Dispatched research ${n("research")} times`,
    );
  }
  if (n("knowledge")) {
    parts.push(`Queried knowledge ${n("knowledge")} time${n("knowledge") === 1 ? "" : "s"}`);
  }
  if (n("memory")) {
    parts.push(`Memory ${n("memory")} call${n("memory") === 1 ? "" : "s"}`);
  }
  if (n("note")) {
    parts.push(`Saved ${n("note")} note${n("note") === 1 ? "" : "s"}`);
  }
  if (n("other")) {
    parts.push(`Called ${n("other")} tool${n("other") === 1 ? "" : "s"}`);
  }

  if (!parts.length) {
    return `Called ${nonThought.length} tool${nonThought.length === 1 ? "" : "s"}`;
  }

  // Prefix total tool count when there are multiple categories
  if (parts.length > 1 || nonThought.length > 1) {
    const head = `${nonThought.length} tool${nonThought.length === 1 ? "" : "s"}`;
    return `${head}: ${parts.join(", ")}`;
  }
  return parts[0];
}
