import { useRef, type ReactNode } from "react";
import { gsap, useGSAP, prefersReducedMotion } from "@/lib/gsap";

/** Smooth glass page swap for chat / scenes / library / knowledge / settings. */
export function PageTransition({ page, children }: { page: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  // Skip first paint — animating from autoAlpha:0 on mount can stick invisible
  // in WebView2 (esp. with CSS filter). Only morph on subsequent page changes.
  const first = useRef(true);

  useGSAP(
    () => {
      const el = ref.current;
      if (!el) return;
      if (first.current) {
        first.current = false;
        gsap.set(el, { clearProps: "all" });
        return;
      }
      if (prefersReducedMotion()) {
        gsap.set(el, { clearProps: "all" });
        return;
      }
      // No CSS filter — WebView2 often blanks the whole surface with filter:blur.
      gsap.fromTo(
        el,
        { autoAlpha: 0, y: 10, scale: 0.99 },
        {
          autoAlpha: 1,
          y: 0,
          scale: 1,
          duration: 0.32,
          ease: "power3.out",
          overwrite: true,
        },
      );
    },
    { dependencies: [page] },
  );

  return (
    <div ref={ref} className="flex min-h-0 flex-1 flex-col will-change-transform">
      {children}
    </div>
  );
}
