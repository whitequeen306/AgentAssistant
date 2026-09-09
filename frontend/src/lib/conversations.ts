import { getApi } from "@/lib/bridge";
import { actions, getState } from "@/lib/store";

/** Conversation CRUD — thin wrappers over the bridge that update the store. */

export async function refreshConversations(query = ""): Promise<void> {
  const api = getApi();
  if (!api) return;
  try {
    const list = await api.list_conversations(query);
    actions.setConversations(list);
  } catch {
    /* keep */
  }
}

export async function switchConversation(convId: string): Promise<void> {
  const api = getApi();
  if (!api) return;
  let messages = [];
  try {
    messages = await api.switch_conversation(convId);
  } catch {
    return;
  }
  actions.setConversations(getState().conversations, convId);
  actions.loadMessages(messages);
  // Persisted subagent tasks of this conversation (best-effort).
  try {
    const tasks = await api.list_subagent_tasks(convId);
    if (getState().activeConv === convId) {
      actions.hydrateSubagents(tasks || []);
    }
  } catch {
    /* task rows are additive; chat is already usable */
  }
  actions.setPage("chat");
  await refreshConversations();
}

export async function newConversation(): Promise<void> {
  const api = getApi();
  if (!api) return;
  try {
    const conv = await api.new_conversation();
    actions.setConversations(getState().conversations, conv.id);
    actions.loadMessages([]);
    actions.setPage("chat");
    await refreshConversations();
  } catch {
    /* ignore */
  }
}

export async function deleteConversation(convId: string): Promise<void> {
  const api = getApi();
  if (!api) return;
  if (!confirm("删除该会话及其消息？")) return;
  await api.delete_conversation(convId);
  if (convId === getState().activeConv) {
    actions.setConversations([], null);
    actions.loadMessages([]);
  }
  await refreshConversations();
}

export async function pinConversation(convId: string, pinned: boolean): Promise<void> {
  const api = getApi();
  if (!api) return;
  await api.pin_conversation(convId, pinned);
  await refreshConversations();
}

export async function renameConversation(convId: string, title: string): Promise<void> {
  const api = getApi();
  if (!api) return;
  await api.rename_conversation(convId, title);
  await refreshConversations();
}
