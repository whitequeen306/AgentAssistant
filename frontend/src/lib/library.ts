import { getApi, prefetchStartChips } from "@/lib/bridge";
import type { Note } from "@/types";

export async function listNotes(): Promise<Note[]> {
  const api = getApi();
  if (!api) return [];
  return api.list_notes();
}

export async function readNote(
  filename: string,
): Promise<{ ok: boolean; content?: string; error?: string }> {
  const api = getApi();
  if (!api) return { ok: false, error: "bridge unavailable" };
  return api.read_note(filename);
}

export async function updateNote(
  filename: string,
  content: string,
  title?: string,
): Promise<{ ok: boolean; error?: string; filename?: string }> {
  const api = getApi();
  if (!api) return { ok: false, error: "bridge unavailable" };
  const res = await api.update_note(filename, content, title);
  // 学习库变更 → 后台预生成开场 chips（防抖）。
  prefetchStartChips();
  return res;
}

export async function deleteNote(
  filename: string,
): Promise<{ ok: boolean; error?: string }> {
  const api = getApi();
  if (!api) return { ok: false, error: "bridge unavailable" };
  const res = await api.delete_note(filename);
  prefetchStartChips();
  return res;
}

/** 导出笔记到本机（系统另存为对话框）；用户取消时 ok=false + cancelled。 */
export async function exportNote(
  filename: string,
): Promise<{ ok: boolean; cancelled?: boolean; path?: string; error?: string }> {
  const api = getApi();
  if (!api) return { ok: false, error: "bridge unavailable" };
  return api.export_note(filename);
}

/** 导出为排版化 Word 文档；旧后端无此能力时降级为 md 导出。 */
export async function exportNoteDocx(
  filename: string,
): Promise<{ ok: boolean; cancelled?: boolean; path?: string; error?: string }> {
  const api = getApi();
  if (!api) return { ok: false, error: "bridge unavailable" };
  if (api.export_note_docx) return api.export_note_docx(filename);
  return exportNote(filename);
}

/** markdown 文本（如调研报告）直接导出 Word。 */
export async function exportMarkdownDocx(
  title: string,
  content: string,
): Promise<{ ok: boolean; cancelled?: boolean; path?: string; error?: string }> {
  const api = getApi();
  if (!api?.export_markdown_docx) return { ok: false, error: "bridge unavailable" };
  return api.export_markdown_docx(title, content);
}

/** 学习库：前端直接存笔记（调研卡片「存入资料库」按钮）。 */
export async function saveNoteFile(
  title: string,
  content: string,
  tag?: string,
): Promise<{ ok: boolean; error?: string; filename?: string; path?: string }> {
  const api = getApi();
  if (!api) return { ok: false, error: "bridge unavailable" };
  const res = await api.save_note_file(title, content, tag);
  prefetchStartChips();
  return res;
}
