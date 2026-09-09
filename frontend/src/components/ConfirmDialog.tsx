import { Button } from "@/components/ui";
import { Modal } from "@/components/ui/Modal";
import { getApi } from "@/lib/bridge";
import { actions, useStore } from "@/lib/store";

/** Framework confirm gate modal (save_note, kill_process, etc.).
 *
 * Docked-search never shows this modal (tip / auto-expand instead).
 * Dismiss only via Allow / Deny — overlay click and Esc must not deny.
 */
export function ConfirmDialog() {
  const req = useStore((s) => s.confirmRequest);
  const state = useStore((s) => s.state);
  const showModal = !!req && state !== "docked-search";

  const respond = (approved: boolean) => {
    actions.clearConfirmRequest();
    actions.clearConfirmTip();
    getApi()?.respond_confirm?.(approved).catch(() => {});
  };

  return (
    <Modal
      open={showModal}
      dismissible={false}
      onOpenChange={() => {
        /* intentional no-op: only Allow / Deny may close */
      }}
      title="需要确认"
      footer={
        <>
          <Button variant="ghost" onClick={() => respond(false)}>
            拒绝
          </Button>
          <Button onClick={() => respond(true)}>允许</Button>
        </>
      }
    >
      <p className="text-sm text-secondary">
        助手请求执行工具{" "}
        <code className="rounded-sm bg-surface-sunken px-1 text-primary">
          {req?.tool || "—"}
        </code>
        ，是否允许？
      </p>
      {req?.description ? (
        <p
          data-selectable
          className="mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded-sm bg-surface-sunken p-2 text-xs text-tertiary"
        >
          {req.description}
        </p>
      ) : null}
    </Modal>
  );
}
