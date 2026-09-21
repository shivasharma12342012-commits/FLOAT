/* Float — the gate.
 *
 * Three ways in, in the order a real teacher meets them:
 *   quick   a PIN, because this is the same laptop as yesterday
 *   signin  email and password, or Google
 *   signup  the first account on the machine, or a school that allows self sign-up
 *
 * The session token lives in sessionStorage and nowhere else. Close the window,
 * close the app, and the token is gone — which is the whole of the
 * "log out when the teacher closes the app" requirement, done honestly.
 * The server backs it up with a per-process boot nonce, so even a copied token
 * is dead once Float restarts.
 */

const KNOWN = "float.known";      // { name, email } — a convenience, never a credential
const TOKEN = "float.token";

const el = (id) => document.getElementById(id);
const modes = { quick: el("mode-quick"), signin: el("mode-signin"), signup: el("mode-signup") };

let state = { google: false, self_signup: false, setup_needed: false, school: "" };
let busy = false;

/* ---------------------------------------------------------------- plumbing */

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Float could not reach its own server.");
  return data;
}

function say(text, kind = "bad") {
  const box = el("gate-msg");
  box.className = "gate-msg " + kind;
  box.textContent = text;
  box.hidden = false;
}

function clearSay() {
  const box = el("gate-msg");
  box.hidden = true;
  box.textContent = "";
  box.className = "";
}

function show(name) {
  clearSay();
  for (const [key, node] of Object.entries(modes)) node.hidden = key !== name;
  const focus = modes[name].querySelector("input:not([hidden])");
  if (focus) setTimeout(() => focus.focus(), 60);
}

function lock(on) {
  busy = on;
  document.querySelectorAll(".gate-submit, .gate-google").forEach((b) => (b.disabled = on));
}

function known() {
  try { return JSON.parse(localStorage.getItem(KNOWN) || "null"); } catch { return null; }
}

function remember(user) {
  try {
    localStorage.setItem(KNOWN, JSON.stringify({ name: user.name, email: user.email }));
  } catch { /* private window; not important */ }
}

function enter(data) {
  sessionStorage.setItem(TOKEN, data.token);
  if (data.user) remember(data.user);
  if (data.must_change) sessionStorage.setItem("float.must_change", "1");
  location.replace("/");
}

/* -------------------------------------------------------------------- boot */

async function boot() {
  if (sessionStorage.getItem(TOKEN)) { location.replace("/"); return; }

  try {
    state = await api("/api/auth/state", {});
  } catch {
    say("Float's server isn't answering. Close the window and start Float again.");
    return;
  }

  if (state.school) {
    el("pull-cite").textContent = state.school;
    document.title = `Sign in · Float · ${state.school}`;
  }

  // Google buttons only appear when the installation actually has credentials.
  for (const id of ["google-signin", "google-signup"]) el(id).hidden = !state.google;
  el("signin-or").hidden = !state.google;
  el("signup-or").hidden = !state.google;

  if (state.setup_needed) {
    el("signup-switch").hidden = true;
    show("signup");
    return;
  }

  el("signup-title").textContent = "Join your staff room.";
  el("signup-sub").textContent = "Float keeps your classes, files and drafts to yourself.";
  el("su-school-field").hidden = true;
  el("signin-switch").hidden = !state.self_signup;

  const who = known();
  if (who && who.email) {
    el("quick-name").textContent = who.name || who.email;
    el("quick-email").textContent = who.email;
    el("quick-avatar").textContent = (who.name || who.email).trim().charAt(0).toUpperCase();
    show("quick");
  } else {
    show("signin");
  }
}

/* ------------------------------------------------------------ quick / PIN */

el("form-quick").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  const who = known();
  const pin = el("quick-pin").value.trim();
  if (!who || !pin) return;

  lock(true);
  try {
    enter(await api("/api/auth/signin", { email: who.email, pin }));
  } catch (err) {
    el("quick-pin").value = "";
    el("quick-pin").focus();
    say(/match/.test(err.message)
      ? "That PIN didn't work. Try again, or use your password."
      : err.message);
  } finally {
    lock(false);
  }
});

el("forget-device").addEventListener("click", () => {
  localStorage.removeItem(KNOWN);
  el("si-email").value = "";
  show("signin");
});

/* --------------------------------------------------------- password signin */

el("form-signin").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  lock(true);
  try {
    enter(await api("/api/auth/signin", {
      email: el("si-email").value.trim(),
      password: el("si-password").value,
    }));
  } catch (err) {
    el("si-password").value = "";
    say(err.message);
  } finally {
    lock(false);
  }
});

/* -------------------------------------------------------------- signing up */

el("form-signup").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  lock(true);
  try {
    enter(await api("/api/auth/signup", {
      name: el("su-name").value.trim(),
      school: el("su-school").value.trim(),
      email: el("su-email").value.trim(),
      password: el("su-password").value,
    }));
  } catch (err) {
    say(err.message);
  } finally {
    lock(false);
  }
});

/* ------------------------------------------------------------------ Google */

async function google() {
  if (busy) return;
  lock(true);
  try {
    const { url } = await api("/api/auth/google/start");
    const popup = window.open(url, "float-google", "width=520,height=640,menubar=no,toolbar=no");
    if (!popup) { say("Your browser blocked the Google window. Allow pop-ups for Float."); return; }
    say("Finish signing in on the Google window…", "good");
  } catch (err) {
    say(err.message);
  } finally {
    lock(false);
  }
}

el("google-signin").addEventListener("click", google);
el("google-signup").addEventListener("click", google);

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin) return;
  const payload = event.data || {};
  if (typeof payload.ok !== "boolean") return;
  if (payload.ok && payload.token) enter({ token: payload.token });
  else say(payload.message || "Google sign-in did not complete.");
});

/* ------------------------------------------------------------------ chrome */

document.querySelectorAll("[data-go]").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.go));
});

// Follow the machine's own light/dark preference. No switch on the gate —
// there is nothing here worth a decision.
const dark = window.matchMedia("(prefers-color-scheme: dark)");
const paint = () => document.documentElement.dataset.theme = dark.matches ? "dark" : "light";
dark.addEventListener("change", paint);
paint();

boot();
