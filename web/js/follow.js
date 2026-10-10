// Follow-along tracking: which sentence of your sources is the tutor talking about right now?
//
// Candidates are the passages the tutor just read or found (the full chunk text from its tool results).
// Each is split into sentences. As the tutor's speech is *heard* (the transcript is released in step with
// the audio, not when it's generated), the last couple of dozen content words are matched against every
// sentence: words that are rare across the candidates count more, recently spoken words count more, and
// moving forward through a passage is preferred, the way an explanation usually goes. A switch needs a
// clear winner and a short pause since the last one, so the highlight doesn't flicker between near-ties.
// Pure logic, no DOM: notebook.js turns a chosen sentence into a page + highlight.

const STOP = new Set(`a about above after again against all also am an and any are as at be because been before
being below between both but by can could did do does doing done down during each either else even ever every
few for from further had has have having he her here hers herself him himself his how however i if in into is it
its itself just let lets like may me might more most much must my myself no nor not now of off on once only or
other our ours out over own per quite rather really said same say says she should since so some such than that
the their theirs them themselves then there these they this those though through thus to too under until up upon
us use used using very via was we well were what whatever when where whether which while who whom whose why will
with within without would yes yet you your yours yourself okay ok right basically actually simply example
examples called call means mean meaning thing things way ways kind kinds lot lots part parts page pages section
sections chapter notes note point points let's here's that's it's there's what's we're you're they're i'm
first second third next last one two three four five also again`.split(/\s+/));

const WINDOW = 18; // content words of recent speech to match
const MIN_SCORE = 3.5; // weaker evidence than this never moves the highlight
const MIN_GAP_MS = 1200; // at most one move per this long

function stem(w) {
  if (w.length > 4 && w.endsWith("ies")) return `${w.slice(0, -3)}y`;
  if (w.endsWith("sses")) return w.slice(0, -2);
  if (w.length > 6 && w.endsWith("ing")) return w.slice(0, -3);
  if (w.length > 5 && w.endsWith("ed")) return w.slice(0, -2);
  if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss") && !w.endsWith("us") && !w.endsWith("is")) return w.slice(0, -1);
  return w;
}

export function contentWords(text) {
  const words = text.normalize("NFKC").toLowerCase().match(/[\p{L}\p{N}]+(?:'[\p{L}]+)?/gu) || [];
  const out = [];
  for (const w of words) {
    if (w.length < 3 || STOP.has(w) || /^\d{1,2}$/.test(w)) continue;
    out.push(stem(w));
  }
  return out;
}

// Chunk text (markdown from the PDF) -> readable sentences of a useful size.
export function sentences(text) {
  const clean = text
    .replace(/\*\*([A-Za-z])\*\*\s?(?=[a-z])/g, "$1") // "**P** honology" (bold initial letters) -> "Phonology"
    .replace(/<\/?[a-zA-Z][^>]*>/g, " ") // <br> from table cells, <mark>, …
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_`#>|]+/g, " ");
  const raw = clean.split(/(?<=[.!?])\s+|\n+/)
    .map((s) => s.replace(/^\s*(?:[-•·▪◦]|\d{1,2}[.)])\s+/, "").replace(/\s+/g, " ").trim())
    .filter((s) => /[\p{L}\p{N}]/u.test(s));
  const out = [];
  let carry = "";
  for (const s of raw) {
    const joined = carry ? `${carry} ${s}` : s;
    if (joined.split(" ").length < 6) { carry = joined; continue; } // headings, labels: join to what follows
    carry = "";
    const words = joined.split(" ");
    if (words.length <= 40) { out.push(joined); continue; }
    for (let i = 0; i < words.length; i += 28) {
      const piece = words.slice(i, i + 28).join(" ");
      if (i && words.length - i < 10) out[out.length - 1] += ` ${piece}`;
      else out.push(piece);
    }
  }
  if (carry) {
    if (out.length) out[out.length - 1] += ` ${carry}`;
    else out.push(carry);
  }
  return out;
}

export class FollowTracker {
  constructor() {
    this.segments = [];
    this.idf = new Map();
    this.current = -1;
    this.heard = "";
    this.lastSwitch = 0;
    this.retryAt = 0; // a move held back by the anti-flicker gap: re-check (hear("")) at this time
  }

  get focus() { return this.segments[this.current] || null; }

  // cands: [{ doc_id, first, last, text, boost? }]. append=false starts over (a new turn's material).
  // Returns the index of the first sentence added (-1 if none), for start().
  setCandidates(cands, append = false) {
    if (!append) { this.segments = []; this.current = -1; }
    const firstNew = this.segments.length;
    const base = this.segments.length ? this.segments[this.segments.length - 1].cand + 1 : 0;
    cands.forEach((c, ci) => {
      if (!c.text) return;
      sentences(c.text).forEach((text, i) => {
        const toks = new Set(contentWords(text));
        if (toks.size) this.segments.push({ doc_id: c.doc_id, first: c.first, last: c.last, text, toks, cand: base + ci, i, boost: c.boost || 1 });
      });
    });
    const df = new Map();
    for (const s of this.segments) for (const t of s.toks) df.set(t, (df.get(t) || 0) + 1);
    const n = this.segments.length;
    this.idf = new Map([...df].map(([t, d]) => [t, Math.log(1 + n / d)]));
    return firstNew < this.segments.length ? firstNew : -1;
  }

  // Make a sentence the current one (e.g. the start of a passage the moment it's opened).
  start(idx) {
    if (!this.segments[idx]) return null;
    this.current = idx;
    this.lastSwitch = performance.now();
    return this.segments[idx];
  }

  newTurn() { this.heard = ""; }

  // Feed speech as it's heard. Returns the sentence to move to, or null to stay put.
  hear(piece) {
    this.heard += piece;
    if (this.heard.length > 4000) this.heard = this.heard.slice(-2000);
    if (!this.segments.length) return null;
    const words = contentWords(this.heard).slice(-WINDOW);
    if (words.length < 3) return null;
    const recency = new Map();
    words.forEach((t, i) => recency.set(t, 0.3 + (0.7 * (i + 1)) / words.length)); // later words win

    const cur = this.segments[this.current];
    let best = -1, bestScore = 0, curScore = 0;
    this.segments.forEach((seg, idx) => {
      let score = 0, hits = 0;
      for (const [t, r] of recency) {
        if (seg.toks.has(t)) { score += (this.idf.get(t) || 1) * r; hits++; }
      }
      if (hits < 2) return;
      score *= seg.boost;
      if (cur && seg.cand === cur.cand) {
        const d = seg.i - cur.i;
        score *= d === 0 ? 1.15 : d > 0 && d <= 3 ? 1.25 : d < 0 ? 0.85 : 1;
      }
      if (idx === this.current) curScore = score;
      if (score > bestScore) { bestScore = score; best = idx; }
    });
    if (best < 0 || best === this.current || bestScore < MIN_SCORE) return null;
    if (bestScore < curScore * 1.2 + 0.5) return null;
    const now = performance.now();
    if (now - this.lastSwitch < MIN_GAP_MS) {
      // Speech often arrives in bursts; without a re-check the last move of a burst would be lost.
      this.retryAt = this.lastSwitch + MIN_GAP_MS;
      return null;
    }
    this.retryAt = 0;
    this.current = best;
    this.lastSwitch = now;
    return this.segments[best];
  }
}
