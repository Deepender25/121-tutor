// Tooltips for any element with data-tip="…" (optionally data-tip-side="top"). One fixed-position bubble on the
// body, so scrolling panels and overflow can't clip it; it's placed from the element's on-screen box, flips above
// or below when there's no room, and is kept inside the window. Mouse/pen hover after a short delay (instant when
// moving between buttons), keyboard focus at once; never for touch, where a tap would leave it stuck. The element
// keeps its own aria-label, so the bubble itself is hidden from screen readers.

const SHOW_DELAY = 450;
const WARM_MS = 500; // after one closes, the next one opens without the delay
const MARGIN = 8; // from the window edge
const GAP = 8; // from the element

export function installTooltips(): () => void {
  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.setAttribute("aria-hidden", "true");
  document.body.appendChild(tip);

  let target: Element | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let lastHidden = 0;

  const place = () => {
    if (!target) return;
    const r = target.getBoundingClientRect();
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const preferTop = target.getAttribute("data-tip-side") === "top";
    const above = r.top - GAP - h;
    const below = r.bottom + GAP;
    let top = preferTop ? above : below;
    if (preferTop && above < MARGIN) top = below;
    if (!preferTop && below + h > innerHeight - MARGIN) top = above;
    top = Math.max(MARGIN, Math.min(top, innerHeight - h - MARGIN));
    const left = Math.max(MARGIN, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - MARGIN));
    tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  };

  const show = (el: Element, immediate: boolean) => {
    if (el === target) return;
    clearTimeout(timer);
    target = el;
    const delay = immediate || performance.now() - lastHidden < WARM_MS ? 0 : SHOW_DELAY;
    timer = setTimeout(() => {
      const text = el.getAttribute("data-tip");
      if (target !== el || !el.isConnected || !text) return;
      tip.textContent = text;
      place();
      tip.classList.add("show");
    }, delay);
  };

  const hide = () => {
    clearTimeout(timer);
    if (tip.classList.contains("show")) lastHidden = performance.now();
    tip.classList.remove("show");
    target = null;
  };

  const tipTarget = (node: EventTarget | null) => (node instanceof Element ? node.closest("[data-tip]") : null);

  const onOver = (e: PointerEvent) => {
    if (e.pointerType === "touch") return;
    const el = tipTarget(e.target);
    if (el) show(el, false);
    else if (target) hide();
  };
  const onFocus = (e: FocusEvent) => {
    const el = tipTarget(e.target);
    if (el && e.target instanceof Element && e.target.matches(":focus-visible")) show(el, true);
  };
  const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") hide(); };

  document.addEventListener("pointerover", onOver);
  document.addEventListener("pointerdown", hide, true);
  document.addEventListener("focusin", onFocus);
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", hide, true);
  document.addEventListener("keydown", onKey);
  window.addEventListener("blur", hide);
  window.addEventListener("resize", hide);
  document.documentElement.addEventListener("pointerleave", hide); // the mouse left the window
  return () => {
    document.documentElement.removeEventListener("pointerleave", hide);
    hide();
    document.removeEventListener("pointerover", onOver);
    document.removeEventListener("pointerdown", hide, true);
    document.removeEventListener("focusin", onFocus);
    document.removeEventListener("focusout", hide);
    document.removeEventListener("scroll", hide, true);
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("blur", hide);
    window.removeEventListener("resize", hide);
    tip.remove();
  };
}
