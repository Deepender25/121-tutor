// Time, greeting and count formatting (shared by home and notebook views).

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  const units: [number, string][] = [[60, "minute"], [3600, "hour"], [86400, "day"], [604800, "week"], [2629800, "month"], [31557600, "year"]];
  for (let i = units.length - 1; i >= 0; i--) {
    const [size, name] = units[i];
    if (seconds >= size) {
      const n = Math.floor(seconds / size);
      return `${n} ${name}${n > 1 ? "s" : ""} ago`;
    }
  }
  return "";
}

export function clockTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function greeting(): string {
  const hour = new Date().getHours();
  return hour < 5 ? "Working late" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

export function dayLabel(iso: string): string {
  const d = new Date(iso);
  return d.toDateString() === new Date().toDateString()
    ? "Today"
    : d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
}

// Headings in PDFs often carry emoji ("🧠 Memory Trick"); the interface shows names as plain text. Removes
// pictographs with their joiners, variation selectors and skin-tone modifiers, then tidies the spaces left behind.
const EMOJI = /(?:\p{Extended_Pictographic}|\p{Regional_Indicator}|[\u{1F3FB}-\u{1F3FF}\u20E3\uFE0F\u200D])+/gu;
export function plain(text: string | null | undefined): string {
  return (text || "").replace(EMOJI, " ").replace(/\s{2,}/g, " ").replace(/\(\s+/g, "(").replace(/\s+\)/g, ")").trim();
}

/** "1 source", "3 sources". */
export const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** A reading position's pages in prose: "Page 12", or "Pages 12–13" (en dash). Chips keep "p. 12-13". */
export function pagesLabel(pages: string | number | null | undefined): string {
  const p = String(pages ?? "").trim();
  if (!p) return "";
  const [first, last] = p.split(/\s*[-–]\s*/);
  return last && last !== first ? `Pages ${first}–${last}` : `Page ${first}`;
}
