/* Float — admin panel.
 *
 * One admin, one small school, one page. Nothing here is trying to be a
 * multi-tenant console — it's the handful of controls a head of department
 * actually reaches for: who's using it, what it's costing, and a switch to
 * turn a teacher's account off when they leave.
 */

const TOKEN = sessionStorage.getItem("float.token");
if (!TOKEN) location.replace("/login");

const $ = (id) => document.getElementById(id);
const make = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

let DATA = null;
let TAB = "overview";

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
  if (res.status === 403) {
    document.body.innerHTML = '<div class="empty" style="padding-top:20vh"><b>Admins only</b>' +
      "This account can't open the admin panel.</div>";
    throw new Error("forbidden");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong.");
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

function rupees(paise) { return "₹" + (paise / 100).toFixed(2); }

const TABS = [
  ["overview", "Overview"],
  ["teachers", "Teachers"],
  ["org", "School settings"],
  ["notices", "Notices"],
  ["audit", "Audit log"],
];

function paintTabs() {
  const bar = $("tabs");
  bar.innerHTML = "";
  TABS.forEach(([id, label]) => {
    const button = make("button", null, label);
    button.setAttribute("aria-selected", String(id === TAB));
    button.addEventListener("click", () => { TAB = id; render(); });
    bar.append(button);
  });
}

async function boot() {
  try {
    DATA = await api("/api/admin/overview");
  } catch {
    return;
  }
  paintTabs();
  render();
}

function render() {
  paintTabs();
  const wrap = $("wrap");
  wrap.innerHTML = "";
  ({ overview: viewOverview, teachers: viewTeachers, org: viewOrg,
     notices: viewNotices, audit: viewAudit }[TAB])(wrap);
}

/* ── overview ──────────────────────────────────────────────────────────── */

function viewOverview(wrap) {
  const t = DATA.totals;
  const grid = make("div", "stat-grid");
  [
    ["Teachers", t.teachers, `${t.active} active`],
    ["Signed in now", t.signed_in, ""],
    ["Answers · 30 days", t.calls, ""],
    ["Tokens · 30 days", t.tokens.toLocaleString("en-IN"), ""],
    ["Estimated spend", "₹" + t.rupees.toFixed(2), "published rates"],
  ].forEach(([label, value, sub]) => {
    const card = make("div", "stat");
    card.append(make("b", null, String(value)));
    card.append(make("small", null, sub ? `${label} · ${sub}` : label));
    grid.append(card);
  });
  wrap.append(grid);

  const chart = make("div", "panel");
  const chartHead = make("div", "panel-head");
  chartHead.append(make("b", null, "Last 14 days"));
  chart.append(chartHead);
  const chartBody = make("div", "panel-body");
  const spark = make("div", "spark");
  const days = DATA.by_day || [];
  const max = Math.max(1, ...days.map((d) => d.calls));
  const byDay = {};
  days.forEach((d) => (byDay[d.day] = d));
  for (let i = 13; i >= 0; i--) {
    const day = new Date(Date.now() - i * 86400000).toISOString().slice(0, 10);
    const entry = byDay[day];
    const bar = make("i");
    const calls = entry ? entry.calls : 0;
    bar.style.height = Math.max(2, (calls / max) * 100) + "%";
    if (i === 0) bar.classList.add("hot");
    bar.title = `${day}: ${calls} answer${calls === 1 ? "" : "s"}`;
    spark.append(bar);
  }
  chartBody.append(spark);
  chart.append(chartBody);
  wrap.append(chart);

  const providers = make("div", "panel");
  const provHead = make("div", "panel-head");
  provHead.append(make("b", null, "Providers"));
  providers.append(provHead);
  const provBody = make("div", "panel-body");
  DATA.providers.forEach((provider) => {
    const row = make("div", "row");
    const grow = make("div", "grow");
    grow.append(make("b", null, provider.label));
    row.append(grow);
    row.append(make("span", provider.allowed ? "pill" : "pill off",
      provider.allowed ? "allowed" : "off"));
    row.append(make("span", provider.key ? "pill admin" : "pill off",
      provider.key ? "school key set" : "no school key"));
    provBody.append(row);
  });
  providers.append(provBody);
  wrap.append(providers);

  if (DATA.notices && DATA.notices.length) {
    const active = make("div", "panel");
    const head = make("div", "panel-head");
    head.append(make("b", null, "Active notices"));
    active.append(head);
    const body = make("div", "panel-body");
    DATA.notices.forEach((n) => {
      const row = make("div", "row");
      const grow = make("div", "grow");
      grow.append(make("b", null, n.title));
      if (n.body) grow.append(make("small", null, n.body));
      row.append(grow);
      body.append(row);
    });
    active.append(body);
    wrap.append(active);
  }
}

/* ── teachers ──────────────────────────────────────────────────────────── */

function viewTeachers(wrap) {
  const head = make("div", "panel");
  const headBody = make("div", "panel-body");
  const row = make("div", "row");
  const grow = make("div", "grow");
  grow.append(make("b", null, "Add a teacher"));
  grow.append(make("small", null, "Float makes a temporary password — hand it over however you normally would."));
  row.append(grow);

  const email = document.createElement("input");
  email.placeholder = "teacher@school.edu.in";
  email.style.cssText = "padding:8px 11px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--paper);width:220px";
  const name = document.createElement("input");
  name.placeholder = "Name";
  name.style.cssText = email.style.cssText.replace("220px", "160px");
  const add = make("button", "btn primary", "Add");
  add.addEventListener("click", async () => {
    if (!email.value.includes("@")) { toast("That email doesn't look right.", true); return; }
    add.disabled = true;
    try {
      const result = await api("/api/admin/users", { body: { email: email.value, name: name.value } });
      DATA.users = result.users;
      alert(`Account created for ${email.value}.\n\nTemporary password: ${result.temp_password}\n\nThey'll be asked to set their own on first sign-in.`);
      email.value = name.value = "";
      render();
    } catch (err) {
      toast(err.message, true);
    } finally {
      add.disabled = false;
    }
  });
  head.append(headBody);
  headBody.append(row, name, email, add);
  row.style.marginBottom = "0";
  wrap.append(head);

  const table = document.createElement("table");
  table.className = "grid";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>Teacher</th><th>Role</th><th class=num>Sign-ins</th>" +
    "<th class=num>Answers</th><th class=num>Tokens</th><th class=num>Est. ₹</th><th></th></tr>";
  table.append(thead);
  const tbody = document.createElement("tbody");

  DATA.users.forEach((user) => {
    const tr = document.createElement("tr");
    if (!user.active) tr.style.opacity = ".5";

    const nameTd = document.createElement("td");
    nameTd.innerHTML = `<b>${escapeHtml(user.name || "—")}</b><br><small style="color:var(--faint)">${escapeHtml(user.email)}</small>`;
    tr.append(nameTd);

    const roleTd = document.createElement("td");
    const pill = make("span", user.role === "admin" ? "pill admin" : "pill", user.role);
    roleTd.append(pill);
    if (!user.active) roleTd.append(make("span", "pill off", "off"));
    tr.append(roleTd);

    [user.sign_ins, user.calls, user.tin + user.tout].forEach((value) => {
      const td = document.createElement("td");
      td.className = "num";
      td.textContent = (value || 0).toLocaleString("en-IN");
      tr.append(td);
    });

    const rupeeTd = document.createElement("td");
    rupeeTd.className = "num";
    rupeeTd.textContent = rupees(user.paise || 0);
    tr.append(rupeeTd);

    const actTd = document.createElement("td");
    actTd.append(actionMenu(user));
    tr.append(actTd);

    tbody.append(tr);
  });

  table.append(tbody);
  const panel = make("div", "panel");
  const panelBody = make("div", "panel-body");
  panelBody.style.overflowX = "auto";
  panelBody.append(table);
  panel.append(panelBody);
  wrap.append(panel);
}

function actionMenu(user) {
  const wrap = make("div");
  wrap.style.cssText = "display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end";

  const act = async (action, confirmText) => {
    if (confirmText && !confirm(confirmText)) return;
    try {
      const result = await api(`/api/admin/users/${user.id}`, { body: { action } });
      DATA.users = result.users;
      toast(result.temp_password
        ? `New password for ${user.email}: ${result.temp_password}`
        : "Done.");
      render();
    } catch (err) { toast(err.message, true); }
  };

  if (user.active) {
    wrap.append(button("Reset password", () =>
      act("reset_password", `Reset ${user.email}'s password? They'll need the new one.`)));
    wrap.append(button("Sign out", () => act("sign_out")));
    if (user.role === "teacher") wrap.append(button("Make admin", () => act("make_admin")));
    else wrap.append(button("Make teacher", () => act("make_teacher")));
    wrap.append(button("Deactivate", () => act("deactivate"), true));
  } else {
    wrap.append(button("Reactivate", () => act("activate")));
  }
  wrap.append(button("Delete", () =>
    act("delete", `Delete ${user.email} and everything they've made? This can't be undone.`), true));

  return wrap;
}

function button(label, onClick, danger) {
  const b = make("button", "btn tiny" + (danger ? " danger" : ""), label);
  b.addEventListener("click", onClick);
  return b;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ── school settings ───────────────────────────────────────────────────── */

function viewOrg(wrap) {
  const org = DATA.org;

  const panel = make("div", "panel");
  const body = make("div", "panel-body");

  const nameField = make("div", "field");
  nameField.append(make("label", null, "School name"));
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.value = org.school_name || "";
  nameField.append(nameInput);
  body.append(nameField);

  const toggles = [
    ["computer_control", "Let Float use the computer", "Mouse and keyboard control for repetitive on-screen tasks."],
    ["teacher_own_keys", "Teachers may add their own API key", "Turn off if only the school key should be used."],
    ["self_signup", "Anyone with the right email can sign up", "Off means you add every teacher yourself, from this page."],
    ["require_pin", "Require a quick PIN", "Ask every teacher to set one, for faster sign-in between periods."],
  ];
  toggles.forEach(([key, label, hint]) => {
    const row = make("div", "row");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = org[key] === "1";
    check.dataset.key = key;
    row.append(check);
    const grow = make("div", "grow");
    grow.append(make("b", null, label));
    grow.append(make("small", null, hint));
    row.append(grow);
    body.append(row);
  });

  const providersField = make("div", "field");
  providersField.append(make("label", null, "Providers teachers may use"));
  const providersBox = make("div", "tagbox");
  const allowed = new Set(DATA.providers.filter((p) => p.allowed).map((p) => p.id));
  DATA.providers.forEach((p) => {
    const tag = make("button", "tag", p.label);
    tag.type = "button";
    tag.dataset.provider = p.id;
    tag.setAttribute("aria-pressed", String(allowed.has(p.id)));
    tag.addEventListener("click", () => {
      if (allowed.has(p.id)) allowed.delete(p.id); else allowed.add(p.id);
      tag.setAttribute("aria-pressed", String(allowed.has(p.id)));
    });
    providersBox.append(tag);
  });
  providersField.append(providersBox);
  body.append(providersField);

  const save = make("button", "btn primary", "Save settings");
  save.style.marginTop = "8px";
  save.addEventListener("click", async () => {
    save.disabled = true;
    const payload = { school_name: nameInput.value, allowed_providers: [...allowed] };
    body.querySelectorAll('input[type=checkbox]').forEach((box) => {
      payload[box.dataset.key] = box.checked ? "1" : "0";
    });
    try {
      const result = await api("/api/admin/org", { body: payload });
      DATA.org = result.org;
      toast("Saved.");
    } catch (err) {
      toast(err.message, true);
    } finally {
      save.disabled = false;
    }
  });
  body.append(save);

  panel.append(body);
  wrap.append(panel);

  const exportPanel = make("div", "panel");
  const exportBody = make("div", "panel-body");
  const exportRow = make("div", "row");
  const exportGrow = make("div", "grow");
  exportGrow.append(make("b", null, "Usage report"));
  exportGrow.append(make("small", null, "Every teacher's last 30 days, as an Excel sheet."));
  exportRow.append(exportGrow);
  const exportBtn = make("button", "btn", "Download .xlsx");
  exportBtn.addEventListener("click", async () => {
    const res = await fetch("/api/admin/export", { headers: { Authorization: "Bearer " + TOKEN } });
    if (!res.ok) { toast("Could not build the report.", true); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "float-usage.xlsx";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  });
  exportRow.append(exportBtn);
  exportBody.append(exportRow);
  exportPanel.append(exportBody);
  wrap.append(exportPanel);
}

/* ── notices ───────────────────────────────────────────────────────────── */

function viewNotices(wrap) {
  wrap.append(make("p", "inline-note",
    "A notice appears as a strip at the top of every teacher's Float, until you retire it."));

  const panel = make("div", "panel");
  const body = make("div", "panel-body");

  if (!DATA.notices.length) {
    const empty = make("div", "empty");
    empty.append(make("b", null, "Nothing posted"));
    body.append(empty);
  }
  DATA.notices.forEach((notice) => {
    const row = make("div", "row");
    const grow = make("div", "grow");
    grow.append(make("b", null, notice.title));
    if (notice.body) grow.append(make("small", null, notice.body));
    row.append(grow);
    const retire = make("button", "btn tiny", "Retire");
    retire.addEventListener("click", async () => {
      const result = await api("/api/admin/notice", { body: { retire: notice.id } });
      DATA.notices = result.notices;
      render();
    });
    row.append(retire);
    body.append(row);
  });
  panel.append(body);
  wrap.append(panel);

  const addPanel = make("div", "panel");
  const addBody = make("div", "panel-body");
  const title = document.createElement("input");
  title.placeholder = "Title — e.g. Report cards due Friday";
  title.style.cssText = "width:100%;padding:8px 11px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--paper);margin-bottom:9px";
  const text = document.createElement("textarea");
  text.placeholder = "A line or two of detail (optional)";
  text.style.cssText = "width:100%;padding:8px 11px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--paper);min-height:60px;margin-bottom:9px";
  addBody.append(title, text);
  const post = make("button", "btn primary", "Post notice");
  post.addEventListener("click", async () => {
    if (!title.value.trim()) { toast("Give it a title.", true); return; }
    try {
      const result = await api("/api/admin/notice", { body: { title: title.value, body: text.value } });
      DATA.notices = result.notices;
      title.value = text.value = "";
      toast("Posted.");
      render();
    } catch (err) { toast(err.message, true); }
  });
  addBody.append(post);
  addPanel.append(addBody);
  wrap.append(addPanel);
}

/* ── audit log ─────────────────────────────────────────────────────────── */

async function viewAudit(wrap) {
  wrap.append(make("p", "inline-note",
    "Sign-ins, key changes, deactivations and anything else that matters later. Nothing here is editable."));

  const panel = make("div", "panel");
  const body = make("div", "panel-body");
  const log = make("div", "log", "Loading…");
  body.append(log);
  panel.append(body);
  wrap.append(panel);

  try {
    const data = await api("/api/admin/audit?limit=300");
    log.innerHTML = "";
    if (!data.entries.length) { log.textContent = "Nothing logged yet."; return; }
    data.entries.forEach((entry) => {
      const row = make("div", entry.level !== "info" ? entry.level : "");
      const time = new Date(entry.created_at * 1000);
      row.append(make("time", null, time.toLocaleString("en-IN", {
        day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
      })));
      row.append(make("span", "who", entry.actor || "system"));
      row.append(make("span", "what", `${entry.action} — ${entry.detail || ""}`));
      log.append(row);
    });
  } catch {
    log.textContent = "Could not load the log.";
  }
}

/* ── chrome ────────────────────────────────────────────────────────────── */

$("back-app").addEventListener("click", () => (location.href = "/"));
$("toggle-theme").addEventListener("click", () => {
  document.documentElement.dataset.theme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
});

const dark = window.matchMedia("(prefers-color-scheme: dark)");
document.documentElement.dataset.theme = dark.matches ? "dark" : "light";

boot();
