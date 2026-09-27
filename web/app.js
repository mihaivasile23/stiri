(() => {
  "use strict";

  const SEEN_KEY = "stiri.seen";   // headlines that were on screen
  const READ_KEY = "stiri.read";   // headlines you opened
  const TAB_KEY = "stiri.tab";
  const KEEP_DAYS = 10;            // longer than any lane looks back
  const SEEN_AFTER_MS = 1000;      // how long a card must be fully visible
  const NEW_VISIT_AFTER_MS = 5 * 60000;

  const $ = (id) => document.getElementById(id);
  const main = $("main");
  const tabsEl = $("tabs");
  const updatedEl = $("updated");
  // Created here if missing, so an older cached index.html can't break the page
  const freshBtn = $("fresh") || (() => {
    const b = document.createElement("button");
    b.id = "fresh"; b.className = "fresh"; b.type = "button"; b.hidden = true;
    b.textContent = "Titluri noi";
    updatedEl.before(b);
    return b;
  })();

  let data = null;
  let pending = null;
  let current = 0;
  let lastFetch = 0;

  // ---------------------------------------------------------------- storage

  const store = {
    get(key, fallback) {
      try {
        const v = localStorage.getItem(key);
        return v === null ? fallback : JSON.parse(v);
      } catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode, full storage */ }
    },
  };

  function loadMap(key) {
    const map = store.get(key, {});
    const cutoff = Date.now() - KEEP_DAYS * 864e5;
    for (const id in map) if (map[id] < cutoff) delete map[id];
    return map;
  }

  const seenMap = loadMap(SEEN_KEY);
  const readMap = loadMap(READ_KEY);
  const has = (map, id) => Object.prototype.hasOwnProperty.call(map, id);

  let saveTimer = null;
  function saveSoon() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 400);
  }
  function saveNow() {
    clearTimeout(saveTimer);
    store.set(SEEN_KEY, seenMap);
    store.set(READ_KEY, readMap);
  }
  window.addEventListener("pagehide", saveNow);

  // What was already seen when this visit started stays hidden until the next visit.
  let hidden = new Set();
  function startVisit() {
    hidden = new Set([...Object.keys(seenMap), ...Object.keys(readMap)]);
  }

  // ---------------------------------------------------------------- words

  const slug = (s) => s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

  // Romanian plural: "20 de ore", "3 ore", "o oră"
  function count(n, one, many) {
    if (n === 1) return one;
    const r = n % 100;
    return n >= 20 && !(r >= 1 && r <= 19) ? `${n} de ${many}` : `${n} ${many}`;
  }

  function sinceLong(date) {
    const min = Math.round((Date.now() - date) / 60000);
    if (min < 2) return "chiar acum";
    if (min < 60) return `acum ${count(min, "un minut", "minute")}`;
    const h = Math.floor(min / 60);
    if (h < 24) return `acum ${count(h, "o oră", "ore")}`;
    const d = Math.floor(h / 24);
    return d === 1 ? "ieri" : `acum ${count(d, "o zi", "zile")}`;
  }

  function sinceShort(date) {
    const min = Math.round((Date.now() - date) / 60000);
    if (min < 2) return "acum";
    if (min < 60) return `${min} min`;
    const h = Math.floor(min / 60);
    if (h < 24) return count(h, "o oră", "ore");
    const d = Math.floor(h / 24);
    if (d === 1) return "ieri";
    if (d < 7) return count(d, "o zi", "zile");
    return date.toLocaleDateString("ro-RO", { day: "numeric", month: "short" });
  }

  function hue(text) {
    let h = 0;
    for (const ch of text) h = (h * 131 + ch.codePointAt(0)) % 9973;
    return Math.round((h * 137.508) % 360);
  }

  const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  // ---------------------------------------------------------------- seen tracking

  const onSeen = new WeakMap();
  const timers = new Map();
  const observer = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => {
        for (const e of entries) {
          const card = e.target;
          if (e.isIntersecting && e.intersectionRatio >= 0.9) {
            if (!timers.has(card)) {
              timers.set(card, setTimeout(() => {
                timers.delete(card);
                observer.unobserve(card);
                if (document.visibilityState === "visible") onSeen.get(card)?.();
              }, SEEN_AFTER_MS));
            }
          } else if (timers.has(card)) {
            clearTimeout(timers.get(card));
            timers.delete(card);
          }
        }
      }, { threshold: [0, 0.9] })
    : null;

  function resetObserver() {
    timers.forEach(clearTimeout);
    timers.clear();
    observer?.disconnect();
  }

  // ---------------------------------------------------------------- render

  function renderTabs() {
    tabsEl.replaceChildren();
    data.pages.forEach((page, i) => {
      const b = el("button", "tab", page.name);
      b.type = "button";
      b.id = `tab-${slug(page.name)}`;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-controls", "main");
      b.setAttribute("aria-selected", String(i === current));
      b.tabIndex = i === current ? 0 : -1;
      b.addEventListener("click", () => selectPage(i, true));
      tabsEl.append(b);
    });
  }

  tabsEl.addEventListener("keydown", (e) => {
    if (!data || !["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    const n = data.pages.length;
    const next = (current + (e.key === "ArrowRight" ? 1 : n - 1)) % n;
    selectPage(next, true);
    tabsEl.children[next].focus();
  });

  function tile(source) {
    const t = el("div", "tile", source);
    t.style.setProperty("--h", hue(source));
    return t;
  }

  function card(item, state, { revealed = false } = {}) {
    const href = safeUrl(item.link);
    const a = el("a", "card");
    if (href) a.href = href;
    a.target = "_blank";
    a.rel = "noopener";
    if (revealed || has(readMap, item.id)) a.classList.add("is-read");

    const fig = el("figure");
    const img = safeUrl(item.image);
    if (img) {
      const im = new Image();
      im.src = img;
      im.alt = "";
      im.loading = "lazy";
      im.decoding = "async";
      im.referrerPolicy = "no-referrer";
      im.addEventListener("error", () => fig.replaceChildren(tile(item.source)), { once: true });
      fig.append(im);
    } else {
      fig.append(tile(item.source));
    }

    const body = el("div", "card-body");
    const meta = el("div", "meta");
    meta.append(el("span", "source", item.source));
    if (item.time) {
      const d = new Date(item.time);
      const t = el("time", "time", sinceShort(d));
      t.dateTime = item.time;
      t.title = d.toLocaleString("ro-RO");
      meta.append(t);
    }
    body.append(meta, el("h3", null, item.title));
    if (item.snippet) body.append(el("p", "snippet", item.snippet));
    a.append(fig, body);

    const see = () => {
      if (has(seenMap, item.id)) return;
      seenMap[item.id] = Date.now();
      saveSoon();
      if (!revealed) { state.fresh -= 1; state.update(); }
    };
    const open = () => {
      see();
      if (!has(readMap, item.id)) { readMap[item.id] = Date.now(); saveNow(); }
      a.classList.add("is-read");
    };
    a.addEventListener("click", open);
    a.addEventListener("auxclick", (e) => { if (e.button === 1) open(); });

    if (!revealed && !has(seenMap, item.id) && observer) {
      onSeen.set(a, see);
      observer.observe(a);
    }
    return a;
  }

  function revealButton(n, onClick) {
    const b = el("button", "more", n === 1 ? "Arată titlul deja văzut" : `Arată ${count(n, "", "titluri")} deja văzute`);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  function lane(laneData, index) {
    const section = el("section", "lane");
    const titleId = `lane-${current}-${index}`;
    section.setAttribute("aria-labelledby", titleId);

    const head = el("div", "lane-head");
    const h2 = el("h2", "lane-title", laneData.name);
    h2.id = titleId;
    const tools = el("div", "lane-tools");
    const counter = el("span", "unread");
    tools.append(counter);
    head.append(h2, tools);
    section.append(head);

    const shown = laneData.items.filter((i) => !hidden.has(i.id));
    const old = laneData.items.filter((i) => hidden.has(i.id));

    const state = {
      fresh: shown.filter((i) => !has(seenMap, i.id)).length,
      update() {
        counter.replaceChildren();
        if (!laneData.items.length) return;
        if (this.fresh > 0) {
          counter.append(el("strong", null, String(this.fresh)), document.createTextNode(this.fresh === 1 ? " nou" : " noi"));
        } else {
          counter.textContent = "ai văzut tot";
        }
      },
    };
    state.update();

    if (!laneData.items.length) {
      section.append(el("p", "empty", "Niciun titlu în perioada aleasă pentru această secțiune."));
      return section;
    }

    const row = el("div", "row");
    row.setAttribute("aria-label", laneData.name);
    const showOld = (btn) => {
      const cards = old.map((i) => card(i, state, { revealed: true }));
      btn.replaceWith(...cards);
      cards[0]?.focus({ preventScroll: true });
    };

    if (!shown.length) {
      const wrap = el("div", "empty");
      wrap.append(el("p", null, "Nimic nou aici de la ultima vizită."));
      const b = revealButton(old.length, () => {
        wrap.replaceWith(row);
        row.append(...old.map((i) => card(i, state, { revealed: true })));
        addNudges();
      });
      b.classList.add("more-inline");
      wrap.append(b);
      section.append(wrap);
    } else {
      for (const item of shown) row.append(card(item, state));
      if (old.length) {
        const b = revealButton(old.length, () => showOld(b));
        row.append(b);
      }
      section.append(row);
      addNudges();
    }

    function addNudges() {
      if (tools.querySelector(".nudge")) return;
      for (const [label, dir, sym] of [["Înapoi", -1, "‹"], ["Înainte", 1, "›"]]) {
        const b = el("button", "nudge", sym);
        b.type = "button";
        b.setAttribute("aria-label", `${label}: ${laneData.name}`);
        b.addEventListener("click", () => row.scrollBy({ left: dir * row.clientWidth * 0.85 }));
        tools.append(b);
      }
    }

    return section;
  }

  function renderPage() {
    resetObserver();
    const page = data.pages[current];
    main.replaceChildren(...page.lanes.map(lane));
  }

  function selectPage(i, userAction) {
    current = i;
    store.set(TAB_KEY, slug(data.pages[i].name));
    if (userAction) history.replaceState(null, "", `#${slug(data.pages[i].name)}`);
    [...tabsEl.children].forEach((b, j) => {
      b.setAttribute("aria-selected", String(j === i));
      b.tabIndex = j === i ? 0 : -1;
    });
    renderPage();
    if (userAction) window.scrollTo({ top: 0 });
  }

  function renderUpdated() {
    if (!data) return;
    const g = new Date(data.generated);
    const hours = (Date.now() - g) / 36e5;
    updatedEl.textContent = hours > 2
      ? `Titlurile nu s-au mai actualizat din ${g.toLocaleString("ro-RO", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`
      : `Actualizat ${sinceLong(g)}`;
  }

  const ERR = {
    "could not connect": "site-ul nu răspunde",
    "timed out": "site-ul răspunde prea încet",
    "not a valid feed": "adresa nu este un flux RSS valid",
    "feed has no recent headlines": "fluxul RSS nu are titluri recente",
  };
  const explain = (msg) => (msg || "").split("; ").map((m) => {
    const backup = m.startsWith("backup: ");
    const core = backup ? m.slice(8) : m;
    const text = ERR[core] || (core.startsWith("HTTP ") ? `eroare ${core.slice(5)}` : core);
    return backup ? `rezerva: ${text}` : text;
  }).join("; ");

  function renderStatus() {
    const s = data.status || [];
    const failed = s.filter((x) => x.status === "failed");
    const backup = s.filter((x) => x.status === "fallback");
    const ok = s.length - failed.length - backup.length;
    $("status-summary").textContent =
      `Starea surselor: ${ok} funcționează` +
      (backup.length ? `, ${backup.length} prin rezervă` : "") +
      (failed.length ? `, ${failed.length} nu răspund` : "");

    const body = $("status-body");
    body.replaceChildren();
    const group = (title, list) => {
      if (!list.length) return;
      const g = el("div", "status-group");
      g.append(el("h4", null, title));
      const ul = el("ul");
      for (const x of list) ul.append(el("li", null, `${x.name} (${x.page}, ${x.lane}): ${explain(x.error)}`));
      g.append(ul);
      body.append(g);
    };
    group("Nu răspund", failed);
    group("Preluate prin căutare Google, pentru că fluxul RSS nu merge", backup);
    if (!failed.length && !backup.length) body.append(el("p", null, "Toate sursele funcționează normal."));
  }

  // Show a new batch: hide everything seen so far and redraw.
  function showNew(next) {
    const first = !data;
    data = next;
    pending = null;
    freshBtn.hidden = true;
    startVisit();
    if (first) {
      const wanted = location.hash.slice(1) || store.get(TAB_KEY, "");
      const found = data.pages.findIndex((p) => slug(p.name) === wanted);
      current = found >= 0 ? found : 0;
    }
    current = Math.min(current, data.pages.length - 1);
    renderTabs();
    renderPage();
    renderStatus();
    renderUpdated();
  }

  freshBtn.addEventListener("click", () => {
    saveNow();
    showNew(pending || data);
    window.scrollTo({ top: 0 });
  });

  // ---------------------------------------------------------------- data

  // newVisit: redraw right away. Otherwise just offer the new headlines,
  // so nothing moves while you're reading.
  async function load(newVisit) {
    lastFetch = Date.now();
    try {
      const res = await fetch(`data.json?t=${Date.now()}`, { cache: "no-store" });
      if (!res.ok) throw new Error(res.status);
      const next = await res.json();
      if (!data || newVisit) {
        saveNow();
        showNew(next);
      } else if (next.generated !== data.generated) {
        pending = next;
        freshBtn.hidden = false;
      }
    } catch {
      if (!data) {
        main.replaceChildren(el("p", "notice", "Titlurile nu s-au putut încărca. Verifică conexiunea la internet și reîncarcă pagina."));
      }
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") saveNow();
    else if (Date.now() - lastFetch > NEW_VISIT_AFTER_MS) load(true);
  });
  setInterval(() => {
    renderUpdated();
    if (document.visibilityState === "visible" && Date.now() - lastFetch > 10 * 60000) load(false);
  }, 60000);
  window.addEventListener("hashchange", () => {
    if (!data) return;
    const i = data.pages.findIndex((p) => slug(p.name) === location.hash.slice(1));
    if (i >= 0 && i !== current) selectPage(i, false);
  });

  load(true);

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
  }
})();
