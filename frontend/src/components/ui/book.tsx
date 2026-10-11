// A 3D book cover (after the Vercel "Book" design): cloth cover with binding shading, a page edge and a back
// cover, so it reads as an object and turns slightly on hover. Styled by the app's own tokens (globals.css,
// "books"); the size comes from --book-w (CSS) unless `width` is given. Covers are decorative: the title is
// repeated in text next to the book wherever it matters, so the whole thing is hidden from screen readers.
import type { CSSProperties } from "react";
import type { Cover } from "@/lib/covers";

interface BookProps {
  title: string;
  subtitle?: string;
  cover?: Cover | null; // null/undefined: a blank paper book
  width?: number; // px; otherwise the CSS default for where it sits
  className?: string;
}

export function Book({ title, subtitle, cover, width, className }: BookProps) {
  const style = {
    ...(width ? { "--book-w": `${width}px` } : {}),
    ...(cover ? { "--book-color": cover.bg, "--book-ink": cover.ink } : {}),
  } as CSSProperties;
  // Short titles set larger, long ones smaller, so every cover is filled like a designed one.
  const len = title.trim().length <= 10 ? " len-s" : title.trim().length <= 22 ? " len-m" : "";
  return (
    <div className={`book${cover ? "" : " blank"}${len}${className ? ` ${className}` : ""}`} style={style} aria-hidden="true">
      <div className="book-3d">
        <div className="book-cover">
          <div className="book-face">
            <span className="book-title">{title}</span>
            {subtitle ? <span className="book-sub">{subtitle}</span> : null}
          </div>
          <div className="book-bind" />
        </div>
        <div className="book-pages" />
        <div className="book-back" />
      </div>
    </div>
  );
}

/** A small flat cover (no 3D) for headers and lists. */
export function CoverChip({ cover, className, children }: { cover?: Cover | null; className?: string; children?: React.ReactNode }) {
  const style = (cover ? { "--book-color": cover.bg, "--book-ink": cover.ink } : {}) as CSSProperties;
  return (
    <span className={`cover-chip${cover ? "" : " blank"}${className ? ` ${className}` : ""}`} style={style}>
      <span className="book-bind" />
      {children}
    </span>
  );
}
