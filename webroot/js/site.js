// Bird Heaven front page. No framework, no build step.
//
// Three requests on load, in parallel: /api/config (site text + PayPal id),
// /api/live (stream status -- a stub today) and /api/videos (the catalog).
// Each part of the page renders independently, so one failing never blanks
// the others.

(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  // hls.js is only fetched when a live HLS stream is actually on and the browser
  // can't play HLS natively (everything except Safari/iOS).
  const HLS_JS = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js";
  const PAGE_SIZE = 12;

  // Swatch colours for the species we expect most. Anything else gets sage.
  const SPECIES_COLORS = {
    "northern cardinal": "#c4402c",
    "blue jay": "#3f7cc4",
    "american goldfinch": "#e6b43c",
    "black-capped chickadee": "#6b6f73",
    "carolina chickadee": "#6b6f73",
    "tufted titmouse": "#9aa3ad",
    "house finch": "#b5524a",
    "purple finch": "#8e3d63",
    "downy woodpecker": "#3a3a3a",
    "red-bellied woodpecker": "#d0573a",
    "white-breasted nuthatch": "#5c7a99",
    "dark-eyed junco": "#4e5560",
    "mourning dove": "#b39b85",
    "american robin": "#cf6a2c",
    "eastern bluebird": "#4e8fd1",
    "house sparrow": "#8b6b4a",
    "song sparrow": "#8b6b4a",
    "european starling": "#3c3f52",
    "ruby-throated hummingbird": "#3c9a6a",
    "eastern gray squirrel": "#8a8378",
  };
  const colorFor = (name) => SPECIES_COLORS[name.toLowerCase()] || null;

  const state = { videos: [], filter: null, shown: PAGE_SIZE };

  async function getJSON(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`${url}: ${res.status}`);
    return res.json();
  }

  // ---------------------------------------------------------------- formatting

  function fmtDuration(sec) {
    if (sec == null || !isFinite(sec)) return "";
    const s = Math.round(sec);
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  }

  function fmtWhen(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const diffDays = (Date.now() - d.getTime()) / 86400000;
    const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    if (diffDays < 1 && new Date().getDate() === d.getDate()) return `Today, ${time}`;
    if (diffDays < 2) return `Yesterday, ${time}`;
    if (diffDays < 7) return `${d.toLocaleDateString(undefined, { weekday: "long" })}, ${time}`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric",
      year: d.getFullYear() === new Date().getFullYear() ? undefined : "numeric" });
  }

  function relative(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const mins = Math.round((Date.now() - d.getTime()) / 60000);
    if (mins < 60) return `${Math.max(mins, 1)} min ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs} hr ago`;
    const days = Math.round(hrs / 24);
    return days === 1 ? "yesterday" : `${days} days ago`;
  }

  function greeting() {
    const h = new Date().getHours();
    if (h < 5) return "Night shift — the owls have the yard";
    if (h < 9) return "Good morning — the early birds are out";
    if (h < 12) return "Late morning at the feeder";
    if (h < 17) return "Afternoon at the feeder";
    if (h < 20) return "Evening — last call at the feeder";
    return "Good evening — the feeder is quiet";
  }

  function tagList(ul, species) {
    ul.replaceChildren(...species.map((s) => {
      const li = document.createElement("li");
      li.textContent = s;
      const c = colorFor(s);
      if (c) li.style.setProperty("--c", c);
      return li;
    }));
  }

  // ---------------------------------------------------------------- config

  function renderConfig(cfg) {
    if (cfg.siteName) $$("[data-site-name]").forEach((el) => { el.textContent = cfg.siteName; });
    if (cfg.location) $("[data-location]").textContent = ` · Filmed in ${cfg.location}`;
    if (cfg.paypalHostedButtonId) {
      $("[data-paypal-id]").value = cfg.paypalHostedButtonId;
      $("[data-support]").hidden = false;
      $$("[data-support-link]").forEach((el) => { el.hidden = false; });
    }
  }

  // ---------------------------------------------------------------- live

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src; s.async = true; s.crossOrigin = "anonymous";
      s.onload = resolve; s.onerror = () => reject(new Error(`failed to load ${src}`));
      document.head.append(s);
    });
  }

  function setPill(live, label) {
    const pill = $("[data-live-pill]");
    pill.classList.toggle("pill-live", live);
    pill.classList.toggle("pill-offline", !live);
    $("[data-live-label]").textContent = label;
  }

  async function renderLive(live) {
    $("[data-live-title]").textContent = live.title || "Feeder cam";
    if (live.status !== "live" || !live.url) {
      setPill(false, "Offline");
      return;
    }
    const player = $("[data-live-player]");
    $("[data-offline]").hidden = true;

    if (live.kind === "iframe") {
      const f = document.createElement("iframe");
      f.src = live.url;
      f.title = live.title || "Live feeder camera";
      f.allow = "autoplay; fullscreen; picture-in-picture";
      f.referrerPolicy = "strict-origin-when-cross-origin";
      player.append(f);
      setPill(true, "Live");
      return;
    }

    const v = document.createElement("video");
    Object.assign(v, { controls: true, muted: true, autoplay: true, playsInline: true });
    player.append(v);
    try {
      if (live.kind === "hls" && !v.canPlayType("application/vnd.apple.mpegurl")) {
        await loadScript(HLS_JS);
        if (!window.Hls || !window.Hls.isSupported()) throw new Error("HLS unsupported");
        const hls = new window.Hls({ liveDurationInfinity: true });
        hls.loadSource(live.url);
        hls.attachMedia(v);
      } else {
        v.src = live.url;
      }
      setPill(true, "Live");
    } catch (err) {
      console.warn(err);
      v.remove();
      $("[data-offline]").hidden = false;
      setPill(false, "Offline");
    }
  }

  // ---------------------------------------------------------------- catalog

  function renderStats(videos) {
    const species = new Set(videos.flatMap((v) => v.species));
    $("[data-stat-clips]").textContent = videos.length.toLocaleString();
    $("[data-stat-species]").textContent = species.size.toLocaleString();
    const latest = videos.find((v) => v.species.length);
    const dd = $("[data-stat-latest]");
    if (latest) {
      dd.textContent = latest.species[0];
      dd.classList.add("small");
      const when = relative(latest.recorded);
      if (when) dd.title = `Recorded ${when}`;
    } else {
      dd.textContent = "—";
    }
  }

  function renderOfflineHighlight(videos) {
    const pick = videos.find((v) => v.featured && v.poster) || videos.find((v) => v.poster) || videos[0];
    if (!pick) return;
    if (pick.poster) {
      const img = $("[data-offline-poster]");
      img.src = pick.poster;
      img.alt = pick.title;
      img.onerror = () => { img.hidden = true; };
      img.hidden = false;
    }
    const btn = $("[data-play-latest]");
    btn.hidden = false;
    btn.onclick = () => openViewer(pick);
  }

  function renderChips(videos) {
    const counts = new Map();
    videos.forEach((v) => v.species.forEach((s) => counts.set(s, (counts.get(s) || 0) + 1)));
    const host = $("[data-chips]");
    if (!counts.size) { host.closest("section").hidden = true; return; }

    const sorted = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    const make = (label, value, count) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.dataset.value = value ?? "";
      b.setAttribute("aria-pressed", String(state.filter === value));
      if (value) {
        const sw = document.createElement("span");
        sw.className = "swatch";
        const c = colorFor(value);
        if (c) sw.style.setProperty("--c", c);
        b.append(sw);
      }
      const name = document.createElement("span");
      name.textContent = label;
      b.append(name);
      if (count != null) {
        const n = document.createElement("span");
        n.className = "count";
        n.textContent = `×${count}`;
        b.append(n);
      }
      b.addEventListener("click", () => {
        state.filter = state.filter === value ? null : value;
        state.shown = PAGE_SIZE;
        $$(".chip", host).forEach((el) =>
          el.setAttribute("aria-pressed", String((el.dataset.value || null) === state.filter)));
        renderGrid();
        if (state.filter) $("#recordings").scrollIntoView({ block: "start" });
      });
      return b;
    };
    host.replaceChildren(make("All visitors", null, null),
      ...sorted.map(([s, n]) => make(s, s, n)));
  }

  function renderGrid() {
    const tpl = $("#card-tpl");
    const grid = $("[data-grid]");
    const list = state.filter ? state.videos.filter((v) => v.species.includes(state.filter)) : state.videos;
    const page = list.slice(0, state.shown);

    grid.replaceChildren(...page.map((v) => {
      const node = tpl.content.firstElementChild.cloneNode(true);
      const thumb = $(".card-thumb", node);
      const img = $("img", node);
      if (v.poster) {
        img.src = v.poster;
        img.onerror = () => thumb.classList.add("no-poster");
      } else {
        thumb.classList.add("no-poster");
      }
      $(".card-dur", node).textContent = fmtDuration(v.duration);
      $(".card-title", node).textContent = v.title;
      $(".card-when", node).textContent = fmtWhen(v.recorded);
      $(".card-btn", node).setAttribute("aria-label", `Play: ${v.title}`);
      $(".card-btn", node).addEventListener("click", () => openViewer(v));
      tagList($(".tags", node), v.species);
      return node;
    }));

    $("[data-empty]").hidden = list.length > 0;
    $("[data-more]").hidden = list.length <= state.shown;
    $("[data-filter-note]").textContent = state.filter
      ? `Showing ${list.length} clip${list.length === 1 ? "" : "s"} featuring the ${state.filter}.`
      : "Short clips, trimmed from the feeder cam. Newest first.";
  }

  // ---------------------------------------------------------------- viewer

  const viewer = $("[data-viewer]");
  const viewerVideo = $("[data-viewer-video]");

  function openViewer(v) {
    $("[data-viewer-title]").textContent = v.title;
    $("[data-viewer-when]").textContent = [fmtWhen(v.recorded), fmtDuration(v.duration)].filter(Boolean).join(" · ");
    $("[data-viewer-desc]").textContent = v.description || "";
    tagList($("[data-viewer-tags]"), v.species);
    $("[data-viewer-error]").hidden = true;
    viewerVideo.hidden = false;
    viewerVideo.poster = v.poster || "";
    viewerVideo.src = v.video;
    viewer.showModal();
    viewerVideo.play().catch(() => { /* autoplay may be blocked; controls remain */ });
    history.replaceState(null, "", `#clip=${encodeURIComponent(v.slug)}`);
  }

  viewerVideo.addEventListener("error", () => {
    if (!viewerVideo.getAttribute("src")) return;
    viewerVideo.hidden = true;
    $("[data-viewer-error]").hidden = false;
  });
  viewer.addEventListener("close", () => {
    viewerVideo.pause();
    viewerVideo.removeAttribute("src");
    viewerVideo.load();
    if (location.hash.startsWith("#clip=")) history.replaceState(null, "", location.pathname);
  });
  // Click on the backdrop closes it.
  viewer.addEventListener("click", (e) => { if (e.target === viewer) viewer.close(); });

  $("[data-more-btn]").addEventListener("click", () => { state.shown += PAGE_SIZE; renderGrid(); });

  // ---------------------------------------------------------------- boot

  $("[data-greeting]").textContent = greeting();
  $("[data-year]").textContent = new Date().getFullYear();

  getJSON("/api/config").then(renderConfig).catch((e) => console.warn(e));
  getJSON("/api/live").then(renderLive).catch((e) => { console.warn(e); setPill(false, "Offline"); });
  getJSON("/api/videos")
    .then(({ videos }) => {
      state.videos = videos || [];
      renderStats(state.videos);
      renderChips(state.videos);
      renderGrid();
      renderOfflineHighlight(state.videos);
      const m = location.hash.match(/^#clip=(.+)$/);
      const linked = m && state.videos.find((v) => v.slug === decodeURIComponent(m[1]));
      if (linked) openViewer(linked);
    })
    .catch((e) => {
      console.warn(e);
      $("[data-empty]").textContent = "The archive is taking a short break. Please check back soon.";
      $("[data-empty]").hidden = false;
    });
})();
