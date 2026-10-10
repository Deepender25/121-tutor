// The Voice Tutor mark as an inline SVG (solid currentColor, so it follows the theme).
import { MARK_D } from "./mark-path.js";

const NS = "http://www.w3.org/2000/svg";

export function markIcon(cls = "") {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("class", `mark ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", MARK_D);
  path.setAttribute("fill", "currentColor");
  svg.append(path);
  return svg;
}
