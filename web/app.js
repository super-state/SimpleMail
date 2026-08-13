/* SimpleMail frontend - talks to Python via pywebview.api */
"use strict";

let api = null;  // set once pywebview has injected its bridge
const state = {
  accounts: [],         // [{id, label, color, identity, signature, ...}]
  activeAccountId: null,
  folders: [],          // [{key, name, server}] for the ACTIVE account
  currentFolder: "inbox",
  messages: [],         // envelope list for current folder (active account)
  selectedUid: null,
};

function activeAccount() {
  return state.accounts.find((a) => a.id === state.activeAccountId) || null;
}

const $ = (id) => document.getElementById(id);

/* ---------------- helpers ---------------- */

function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  t.style.display = "block";
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.style.display = "none"), 3200);
}

function fmtDate(s) {
  try {
    const d = new Date(s);
    if (isNaN(d)) return s;
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay
      ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString([], { day: "numeric", month: "short" });
  } catch { return s; }
}

function fmtSize(n) {
  if (!n) return "?";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function shortFrom(s) {
  // "Name <a@b.c>" -> "Name" (or the address)
  const m = s.match(/^([^<]+?)\s*<.*>$/);
  return (m ? m[1] : s).trim() || s;
}

/* sender initials + deterministic avatar color */
const AVATAR_COLORS = [
  "#e53935", "#d81b60", "#8e24aa", "#5e35b1", "#3949ab", "#1e88e5",
  "#039be5", "#00acc1", "#00897b", "#43a047", "#7cb342", "#f4511e",
  "#6d4c41", "#546e7a",
];
function hashColor(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
function initials(s) {
  const clean = shortFrom(s);
  const words = clean.split(/[\s.]+/).filter(Boolean);
  if (!words.length) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

/* ---------------- accounts ---------------- */

function renderAccounts() {
  const box = $("accounts");
  box.innerHTML = "";
  if (state.accounts.length < 2 && state.accounts.length !== 0) {
    // single account: no switcher chrome needed, but still show whose mail
    const a = state.accounts[0];
    box.innerHTML = `<div class="account single" title="${escapeHtml(a.identity)}">
      <span class="acct-dot" style="background:${escapeHtml(a.color)}"></span>
      <span class="acct-label">${escapeHtml(a.label)}</span></div>`;
    return;
  }
  state.accounts.forEach((a) => {
    const btn = document.createElement("button");
    btn.className = "account" + (a.id === state.activeAccountId ? " active" : "");
    btn.title = a.identity;
    btn.innerHTML = `<span class="acct-dot" style="background:${escapeHtml(a.color)}"></span>
      <span class="acct-label">${escapeHtml(a.label)}</span>`;
    btn.addEventListener("click", () => selectAccount(a.id));
    box.appendChild(btn);
  });
}

async function selectAccount(accountId) {
  if (!state.accounts.some((a) => a.id === accountId)) return;
  state.activeAccountId = accountId;
  state.selectedUid = null;
  state.messages = [];
  renderAccounts();
  $("msg-list").innerHTML = '<div class="empty">Loading…</div>';
  $("read-header").style.display = "none";
  $("read-body").innerHTML = '<div id="loading">Select a message</div>';
  try { api.set_active_account(accountId); } catch {}
  try {
    const res = await api.get_folders(accountId);
    state.folders = res.folders;
    if (res.error) toast(res.error, true);
  } catch (e) {
    state.folders = [{ key: "inbox", name: "Inbox", server: "INBOX", unread: 0 }];
    toast(String(e), true);
  }
  state.currentFolder = "inbox";
  renderFolders();
  selectFolder("inbox");
}

/* ---------------- folders ---------------- */

function renderFolders() {
  const box = $("folders");
  box.innerHTML = "";
  const icons = {
    inbox: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>',
    sent: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/></svg>',
    drafts: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
    junk: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
    trash: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  };
  state.folders.forEach((f) => {
    const btn = document.createElement("button");
    btn.className = "folder" + (f.key === state.currentFolder ? " active" : "");
    btn.dataset.key = f.key;
    // only show unread badge on Inbox (user wants Junk/Trash quiet)
    const badge = f.key === "inbox" && f.unread > 0
      ? `<span class="badge">${f.unread > 99 ? "99+" : f.unread}</span>` : "";
    btn.innerHTML = (icons[f.key] || "") + `<span>${f.name}</span>` + badge;
    btn.addEventListener("click", () => selectFolder(f.key));
    box.appendChild(btn);
  });
}

function selectFolder(key) {
  state.currentFolder = key;
  state.selectedUid = null;
  $("search-box").value = "";
  document.querySelectorAll(".folder").forEach((b) =>
    b.classList.toggle("active", b.dataset.key === key));
  const folder = state.folders.find((f) => f.key === key);
  $("folder-title").textContent = folder ? folder.name : key;
  $("msg-list").innerHTML = '<div class="empty">Loading…</div>';
  $("read-header").style.display = "none";
  $("read-body").innerHTML = '<div id="loading">Select a message</div>';
  loadMessages();
}

async function loadMessages() {
  const folder = state.folders.find((f) => f.key === state.currentFolder);
  try {
    const res = await api.list_messages(state.activeAccountId, folder.server);
    state.messages = res.envelopes;
    renderMessages();
    $("folder-count").textContent = `${res.unread} unread · ${state.messages.length} shown`;
    // update inbox badge in sidebar
    const ib = state.folders.find((f) => f.key === "inbox");
    if (ib && state.currentFolder === "inbox") ib.unread = res.unread;
    renderFolders();
  } catch (e) {
    $("msg-list").innerHTML = '<div class="empty">Failed to load. Check settings.</div>';
    toast(String(e), true);
  }
}

function renderMessages() {
  const box = $("msg-list");
  box.innerHTML = "";
  const q = ($("search-box").value || "").toLowerCase().trim();
  let list = state.messages;
  if (q) {
    list = state.messages.filter((m) =>
      m.sender.toLowerCase().includes(q) || m.subject.toLowerCase().includes(q));
  }
  if (!list.length) {
    box.innerHTML = '<div class="empty">' + (q ? "No matches" : "No messages") + "</div>";
    return;
  }
  list.forEach((m) => {
    const div = document.createElement("div");
    div.className = "msg" + (m.seen ? " seen" : " unread") + (m.uid === state.selectedUid ? " selected" : "");
    div.dataset.uid = m.uid;
    div.innerHTML = `
      <span class="udot"></span>
      <div class="avatar" style="background:${hashColor(m.sender)}" title="${escapeHtml(shortFrom(m.sender))}">${escapeHtml(initials(m.sender))}</div>
      <div class="content">
        <div class="row1">
          <span class="from">${escapeHtml(shortFrom(m.sender))}</span>
          <span class="date">${fmtDate(m.date)}</span>
        </div>
        <div class="subject">${escapeHtml(m.subject)}</div>
        <div class="snippet">${escapeHtml(m.snippet || "")}</div>
      </div>`;
    div.addEventListener("click", () => openMessage(m.uid, div));
    div.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();  // don't trigger the document-level close
      openCtxMenu(e.clientX, e.clientY, m.uid);
    });
    box.appendChild(div);
  });
}

/* ---------------- reading ---------------- */

async function openMessage(uid, el) {
  state.selectedUid = uid;
  document.querySelectorAll(".msg").forEach((m) =>
    m.classList.toggle("selected", m.dataset.uid === uid));
  $("read-header").style.display = "none";
  $("read-body").innerHTML = '<div id="loading"><span class="spinner"></span> Loading…</div>';
  const folder = state.folders.find((f) => f.key === state.currentFolder);
  try {
    const msg = await api.get_message(state.activeAccountId, folder.server, uid);
    $("read-header").style.display = "block";
    $("read-subject").textContent = msg.subject;
    $("read-meta").innerHTML =
      `<div class="sender-line">${escapeHtml(msg.sender)}</div>` +
      `<div class="meta-line"><span class="lbl">To:</span> ${escapeHtml(msg.to)}</div>` +
      `<div class="meta-line"><span class="lbl">Date:</span> ${escapeHtml(msg.date)}</div>`;
    $("read-body").innerHTML = "";
    if (msg.attachments && msg.attachments.length) {
      const wrap = document.createElement("div");
      wrap.id = "attachments";
      wrap.innerHTML = msg.attachments.map((a, i) => `
        <div class="att">
          <span class="name" title="${escapeHtml(a.name)}">📎 ${escapeHtml(a.name)} (${fmtSize(a.size)})</span>
          <button class="outline" data-idx="${a.index}">Save</button>
        </div>`).join("");
      wrap.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          btn.textContent = "…";
          try {
            const res = await api.save_attachment(state.activeAccountId, folder.server, uid, btn.dataset.idx);
            toast("Saved to " + res.path);
          } catch (e) {
            toast("Save failed: " + e, true);
          } finally {
            btn.disabled = false;
            btn.textContent = "Save";
          }
        });
      });
      $("read-body").appendChild(wrap);
    }
    if (msg.html) {
      const iframe = document.createElement("iframe");
      iframe.sandbox = ""; // no scripts, no same-origin
      iframe.srcdoc = sanitizeHtml(msg.html);
      $("read-body").appendChild(iframe);
    } else {
      const pre = document.createElement("pre");
      pre.textContent = msg.text || "(no content)";
      $("read-body").appendChild(pre);
    }
    // mark row as read locally; backend already marked it seen
    if (el) { el.classList.remove("unread"); el.classList.add("seen"); el.classList.add("selected"); }
    const idx = state.messages.findIndex((m) => m.uid === uid);
    if (idx >= 0) state.messages[idx].seen = true;
    updateUnreadCount();
  } catch (e) {
    $("read-body").innerHTML = '<div class="empty">Failed to open message.</div>';
    toast(String(e), true);
  }
}

function updateUnreadCount() {
  const folder = state.folders.find((f) => f.key === state.currentFolder);
  const unread = state.messages.filter((m) => !m.seen).length;
  $("folder-count").textContent = `${unread} unread · ${state.messages.length} shown`;
}

/* ---------------- compose ----------------
   The compose window is BOUND to one account at open time. The From line is
   informational only - the backend derives the real identity from the account
   id, so there is no way to send from the wrong mailbox. */

let composeAccountId = null;

function openCompose(to = "", subject = "", quoteHtml = "") {
  const acct = activeAccount();
  if (!acct) { toast("Add an account in Settings first", true); return; }
  composeAccountId = acct.id;
  $("compose-from").innerHTML =
    `<span class="acct-dot" style="background:${escapeHtml(acct.color)}"></span>` +
    `${escapeHtml(acct.label)} &lt;${escapeHtml(acct.identity)}&gt;`;
  $("compose-to").value = to;
  $("compose-subject").value = subject;
  const body = $("compose-body");
  body.innerHTML = "";
  if (quoteHtml) {
    const bq = document.createElement("blockquote");
    bq.style.cssText = "border-left:3px solid #cbd5e1;margin:0 0 10px;padding:2px 12px;color:#475569;";
    bq.innerHTML = quoteHtml;
    body.appendChild(bq);
  }
  if (acct.signature) {
    const sig = document.createElement("div");
    sig.innerHTML = acct.signature.replace(/\n/g, "<br>");
    body.appendChild(sig);
  }
  $("compose-backdrop").classList.add("show");
  $("compose-to").focus();
}

function composeText() {
  return ($("compose-body").innerText || "").trim();
}

function composeHtml() {
  return $("compose-body").innerHTML;
}

async function sendCompose() {
  const to = $("compose-to").value.trim();
  const subject = $("compose-subject").value.trim() || "(no subject)";
  const text = composeText();
  const html = composeHtml();
  if (!to) { toast("Enter a recipient", true); return; }
  if (!composeAccountId) { toast("No account bound to this message", true); return; }
  $("send-btn").disabled = true;
  $("send-btn").innerHTML = '<span class="spinner"></span> Sending…';
  try {
    const res = await api.send_mail(composeAccountId, to, subject, text, html);
    $("compose-backdrop").classList.remove("show");
    toast("Sent as " + (res.sent_as || ""));
    if (state.currentFolder === "sent" && composeAccountId === state.activeAccountId) loadMessages();
  } catch (e) {
    toast("Send failed: " + e, true);
  } finally {
    $("send-btn").disabled = false;
    $("send-btn").textContent = "Send";
  }
}

async function saveDraft() {
  const to = $("compose-to").value.trim();
  const subject = $("compose-subject").value.trim() || "(no subject)";
  const text = composeText();
  const html = composeHtml();
  if (!composeAccountId) { toast("No account bound to this message", true); return; }
  try {
    await api.save_draft(composeAccountId, to, subject, text, html);
    $("compose-backdrop").classList.remove("show");
    toast("Draft saved");
    if (state.currentFolder === "drafts" && composeAccountId === state.activeAccountId) loadMessages();
  } catch (e) {
    toast("Could not save draft: " + e, true);
  }
}

/* ---------------- settings ----------------
   Accounts are edited on a working copy; nothing persists until Save. */

let editAccounts = [];   // working copy of account dicts
let editIndex = 0;       // which account the form is showing

const BLANK_ACCOUNT = {
  id: "", label: "", color: "#2563eb", email: "", password: "",
  from_email: "", imap_host: "mail.livemail.co.uk", imap_port: 993,
  smtp_host: "smtp.fasthosts.co.uk", smtp_port: 587, smtp_starttls: true,
  smtp_user: "", smtp_password: "", signature: "", rules: {},
};

function renderRules(accountId, rules) {
  const box = $("rules-list");
  box.innerHTML = "";
  const entries = Object.entries(rules || {});
  if (!entries.length) {
    box.innerHTML = '<span style="color:var(--pico-muted-color)">No rules yet — right-click a message and pick "Move to…" and the app remembers the sender (per mailbox).</span>';
    return;
  }
  entries.forEach(([domain, folder]) => {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--pico-muted-border-color);";
    row.innerHTML = `<span style="flex:1">📧 <b>${escapeHtml(domain)}</b> → ${escapeHtml(folder)}</span>
      <button class="outline secondary" style="padding:2px 10px;font-size:0.75rem;margin:0;width:auto">Remove</button>`;
    row.querySelector("button").addEventListener("click", async () => {
      try {
        await api.remove_rule(accountId, domain);
        delete rules[domain];
        toast("Rule removed");
        renderRules(accountId, rules);
      } catch (e) {
        toast("Failed: " + e, true);
      }
    });
    box.appendChild(row);
  });
}

function renderAccountPicker() {
  const sel = $("set-account-picker");
  sel.innerHTML = "";
  editAccounts.forEach((a, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = (a.label || a.email || "New account") + (a.from_email ? ` — sends as ${a.from_email}` : "");
    sel.appendChild(opt);
  });
  sel.value = String(editIndex);
}

function stashAccountForm() {
  const a = editAccounts[editIndex];
  if (!a) return;
  a.label = $("set-label").value.trim();
  a.color = $("set-color").value;
  a.email = $("set-email").value.trim();
  a.password = $("set-password").value;
  a.from_email = $("set-from-email").value.trim();
  a.imap_host = $("set-imap-host").value.trim();
  a.imap_port = parseInt($("set-imap-port").value, 10) || 993;
  a.smtp_host = $("set-smtp-host").value.trim();
  a.smtp_port = parseInt($("set-smtp-port").value, 10) || 587;
  a.smtp_user = $("set-smtp-user").value.trim();
  a.smtp_password = $("set-smtp-password").value;
  a.signature = $("set-signature").value;
}

function showAccountForm(i) {
  editIndex = i;
  const a = editAccounts[i] || { ...BLANK_ACCOUNT };
  $("set-label").value = a.label || "";
  $("set-color").value = a.color || "#2563eb";
  $("set-email").value = a.email || "";
  $("set-password").value = a.password || "";
  $("set-from-email").value = a.from_email || "";
  $("set-imap-host").value = a.imap_host || "";
  $("set-imap-port").value = a.imap_port || 993;
  $("set-smtp-host").value = a.smtp_host || "";
  $("set-smtp-port").value = a.smtp_port || 587;
  $("set-smtp-user").value = a.smtp_user || "";
  $("set-smtp-password").value = a.smtp_password || "";
  $("set-signature").value = a.signature || "";
  renderSigPreview();
  renderRules(a.id, a.rules || {});
  renderAccountPicker();
}

function switchEditedAccount() {
  stashAccountForm();
  showAccountForm(parseInt($("set-account-picker").value, 10) || 0);
}

function addAccount() {
  stashAccountForm();
  editAccounts.push({ ...BLANK_ACCOUNT });
  showAccountForm(editAccounts.length - 1);
  $("set-label").focus();
}

function removeAccount() {
  if (editAccounts.length <= 1) { toast("Keep at least one account", true); return; }
  const a = editAccounts[editIndex];
  if (!confirm(`Remove the "${a.label || a.email || "new"}" account from this app?\n(The mailbox itself is untouched.)`)) return;
  editAccounts.splice(editIndex, 1);
  showAccountForm(Math.max(0, editIndex - 1));
}

async function openSettings() {
  const cfg = await api.get_config();
  editAccounts = (cfg.accounts || []).map((a) => ({ ...a }));
  if (!editAccounts.length) editAccounts = [{ ...BLANK_ACCOUNT }];
  const activeIdx = editAccounts.findIndex((a) => a.id === cfg.active_account);
  $("set-scale").value = cfg.ui_scale || "default";
  $("set-version").textContent = cfg.version || "?";
  showAccountForm(activeIdx >= 0 ? activeIdx : 0);
  $("settings-backdrop").classList.add("show");
}

function renderSigPreview() {
  $("sig-preview").innerHTML = $("set-signature").value
    .replace(/\n/g, "<br>");
}

async function addSigImage() {
  try {
    const img = await api.pick_image();
    if (!img) return;  // cancelled
    const cur = $("set-signature").value;
    const tag = `<img src="${img.data_uri}" alt="${img.name}" style="max-width:220px">`;
    $("set-signature").value = (cur ? cur + "\n" : "") + tag;
    renderSigPreview();
    toast("Image added - click Save to keep");
  } catch (e) {
    toast("Could not add image: " + e, true);
  }
}

async function saveSettings() {
  stashAccountForm();
  const bad = editAccounts.find((a) => !a.email);
  if (bad) { toast("Every account needs a mailbox email address", true); return; }
  $("settings-save").disabled = true;
  try {
    await api.save_config({
      accounts: editAccounts,
      active_account: state.activeAccountId || "",
      ui_scale: $("set-scale").value,
    });
    applyScale($("set-scale").value);
    $("settings-backdrop").classList.remove("show");
    toast("Settings saved");
    await reloadAccounts();
  } catch (e) {
    toast("Save failed: " + e, true);
  } finally {
    $("settings-save").disabled = false;
  }
}

function applyScale(scale) {
  const root = document.documentElement;
  const sizes = { compact: "13px", default: "15px", large: "17.5px" };
  root.style.fontSize = sizes[scale] || sizes.default;
  document.body.classList.toggle("compact", scale === "compact");
  document.body.classList.toggle("large", scale === "large");
}

async function testConnection() {
  stashAccountForm();
  $("test-btn").disabled = true;
  $("test-btn").textContent = "Testing…";
  try {
    const res = await api.test_connection(editAccounts[editIndex]);
    toast(res.ok ? "Connected!" : res.error || "Failed", !res.ok);
  } catch (e) {
    toast(String(e), true);
  } finally {
    $("test-btn").disabled = false;
    $("test-btn").textContent = "Test connection";
  }
}

/* ---------------- boot ---------------- */

async function reloadAccounts() {
  const data = await api.get_config();
  state.accounts = data.accounts || [];
  applyScale(data.ui_scale || "default");
  if (!state.accounts.length) {
    state.activeAccountId = null;
    renderAccounts();
    return;
  }
  const wanted = state.accounts.some((a) => a.id === state.activeAccountId)
    ? state.activeAccountId
    : (data.active_account && state.accounts.some((a) => a.id === data.active_account)
        ? data.active_account : state.accounts[0].id);
  await selectAccount(wanted);
}

async function init() {
  if (!api) {
    document.body.innerHTML = '<div style="padding:40px">App backend not connected.</div>';
    return;
  }
  $("compose-btn").addEventListener("click", () => openCompose());
  $("send-btn").addEventListener("click", sendCompose);
  $("draft-btn").addEventListener("click", saveDraft);
  $("cancel-btn").addEventListener("click", () => $("compose-backdrop").classList.remove("show"));
  $("settings-btn").addEventListener("click", openSettings);
  $("settings-save").addEventListener("click", saveSettings);
  $("settings-cancel").addEventListener("click", () => $("settings-backdrop").classList.remove("show"));
  $("test-btn").addEventListener("click", testConnection);
  $("set-account-picker").addEventListener("change", switchEditedAccount);
  $("acct-add-btn").addEventListener("click", addAccount);
  $("acct-remove-btn").addEventListener("click", removeAccount);
  $("delete-btn").addEventListener("click", deleteSelected);
  $("reply-btn").addEventListener("click", replyTo);
  $("forward-btn").addEventListener("click", forwardMessage);
  $("unread-btn").addEventListener("click", markUnread);
  $("markall-btn").addEventListener("click", markAllRead);
  $("refresh-btn").addEventListener("click", () => loadMessages());
  $("sig-image-btn").addEventListener("click", addSigImage);
  $("search-box").addEventListener("input", () => renderMessages());
  $("upd-now").addEventListener("click", doUpdate);
  $("upd-later").addEventListener("click", () => $("update-backdrop").classList.remove("show"));
  $("upd-check").addEventListener("click", () => checkForUpdates(false));
  setupDivider();
  document.addEventListener("click", closeCtxMenu);
  document.addEventListener("contextmenu", closeCtxMenu);
  window.addEventListener("blur", closeCtxMenu);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("compose-backdrop").classList.remove("show");
      $("settings-backdrop").classList.remove("show");
    }
  });

  try {
    const data = await api.get_config();
    if (!data.accounts || !data.accounts.length) {
      toast("Add your first mail account to get started");
      openSettings();
      return;
    }
    state.accounts = data.accounts;
    applyScale(data.ui_scale || "default");
    const startId = state.accounts.some((a) => a.id === data.active_account)
      ? data.active_account : state.accounts[0].id;
    await selectAccount(startId);
    checkForUpdates(true);  // silent check on startup
  } catch (e) {
    document.getElementById("msg-list").innerHTML =
      '<div class="empty">Failed to start: ' + escapeHtml(String(e)) + "</div>";
  }
}

/* ---------------- delete / reply / forward ---------------- */

async function markAllRead() {
  const folder = state.folders.find((f) => f.key === state.currentFolder);
  if (!folder) return;
  try {
    const res = await api.mark_all_read(state.activeAccountId, folder.server);
    toast(`Marked ${res.count} message${res.count === 1 ? "" : "s"} as read`);
    await loadMessages();
  } catch (e) {
    toast("Failed: " + e, true);
  }
}

async function markUnread() {
  if (!state.selectedUid) return;
  const folder = state.folders.find((f) => f.key === state.currentFolder);
  try {
    await api.set_seen(state.activeAccountId, folder.server, state.selectedUid, false);
    const idx = state.messages.findIndex((m) => m.uid === state.selectedUid);
    if (idx >= 0) state.messages[idx].seen = false;
    renderMessages();
    updateUnreadCount();
    toast("Marked as unread");
  } catch (e) {
    toast("Failed: " + e, true);
  }
}

async function deleteSelected() {
  if (!state.selectedUid) return;
  if (!confirm("Delete this message?")) return;
  const folder = state.folders.find((f) => f.key === state.currentFolder);
  try {
    await api.delete_message(state.activeAccountId, folder.server, state.selectedUid);
    state.messages = state.messages.filter((m) => m.uid !== state.selectedUid);
    state.selectedUid = null;
    $("read-header").style.display = "none";
    $("read-body").innerHTML = '<div id="loading">Select a message</div>';
    renderMessages();
    updateUnreadCount();
    toast("Deleted");
  } catch (e) {
    toast("Delete failed: " + e, true);
  }
}

function replyTo() {
  const m = state.messages.find((x) => x.uid === state.selectedUid);
  if (!m) return;
  let to = m.sender;
  const addr = m.sender.match(/<([^>]+)>/);
  if (addr) to = addr[1];
  let subject = m.subject;
  if (!/^re:/i.test(subject)) subject = "Re: " + subject;
  quoteOriginal(m).then((quote) => openCompose(to, subject, quote));
}

async function forwardMessage() {
  const m = state.messages.find((x) => x.uid === state.selectedUid);
  if (!m) return;
  let subject = m.subject;
  if (!/^fwd?:/i.test(subject)) subject = "Fwd: " + subject;
  const quote = await quoteOriginal(m);
  openCompose("", subject, quote);
}

async function quoteOriginal(m) {
  // fetch the full message to quote its body
  const folder = state.folders.find((f) => f.key === state.currentFolder);
  try {
    const msg = await api.get_message(state.activeAccountId, folder.server, m.uid);
    const src = msg.html || `<pre>${escapeHtml(msg.text || "")}</pre>`;
    const fromLine = `On ${msg.date}, ${escapeHtml(shortFrom(msg.sender))} wrote:`;
    return `<div><i>${fromLine}</i></div>${src}`;
  } catch {
    return `<div><i>Original message</i></div>`;
  }
}

/* ---------------- right-click context menu ---------------- */

let ctxUid = null;

function openCtxMenu(x, y, uid) {
  ctxUid = uid;
  const menu = $("ctx-menu");
  const msg = state.messages.find((m) => m.uid === uid);
  const current = state.folders.find((f) => f.key === state.currentFolder);
  const sender = msg ? shortFrom(msg.sender) : "";
  const dom = msg ? (msg.sender.match(/<([^>]+)>/) || [msg.sender, msg.sender])[1].split("@")[1] || "" : "";
  let html = `<div class="ctx-head">${escapeHtml(sender)}<br><span style="font-weight:400">${escapeHtml(dom || "")}</span></div>`;
  state.folders.forEach((f) => {
    if (f.server === current.server) return;  // skip current folder
    const colors = { inbox: "#2563eb", sent: "#059669", drafts: "#d97706", junk: "#dc2626", trash: "#6b7280" };
    const color = colors[f.key] || "#8b5cf6";
    html += `<div class="ctx-item" data-act="move" data-target="${escapeHtml(f.server)}">
      <span class="folder-ico" style="background:${color}">${escapeHtml(initials(f.name))}</span>
      Move to ${escapeHtml(f.name)}
    </div>`;
  });
  html += `<div class="ctx-sep"></div>
    <div class="ctx-item" data-act="newfolder">📁 New folder…</div>
    <div class="ctx-item" data-act="unread">🔵 Mark unread</div>
    <div class="ctx-item" data-act="delete" style="color:#b91c1c">🗑 Delete</div>`;
  menu.innerHTML = html;
  menu.classList.add("show");
  // keep menu inside the window
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  menu.style.left = Math.min(x, window.innerWidth - mw - 8) + "px";
  menu.style.top = Math.min(y, window.innerHeight - mh - 8) + "px";

  menu.querySelectorAll(".ctx-item").forEach((el) => {
    el.addEventListener("click", () => {
      const act = el.dataset.act;
      menu.classList.remove("show");
      if (act === "move") ctxMoveTo(el.dataset.target);
      else if (act === "newfolder") ctxNewFolder();
      else if (act === "unread") markUnread();
      else if (act === "delete") deleteSelected();
    });
  });
}

async function ctxMoveTo(target) {
  const msg = state.messages.find((m) => m.uid === ctxUid);
  const folder = state.folders.find((f) => f.key === state.currentFolder);
  if (!msg || !folder) return;
  try {
    const res = await api.move_message(state.activeAccountId, folder.server, ctxUid, target, msg.sender, true);
    state.messages = state.messages.filter((m) => m.uid !== ctxUid);
    if (state.selectedUid === ctxUid) {
      state.selectedUid = null;
      $("read-header").style.display = "none";
      $("read-body").innerHTML = '<div id="loading">Select a message</div>';
    }
    renderMessages();
    updateUnreadCount();
    if (res.learned) {
      toast(`Moved & learned: mail from ${res.learned} → ${target} from now on`);
    } else {
      toast("Moved");
    }
  } catch (e) {
    toast("Move failed: " + e, true);
  }
}

async function ctxNewFolder() {
  const name = prompt("New folder name:", "Marketing");
  if (!name || !name.trim()) return;
  try {
    await api.create_folder(state.activeAccountId, name.trim());
    await selectAccount(state.activeAccountId);  // reload this account's folders
    toast(`Folder "${name.trim()}" created`);
  } catch (e) {
    toast("Create failed: " + e, true);
  }
}

function closeCtxMenu() {
  $("ctx-menu").classList.remove("show");
}

/* ---------------- auto-update ---------------- */

async function checkForUpdates(silent = false) {
  try {
    const res = await api.check_update();
    if (res.available) {
      $("upd-version").textContent = res.new_version + "  (you have " + res.local_version + ")";
      $("upd-notes").innerHTML = "<b>What's new:</b><br>" + escapeHtml(res.notes || "—").replace(/\n/g, "<br>");
      $("upd-progress").style.display = "none";
      $("update-backdrop").classList.add("show");
      return true;
    }
    if (!silent) toast("You're on the latest version (" + (res.local_version || "?") + ")");
    return false;
  } catch (e) {
    if (!silent) toast("Update check failed: " + e, true);
    return false;
  }
}

async function doUpdate() {
  $("upd-now").disabled = true;
  $("upd-later").disabled = true;
  $("upd-progress").style.display = "block";
  $("upd-status").textContent = "Downloading and applying…";
  try {
    await api.apply_update();
    // process exits and relaunches via _update.bat
  } catch (e) {
    toast("Update failed: " + e, true);
    $("upd-now").disabled = false;
    $("upd-later").disabled = false;
    $("upd-progress").style.display = "none";
  }
}

/* ---------------- sanitize (keep it light - sandbox iframe does the heavy lifting) ---------------- */

function sanitizeHtml(html) {
  // Strip <script>/<style> and event handlers as defense-in-depth.
  let s = html;
  s = s.replace(/<script[\s\S]*?<\/script>/gi, "");
  s = s.replace(/<style[\s\S]*?<\/style>/gi, "");
  s = s.replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "");
  return s;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

window.addEventListener("DOMContentLoaded", boot);

/* ---------------- draggable pane divider ---------------- */

function setupDivider() {
  const divider = $("divider");
  const listPane = $("list-pane");
  let dragging = false;
  const MIN = 240, MAX = 700;

  const clamp = (w) => Math.max(MIN, Math.min(MAX, w));

  divider.addEventListener("mousedown", (e) => {
    dragging = true;
    document.body.classList.add("dragging");
    divider.classList.add("dragging");
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    // measure from the LIST PANE's own left edge, not the body:
    // the pane starts after the sidebar, so using the body origin
    // made the divider trail behind the cursor.
    const listRect = listPane.getBoundingClientRect();
    const w = clamp(e.clientX - listRect.left);
    listPane.style.width = w + "px";
    listPane.style.minWidth = "0";
    try { localStorage.setItem("sm-list-width", String(w)); } catch {}
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("dragging");
    divider.classList.remove("dragging");
  });

  try {
    const saved = parseInt(localStorage.getItem("sm-list-width") || "", 10);
    if (saved) listPane.style.width = clamp(saved) + "px";
  } catch {}
}

function boot() {
  // pywebview injects window.pywebview AFTER DOMContentLoaded and fires
  // 'pywebviewready'. Wait for it (with a poll fallback), then start.
  const start = () => {
    if (window.pywebview && window.pywebview.api) {
      api = window.pywebview.api;
      init();
      return true;
    }
    return false;
  };
  if (start()) return;
  let attempts = 0;
  const poll = setInterval(() => {
    attempts += 1;
    if (start() || attempts > 100) clearInterval(poll);  // ~25s max
  }, 250);
  window.addEventListener("pywebviewready", () => {
    if (start()) clearInterval(poll);
  });
}
