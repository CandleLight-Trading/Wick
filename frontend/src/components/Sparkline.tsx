/** Inline SVG line of the last ~24 hourly closes. Colour follows first-to-last direction. */
export default function Sparkline({ values, width = 96, height = 24 }: { values: number[] | null; width?: number; height?: number }) {
  if (!values || values.length < 2) return <span className="text-zinc-500 text-sm">–</span>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values
    .map((v, i) => `${(i / (values.length - 1)) * width},${height - ((v - min) / span) * (height - 2) - 1}`)
    .join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={width} height={height} className="block">
      <polyline points={pts} fill="none" stroke={up ? "#34d399" : "#f87171"} strokeWidth="1.25" />
    </svg>
  );
}
