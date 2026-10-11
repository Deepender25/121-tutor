// Follow-along tracking: which sentence of your sources is the tutor talking about right now?
//
// Candidates are the passages (chunks) the tutor just read or found, each split into sentences. As the
// tutor's speech is *heard* (the transcript is released in step with the audio), it's matched in two steps,
// because the two kinds of mistake cost very different amounts:
//   1. Which passage. Moving to another passage usually means another page or section, so it takes real,
//      sustained evidence: distinctive words from that passage, accumulated over the last few sentences of
//      speech, clearly ahead of the current passage and of every other one, holding for a moment, and not
//      too soon after the last switch. Small talk ("great, let's move on") never moves the page.
//   2. Which sentence inside that passage. Cheap to get slightly wrong (same page, a line or two), so it
//      follows the last few words closely, preferring to move forward the way explanations go.
// When the evidence isn't there, the highlight stays where it is: a still page beats a wrong one.
// The thresholds were tuned by replaying real sessions against an independent semantic judge (DESIGN §24).
// Pure logic, no DOM: the notebook controller turns a chosen sentence into a page + highlight.

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
first second third next last one two three four five great good sure exactly important move moving topic topics
talk talking look looking explain explained explains discuss discussed discusses know need want see go going get
got come tell`.split(/\s+/));

// Which passage
const PASSAGE_WINDOW = 30; // content words of speech that count as evidence
const PASSAGE_HALF_LIFE = 12; // words: older evidence fades
const ADOPT_MIN = 7; // evidence a passage needs before the page goes there at all
const ADOPT_TERMS = 3; // ... from at least this many different words
const SWITCH_RATIO = 1.6; // how far ahead of the current passage it must be to take over
const RIVAL_RATIO = 1.35; // ... and of every other passage (near-ties never move the page)
const SUSTAIN_MS = 700; // the lead has to hold this long
const DWELL_MS = 2500; // at least this long on a passage before switching again
// Which sentence
const SENTENCE_WINDOW = 12; // recent content words that place the sentence inside the passage
const SENTENCE_TERMS = 3; // a sentence needs this many of them
const SENTENCE_MIN = 3; // weaker evidence than this never moves the highlight
const SENTENCE_GAP_MS = 1200; // at most one move per this long

function stem(w: string): string {
  if (w.length > 4 && w.endsWith("ies")) return `${w.slice(0, -3)}y`;
  if (w.endsWith("sses")) return w.slice(0, -2);
  if (w.length > 6 && w.endsWith("ing")) return w.slice(0, -3);
  if (w.length > 5 && w.endsWith("ed")) return w.slice(0, -2);
  if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss") && !w.endsWith("us") && !w.endsWith("is")) return w.slice(0, -1);
  return w;
}

export function contentWords(text: string): string[] {
  const words = text.normalize("NFKC").toLowerCase().match(/[\p{L}\p{N}]+(?:'[\p{L}]+)?/gu) || [];
  const out: string[] = [];
  for (const w of words) {
    if (w.length < 3 || STOP.has(w) || /^\d{1,2}$/.test(w)) continue;
    out.push(stem(w));
  }
  return out;
}

// Chunk text (markdown from the PDF) -> readable sentences of a useful size.
export function sentences(text: string): string[] {
  const clean = text
    .replace(/\*\*([A-Za-z])\*\*\s?(?=[a-z])/g, "$1") // "**P** honology" (bold initial letters) -> "Phonology"
    .replace(/<\/?[a-zA-Z][^>]*>/g, " ") // <br> from table cells, <mark>, …
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_`#>|]+/g, " ");
  const raw = clean.split(/(?<=[.!?])\s+|\n+/)
    .map((s) => s.replace(/^\s*(?:[-•·▪◦]|\d{1,2}[.)])\s+/, "").replace(/\s+/g, " ").trim())
    .filter((s) => /[\p{L}\p{N}]/u.test(s));
  const out: string[] = [];
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

export interface Candidate {
  doc_id: string;
  first: number;
  last: number;
  text?: string;
  boost?: number; // a passage being taught (read_passage) gets > 1; search hits fall off a little by rank
}

export interface Segment {
  doc_id: string;
  first: number;
  last: number;
  text: string;
  toks: Set<string>;
  cand: number; // which passage
  i: number; // which sentence of it
}

interface Passage {
  segs: number[]; // indices into segments, in reading order
  toks: Set<string>;
  boost: number;
}

export class FollowTracker {
  segments: Segment[] = [];
  passages: Passage[] = [];
  current = -1; // the highlighted sentence
  heard = "";
  retryAt = 0; // a decision waiting on time (a lead that must hold, a gap): re-check with hear("") then
  private idf = new Map<string, number>(); // rarity of a word across all candidate sentences
  private passageWeight = new Map<string, number>(); // ... discounted when most passages have it
  private lastPassageSwitch = -Infinity;
  private lastSentenceMove = -Infinity;
  private pending = -1; // a passage that's ahead but hasn't held its lead long enough yet
  private pendingSince = 0;

  get focus(): Segment | null { return this.segments[this.current] || null; }

  // append=false starts over (a new turn's material). The current sentence is kept if it's among the new
  // candidates too, so looking up the same passage again doesn't reset anything. Returns the index of the
  // first sentence added (-1 if none), for start().
  setCandidates(cands: Candidate[], append = false): number {
    const keep = append ? null : this.focus;
    if (!append) { this.segments = []; this.passages = []; this.current = -1; this.pending = -1; }
    const firstNew = this.segments.length;
    let rank = 0;
    for (const c of cands) {
      if (!c.text) continue;
      const cand = this.passages.length;
      const p: Passage = { segs: [], toks: new Set(), boost: c.boost ?? Math.max(0.85, 1 - 0.04 * rank++) };
      sentences(c.text).forEach((text, i) => {
        const words = contentWords(text);
        if (!words.length) return;
        p.segs.push(this.segments.length);
        this.segments.push({ doc_id: c.doc_id, first: c.first, last: c.last, text, toks: new Set(words), cand, i });
        for (const t of words) p.toks.add(t);
      });
      if (p.segs.length) this.passages.push(p);
    }
    const df = new Map<string, number>();
    for (const s of this.segments) for (const t of s.toks) df.set(t, (df.get(t) || 0) + 1);
    const n = this.segments.length;
    this.idf = new Map([...df].map(([t, d]) => [t, Math.log(1 + n / d)]));
    const pdf = new Map<string, number>();
    for (const p of this.passages) for (const t of p.toks) pdf.set(t, (pdf.get(t) || 0) + 1);
    const np = this.passages.length;
    this.passageWeight = new Map([...pdf].map(([t, d]) =>
      [t, (this.idf.get(t) || 1) * (np > 1 ? Math.log(1 + np / d) / Math.log(1 + np) : 1)]));
    if (keep) {
      const again = this.segments.findIndex((s) => s.doc_id === keep.doc_id && s.text === keep.text);
      if (again >= 0) this.current = again;
    }
    return firstNew < this.segments.length ? firstNew : -1;
  }

  // Make a sentence the current one (the start of a passage the moment it's opened for teaching).
  start(idx: number): Segment | null {
    if (!this.segments[idx]) return null;
    this.current = idx;
    this.lastPassageSwitch = this.lastSentenceMove = performance.now();
    this.pending = -1;
    return this.segments[idx];
  }

  // A new exchange: what was said before is no longer evidence. The highlight stays where it is.
  newTurn() { this.heard = ""; this.pending = -1; this.retryAt = 0; }

  // Feed speech as it's heard. Returns the sentence to move to, or null to stay put.
  hear(piece: string): Segment | null {
    this.heard += piece;
    if (this.heard.length > 4000) this.heard = this.heard.slice(-2000);
    this.retryAt = 0;
    if (!this.segments.length) return null;
    const words = contentWords(this.heard);
    if (words.length < 3) return null;
    const now = performance.now();
    const cur = this.focus;
    const passage = this.choosePassage(words.slice(-PASSAGE_WINDOW), cur ? cur.cand : -1, now);
    if (passage < 0) return null;
    const recent = words.slice(-SENTENCE_WINDOW);
    if (!cur || passage !== cur.cand) {
      // Entering a passage: the sentence the speech is about, or its opening if none stands out yet.
      const best = this.bestSentence(passage, recent, -1);
      this.current = best.idx >= 0 ? best.idx : this.passages[passage].segs[0];
      this.lastPassageSwitch = this.lastSentenceMove = now;
      this.pending = -1;
      return this.segments[this.current];
    }
    const best = this.bestSentence(passage, recent, this.current);
    if (best.idx < 0 || best.idx === this.current || best.score < SENTENCE_MIN) return null;
    if (best.score < best.curScore * 1.25 + 0.3) return null;
    if (now - this.lastSentenceMove < SENTENCE_GAP_MS) {
      this.wakeAt(this.lastSentenceMove + SENTENCE_GAP_MS); // the last move of a burst of speech mustn't be lost
      return null;
    }
    this.current = best.idx;
    this.lastSentenceMove = now;
    return this.segments[best.idx];
  }

  private wakeAt(t: number) { this.retryAt = this.retryAt ? Math.min(this.retryAt, t) : t; }

  // The passage the speech is about: the current one unless another has clearly and steadily taken over.
  private choosePassage(words: string[], cur: number, now: number): number {
    const n = words.length;
    const ev = this.passages.map((p) => {
      const seen = new Map<string, number>(); // word -> its weight at its most recent mention
      words.forEach((t, k) => {
        if (!p.toks.has(t)) return;
        const w = (this.passageWeight.get(t) || 0) * 0.5 ** ((n - 1 - k) / PASSAGE_HALF_LIFE);
        if (w > (seen.get(t) || 0)) seen.set(t, w);
      });
      let score = 0;
      for (const w of seen.values()) score += w;
      return { score: score * p.boost, terms: seen.size };
    });
    let best = -1;
    ev.forEach((e, i) => { if (best < 0 || e.score > ev[best].score) best = i; });
    if (best < 0 || best === cur) { this.pending = -1; return cur; }
    const b = ev[best];
    const rival = Math.max(0, ...ev.map((e, i) => (i === best ? 0 : e.score)));
    const clear = b.score >= ADOPT_MIN && b.terms >= ADOPT_TERMS && b.score >= rival * RIVAL_RATIO
      && (cur < 0 || b.score >= ev[cur].score * SWITCH_RATIO + 1);
    if (!clear) { this.pending = -1; return cur; }
    if (this.pending !== best) { this.pending = best; this.pendingSince = now; }
    const ready = Math.max(this.pendingSince + SUSTAIN_MS, cur >= 0 ? this.lastPassageSwitch + DWELL_MS : 0);
    if (now < ready) { this.wakeAt(ready); return cur; }
    return best;
  }

  private bestSentence(passage: number, words: string[], current: number) {
    const recency = new Map<string, number>();
    words.forEach((t, i) => recency.set(t, 0.3 + (0.7 * (i + 1)) / words.length)); // later words win
    const cur = this.segments[current];
    let idx = -1, score = 0, curScore = 0;
    for (const s of this.passages[passage].segs) {
      const seg = this.segments[s];
      let sc = 0, hits = 0;
      for (const [t, r] of recency) if (seg.toks.has(t)) { sc += (this.idf.get(t) || 1) * r; hits++; }
      if (hits < SENTENCE_TERMS) continue;
      if (cur) {
        const d = seg.i - cur.i;
        sc *= d === 0 ? 1.1 : d > 0 && d <= 3 ? 1.2 : d < 0 ? 0.75 : 1; // forward, as explanations go
      }
      if (s === current) curScore = sc;
      if (sc > score) { score = sc; idx = s; }
    }
    return { idx, score, curScore };
  }
}
