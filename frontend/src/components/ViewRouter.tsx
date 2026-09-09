import { useEffect, useRef, useState, type ReactNode } from "react";
import { gsap, useGSAP, prefersReducedMotion } from "@/lib/gsap";
import { useStore } from "@/lib/store";
import { DockedSearch } from "@/views/DockedSearch";
import { PopupChat } from "@/views/PopupChat";
import { MainView } from "@/views/MainView";
import type { WindowState } from "@/types";

const VIEWS: Record<WindowState, () => ReactNode> = {
  "docked-search": () => <DockedSearch />,
  "popup-chat": () => <PopupChat />,
  main: () => <MainView />,
};

/**
 * Crossfade between the three window morphs:
 * docked-search (pill) → popup-chat (compact) → main (full app).
 */
export function ViewRouter() {
  const state = useStore((s) => s.state);
  const container = useRef<HTMLDivElement>(null);
  const incomingRef = useRef<HTMLDivElement>(null);
  const outgoingRef = useRef<HTMLDivElement>(null);
  const prev = useRef<WindowState>(state);
  const [displayed, setDisplayed] = useState<WindowState>(state);
  const [outgoing, setOutgoing] = useState<WindowState | null>(null);

  useEffect(() => {
    document.documentElement.dataset.windowState = state;
    return () => {
      delete document.documentElement.dataset.windowState;
    };
  }, [state]);

  useEffect(() => {
    if (state === prev.current) return;
    const from = prev.current;
    prev.current = state;
    if (prefersReducedMotion()) {
      setOutgoing(null);
      setDisplayed(state);
      return;
    }
    setOutgoing(from);
    setDisplayed(state);
  }, [state]);

  useGSAP(
    () => {
      if (!incomingRef.current) return;
      if (!outgoing) {
        gsap.set(incomingRef.current, { clearProps: "all" });
        return;
      }
      if (prefersReducedMotion()) {
        gsap.set(incomingRef.current, { clearProps: "all" });
        return;
      }
      gsap.fromTo(
        incomingRef.current,
        { autoAlpha: 0, scale: 0.96, transformOrigin: "top center" },
        {
          autoAlpha: 1,
          scale: 1,
          transformOrigin: "top center",
          duration: 0.32,
          ease: "power3.out",
          overwrite: true,
        },
      );
    },
    { dependencies: [displayed, outgoing], scope: container },
  );

  useGSAP(
    (_ctx, contextSafe) => {
      if (!outgoingRef.current || !outgoing) return;
      if (prefersReducedMotion()) {
        setOutgoing(null);
        return;
      }
      const clear = () => setOutgoing(null);
      const done = contextSafe ? contextSafe(clear) : clear;
      gsap.to(outgoingRef.current, {
        autoAlpha: 0,
        scale: 0.96,
        transformOrigin: "top center",
        duration: 0.22,
        ease: "power2.in",
        overwrite: true,
        onComplete: done,
      });
    },
    { dependencies: [outgoing], scope: container },
  );

  const Disp = VIEWS[displayed] ?? VIEWS.main;
  const Out = outgoing ? VIEWS[outgoing] : null;

  return (
    <div ref={container} data-card-root className="relative h-full w-full">
      <div ref={incomingRef} className="h-full w-full">
        {Disp()}
      </div>
      {Out && (
        <div ref={outgoingRef} className="pointer-events-none absolute inset-0 h-full w-full">
          {Out()}
        </div>
      )}
    </div>
  );
}
