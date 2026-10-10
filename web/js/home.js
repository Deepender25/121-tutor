// Home: greeting hero, "continue where you left off", notebook grid with search.
import { api } from "./api.js";
import { markIcon } from "./mark.js";
import { openSettings, statusPill } from "./main.js";
import { confirmDialog, greeting, h, icon, notebookIcon, openMenu, promptDialog, ring, timeAgo, toast } from "./ui.js";

function stats(nb) {
  const pages = nb.sources.reduce((n, s) => n + (s.pages || 0), 0);
  const covered = nb.sources.length ? Math.round(nb.sources.reduce((n, s) => n + (s.covered || 0), 0) / nb.sources.length) : 0;
  return { pages, covered };
}

export async function renderHome(root) {
  const notebooks = await api.notebooks();
  document.title = "Voice Tutor";
  let query = "";

  const create = async () => {
    const name = await promptDialog({ title: "New notebook", body: "Give it a name. You'll add PDFs next.", placeholder: "e.g. Organic Chemistry, Unit 3", confirm: "Create notebook" });
    if (!name) return;
    try {
      const nb = await api.createNotebook(name);
      location.hash = `#/nb/${nb.id}`;
    } catch (e) { toast(e.message, "error"); }
  };

  const open = (nb) => { location.hash = `#/nb/${nb.id}`; };

  const cardMenu = (nb) => h("button", {
    class: "icon-btn sm card-menu", "aria-label": `Options for ${nb.name}`,
    onclick: (e) => {
      e.stopPropagation();
      openMenu(e.currentTarget, [
        { label: "Rename", icon: "edit", action: async () => {
          const name = await promptDialog({ title: "Rename notebook", value: nb.name });
          if (!name) return;
          try { await api.updateNotebook(nb.id, { name }); await renderHome(root); toast("Renamed", "success"); } catch (err) { toast(err.message, "error"); }
        } },
        { label: "Delete", icon: "trash", danger: true, action: async () => {
          const ok = await confirmDialog({ title: `Delete “${nb.name}”?`, body: "Its conversation history and any sources not used by another notebook will be removed. This can't be undone." });
          if (!ok) return;
          try { await api.deleteNotebook(nb.id); await renderHome(root); toast("Notebook deleted", "success"); } catch (err) { toast(err.message, "error"); }
        } },
      ]);
    },
  }, icon("more"));

  const card = (nb) => {
    const { pages, covered } = stats(nb);
    return h("article", {
      class: "nb-card", role: "link", tabindex: "0",
      onclick: () => open(nb), onkeydown: (e) => { if (e.key === "Enter") open(nb); },
    },
      h("div", { class: "card-top" }, h("span", { class: "icon-tile" }, notebookIcon(nb.icon)), cardMenu(nb)),
      h("div", { class: "card-body" },
        h("h3", {}, nb.name),
        h("div", { class: "chips" },
          h("span", { class: "chip" }, icon("file", "sm"), nb.sources.length ? `${nb.sources.length} source${nb.sources.length > 1 ? "s" : ""}` : "No sources"),
          pages ? h("span", { class: "chip" }, icon("layers", "sm"), `${pages} pages`) : null)),
      h("div", { class: "card-foot" },
        nb.sources.length ? h("span", { class: "progress" }, ring(covered, 20, 2.5), `${covered}% covered`) : h("span", { class: "progress muted" }, "Add a PDF to start"),
        h("span", { class: "when" }, timeAgo(nb.updated_at))));
  };

  const grid = h("div", { class: "nb-grid" });
  const countEl = h("span", { class: "count" });
  const paintGrid = () => {
    const q = query.trim().toLowerCase();
    const shown = notebooks.filter((nb) => !q || nb.name.toLowerCase().includes(q) || nb.sources.some((s) => (s.title || "").toLowerCase().includes(q)));
    countEl.textContent = q ? `${shown.length} of ${notebooks.length}` : `${notebooks.length}`;
    grid.replaceChildren(...[
      ...shown.map(card),
      shown.length ? null : h("div", { class: "no-results" }, `No notebooks match “${query}”.`),
    ].filter(Boolean)); // replaceChildren would print null as text
  };

  const recent = notebooks.find((nb) => nb.sources.length);
  const continueCard = recent ? (() => {
    const active = recent.sources.find((s) => s.doc_id === recent.active_source) || recent.sources[0];
    const { covered } = stats(recent);
    const where = active?.position?.started ? `${active.position.section} · p. ${active.position.pages}` : `Not started · ${active?.title || ""}`;
    return h("section", { class: "continue", onclick: () => open(recent), role: "link", tabindex: "0",
      onkeydown: (e) => { if (e.key === "Enter") open(recent); } },
      h("span", { class: "icon-tile lg" }, notebookIcon(recent.icon)),
      h("div", { class: "continue-text" },
        h("span", { class: "eyebrow" }, "Continue where you left off"),
        h("h2", {}, recent.name),
        h("p", {}, where)),
      h("div", { class: "continue-progress" }, ring(covered, 46, 3.5), h("span", {}, `${covered}%`, h("small", {}, "covered"))),
      h("button", { class: "btn primary", onclick: (e) => { e.stopPropagation(); open(recent); } }, "Resume", icon("arrowRight", "sm")));
  })() : null;

  const search = h("label", { class: "search" }, icon("search", "sm"),
    h("input", { type: "search", placeholder: "Search notebooks", "aria-label": "Search notebooks",
      oninput: (e) => { query = e.target.value; paintGrid(); } }));

  const heroMark = h("div", { class: "hero-mark" }, markIcon());
  const body = notebooks.length
    ? h("div", { class: "home-inner" },
        h("section", { class: "hero" },
          h("div", { class: "hero-text" },
            h("span", { class: "eyebrow" }, greeting()),
            h("h1", {}, "What shall we learn today?"),
            h("p", {}, "Open a notebook and just talk. Your tutor teaches, explains, researches and brainstorms, and always says what's from your sources and what's from the web."),
            h("div", { class: "hero-actions" }, h("button", { class: "btn primary lg", onclick: create }, icon("plus"), "New notebook"))),
          heroMark),
        continueCard,
        h("div", { class: "section-bar" }, h("h2", {}, "Notebooks ", countEl), search),
        grid)
    : h("div", { class: "home-inner" },
        h("section", { class: "welcome" },
          heroMark,
          h("span", { class: "eyebrow" }, greeting()),
          h("h1", {}, "Talk to your documents"),
          h("p", {}, "Create a notebook, drop in a PDF, and have a real conversation about it: step-by-step lessons, answers with page references, web research and brainstorming."),
          h("button", { class: "btn primary lg", onclick: create }, icon("plus"), "Create your first notebook"),
          h("div", { class: "feature-row" },
            feature("book", "Tutor", "Teaches your PDF in order and remembers where you stopped."),
            feature("search", "Explainer", "Answers from your sources, with page references."),
            feature("globe", "Researcher", "Checks the web and tells you which is which."),
            feature("lightbulb", "Brainstormer", "Builds new ideas on top of your material."))));

  root.replaceChildren(h("div", { class: "home" },
    h("header", { class: "topbar" },
      h("a", { class: "brand", href: "#/" }, markIcon("brand-mark"), h("span", {}, "Voice Tutor")),
      h("div", { class: "spacer" }),
      h("div", { class: "topbar-actions" }, statusPill(),
        h("button", { class: "icon-btn", "aria-label": "Settings", "data-tip": "Settings", onclick: () => openSettings() }, icon("settings")))),
    h("main", { class: "home-main scroll" }, body)));
  if (notebooks.length) paintGrid();
  return () => {};
}

function feature(ic, title, text) {
  return h("div", { class: "feature" }, h("span", { class: "feature-ic" }, icon(ic)), h("b", {}, title), h("span", {}, text));
}
