import { getApi } from "@/lib/bridge";
import { actions, getState } from "@/lib/store";

/** Toggle continuous dictation on/off (PTT). The active ChatInput snapshots
 * its current text as the dictation base when it notices dictating flip on. */
export async function togglePtt(): Promise<void> {
  const api = getApi();
  if (!api) return;
  if (!getState().dictating) {
    const ok = await api.start_dictation();
    if (ok) actions.setDictating(true);
  } else {
    actions.setDictating(false);
    await api.stop_dictation();
  }
}
