// Continuous PDF viewer: every page stacked vertically in reading order, like a desktop PDF reader.
// The whole document is laid out up front from the page sizes (so nothing jumps while you scroll); a page
// is rendered by the server only when it comes near the screen, at the resolution the screen needs, and
// released again when it's far away, so long documents stay light. Zooming (buttons, Ctrl+wheel, trackpad
// pinch) and resizing keep the spot you're reading in place.
import { api } from "./api.js";
import { h } from "./ui.js";

const FIT_MAX = 920; // 100% = fit the width, but never wider than this (comfortable in full screen)
const PAD = 22; // horizontal padding around the pages (matches .pdf-pages)
const ZOOM_MIN = 0.5;
const ZOOM_MAX = 3;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export class PdfView {
  constructor({ onPage = () => {}, onZoom = () => {}, onUserScroll = () => {} } = {}) {
    this.onPage = onPage;
    this.onZoom = onZoom;
    this.onUserScroll = onUserScroll;
    this.el = h("div", { class: "viewer-stage scroll", tabindex: "-1" });
    this.inner = h("div", { class: "pdf-pages" });
    this.docId = null;
    this.pages = []; // { n, w, h, r, el, img, scale }
    this.page = 1;
    this.zoom = 1;
    this.fit = 600;
    this.highlights = [];
    this.flags = new Set();
    this.raf = 0;
    this.rescaleTimer = 0;
    this.token = 0;
    this.pending = null; // { n, frac } to scroll to once the viewer is visible (it can't scroll while hidden)

    // Pages within ~1.5 screens are rendered; pages that leave that band are released.
    this.io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const p = this.pages[Number(e.target.dataset.n) - 1];
        if (!p) continue;
        if (e.isIntersecting) this._load(p);
        else this._unload(p);
      }
    }, { root: this.el, rootMargin: "150% 0px" });
    this.ro = new ResizeObserver(() => this._schedule("layout"));
    this.ro.observe(this.el);
    this.el.addEventListener("scroll", () => this._schedule("scroll"), { passive: true });
    // Scrolling by hand (as opposed to follow-along moving the page) is reported, so following can pause.
    const byHand = () => { if (this.pages.length) this.onUserScroll(); };
    this.el.addEventListener("wheel", (e) => { if (!e.ctrlKey) byHand(); }, { passive: true });
    this.el.addEventListener("touchmove", byHand, { passive: true });
    this.el.addEventListener("pointerdown", (e) => { if (e.target === this.el) byHand(); }); // scrollbar
    this.el.addEventListener("keydown", (e) => {
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(e.key)) byHand();
    });
    this.el.addEventListener("wheel", (e) => {
      if (!e.ctrlKey || !this.pages.length) return; // trackpad pinch arrives as Ctrl+wheel
      e.preventDefault();
      this.setZoom(this.zoom * Math.exp(-e.deltaY * 0.0025), e.clientY - this.el.getBoundingClientRect().top);
    }, { passive: false });
  }

  get total() { return this.pages.length; }

  // Show a document. Resolves false if another open() superseded this one.
  async open(docId, fallbackTotal = 1) {
    if (docId === this.docId && this.pages.length) return true;
    const token = ++this.token;
    let sizes = null;
    try { sizes = (await api.pageSizes(docId)).sizes; } catch { /* lay out as Letter pages instead */ }
    if (token !== this.token) return false;
    if (!sizes?.length) sizes = Array.from({ length: Math.max(1, fallbackTotal) }, () => [612, 792]);
    this._reset();
    this.docId = docId;
    const widths = sizes.map(([w]) => w).sort((a, b) => a - b);
    const ref = widths[Math.floor(widths.length / 2)] || 612; // pages wider than usual (landscape) stay wider
    this.pages = sizes.map(([w, hgt], i) => {
      const img = h("img", { alt: `Page ${i + 1}`, draggable: "false", decoding: "async" });
      const el = h("div", { class: "pdf-page", dataset: { n: String(i + 1) },
        style: { "--r": (w / ref).toFixed(4), "--ar": `${w} / ${hgt}` } }, img);
      const p = { n: i + 1, w, h: hgt, r: w / ref, el, img, scale: 0 };
      img.addEventListener("load", () => { if (img.getAttribute("src")) el.classList.add("ready"); });
      return p;
    });
    this.inner.replaceChildren(...this.pages.map((p) => p.el));
    this.el.replaceChildren(this.inner);
    this.fit = this._fitWidth();
    this._applyWidth();
    this.el.scrollTop = 0;
    this.el.scrollLeft = 0;
    this.pending = this.el.clientWidth ? null : { n: 1, frac: 0 };
    this._setPage(1, true);
    for (const p of this.pages) this.io.observe(p.el);
    return true;
  }

  // Replace the pages with a message (no document, or an error).
  empty(node) {
    this.token++;
    this._reset();
    this.docId = null;
    this.el.replaceChildren(node);
  }

  goTo(n, behavior) {
    const p = this.pages[clamp(Math.round(n) || 1, 1, this.total) - 1];
    if (!p) return;
    if (!this.el.clientWidth) { this.pending = { n: p.n, frac: 0 }; this._setPage(p.n); return; }
    const far = Math.abs(p.n - this.page) > 3;
    this.el.scrollTo({ top: p.el.offsetTop - 12, behavior: behavior || (far ? "auto" : "smooth") });
    this._setPage(p.n);
  }

  next() { this.goTo(this.page + 1); }
  prev() { this.goTo(this.page - 1); }

  // rects: [[x0, y0, x1, y1]] as fractions of the page. scroll: "reveal" brings the highlight into view only
  // if it isn't comfortably visible already (follow-along); true always moves it to the upper third; false
  // just paints it.
  highlight(n, rects, scroll = true) {
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
  setZoom(z, anchorY) {
    const zoom = clamp(Math.round(z * 100) / 100, ZOOM_MIN, ZOOM_MAX);
    if (zoom === this.zoom) return;
    const anchor = this._anchor(anchorY);
    this.zoom = zoom;
    this._applyWidth(anchor);
    this.onZoom(zoom);
    this._rescaleSoon();
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
  }

  // ---------------------------------------------------------------- internals

  _reset() {
    this.io.disconnect();
    this.clearHighlight();
    this.pages = [];
    clearTimeout(this.rescaleTimer);
  }

  _fitWidth() {
    return Math.max(160, Math.min(FIT_MAX, this.el.clientWidth - PAD * 2));
  }

  _applyWidth(anchor) {
    this.inner.style.setProperty("--page-w", `${Math.round(this.fit * this.zoom)}px`);
    if (anchor) this._restore(anchor);
  }

  // Coalesce scroll and resize work into one frame.
  _schedule(flag) {
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
        this.fit = this._fitWidth();
        this._applyWidth();
        const p = this.pages[n - 1];
        if (p) this.el.scrollTop = Math.max(0, p.el.offsetTop + frac * p.el.offsetHeight - (frac ? this.el.clientHeight * 0.28 : 12));
        this._rescaleSoon();
      } else if (flags.has("layout")) {
        const fit = this._fitWidth();
        if (Math.abs(fit - this.fit) >= 1) {
          const anchor = this._anchor(0);
          this.fit = fit;
          this._applyWidth(anchor);
          this._rescaleSoon();
        }
      }
      this._trackPage();
    });
  }

  _trackPage() {
    const el = this.el;
    const atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 2;
    const p = atEnd ? this.pages[this.total - 1] : this._pageAt(el.scrollTop + el.clientHeight * 0.4);
    if (p) this._setPage(p.n);
  }

  _setPage(n, force = false) {
    if (n === this.page && !force) return;
    this.page = n;
    this.onPage(n, this.total);
  }

  // Last page whose top is above y (content coordinates).
  _pageAt(y) {
    let lo = 0, hi = this.pages.length - 1, found = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.pages[mid].el.offsetTop <= y) { found = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return this.pages[found];
  }

  _anchor(y = this.el.clientHeight * 0.4) {
    if (!this.pages.length) return null;
    const el = this.el;
    const p = this._pageAt(el.scrollTop + y);
    return {
      p, y,
      frac: (el.scrollTop + y - p.el.offsetTop) / (p.el.offsetHeight || 1),
      x: el.scrollWidth > el.clientWidth ? (el.scrollLeft + el.clientWidth / 2) / el.scrollWidth : 0.5,
    };
  }

  _restore(a) {
    const el = this.el;
    el.scrollTop = a.p.el.offsetTop + a.frac * a.p.el.offsetHeight - a.y;
    if (el.scrollWidth > el.clientWidth) el.scrollLeft = a.x * el.scrollWidth - el.clientWidth / 2;
  }

  _scaleFor(p) {
    const cssWidth = this.fit * this.zoom * p.r;
    const raw = (cssWidth * (window.devicePixelRatio || 1)) / p.w;
    return clamp(Math.ceil(raw * 4) / 4, 0.75, 3); // quarter steps keep the server's render cache useful
  }

  _load(p) {
    const scale = this._scaleFor(p);
    if (p.scale === scale) return;
    p.scale = scale;
    const url = api.pageUrl(this.docId, p.n, scale);
    if (!p.img.getAttribute("src")) { p.img.src = url; return; }
    // Sharper version after a zoom: keep showing the current image until the new one is ready.
    const next = new Image();
    next.onload = () => { if (p.scale === scale) p.img.src = url; };
    next.src = url;
  }

  _unload(p) {
    if (!p.img.getAttribute("src")) return;
    p.img.removeAttribute("src");
    p.el.classList.remove("ready");
    p.scale = 0;
  }

  _rescaleSoon() {
    clearTimeout(this.rescaleTimer);
    this.rescaleTimer = setTimeout(() => {
      for (const p of this.pages) if (p.scale) this._load(p);
    }, 260);
  }
}
