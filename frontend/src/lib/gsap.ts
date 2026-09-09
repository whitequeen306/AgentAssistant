import gsap from "gsap";
import { useGSAP } from "@gsap/react";

// Register the React plugin once (importing this module performs registration).
gsap.registerPlugin(useGSAP);

export { gsap, useGSAP };

/** True when the user has requested reduced motion — gate all non-essential
 *  animation. Under reduced-motion we skip gsap tweens and leave content in
 *  its natural visible state (progressive enhancement). */
export const prefersReducedMotion = (): boolean =>
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;
