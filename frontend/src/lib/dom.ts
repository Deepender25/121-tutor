// Tiny element builder for the imperative engines (PDF viewer) that manage their own DOM outside React.
import { LUCIDE } from "./icons";

type Props = Record<string, unknown> & { class?: string; style?: Record<string, string>; dataset?: Record<string, string> };

export function h(tag: string, props: Props = {}, ...children: (Node | string | null | undefined | false)[]): HTMLElement {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = String(value);
    else if (key === "style") {
      for (const [prop, v] of Object.entries(value as Record<string, string>)) {
        if (prop.startsWith("--")) el.style.setProperty(prop, v);
        else (el.style as unknown as Record<string, string>)[prop] = v;
      }
    } else if (key === "dataset") Object.assign(el.dataset, value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// Same markup as the <Icon> component, for DOM built outside React.
export function iconEl(name: string, cls = ""): SVGElement {
  const span = document.createElement("span");
  span.innerHTML = `<svg class="icon ic-${name} ${cls}" viewBox="0 0 24 24" aria-hidden="true">${LUCIDE[name] || LUCIDE.sparkles}</svg>`;
  return span.firstChild as SVGElement;
}
