import { ChevronsUp } from "lucide-react";
import { IconButton } from "@/components/ui";
import { Tooltip } from "@/components/ui/Tooltip";
import { goState } from "@/lib/bridge";

/** Collapse the main window into the top search pill. */
export function DockMinimizeButton() {
  return (
    <Tooltip label="最小化到搜索栏">
      <IconButton
        onClick={() => goState("docked-search")}
        aria-label="最小化到搜索栏"
      >
        <ChevronsUp className="h-4 w-4" />
      </IconButton>
    </Tooltip>
  );
}
