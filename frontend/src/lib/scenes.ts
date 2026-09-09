import { getApi } from "@/lib/bridge";
import { actions } from "@/lib/store";
import type { Scene } from "@/types";

export async function refreshScenes(): Promise<void> {
  const api = getApi();
  if (!api) return;
  try {
    const list = await api.list_scenes();
    actions.setScenes(list);
  } catch {
    /* keep */
  }
}

export async function saveScene(scene: Scene): Promise<void> {
  const api = getApi();
  if (!api) return;
  await api.save_scene(scene);
  await refreshScenes();
}

export async function deleteScene(id: string): Promise<void> {
  const api = getApi();
  if (!api) return;
  await api.delete_scene(id);
  await refreshScenes();
}

export async function testScene(id: string): Promise<void> {
  const api = getApi();
  if (!api) return;
  // Backend creates a fresh conversation and pushes scene_test;
  // App.tsx switches into it — do not inject into the current chat.
  await api.test_scene(id);
}
