/* AgentAssistant browser extension — "交给助手" context menu (J1, 08 §4.5).
 *
 * Right-click selected text → forward to the assistant via native messaging.
 * The native host (agent_assistant_host.py) sends {type: invoke_on_text, text}
 * to the running instance over the single-instance IPC; the UI opens a fresh
 * conversation and the agent offers how to handle the text.
 */
const NATIVE_HOST = "com.agent_assistant.host";

/** Create (or recreate) the selection context-menu item.
 *
 * Must run on every service-worker boot AND on install/update/reload.
 * Edge/Chrome often fire onInstalled with reason "update" when you click
 * Reload on an unpacked extension — the old code skipped create on update,
 * so the menu silently disappeared.
 */
function ensureContextMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create(
      {
        id: "send-to-assistant",
        title: "交给助手",
        contexts: ["selection"],
      },
      () => {
        if (chrome.runtime.lastError) {
          console.error(
            "AgentAssistant: contextMenus.create failed:",
            chrome.runtime.lastError.message,
          );
        }
      },
    );
  });
}

chrome.runtime.onInstalled.addListener(ensureContextMenu);
chrome.runtime.onStartup.addListener(ensureContextMenu);
// MV3 service worker cold start (no install event) — still need the menu.
ensureContextMenu();

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== "send-to-assistant") return;
  const text = (info.selectionText || "").trim();
  if (!text) return;
  chrome.runtime.sendNativeMessage(NATIVE_HOST, { text }, (response) => {
    if (chrome.runtime.lastError) {
      console.error(
        "AgentAssistant: native message failed:",
        chrome.runtime.lastError.message,
      );
      return;
    }
    if (!response || !response.ok) {
      console.error(
        "AgentAssistant: host rejected:",
        (response && response.error) || "no response",
      );
    }
  });
});
