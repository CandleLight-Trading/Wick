import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { GLOSSARY } from "../glossary";

const HOVER_DELAY_MS = 600;   // common tooltip delay on the web is 300-700 ms; long enough to skip fly-bys, short enough to feel instant

/**
 * Wraps a term. After a short hover (600 ms, so a passing cursor does not spawn popups)
 * it shows a plain-English explanation and, where the value is known, what it implies here.
 * Rendered through a portal at a fixed screen position so no container can clip it.
 * A dotted underline appears on hover as the cue that help is coming.
 */
export default function Term({ k, value, detail, children, className }: {
  k: keyof typeof GLOSSARY; value?: unknown; detail?: string; children: React.ReactNode; className?: string;
}) {
  const [pos, setPos] = useState<{ x: number; y: number; up: boolean } | null>(null);
  const ref = useRef<HTMLSpanElement>(null);
  const timer = useRef<number | null>(null);
  const clear = () => { if (timer.current) { window.clearTimeout(timer.current); timer.current = null; } };
  useEffect(() => clear, []);
  const g = GLOSSARY[k]?.(value, detail);
  if (!g) return <span className={className}>{children}</span>;
  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const up = r.bottom > window.innerHeight - 220;
    setPos({ x: Math.min(r.left, window.innerWidth - 340), y: up ? r.top - 6 : r.bottom + 6, up });
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
          className="fixed z-[100] w-80 rounded border border-zinc-600 bg-zinc-900 p-3 text-sm normal-case tracking-normal font-normal text-left shadow-2xl pointer-events-none"
          style={{ left: pos.x, top: pos.up ? undefined : pos.y, bottom: pos.up ? window.innerHeight - pos.y : undefined }}
        >
          <div className="font-semibold text-zinc-100 mb-1">{g.title}</div>
          <div className="text-zinc-300">{g.what}</div>
          {g.here && <div className="text-sky-200 mt-1">{g.here}</div>}
        </div>,
        document.body,
      )}
    </span>
  );
}
