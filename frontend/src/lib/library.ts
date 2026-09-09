import { getApi } from "@/lib/bridge";
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
  return api.update_note(filename, content, title);
}

export async function deleteNote(
  filename: string,
): Promise<{ ok: boolean; error?: string }> {
  const api = getApi();
  if (!api) return { ok: false, error: "bridge unavailable" };
  return api.delete_note(filename);
}
