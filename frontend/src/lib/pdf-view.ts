// Continuous PDF viewer: every page stacked vertically in reading order, like a desktop PDF reader.
// The whole document is laid out up front from the page sizes (so nothing jumps while you scroll); a page
// is rendered by the server only when it comes near the screen, at the resolution the screen needs, and
// released again when it's far away, so long documents stay light. Zooming (buttons, Ctrl+wheel, trackpad
// pinch) and resizing keep the spot you're reading in place.
//
// It owns the children of `el` (the .viewer-stage element React renders empty).
import { api } from "./api";
import { h } from "./dom";

const FIT_MAX = 920; // 100% = fit the width, but never wider than this (comfortable in full screen)
const PAD = 22; // horizontal padding around the pages (matches .pdf-pages)
const ZOOM_MIN = 0.5;
const ZOOM_MAX = 3;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

interface Page { n: number; w: number; h: number; r: number; el: HTMLElement; img: HTMLImageElement; scale: number }
interface Anchor { p: Page; y: number; frac: number; x: number }

export interface PdfViewOptions {
  el: HTMLElement;
  onPage?: (n: number, total: number) => void;
  onZoom?: (zoom: number) => void;
  onUserScroll?: () => void;
}

export class PdfView {
  el: HTMLElement;
  docId: string | null = null;
  page = 1;
  zoom = 1;
  private onPage: (n: number, total: number) => void;
  private onZoom: (zoom: number) => void;
  private onUserScroll: () => void;
  private inner: HTMLElement;
  private pages: Page[] = [];
  private fit = 600;
  private highlights: HTMLElement[] = [];
  private flags = new Set<string>();
  private raf = 0;
  private rescaleTimer: ReturnType<typeof setTimeout> | undefined;
  private token = 0;
  private pending: { n: number; frac: number } | null = null; // where to go once visible (can't scroll while hidden)
  private io: IntersectionObserver;
  private ro: ResizeObserver;
  private listeners: [string, EventListener, AddEventListenerOptions?][] = [];

  constructor({ el, onPage = () => {}, onZoom = () => {}, onUserScroll = () => {} }: PdfViewOptions) {
    this.el = el;
    this.onPage = onPage;
    this.onZoom = onZoom;
    this.onUserScroll = onUserScroll;
    this.inner = h("div", { class: "pdf-pages" });

    // Pages within ~1.5 screens are rendered; pages that leave that band are released.
    this.io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const p = this.pages[Number((e.target as HTMLElement).dataset.n) - 1];
        if (!p) continue;
        if (e.isIntersecting) this.load(p);
        else this.unload(p);
      }
    }, { root: this.el, rootMargin: "150% 0px" });
    this.ro = new ResizeObserver(() => this.schedule("layout"));
    this.ro.observe(this.el);

    const on = (type: string, fn: EventListener, opts?: AddEventListenerOptions) => {
      this.el.addEventListener(type, fn, opts);
      this.listeners.push([type, fn, opts]);
    };
    on("scroll", () => this.schedule("scroll"), { passive: true });
    // Scrolling by hand (as opposed to follow-along moving the page) is reported, so following can pause.
    const byHand = () => { if (this.pages.length) this.onUserScroll(); };
    on("wheel", ((e: WheelEvent) => { if (!e.ctrlKey) byHand(); }) as EventListener, { passive: true });
    on("touchmove", byHand, { passive: true });
    // Two fingers zoom the pages around the point between them (CSS touch-action stops the browser zooming
    // the whole app instead); one finger keeps scrolling.
    let pinch: { dist: number; zoom: number } | null = null;
    const spread = (t: TouchList) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    on("touchstart", ((e: TouchEvent) => {
      if (e.touches.length === 2 && this.pages.length) pinch = { dist: spread(e.touches) || 1, zoom: this.zoom };
    }) as EventListener, { passive: true });
    on("touchmove", ((e: TouchEvent) => {
      if (!pinch || e.touches.length !== 2) return;
      e.preventDefault();
      const midY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - this.el.getBoundingClientRect().top;
      this.setZoom(pinch.zoom * (spread(e.touches) / pinch.dist), midY);
    }) as EventListener, { passive: false });
    const endPinch = ((e: TouchEvent) => { if (e.touches.length < 2) pinch = null; }) as EventListener;
    on("touchend", endPinch, { passive: true });
    on("touchcancel", endPinch, { passive: true });
    on("pointerdown", ((e: PointerEvent) => { if (e.target === this.el) byHand(); }) as EventListener); // scrollbar
    on("keydown", ((e: KeyboardEvent) => {
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(e.key)) byHand();
    }) as EventListener);
    on("wheel", ((e: WheelEvent) => {
      if (!e.ctrlKey || !this.pages.length) return; // trackpad pinch arrives as Ctrl+wheel
      e.preventDefault();
      this.setZoom(this.zoom * Math.exp(-e.deltaY * 0.0025), e.clientY - this.el.getBoundingClientRect().top);
    }) as EventListener, { passive: false });
  }

  get total() { return this.pages.length; }

  // Show a document. Resolves false if another open() superseded this one.
  async open(docId: string, fallbackTotal = 1): Promise<boolean> {
    if (docId === this.docId && this.pages.length) return true;
    const token = ++this.token;
    let sizes: [number, number][] | null = null;
    try { sizes = (await api.pageSizes(docId)).sizes; } catch { /* lay out as Letter pages instead */ }
    if (token !== this.token) return false;
    if (!sizes?.length) sizes = Array.from({ length: Math.max(1, fallbackTotal) }, () => [612, 792] as [number, number]);
    this.reset();
    this.docId = docId;
    const widths = sizes.map(([w]) => w).sort((a, b) => a - b);
    const ref = widths[Math.floor(widths.length / 2)] || 612; // pages wider than usual (landscape) stay wider
    this.pages = sizes.map(([w, hgt], i) => {
      const img = h("img", { alt: `Page ${i + 1}`, draggable: "false", decoding: "async" }) as HTMLImageElement;
      const el = h("div", { class: "pdf-page", dataset: { n: String(i + 1) },
        style: { "--r": (w / ref).toFixed(4), "--ar": `${w} / ${hgt}` } }, img);
      img.addEventListener("load", () => { if (img.getAttribute("src")) el.classList.add("ready"); });
      return { n: i + 1, w, h: hgt, r: w / ref, el, img, scale: 0 };
    });
    this.inner.replaceChildren(...this.pages.map((p) => p.el));
    this.el.replaceChildren(this.inner);
    this.fit = this.fitWidth();
    this.applyWidth();
    this.el.scrollTop = 0;
    this.el.scrollLeft = 0;
    this.pending = this.el.clientWidth ? null : { n: 1, frac: 0 };
    this.setPage(1, true);
    for (const p of this.pages) this.io.observe(p.el);
    return true;
  }

  // Replace the pages with a message (no document, or an error).
  empty(node: Node) {
    this.token++;
    this.reset();
    this.docId = null;
    this.el.replaceChildren(node);
  }

  goTo(n: number, behavior?: ScrollBehavior) {
    const p = this.pages[clamp(Math.round(n) || 1, 1, this.total) - 1];
    if (!p) return;
    if (!this.el.clientWidth) { this.pending = { n: p.n, frac: 0 }; this.setPage(p.n); return; }
    const far = Math.abs(p.n - this.page) > 3;
    this.el.scrollTo({ top: p.el.offsetTop - 12, behavior: behavior || (far ? "auto" : "smooth") });
    this.setPage(p.n);
  }

  next() { this.goTo(this.page + 1); }
  prev() { this.goTo(this.page - 1); }

  // rects: [[x0, y0, x1, y1]] as fractions of the page. scroll: "reveal" brings the highlight into view only
  // if it isn't comfortably visible already (follow-along); true always moves it to the upper third; false
  // just paints it.
  highlight(n: number, rects: number[][], scroll: boolean | "reveal" = true) {
    this.clearHighlight();
    const p = this.pages[n - 1];
    if (!p || !rects?.length) return;
    for (const [x0, y0, x1, y1] of rects) {
      const el = h("div", { class: "hl", style: { left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` } });
      p.el.append(el);
      this.highlights.push(el);
    }
    if (scroll && !this.el.clientWidth) this.pending = { n, frac: rects[0][1] };
    else if (scroll) {
      const el = this.el;
      const top = p.el.offsetTop + rects[0][1] * p.el.offsetHeight;
      const bottom = p.el.offsetTop + rects[rects.length - 1][3] * p.el.offsetHeight;
      const visible = top >= el.scrollTop + el.clientHeight * 0.08 && bottom <= el.scrollTop + el.clientHeight * 0.8;
      if (scroll === "reveal" && visible) return;
      const target = Math.max(0, top - el.clientHeight * 0.28);
      const far = Math.abs(target - el.scrollTop) > el.clientHeight * 3;
      el.scrollTo({ top: target, behavior: far ? "auto" : "smooth" });
    }
  }

  clearHighlight() {
    for (const el of this.highlights) el.remove();
    this.highlights = [];
  }

  // anchorY: where in the view to keep still (px from the top); defaults to the reading line.
  setZoom(z: number, anchorY?: number) {
    const zoom = clamp(Math.round(z * 100) / 100, ZOOM_MIN, ZOOM_MAX);
    if (zoom === this.zoom) return;
    const anchor = this.anchor(anchorY);
    this.zoom = zoom;
    this.applyWidth(anchor);
    this.onZoom(zoom);
    this.rescaleSoon();
  }

  zoomIn() { this.setZoom(this.zoom >= 1 ? this.zoom + 0.25 : this.zoom + 0.1); }
  zoomOut() { this.setZoom(this.zoom > 1 ? this.zoom - 0.25 : this.zoom - 0.1); }

  focus() { this.el.focus({ preventScroll: true }); }

  destroy() {
    this.token++;
    this.io.disconnect();
    this.ro.disconnect();
    cancelAnimationFrame(this.raf);
    clearTimeout(this.rescaleTimer);
    for (const [type, fn, opts] of this.listeners) this.el.removeEventListener(type, fn, opts);
    this.listeners = [];
    this.el.replaceChildren();
  }

  // ---------------------------------------------------------------- internals

  private reset() {
    this.io.disconnect();
    this.clearHighlight();
    this.pages = [];
    clearTimeout(this.rescaleTimer);
  }

  private fitWidth() {
    return Math.max(160, Math.min(FIT_MAX, this.el.clientWidth - PAD * 2));
  }

  private applyWidth(anchor?: Anchor | null) {
    this.inner.style.setProperty("--page-w", `${Math.round(this.fit * this.zoom)}px`);
    if (anchor) this.restore(anchor);
  }

  // Coalesce scroll and resize work into one frame.
  private schedule(flag: string) {
    this.flags.add(flag);
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = 0;
      const flags = new Set(this.flags);
      this.flags.clear();
      if (!this.pages.length || !this.el.clientWidth) return; // hidden
      if (flags.has("layout") && this.pending) {
        // First layout since becoming visible: size the pages, then go where we were asked to while hidden.
        const { n, frac } = this.pending;
        this.pending = null;
        this.fit = this.fitWidth();
        this.applyWidth();
        const p = this.pages[n - 1];
        if (p) this.el.scrollTop = Math.max(0, p.el.offsetTop + frac * p.el.offsetHeight - (frac ? this.el.clientHeight * 0.28 : 12));
        this.rescaleSoon();
      } else if (flags.has("layout")) {
        const fit = this.fitWidth();
        if (Math.abs(fit - this.fit) >= 1) {
          const anchor = this.anchor(0);
          this.fit = fit;
          this.applyWidth(anchor);
          this.rescaleSoon();
        }
      }
      this.trackPage();
    });
  }

  private trackPage() {
    const el = this.el;
    const atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 2;
    const p = atEnd ? this.pages[this.total - 1] : this.pageAt(el.scrollTop + el.clientHeight * 0.4);
    if (p) this.setPage(p.n);
  }

  private setPage(n: number, force = false) {
    if (n === this.page && !force) return;
    this.page = n;
    this.onPage(n, this.total);
  }

  // Last page whose top is above y (content coordinates).
  private pageAt(y: number): Page {
    let lo = 0, hi = this.pages.length - 1, found = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.pages[mid].el.offsetTop <= y) { found = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return this.pages[found];
  }

  private anchor(y = this.el.clientHeight * 0.4): Anchor | null {
    if (!this.pages.length) return null;
    const el = this.el;
    const p = this.pageAt(el.scrollTop + y);
    return {
      p, y,
      frac: (el.scrollTop + y - p.el.offsetTop) / (p.el.offsetHeight || 1),
      x: el.scrollWidth > el.clientWidth ? (el.scrollLeft + el.clientWidth / 2) / el.scrollWidth : 0.5,
    };
  }

  private restore(a: Anchor) {
    const el = this.el;
    el.scrollTop = a.p.el.offsetTop + a.frac * a.p.el.offsetHeight - a.y;
    if (el.scrollWidth > el.clientWidth) el.scrollLeft = a.x * el.scrollWidth - el.clientWidth / 2;
  }

  private scaleFor(p: Page) {
    const cssWidth = this.fit * this.zoom * p.r;
    const raw = (cssWidth * (window.devicePixelRatio || 1)) / p.w;
    return clamp(Math.ceil(raw * 4) / 4, 0.75, 3); // quarter steps keep the server's render cache useful
  }

  private load(p: Page) {
    const scale = this.scaleFor(p);
    if (p.scale === scale || !this.docId) return;
    p.scale = scale;
    const url = api.pageUrl(this.docId, p.n, scale);
    if (!p.img.getAttribute("src")) { p.img.src = url; return; }
    // Sharper version after a zoom: keep showing the current image until the new one is ready.
    const next = new Image();
    next.onload = () => { if (p.scale === scale) p.img.src = url; };
    next.src = url;
  }

  private unload(p: Page) {
    if (!p.img.getAttribute("src")) return;
    p.img.removeAttribute("src");
    p.el.classList.remove("ready");
    p.scale = 0;
  }

  private rescaleSoon() {
    clearTimeout(this.rescaleTimer);
    this.rescaleTimer = setTimeout(() => {
      for (const p of this.pages) if (p.scale) this.load(p);
    }, 260);
  }
}
