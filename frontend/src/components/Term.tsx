import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { GLOSSARY, type Ctx } from "../glossary";

const HOVER_DELAY_MS = 600;   // long enough to skip fly-bys, short enough to feel instant

/**
 * Wraps a term. After a short hover it explains the concept the way it is learned:
 * the idea in plain words, what the number on screen means here, then the formal term.
 * Rendered through a portal at a fixed screen position so no container can clip it.
 * A dotted underline appears on hover as the cue that help is coming. Deterministic
 * templates only; no model call is ever made for a tooltip.
 */
export default function Term({ k, value, detail, ctx, children, className }: {
  k: keyof typeof GLOSSARY; value?: unknown; detail?: string; ctx?: Ctx; children: React.ReactNode; className?: string;
}) {
  const [pos, setPos] = useState<{ x: number; y: number; up: boolean } | null>(null);
  const ref = useRef<HTMLSpanElement>(null);
  const timer = useRef<number | null>(null);
  const clear = () => { if (timer.current) { window.clearTimeout(timer.current); timer.current = null; } };
  useEffect(() => clear, []);
  const g = GLOSSARY[k]?.(value, detail, ctx);
  if (!g) return <span className={className}>{children}</span>;
  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const up = r.bottom > window.innerHeight - 260;
    setPos({ x: Math.min(r.left, window.innerWidth - 360), y: up ? r.top - 6 : r.bottom + 6, up });
  };
  return (
    <span
      ref={ref}
      className={`hover:underline decoration-dotted decoration-zinc-500 underline-offset-4 ${className ?? ""}`}
      onMouseEnter={() => { clear(); timer.current = window.setTimeout(show, HOVER_DELAY_MS); }}
      onMouseLeave={() => { clear(); setPos(null); }}
    >
      {children}
      {pos && createPortal(
        <div
          className="fixed z-[100] w-[21rem] rounded border border-zinc-600 bg-zinc-900 p-3 text-sm normal-case tracking-normal font-normal text-left shadow-2xl pointer-events-none"
          style={{ left: pos.x, top: pos.up ? undefined : pos.y, bottom: pos.up ? window.innerHeight - pos.y : undefined }}
        >
          <div className="font-semibold text-zinc-100">{g.title}</div>
          {g.term && <div className="text-xs text-zinc-500 mb-1.5">{g.term}</div>}
          {g.here && <div className="text-sky-200 mb-1.5">{g.here}</div>}
          <div className="text-zinc-300">{g.what}</div>
          {g.formal && <div className="text-xs text-zinc-500 mt-1.5">{g.formal}</div>}
        </div>,
        document.body,
      )}
    </span>
  );
}
