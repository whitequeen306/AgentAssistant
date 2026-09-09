import { useEffect, useRef } from "react";
import { useStore } from "@/lib/store";

/** Decorative canvas waveform shown while dictating/listening. */
export function VoiceWave({ height = 40 }: { height?: number }) {
  const voice = useStore((s) => s.voiceState);
  const dictating = useStore((s) => s.dictating);
  const active = dictating || voice === "listening";
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (!active) {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      return;
    }
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let t = 0;
    const accent =
      getComputedStyle(document.documentElement).getPropertyValue("--color-accent").trim() ||
      "#007aff";
    const draw = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = accent;
      ctx.lineWidth = 2;
      ctx.beginPath();
      const mid = canvas.height / 2;
      for (let x = 0; x <= canvas.width; x += 2) {
        const env = Math.sin((x / canvas.width) * Math.PI);
        const y =
          mid +
          Math.sin(x * 0.07 + t) *
            env *
            (mid - 4) *
            (0.4 + 0.6 * Math.abs(Math.sin(t * 0.7)));
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
      t += 0.25;
      rafRef.current = requestAnimationFrame(draw);
    };
    draw();
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [active]);

  if (!active) return null;
  return (
    <canvas
      ref={canvasRef}
      width={380}
      height={height}
      className="mx-4 mb-1 rounded-sm bg-surface-sunken"
      aria-hidden
    />
  );
}
