import { INTERVALS, type Interval } from "../types";

export default function IntervalToggle({ value, onChange }: { value: Interval; onChange: (i: Interval) => void }) {
  return (
    <div className="inline-flex rounded border border-zinc-800 overflow-hidden">
      {INTERVALS.map((iv) => (
        <button
          key={iv}
          onClick={() => onChange(iv)}
          className={`px-2.5 py-1 text-sm ${iv === value ? "bg-zinc-700 text-white" : "text-zinc-400 hover:bg-zinc-800"}`}
        >
          {iv}
        </button>
      ))}
    </div>
  );
}
