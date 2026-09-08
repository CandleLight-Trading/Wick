import { useMemo, useState } from "react";
import type { SymbolInfo } from "../types";

interface Props {
  all: SymbolInfo[];
  selected: string[];
  onChange: (next: string[]) => void;
  max?: number;
}

/** Searchable multi-select over the whole exchangeInfo list. USDT pairs by default;
 *  type a quote like "BTC" or "/" to widen the search. Selection order is display order. */
export default function SymbolPicker({ all, selected, onChange, max = 8 }: Props) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);

  const matches = useMemo(() => {
    const needle = q.trim().toUpperCase();
    const pool = needle.includes("/") || needle.length > 5 ? all : all.filter((s) => s.quote === "USDT");
    const term = needle.replace("/", "");
    return pool
      .filter((s) => !selected.includes(s.symbol) && (!term || s.symbol.includes(term)))
      .slice(0, 40);
  }, [all, q, selected]);

  const add = (sym: string) => {
    if (selected.length >= max) return;
    onChange([...selected, sym]);
    setQ("");
  };
  const remove = (sym: string) => onChange(selected.filter((s) => s !== sym));
  const focus = (sym: string) => onChange([sym, ...selected.filter((s) => s !== sym)]);

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {selected.map((sym, i) => (
        <span
          key={sym}
          className={`flex items-center gap-1 pl-2 pr-1 py-1 rounded text-sm ${i === 0 ? "bg-zinc-700 text-white" : "bg-zinc-800 text-zinc-300"}`}
        >
          <button onClick={() => focus(sym)} title="Move to first slot">{sym}</button>
          <button onClick={() => remove(sym)} className="text-zinc-500 hover:text-white px-1">×</button>
        </span>
      ))}
      <div className="relative">
        <input
          value={q}
          onChange={(e) => { setQ(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          placeholder={selected.length >= max ? `Max ${max}` : "Add symbol…"}
          disabled={selected.length >= max}
          className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-sm w-36 outline-none focus:border-zinc-600"
        />
        {open && matches.length > 0 && (
          <ul className="absolute z-20 mt-1 max-h-64 w-56 overflow-auto rounded border border-zinc-800 bg-zinc-900 shadow-xl text-sm">
            {matches.map((s) => (
              <li
                key={s.symbol}
                onMouseDown={() => add(s.symbol)}
                className="px-2 py-1 cursor-pointer hover:bg-zinc-800 flex justify-between"
              >
                <span>{s.symbol}</span>
                <span className="text-zinc-500">{s.tracked ? "tracked" : ""}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
