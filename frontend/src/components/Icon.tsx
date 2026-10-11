// Icons: Lucide (https://lucide.dev, ISC). Each icon gets an `ic-<name>` class so CSS can give it a small,
// meaningful motion on hover/press (see "animated icons" in globals.css).
import { LUCIDE } from "@/lib/icons";
import { MARK_D } from "@/lib/mark-path";

export function Icon({ name, className = "" }: { name: string; className?: string }) {
  return (
    <svg
      className={`icon ic-${name} ${className}`}
      viewBox="0 0 24 24"
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: LUCIDE[name] || LUCIDE.sparkles }}
    />
  );
}

// Notebook icons (stored by the server without the "nb-" prefix).
export const NOTEBOOK_ICONS = ["book", "cap", "flask", "brain", "atom", "globe", "landmark", "code", "calculator",
  "scale", "stethoscope", "palette", "music", "leaf", "rocket", "briefcase", "chart", "languages", "dna",
  "telescope", "pen", "map", "lightbulb", "cpu"];

export function NotebookIcon({ name, className = "" }: { name: string; className?: string }) {
  return <Icon name={`nb-${NOTEBOOK_ICONS.includes(name) ? name : "book"}`} className={className} />;
}

// The Voice Tutor mark (solid currentColor, so it follows the theme).
export function Mark({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 100 100" className={`mark ${className}`.trim()} aria-hidden="true">
      <path d={MARK_D} fill="currentColor" />
    </svg>
  );
}

// Circular progress ring (0-100). Size in px.
export function Ring({ percent, size = 28, stroke = 3 }: { percent: number | undefined; size?: number; stroke?: number }) {
  const p = Math.max(0, Math.min(100, Math.round(percent || 0)));
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const half = size / 2;
  return (
    <span className="ring" style={{ width: size, height: size }} title={`${p}% covered`}>
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} aria-hidden="true">
        <circle className="ring-track" cx={half} cy={half} r={r} strokeWidth={stroke} fill="none" />
        <circle className="ring-fill" cx={half} cy={half} r={r} strokeWidth={stroke} fill="none"
          strokeDasharray={c} strokeDashoffset={c * (1 - p / 100)} strokeLinecap="round"
          transform={`rotate(-90 ${half} ${half})`} />
      </svg>
    </span>
  );
}
