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
