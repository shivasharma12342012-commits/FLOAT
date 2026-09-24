/* Float — the working surface.
 *
 * One script, no build step, no framework. It has to start instantly on a
 * ₹28,000 school laptop, so everything here is plain DOM and one fetch stream.
 *
 * Layout of this file:
 *   1  state + plumbing
 *   2  boot and the greeting
 *   3  the conversation: sending, streaming, rendering
 *   4  markdown
 *   5  cards (files, steps, tasks)
 *   6  the drawer: toolkit, classes, to-do, files
 *   7  settings
 *   8  computer control
 */

/* ══════════════════════════════════ 1. state + plumbing ══════════════════ */

const TOKEN = sessionStorage.getItem("float.token");
if (!TOKEN) location.replace("/login");

const $ = (id) => document.getElementById(id);
const make = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const S = {
  boot: null,
  user: null,
  conv: null,           // current conversation id
  model: localStorage.getItem("float.model") || "",
  attachments: [],
  streaming: false,
  abort: null,
  driving: false,
};

async function api(path, options = {}) {
  const init = {
    method: options.method || (options.body === undefined ? "GET" : "POST"),
    headers: { Authorization: "Bearer " + TOKEN },
  };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, init);
  if (res.status === 401) { sessionStorage.removeItem("float.token"); location.replace("/login"); return; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong at Float's end.");
  return data;
}

let toastTimer = null;
function toast(text, bad = false) {
  const node = $("toast");
  node.textContent = text;
  node.className = "toast" + (bad ? " bad" : "");
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), bad ? 5200 : 3200);
}

const escapeHtml = (text) =>
  String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function bytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function when(seconds) {
  const gap = Date.now() / 1000 - seconds;
  if (gap < 90) return "just now";
  if (gap < 3600) return Math.round(gap / 60) + " min ago";
  if (gap < 86400) return Math.round(gap / 3600) + " h ago";
  if (gap < 7 * 86400) return Math.round(gap / 86400) + " d ago";
  return new Date(seconds * 1000).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

/* ══════════════════════════════════ 2. boot ═════════════════════════════ */

async function boot() {
  let data;
  try {
    data = await api("/api/bootstrap");
  } catch (err) {
    document.body.innerHTML =
      '<div class="empty" style="padding-top:22vh"><b>Float could not start</b>' +
      escapeHtml(err.message) + "</div>";
    return;
  }
  if (!data) return;

  S.boot = data;
  S.user = data.user;

  theme(data.user.appearance || "system");
  paintIdentity();
  paintGreeting(data.greeting, data.reflection);
  paintQuick(data.greeting);
  paintThreads(data.conversations);
  paintNotices(data.notices);
  paintCounts();
  paintModelChip();
  paintAssistChip();

  $("count-kit").textContent = data.toolkit.length;
  $("open-admin").hidden = data.user.role !== "admin";

  if (sessionStorage.getItem("float.must_change")) {
    sessionStorage.removeItem("float.must_change");
    openSettings("security");
    toast("Your admin gave you a temporary password. Set your own now.");
  } else if (!Object.keys(data.keys || {}).length && !data.models.some((m) => m.ready)) {
    openSettings("models");
  }

  pollComputer();
}

function paintIdentity() {
  const name = S.user.name || S.user.email;
  $("me-name").textContent = name;
  $("me-avatar").textContent = name.trim().charAt(0).toUpperCase();
  $("me-sub").textContent = S.user.role === "admin" ? "Admin · Settings" : "Settings";
  $("school-tag").textContent = S.user.school || "";
}

/** The greeting. Times New Roman, no network, nothing to click through. */
function paintGreeting(greeting, reflection) {
  $("g-date").textContent = `${greeting.date} · ${greeting.clock}`;
  $("g-headline").textContent = greeting.headline;
  $("g-detail").textContent = greeting.detail || "";
  $("g-detail").className = "detail" + (greeting.tone === "urgent" ? " urgent" : "");
  $("g-opener").textContent = greeting.opener || "";

  if (reflection) {
    $("reflection").hidden = false;
    $("reflection-text").textContent = reflection;
  }
}

/** Five openers, chosen for the hour of the day rather than at random. */
function paintQuick(greeting) {
  const box = $("quick");
  box.innerHTML = "";

  const byPeriod = greeting.period && greeting.period.label
    ? [`Plan ${greeting.period.label}`]
    : [];

  const picks = [
    ...byPeriod,
    "Make a worksheet for tomorrow",
    "Write a question paper",
    "Draft a message for parents",
    "Help me mark a set of answers",
  ].slice(0, 5);

  picks.forEach((text) => {
    const button = make("button", null, text);
    button.addEventListener("click", () => {
      $("input").value = text;
      grow();
      $("input").focus();
    });
    box.append(button);
  });
}

function paintThreads(list) {
  const box = $("threads");
  box.innerHTML = "";
  if (!list.length) {
    box.append(make("div", "rail-heading", "Nothing yet."));
    return;
  }
  list.forEach((conv) => {
    const row = make("button", "thread");
    row.setAttribute("aria-current", String(conv.id) === String(S.conv));
    row.append(make("span", null, conv.title || "Untitled"));

    const close = make("button", "x", "×");
    close.title = "Delete";
    close.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (!confirm(`Delete "${conv.title}"? The files it made stay in Files.`)) return;
      await api(`/api/conversations/${conv.id}`, { method: "DELETE", body: {} });
      if (String(S.conv) === String(conv.id)) newThread();
      refreshThreads();
    });
    row.append(close);

    row.addEventListener("click", () => openThread(conv.id));
    box.append(row);
  });
}

async function refreshThreads() {
  const data = await api("/api/conversations");
  if (data) paintThreads(data.conversations);
}

function paintNotices(notices) {
  const strip = $("notice-strip");
  if (!notices || !notices.length) { strip.hidden = true; return; }
  strip.hidden = false;
  strip.innerHTML = "";
  strip.className = "notice-strip" + (notices[0].level === "warn" ? " warn" : "");
  notices.slice(0, 2).forEach((notice) => {
    strip.append(make("b", null, notice.title));
    if (notice.body) strip.append(make("span", null, notice.body));
  });
}

function paintCounts() {
  $("count-classes").textContent = S.boot.classes.length || "";
  $("count-tasks").textContent = S.boot.tasks.length || "";
  $("count-files").textContent = S.boot.artifacts.length || "";
}

function paintModelChip() {
  const models = S.boot.models || [];
  const ready = models.filter((m) => m.ready);
  const chosen = models.find((m) => m.id === S.model) || ready[0];
  if (chosen) {
    S.model = chosen.id;
    $("model-label").textContent = chosen.label;
  } else {
    $("model-label").textContent = "Add a key";
    $("chip-model").classList.add("on");
  }
}

function paintAssistChip() {
  const levels = { coach: "Coach me", draft: "Draft it", full: "Do it all" };
  $("assist-label").textContent = levels[S.user.assist_level] || "Draft it";
}

/* ══════════════════════════════════ 3. the conversation ═════════════════ */

function newThread() {
  S.conv = null;
  $("turns").innerHTML = "";
  $("greeting").hidden = false;
  $("chip-thread").hidden = true;
  document.querySelectorAll(".thread").forEach((t) => t.setAttribute("aria-current", "false"));
  $("input").focus();
}

async function openThread(convId) {
  const data = await api(`/api/conversations/${convId}`);
  if (!data) return;
  S.conv = convId;
  $("greeting").hidden = true;
  $("chip-thread").hidden = false;
  $("chip-thread-title").textContent = data.conversation.title;
  $("turns").innerHTML = "";

  const files = {};
  (data.artifacts || []).forEach((a) => (files[a.id] = a));

  data.messages.forEach((message) => {
    if (message.role === "user") addUserTurn(message.content);
    else {
      const turn = addFloatTurn();
      turn.prose.innerHTML = markdown(message.content);
    }
  });

  // File cards belong at the end of a reopened thread — the model's words
  // referring to them have already been read.
  const last = $("turns").lastElementChild;
  if (last && Object.keys(files).length) {
    Object.values(files).forEach((artifact) => last.append(fileCard(artifact)));
  }

  document.querySelectorAll(".thread").forEach((t) => t.setAttribute("aria-current", "false"));
  scrollDown();
  closeDrawer();
}

function addUserTurn(text, attachments) {
  const turn = make("div", "turn");
  const who = make("div", "turn-who");
  who.append(make("span", "avatar", (S.user.name || "?").charAt(0).toUpperCase()));
  who.append(make("span", null, "You"));
  turn.append(who);
  turn.append(make("div", "user-said", text));

  if (attachments && attachments.length) {
    const strip = make("div", "turn-files");
    attachments.forEach((file) => {
      if (file.kind === "image") {
        const img = document.createElement("img");
        img.src = `data:${file.mime};base64,${file.data}`;
        img.alt = file.name;
        strip.append(img);
      } else {
        strip.append(make("span", "doc", file.name));
      }
    });
    turn.append(strip);
  }

  $("turns").append(turn);
  scrollDown();
  return turn;
}

function addFloatTurn() {
  const turn = make("div", "turn");
  const who = make("div", "turn-who");
  who.append(make("span", "float-mark", "F"));
  who.append(make("span", null, "Float"));
  turn.append(who);

  const prose = make("div", "prose");
  turn.append(prose);
  $("turns").append(turn);
  return { node: turn, prose, append: (child) => turn.append(child) };
}

function scrollDown() {
  const stream = $("stream");
  stream.scrollTop = stream.scrollHeight;
}

/** Send whatever is in the composer (or a prompt handed in by the toolkit). */
async function send(prompt, convId) {
  if (S.streaming) return;
  const input = $("input");
  const text = (prompt !== undefined ? prompt : input.value).trim();
  const attachments = S.attachments.slice();
  if (!text && !attachments.length) return;

  if (convId !== undefined) S.conv = convId;
  if (prompt === undefined) { input.value = ""; grow(); }
  clearAttachments();

  $("greeting").hidden = true;
  addUserTurn(text, attachments);

  const turn = addFloatTurn();
  const waiting = make("div", "working");
  waiting.append(make("span", "pulse"));
  waiting.append(make("span", null, "Thinking…"));
  turn.node.append(waiting);
  scrollDown();

  setStreaming(true);
  let raw = "";
  let stepsCard = null;

  try {
    S.abort = new AbortController();
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + TOKEN },
      signal: S.abort.signal,
      body: JSON.stringify({
        message: text,
        conv_id: S.conv,
        model: S.model,
        attachments,
        allow_computer: true,
      }),
    });

    if (!res.ok) {
      const problem = await res.json().catch(() => ({}));
      throw new Error(problem.error || "Float could not start that answer.");
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let cut;
      while ((cut = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;

        let event;
        try { event = JSON.parse(line.slice(5).trim()); } catch { continue; }

        if (event.type === "start") {
          S.conv = event.conv_id;
          $("chip-thread").hidden = false;
          $("chip-thread-title").textContent = event.title;
        } else if (event.type === "text") {
          waiting.remove();
          raw += event.text;
          turn.prose.innerHTML = markdown(raw) + '<span class="caret"></span>';
          scrollDown();
        } else if (event.type === "tool") {
          waiting.hidden = false;
          waiting.lastChild.textContent = event.label + "…";
          turn.node.append(waiting);
          scrollDown();
        } else if (event.type === "computer_step") {
          stepsCard = stepsCard || stepCard(turn);
          stepsCard.add(event.text);
          $("driving-step").textContent = event.text;
          scrollDown();
        } else if (event.type === "card") {
          waiting.remove();
          turn.prose.innerHTML = markdown(raw);
          if (event.card.type === "file") {
            turn.node.append(fileCard(event.card));
            refreshFiles();
          } else if (event.card.type === "task") {
            turn.node.append(taskCard(event.card));
          } else if (event.card.type === "computer") {
            stepsCard = stepsCard || stepCard(turn);
            (event.card.steps || []).forEach((s) => stepsCard.add(s));
            stepsCard.finish(event.card.stopped);
          }
          scrollDown();
        } else if (event.type === "error") {
          waiting.remove();
          turn.node.append(errorCard(event.message));
        } else if (event.type === "done") {
          waiting.remove();
          turn.prose.innerHTML = markdown(raw);
          const usage = event.usage || {};
          $("composer-note").textContent =
            `${usage.seconds}s · ₹${((usage.paise || 0) / 100).toFixed(2)}`;
        }
      }
    }
  } catch (err) {
    waiting.remove();
    if (err.name !== "AbortError") turn.node.append(errorCard(err.message));
    else turn.node.append(errorCard("Stopped. Nothing was lost — the part above is saved."));
  } finally {
    setStreaming(false);
    turn.prose.innerHTML = markdown(raw);
    refreshThreads();
    scrollDown();
  }
}

function setStreaming(on) {
  S.streaming = on;
  const button = $("send");
  button.classList.toggle("stop", on);
  button.setAttribute("aria-label", on ? "Stop" : "Send");
  $("input").disabled = false;
}

/* ══════════════════════════════════ 4. markdown ═════════════════════════ */

/** Small on purpose: headings, lists, tables, quotes, code, rules, inline runs. */
function markdown(src) {
  if (!src) return "";
  const lines = String(src).replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;

  const inline = (text) =>
    escapeHtml(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/(^|[\s(])_([^_\n]+)_/g, "$1<em>$2</em>");

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    // fenced code
    if (/^```/.test(line)) {
      const body = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) body.push(lines[i++]);
      i++;
      out.push(`<pre><code>${escapeHtml(body.join("\n"))}</code></pre>`);
      continue;
    }

    // heading
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }

    // rule
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }

    // table
    if (line.includes("|") && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1] || "")) {
      const cells = (row) => row.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|");
      const head = cells(line).map((c) => `<th>${inline(c.trim())}</th>`).join("");
      i += 2;
      const body = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        body.push("<tr>" + cells(lines[i]).map((c) => `<td>${inline(c.trim())}</td>`).join("") + "</tr>");
        i++;
      }
      out.push(`<table><thead><tr>${head}</tr></thead><tbody>${body.join("")}</tbody></table>`);
      continue;
    }

    // quote
    if (/^>\s?/.test(line)) {
      const body = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) body.push(lines[i++].replace(/^>\s?/, ""));
      out.push(`<blockquote>${inline(body.join(" "))}</blockquote>`);
      continue;
    }

    // lists
    if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      while (i < lines.length &&
             (ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/).test(lines[i])) {
        let item = lines[i].replace(ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/, "");
        i++;
        // continuation lines that belong to the same bullet
        while (i < lines.length && lines[i].trim() &&
               !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) && !/^#{1,4}\s/.test(lines[i])) {
          item += " " + lines[i++].trim();
        }
        items.push(`<li>${inline(item)}</li>`);
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }

    // paragraph
    const body = [];
    while (i < lines.length && lines[i].trim() &&
           !/^(#{1,4}\s|>|```|\s*[-*+]\s|\s*\d+[.)]\s)/.test(lines[i])) {
      body.push(lines[i++]);
    }
    out.push(`<p>${inline(body.join(" "))}</p>`);
  }

  return out.join("");
}

/* ══════════════════════════════════ 5. cards ════════════════════════════ */

const GLYPH = { docx: "DOC", pdf: "PDF", xlsx: "XLS", csv: "CSV", html: "WEB", md: "MD", txt: "TXT" };

/** Float made a file. The teacher never asked where to put it — so ask here. */
function fileCard(artifact) {
  const card = make("div", "card-file");
  card.append(make("span", "glyph", GLYPH[artifact.kind] || "FILE"));

  const meta = make("div", "meta");
  meta.append(make("b", null, artifact.name));
  const line = make("small", null,
    `${artifact.label || (artifact.kind || "").toUpperCase()} · ${bytes(artifact.size || 0)}`);
  meta.append(line);
  if (artifact.saved_to) meta.append(make("em", null, " Saved"));
  card.append(meta);

  const acts = make("div", "acts");

  const desktop = make("button", "btn gold", "Desktop");
  desktop.title = "Save into a Float folder on your Desktop";
  desktop.addEventListener("click", () => saveFile(artifact, "desktop", acts));
  acts.append(desktop);

  const drive = make("button", "btn", "Drive");
  drive.title = S.boot.capabilities.drive
    ? "Save to your Google Drive"
    : "Connect Google Drive first — Settings → Google";
  drive.addEventListener("click", () => saveFile(artifact, "drive", acts));
  acts.append(drive);

  const down = make("button", "btn", "Download");
  down.addEventListener("click", () => {
    const link = document.createElement("a");
    link.href = `/api/artifacts/${artifact.id}/download`;
    link.rel = "noopener";
    // The download route needs the bearer token, so fetch and hand over a blob.
    fetch(link.href, { headers: { Authorization: "Bearer " + TOKEN } })
      .then((r) => r.blob())
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = artifact.name;
        anchor.click();
        setTimeout(() => URL.revokeObjectURL(url), 4000);
      })
      .catch(() => toast("Could not download that file.", true));
  });
  acts.append(down);

  card.append(acts);
  return card;
}

async function saveFile(artifact, where, acts) {
  acts.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const result = await api(`/api/artifacts/${artifact.id}/save`, { body: { where } });
    toast(result.message);
    if (where === "drive" && result.link) window.open(result.link, "_blank", "noopener");
    refreshFiles();
  } catch (err) {
    toast(err.message, true);
  } finally {
    acts.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
}

function stepCard(turn) {
  const card = make("div", "card-steps");
  card.append(make("b", null, "On your computer"));
  const list = document.createElement("ol");
  card.append(list);
  turn.node.append(card);
  return {
    add: (text) => list.append(make("li", null, text)),
    finish: (stopped) => {
      if (stopped) card.append(make("b", null, "You stopped it here."));
    },
  };
}

function taskCard(card) {
  const node = make("div", "card-steps");
  node.append(make("b", null, "Added to your to-do"));
  node.append(make("div", null, card.title + (card.due ? ` — due ${card.due}` : "")));
  refreshTasks();
  return node;
}

function errorCard(message) {
  const node = make("div", "err");
  node.append(make("b", null, "That didn't go through"));
  node.append(make("span", null, message));
  return node;
}

async function refreshFiles() {
  const data = await api("/api/artifacts");
  if (!data) return;
  S.boot.artifacts = data.artifacts;
  paintCounts();
  if (currentPanel === "files") panelFiles();
}

async function refreshTasks() {
  const data = await api("/api/tasks");
  if (!data) return;
  S.boot.tasks = data.tasks;
  paintCounts();
  if (currentPanel === "tasks") panelTasks();
}

/* ══════════════════════════════════ 6. the drawer ═══════════════════════ */

let currentPanel = null;

function openDrawer(title) {
  $("drawer-title").textContent = title;
  $("drawer").hidden = false;
  $("drawer-scrim").hidden = false;
  requestAnimationFrame(() => {
    $("drawer").classList.add("open");
    $("drawer-scrim").classList.add("on");
  });
}

function closeDrawer() {
  currentPanel = null;
  $("drawer").classList.remove("open");
  $("drawer-scrim").classList.remove("on");
  document.querySelectorAll(".rail-link").forEach((l) => l.setAttribute("aria-current", "false"));
  setTimeout(() => { $("drawer").hidden = true; $("drawer-scrim").hidden = true; }, 260);
}

function showPanel(name) {
  currentPanel = name;
  document.querySelectorAll(".rail-link").forEach((l) =>
    l.setAttribute("aria-current", String(l.dataset.panel === name)));
  ({ toolkit: panelToolkit, classes: panelClasses, tasks: panelTasks, files: panelFiles }[name])();
}

/* ── toolkit ───────────────────────────────────────────────────────────── */

function panelToolkit(filter = "") {
  openDrawer("Toolkit");
  const body = $("drawer-body");
  body.innerHTML = "";

  const search = make("input", "kit-search");
  search.placeholder = "Search 19 tools — worksheet, remarks, seating…";
  search.value = filter;
  search.addEventListener("input", () => renderKit(search.value));
  body.append(search);

  const list = make("div");
  body.append(list);

  function renderKit(text) {
    list.innerHTML = "";
    const needle = text.trim().toLowerCase();
    S.boot.toolkit_groups.forEach((group) => {
      const entries = S.boot.toolkit.filter((entry) =>
        entry.group === group &&
        (!needle ||
          entry.name.toLowerCase().includes(needle) ||
          entry.blurb.toLowerCase().includes(needle)));
      if (!entries.length) return;
      list.append(make("div", "kit-group", group.toUpperCase()));
      entries.forEach((entry) => {
        const item = make("button", "kit-item");
        item.append(make("b", null, entry.name));
        item.append(make("small", null, entry.blurb));
        item.addEventListener("click", () => toolkitForm(entry));
        list.append(item);
      });
    });
    if (!list.children.length) {
      list.append(make("div", "empty", "Nothing matches that. Try 'paper', 'parent' or 'plan'."));
    }
  }

  renderKit(filter);
  setTimeout(() => search.focus(), 80);
}

/** A tool's form. Built from the field list the server sent — no duplication. */
function toolkitForm(entry) {
  openDrawer(entry.name);
  const body = $("drawer-body");
  body.innerHTML = "";

  const back = make("button", "btn tiny", "‹ All tools");
  back.addEventListener("click", () => panelToolkit());
  body.append(back);

  body.append(make("p", "inline-note", entry.blurb));

  const values = {};
  const form = make("div");

  entry.fields.forEach((field) => {
    const wrap = make("div", "field");
    wrap.append(make("label", null, field.label));

    if (field.type === "select") {
      const select = document.createElement("select");
      (field.options || []).forEach((option) => {
        const node = document.createElement("option");
        node.value = option;
        node.textContent = option;
        select.append(node);
      });
      const preset = field.key === "board" ? S.user.board : field.value;
      if (preset) select.value = preset;
      values[field.key] = select.value;
      select.addEventListener("change", () => (values[field.key] = select.value));
      wrap.append(select);
    } else if (field.type === "textarea") {
      const area = document.createElement("textarea");
      area.placeholder = field.placeholder || "";
      area.addEventListener("input", () => (values[field.key] = area.value));
      wrap.append(area);
    } else {
      const input = document.createElement("input");
      input.type = field.type === "number" ? "number" : field.type === "date" ? "date" : "text";
      input.placeholder = field.placeholder || "";
      if (field.min !== undefined) input.min = field.min;
      if (field.max !== undefined) input.max = field.max;
      if (field.value !== undefined) { input.value = field.value; values[field.key] = String(field.value); }
      // Sensible defaults out of the teacher's own profile.
      if (field.key === "subject" && !input.value && (S.user.subjects || [])[0]) {
        input.value = S.user.subjects[0];
        values.subject = input.value;
      }
      if (field.key === "klass" && !input.value && (S.boot.classes[0] || {}).name) {
        input.value = S.boot.classes[0].name;
        values.klass = input.value;
      }
      input.addEventListener("input", () => (values[field.key] = input.value));
      wrap.append(input);
    }

    if (field.hint) wrap.append(make("div", "hint", field.hint));
    form.append(wrap);
  });

  body.append(form);

  const go = make("button", "btn primary", `Make the ${entry.name.toLowerCase()}`);
  go.style.width = "100%";
  go.style.padding = "10px";
  go.addEventListener("click", async () => {
    go.disabled = true;
    go.textContent = "Setting it up…";
    try {
      const result = await api(`/api/toolkit/${entry.id}`, { body: { values } });
      closeDrawer();
      $("turns").innerHTML = "";
      send(result.prompt, result.conv_id);
    } catch (err) {
      toast(err.message, true);
      go.disabled = false;
      go.textContent = `Make the ${entry.name.toLowerCase()}`;
    }
  });
  body.append(go);
}

/* ── classes ───────────────────────────────────────────────────────────── */

function panelClasses() {
  openDrawer("Classes");
  const body = $("drawer-body");
  body.innerHTML = "";

  body.append(make("p", "inline-note",
    "Register a class once and Float stops asking who is in it — names, marks and " +
    "attendance come from here."));

  if (!S.boot.classes.length) {
    const empty = make("div", "empty");
    empty.append(make("b", null, "No classes yet"));
    empty.append(make("span", null, "Add one below. Paste the roll list straight from your register."));
    body.append(empty);
  }

  S.boot.classes.forEach((klass) => {
    const panel = make("div", "panel");
    const head = make("div", "panel-head");
    head.append(make("b", null, klass.name));
    head.append(make("span", "spacer"));
    head.append(make("span", "pill", `${klass.students} students`));
    panel.append(head);

    const inner = make("div", "panel-body");
    inner.append(make("div", "hint",
      [klass.subject, klass.grade && `Class ${klass.grade}`, klass.section]
        .filter(Boolean).join(" · ")));

    const acts = make("div", "stack");
    const ask = make("button", "btn tiny", "Ask Float about this class");
    ask.addEventListener("click", () => {
      closeDrawer();
      $("input").value = `Tell me how ${klass.name} is doing — marks, attendance, who needs help.`;
      send();
    });
    const drop = make("button", "btn tiny danger", "Remove class");
    drop.addEventListener("click", async () => {
      if (!confirm(`Remove ${klass.name} and its student list?`)) return;
      await api(`/api/classes/${klass.id}`, { method: "DELETE", body: {} });
      const data = await api("/api/classes");
      S.boot.classes = data.classes;
      paintCounts();
      panelClasses();
    });
    acts.append(ask, drop);
    inner.append(acts);
    panel.append(inner);
    body.append(panel);
  });

  // add form
  const panel = make("div", "panel");
  const head = make("div", "panel-head");
  head.append(make("b", null, "Add a class"));
  panel.append(head);
  const inner = make("div", "panel-body");

  const name = field(inner, "Class name", "text", "8B");
  const row = make("div", "field-row");
  inner.append(row);
  const grade = field(row, "Grade", "text", "8");
  const section = field(row, "Section", "text", "B");
  const subject = field(inner, "Subject you teach them", "text", "Science");
  const roster = field(inner, "Roll list (optional)", "textarea",
    "1 Aarav Kumar\n2 Diya Sharma\n3 Ishaan Patel");

  const add = make("button", "btn primary", "Add class");
  add.addEventListener("click", async () => {
    if (!name.value.trim()) { toast("Give the class a name.", true); return; }
    add.disabled = true;
    try {
      await api("/api/classes", {
        body: {
          name: name.value, grade: grade.value, section: section.value,
          subject: subject.value, roster: roster.value,
        },
      });
      const data = await api("/api/classes");
      S.boot.classes = data.classes;
      paintCounts();
      panelClasses();
      toast("Class added.");
    } catch (err) {
      toast(err.message, true);
      add.disabled = false;
    }
  });
  inner.append(add);
  panel.append(inner);
  body.append(panel);
}

function field(parent, label, type, placeholder, value) {
  const wrap = make("div", "field");
  wrap.append(make("label", null, label));
  const node = document.createElement(type === "textarea" ? "textarea" : "input");
  if (type !== "textarea") node.type = type;
  node.placeholder = placeholder || "";
  if (value !== undefined) node.value = value;
  wrap.append(node);
  parent.append(wrap);
  return node;
}

/* ── to-do ─────────────────────────────────────────────────────────────── */

function panelTasks() {
  openDrawer("To do");
  const body = $("drawer-body");
  body.innerHTML = "";

  const panel = make("div", "panel");
  const inner = make("div", "panel-body");

  if (!S.boot.tasks.length) {
    const empty = make("div", "empty");
    empty.append(make("b", null, "Clear"));
    empty.append(make("span", null, "Float adds things here when you ask it to remember something."));
    inner.append(empty);
  }

  S.boot.tasks.forEach((task) => {
    const row = make("div", "row");
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.checked = !!task.done;
    tick.addEventListener("change", async () => {
      await api(`/api/tasks/${task.id}`, { body: { done: tick.checked } });
      refreshTasks();
    });
    row.append(tick);

    const grow = make("div", "grow");
    grow.append(make("b", null, task.title));
    if (task.due || task.klass) {
      grow.append(make("small", null, [task.klass, task.due && `due ${task.due}`]
        .filter(Boolean).join(" · ")));
    }
    row.append(grow);

    const drop = make("button", "btn tiny", "×");
    drop.addEventListener("click", async () => {
      await api(`/api/tasks/${task.id}`, { method: "DELETE", body: {} });
      refreshTasks();
    });
    row.append(drop);
    inner.append(row);
  });

  panel.append(inner);
  body.append(panel);

  const add = make("div", "panel");
  const addBody = make("div", "panel-body");
  const title = field(addBody, "Add something", "text", "Return Class 9 fair copies");
  const due = field(addBody, "By when", "date");
  const save = make("button", "btn primary", "Add");
  save.addEventListener("click", async () => {
    if (!title.value.trim()) return;
    await api("/api/tasks", { body: { title: title.value, due: due.value } });
    await refreshTasks();
    panelTasks();
  });
  addBody.append(save);
  add.append(addBody);
  body.append(add);
}

/* ── files ─────────────────────────────────────────────────────────────── */

function panelFiles() {
  openDrawer("Files");
  const body = $("drawer-body");
  body.innerHTML = "";

  body.append(make("p", "inline-note",
    "Everything Float has made for you. Files stay on this computer until you save them."));

  if (!S.boot.artifacts.length) {
    const empty = make("div", "empty");
    empty.append(make("b", null, "Nothing made yet"));
    empty.append(make("span", null, "Ask for a worksheet or a question paper and it will appear here."));
    body.append(empty);
    return;
  }

  S.boot.artifacts.forEach((artifact) => body.append(fileCard(artifact)));
}

/* ══════════════════════════════════ 7. settings ═════════════════════════ */

const TABS = [
  ["you", "You"],
  ["models", "Model & keys"],
  ["assist", "How Float helps"],
  ["security", "Security"],
  ["google", "Google"],
  ["about", "About"],
];

function openSettings(tab = "you") {
  $("modal-scrim").hidden = false;
  $("modal-title").textContent = "Settings";

  let bar = document.querySelector(".modal .tabs");
  if (!bar) {
    bar = make("div", "tabs");
    document.querySelector(".modal-head").after(bar);
  }
  bar.innerHTML = "";
  TABS.forEach(([id, label]) => {
    const button = make("button", null, label);
    button.setAttribute("aria-selected", String(id === tab));
    button.addEventListener("click", () => openSettings(id));
    bar.append(button);
  });

  $("modal-foot").innerHTML = "";
  const close = make("button", "btn", "Close");
  close.addEventListener("click", closeModal);
  $("modal-foot").append(close);

  ({ you: tabYou, models: tabModels, assist: tabAssist,
     security: tabSecurity, google: tabGoogle, about: tabAbout }[tab])();
}

function closeModal() { $("modal-scrim").hidden = true; }

function tabYou() {
  const body = $("modal-body");
  body.innerHTML = "";
  body.append(make("p", "inline-note",
    "Float uses this to pitch its answers right — the board, the grades, the language " +
    "you'd use with parents. Nothing here leaves your computer."));

  const name = field(body, "Name", "text", "", S.user.name);
  const school = field(body, "School", "text", "", S.user.school);

  const boardWrap = make("div", "field");
  boardWrap.append(make("label", null, "Board"));
  const board = document.createElement("select");
  S.boot.options.boards.forEach((option) => {
    const node = document.createElement("option");
    node.value = option; node.textContent = option;
    board.append(node);
  });
  board.value = S.user.board || S.boot.options.boards[0];
  boardWrap.append(board);
  body.append(boardWrap);

  const langWrap = make("div", "field");
  langWrap.append(make("label", null, "Language for parent messages"));
  const language = document.createElement("select");
  Object.entries(S.boot.options.languages).forEach(([code, label]) => {
    const node = document.createElement("option");
    node.value = code; node.textContent = label;
    language.append(node);
  });
  language.value = S.user.language || "en";
  langWrap.append(language);
  body.append(langWrap);

  const subjects = pickMany(body, "Subjects you teach", S.boot.options.subjects, S.user.subjects || []);
  const grades = pickMany(body, "Grades", S.boot.options.grades.map(String), S.user.grades || []);

  const appearanceWrap = make("div", "field");
  appearanceWrap.append(make("label", null, "Appearance"));
  const seg = make("div", "seg");
  [["system", "Match my computer"], ["light", "Light"], ["dark", "Dark"]].forEach(([id, label]) => {
    const button = make("button", null, label);
    button.setAttribute("aria-pressed", String((S.user.appearance || "system") === id));
    button.addEventListener("click", () => {
      S.user.appearance = id;
      theme(id);
      seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false"));
      button.setAttribute("aria-pressed", "true");
    });
    seg.append(button);
  });
  appearanceWrap.append(seg);
  body.append(appearanceWrap);

  const save = make("button", "btn primary", "Save");
  save.addEventListener("click", async () => {
    save.disabled = true;
    const data = await api("/api/me", {
      body: {
        name: name.value, school: school.value, board: board.value,
        language: language.value, appearance: S.user.appearance || "system",
        subjects: subjects(), grades: grades(),
      },
    });
    S.user = data.user;
    paintIdentity();
    toast("Saved.");
    save.disabled = false;
  });
  $("modal-foot").prepend(save);
}

function pickMany(parent, label, options, chosen) {
  const wrap = make("div", "field");
  wrap.append(make("label", null, label));
  const box = make("div", "tagbox");
  const picked = new Set(chosen);
  options.forEach((option) => {
    const tag = make("button", "tag", option);
    tag.setAttribute("aria-pressed", String(picked.has(option)));
    tag.addEventListener("click", () => {
      if (picked.has(option)) picked.delete(option); else picked.add(option);
      tag.setAttribute("aria-pressed", String(picked.has(option)));
    });
    box.append(tag);
  });
  wrap.append(box);
  parent.append(wrap);
  return () => [...picked];
}

function tabModels() {
  const body = $("modal-body");
  body.innerHTML = "";
  body.append(make("p", "inline-note",
    "Float runs on your own API key. The key is encrypted on this computer with a device " +
    "key that never leaves it, and Float only ever shows you the last four characters."));

  const chooser = make("div", "field");
  chooser.append(make("label", null, "Model for new answers"));
  const select = document.createElement("select");
  S.boot.models.forEach((model) => {
    const node = document.createElement("option");
    node.value = model.id;
    node.textContent = `${model.label} — ${model.provider_label}${model.ready ? "" : " (no key)"}`;
    node.disabled = !model.ready;
    select.append(node);
  });
  select.value = S.model;
  select.addEventListener("change", () => {
    S.model = select.value;
    localStorage.setItem("float.model", S.model);
    paintModelChip();
  });
  chooser.append(select);
  if (!S.boot.models.some((m) => m.ready)) {
    chooser.append(make("div", "hint", "Add a key below and this list comes alive."));
  }
  body.append(chooser);

  S.boot.providers.forEach((provider) => {
    const status = (S.boot.keys || {})[provider.id] || {};
    const panel = make("div", "panel");

    const head = make("div", "panel-head");
    head.append(make("b", null, provider.label));
    head.append(make("span", "spacer"));
    if (status.mine) head.append(make("span", "pill", "your key · " + status.mine.hint));
    else if (status.org) head.append(make("span", "pill admin", "school key"));
    else head.append(make("span", "pill off", "no key"));
    panel.append(head);

    const inner = make("div", "panel-body");
    inner.append(make("div", "hint", provider.help));

    const input = document.createElement("input");
    input.type = "password";
    input.placeholder = "Paste the key here";
    input.className = "mono key-input";
    input.style.width = "100%";
    input.style.padding = "8px 11px";
    input.style.marginTop = "8px";
    inner.append(input);

    const acts = make("div", "stack");
    acts.style.flexDirection = "row";
    acts.style.marginTop = "9px";

    const save = make("button", "btn primary", "Save & test");
    save.addEventListener("click", async () => {
      save.disabled = true;
      save.textContent = "Testing…";
      try {
        const result = await api("/api/keys", { body: { provider: provider.id, key: input.value } });
        S.boot.keys = result.keys;
        toast(result.detail || "Key saved.");
        const fresh = await api("/api/bootstrap");
        S.boot.models = fresh.models;
        paintModelChip();
        tabModels();
      } catch (err) {
        toast(err.message, true);
        save.disabled = false;
        save.textContent = "Save & test";
      }
    });
    acts.append(save);

    if (S.user.role === "admin") {
      const forSchool = make("button", "btn", "Save as school key");
      forSchool.addEventListener("click", async () => {
        try {
          const result = await api("/api/keys", {
            body: { provider: provider.id, key: input.value, scope: "org" },
          });
          S.boot.keys = result.keys;
          toast("School key saved — every teacher can use it.");
          tabModels();
        } catch (err) { toast(err.message, true); }
      });
      acts.append(forSchool);
    }

    if (status.mine) {
      const drop = make("button", "btn danger", "Remove mine");
      drop.addEventListener("click", async () => {
        const result = await api(`/api/keys/${provider.id}?scope=user`, { method: "DELETE", body: {} });
        S.boot.keys = result.keys;
        tabModels();
      });
      acts.append(drop);
    }

    const open = make("button", "btn", "Get a key ↗");
    open.addEventListener("click", () => window.open(provider.console, "_blank", "noopener"));
    acts.append(open);

    inner.append(acts);
    panel.append(inner);
    body.append(panel);
  });

  const usage = S.boot.usage || {};
  const spend = make("div", "panel");
  const spendBody = make("div", "panel-body");
  spendBody.append(make("b", null, "Your last 30 days"));
  const ledger = make("div", "ledger");
  ledger.append(make("span", null, ""));
  const one = make("span"); one.append(make("b", null, String(usage.calls || 0))); one.append(" answers");
  const two = make("span"); two.append(make("b", null, "₹" + ((usage.paise || 0) / 100).toFixed(2))); two.append(" estimated");
  ledger.append(one, two);
  spendBody.append(ledger);
  spendBody.append(make("div", "hint",
    "An estimate from published per-token prices. Your provider's bill is the real number."));
  spend.append(spendBody);
  body.append(spend);
}

function tabAssist() {
  const body = $("modal-body");
  body.innerHTML = "";
  body.append(make("p", "inline-note",
    "Float is meant to take the typing off your hands, not the teaching. This setting " +
    "decides how much it does before handing back."));

  const levels = [
    ["coach", "Coach me",
      "Float asks questions, offers an outline and reacts to what you write. You do the writing."],
    ["draft", "Draft it",
      "Float writes a first version and says plainly what it guessed at, so you know where to look."],
    ["full", "Do it all",
      "Float finishes the job. Useful for routine paperwork — notices, seating, mark sheets."],
  ];

  levels.forEach(([id, title, blurb]) => {
    const panel = make("div", "panel");
    const inner = make("div", "panel-body");
    const row = make("div", "row");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "assist";
    radio.checked = (S.user.assist_level || "draft") === id;
    radio.addEventListener("change", async () => {
      const data = await api("/api/me", { body: { assist_level: id } });
      S.user = data.user;
      paintAssistChip();
      toast("Float will " + title.toLowerCase() + " from now on.");
    });
    row.append(radio);
    const grow = make("div", "grow");
    grow.append(make("b", null, title));
    grow.append(make("small", null, blurb));
    row.append(grow);
    inner.append(row);
    panel.append(inner);
    body.append(panel);
  });

  const own = S.boot.own_work || {};
  const ledgerPanel = make("div", "panel");
  const ledgerBody = make("div", "panel-body");
  ledgerBody.append(make("b", null, "Your share this week"));
  const bar = make("div", "bar");
  const fill = make("i");
  fill.style.width = Math.max(3, own.own_share || 0) + "%";
  bar.append(fill);
  ledgerBody.append(bar);
  ledgerBody.append(make("div", "hint",
    `${own.own_share || 0}% of what went out this week was in your own words, across ` +
    `${own.n || 0} pieces. Float shows this because a teacher who edits everything ` +
    "teaches better than one who forwards it."));
  ledgerPanel.append(ledgerBody);
  body.append(ledgerPanel);
}

function tabSecurity() {
  const body = $("modal-body");
  body.innerHTML = "";
  body.append(make("p", "inline-note",
    "Float signs you out the moment this window closes, and again when the app itself " +
    "restarts. Nobody who picks up your laptop later gets back in."));

  const passPanel = make("div", "panel");
  const passHead = make("div", "panel-head");
  passHead.append(make("b", null, "Password"));
  passPanel.append(passHead);
  const passBody = make("div", "panel-body");
  const current = field(passBody, "Current password", "password");
  const next = field(passBody, "New password", "password");
  passBody.append(make("div", "hint", "At least 10 characters. A short sentence beats a scramble."));
  const changePass = make("button", "btn primary", "Change password");
  changePass.addEventListener("click", async () => {
    try {
      await api("/api/me/password", { body: { current: current.value, next: next.value } });
      current.value = next.value = "";
      toast("Password changed.");
    } catch (err) { toast(err.message, true); }
  });
  passBody.append(changePass);
  passPanel.append(passBody);
  body.append(passPanel);

  const pinPanel = make("div", "panel");
  const pinHead = make("div", "panel-head");
  pinHead.append(make("b", null, "Quick PIN"));
  pinHead.append(make("span", "spacer"));
  pinHead.append(make("span", S.user.has_pin ? "pill" : "pill off",
    S.user.has_pin ? "set" : "not set"));
  pinPanel.append(pinHead);
  const pinBody = make("div", "panel-body");
  pinBody.append(make("div", "hint",
    "Between periods you don't want to type a password. Set a 4–8 digit PIN and Float " +
    "asks only for that on this computer. Your password still works everywhere."));
  const pinPass = field(pinBody, "Your password", "password");
  const pin = field(pinBody, "New PIN", "password", "4–8 digits");
  const setPin = make("button", "btn primary", "Set PIN");
  setPin.addEventListener("click", async () => {
    try {
      await api("/api/me/pin", { body: { password: pinPass.value, pin: pin.value } });
      S.user.has_pin = !!pin.value;
      pinPass.value = pin.value = "";
      toast(S.user.has_pin ? "PIN set." : "PIN removed.");
      tabSecurity();
    } catch (err) { toast(err.message, true); }
  });
  pinBody.append(setPin);
  if (S.user.has_pin) {
    const clear = make("button", "btn danger", "Remove PIN");
    clear.style.marginLeft = "7px";
    clear.addEventListener("click", async () => {
      try {
        await api("/api/me/pin", { body: { password: pinPass.value, pin: "" } });
        S.user.has_pin = false;
        toast("PIN removed.");
        tabSecurity();
      } catch (err) { toast(err.message, true); }
    });
    pinBody.append(clear);
  }
  pinPanel.append(pinBody);
  body.append(pinPanel);
}

function tabGoogle() {
  const body = $("modal-body");
  body.innerHTML = "";
  const caps = S.boot.capabilities;

  if (!caps.google) {
    body.append(make("p", "inline-note",
      "Google isn't set up on this installation. Float works perfectly without it — " +
      "files save to your Desktop instead. To switch it on, put a Google client ID and " +
      "secret in the .env file beside the app and restart."));
    return;
  }

  body.append(make("p", "inline-note",
    "Connecting Drive lets the Save to Drive button work. Float only ever sees the files " +
    "it puts there itself — it cannot read the rest of your Drive."));

  const panel = make("div", "panel");
  const inner = make("div", "panel-body");
  const row = make("div", "row");
  const grow = make("div", "grow");
  grow.append(make("b", null, "Google Drive"));
  grow.append(make("small", null, caps.drive
    ? "Connected for this session."
    : "Not connected. Files still save to your Desktop."));
  row.append(grow);

  const connect = make("button", "btn primary", caps.drive ? "Reconnect" : "Connect Drive");
  connect.addEventListener("click", async () => {
    try {
      const { url } = await api("/api/auth/google/start?drive=1");
      window.open(url, "float-drive", "width=520,height=640");
    } catch (err) { toast(err.message, true); }
  });
  row.append(connect);
  inner.append(row);
  inner.append(make("div", "hint",
    "The Drive connection lives in memory only. Closing Float disconnects it."));
  panel.append(inner);
  body.append(panel);
}

/** About, and — for one person — the way into the local runtime. */
let aboutTaps = 0;
function tabAbout() {
  const body = $("modal-body");
  body.innerHTML = "";

  body.append(make("p", "inline-note",
    "Float is a working companion for schoolteachers. It runs entirely on this computer: " +
    "your classes, your drafts and your files never go anywhere except to the model " +
    "provider whose key you supplied."));

  const panel = make("div", "panel");
  const inner = make("div", "panel-body");

  const rows = [
    ["Version", "1.0.0"],
    ["Data", "data/float.sqlite3, beside the app"],
    ["Files", "data/files — until you save them elsewhere"],
    ["Sessions", "end when the app closes"],
  ];
  rows.forEach(([label, value]) => {
    const row = make("div", "row");
    const grow = make("div", "grow");
    grow.append(make("b", null, label));
    row.append(grow);
    row.append(make("small", null, value));
    inner.append(row);
  });
  panel.append(inner);
  body.append(panel);

  const mark = make("p", "hint", "Float · Your teaching, lighter.");
  mark.style.textAlign = "center";
  mark.style.cursor = "default";
  mark.addEventListener("click", () => {
    aboutTaps += 1;
    if (aboutTaps < 7) return;
    aboutTaps = 0;
    if (body.querySelector("#rt-phrase")) return;

    const gate = make("div", "field");
    gate.append(make("label", null, "Passphrase"));
    const input = document.createElement("input");
    input.type = "password";
    input.id = "rt-phrase";
    input.autocomplete = "off";
    gate.append(input);
    const go = make("button", "btn", "Enter");
    go.addEventListener("click", async () => {
      try {
        const result = await api("/api/unlock", { body: { phrase: input.value, enable: true } });
        toast(result.enabled ? "Local models available. Reopen the model list." : "Off.");
        const fresh = await api("/api/bootstrap");
        S.boot.models = fresh.models;
        paintModelChip();
        closeModal();
      } catch {
        toast("Not recognised.", true);
      }
    });
    gate.append(go);
    body.append(gate);
    input.focus();
  });
  body.append(mark);
}

/* ══════════════════════════════════ 8. computer control ═════════════════ */

async function pollComputer() {
  try {
    const status = await api("/api/computer/status");
    if (!status) return;
    const running = !!status.running;
    if (running !== S.driving) {
      S.driving = running;
      $("driving").hidden = !running;
      if (running) $("driving-what").textContent = status.intent || "Float is using your computer";
      else $("driving-step").textContent = "";
    }
  } catch { /* the poll is a nicety; never let it break the app */ }
  setTimeout(pollComputer, S.driving ? 900 : 4000);
}

async function stopComputer() {
  try {
    await api("/api/computer/stop", { body: {} });
    toast("Stopped. Float let go of the mouse.");
  } catch (err) { toast(err.message, true); }
}

/* ══════════════════════════════════ wiring ══════════════════════════════ */

function theme(choice) {
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  const apply = () => {
    document.documentElement.dataset.theme =
      choice === "system" ? (dark.matches ? "dark" : "light") : choice;
  };
  dark.onchange = choice === "system" ? apply : null;
  apply();
}

function grow() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px";
}

function clearAttachments() {
  S.attachments = [];
  $("attach-bar").innerHTML = "";
  $("attach-bar").hidden = true;
}

async function attach(files) {
  for (const file of files) {
    if (file.size > 11 * 1024 * 1024) { toast(`${file.name} is too big (11 MB limit).`, true); continue; }
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1]);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
    const kind = file.type.startsWith("image/") ? "image" : "text";
    S.attachments.push({ name: file.name, mime: file.type || "text/plain", kind, data });

    const chip = make("div", "attach-chip");
    chip.append(make("span", null, file.name));
    const drop = make("button", null, "×");
    drop.addEventListener("click", () => {
      S.attachments = S.attachments.filter((a) => a.name !== file.name);
      chip.remove();
      if (!S.attachments.length) $("attach-bar").hidden = true;
    });
    chip.append(drop);
    $("attach-bar").hidden = false;
    $("attach-bar").append(chip);
  }
}

$("input").addEventListener("input", grow);
$("input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); }
});

$("send").addEventListener("click", () => {
  if (S.streaming) { S.abort && S.abort.abort(); return; }
  send();
});

$("new-thread").addEventListener("click", newThread);
$("open-toolkit").addEventListener("click", () => showPanel("toolkit"));
$("drawer-close").addEventListener("click", closeDrawer);
$("drawer-scrim").addEventListener("click", closeDrawer);
$("modal-close").addEventListener("click", closeModal);
$("modal-scrim").addEventListener("click", (event) => {
  if (event.target === $("modal-scrim")) closeModal();
});

document.querySelectorAll(".rail-link").forEach((link) =>
  link.addEventListener("click", () => showPanel(link.dataset.panel)));

$("open-settings").addEventListener("click", () => openSettings("you"));
$("chip-model").addEventListener("click", () => openSettings("models"));
$("chip-assist").addEventListener("click", () => openSettings("assist"));
$("open-admin").addEventListener("click", () => window.open("/admin", "_blank", "noopener"));

$("toggle-theme").addEventListener("click", async () => {
  const now = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  S.user.appearance = now;
  theme(now);
  await api("/api/me", { body: { appearance: now } });
});

$("attach").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (event) => {
  attach(event.target.files);
  event.target.value = "";
});

$("rail-toggle").addEventListener("click", () => $("rail").classList.toggle("open"));
$("driving-stop").addEventListener("click", stopComputer);

$("sign-out").addEventListener("click", async () => {
  try { await api("/api/auth/signout", { body: {} }); } catch { /* going anyway */ }
  sessionStorage.removeItem("float.token");
  location.replace("/login");
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (S.driving) { stopComputer(); return; }
    if (!$("modal-scrim").hidden) { closeModal(); return; }
    if (!$("drawer").hidden) closeDrawer();
  }
  if ((event.metaKey || event.ctrlKey) && event.key === "k") {
    event.preventDefault();
    showPanel("toolkit");
  }
  if ((event.metaKey || event.ctrlKey) && event.key === "/") {
    event.preventDefault();
    newThread();
  }
});

// Drag a photo of the blackboard straight onto the window.
document.addEventListener("dragover", (event) => event.preventDefault());
document.addEventListener("drop", (event) => {
  event.preventDefault();
  if (event.dataTransfer.files.length) attach(event.dataTransfer.files);
});

boot();
