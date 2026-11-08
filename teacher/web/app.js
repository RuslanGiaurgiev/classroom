// Панель учителя: вкладка «Класс» и общие помощники.
// Состояние приходит по WebSocket, команды уходят через API.
"use strict";

// Токен панели передаётся в адресе страницы (?t=...). Сервер пускает в API только с ним.
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);

let state = { laptops: [], pair_requests: [], teacher: {}, settings: {} };
const selected = new Set();
const stateListeners = [];   // другие модули (projects.js) подписываются на обновления

// ---------- Помощники ----------
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-UI-Token": TOKEN },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Ошибка ${res.status}`);
  return data;
}

// Создать элемент: el("button", {className: "btn", onclick}, "текст", дочерние...)
function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "dataset") Object.assign(e.dataset, v);
    else if (k in e) e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) if (c != null) e.append(c);
  return e;
}

function log(text, isError = false) {
  const li = el("li", { className: isError ? "err" : "" },
    el("time", {}, new Date().toLocaleTimeString("ru-RU")), text);
  $("log").prepend(li);
  while ($("log").children.length > 300) $("log").lastChild.remove();
}

window.App = { api, el, log, $, get state() { return state; }, onState: (fn) => stateListeners.push(fn), sendCommand: null };

// ---------- Вкладки ----------
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
    document.querySelectorAll(".tabpage").forEach((p) => (p.hidden = p.id !== "tab-" + t.dataset.tab));
  };
});

{ // Открыть вкладку из адреса (?tab=projects)
  const t = new URLSearchParams(location.search).get("tab");
  const btn = t && document.querySelector(`.tab[data-tab="${t}"]`);
  if (btn) btn.click();
}

// ---------- Кому отправлять ----------
function targets() {
  const mode = document.querySelector('input[name="target"]:checked').value;
  return mode === "all" ? "all" : [...selected];
}
function targetTitle() {
  const t = targets();
  return t === "all" ? "всем ноутбукам" : `ноутбукам ${t.join(", ")}`;
}

async function sendCommand(action, params, title) {
  const t = targets();
  if (Array.isArray(t) && t.length === 0) { log("Выберите ноутбуки (галочки на карточках) или переключите «Кому: всем»", true); return; }
  try {
    const r = await api("POST", "/ui/api/commands", { targets: t, action, params });
    const sent = Object.entries(r.statuses).filter(([, s]) => s === "sent").map(([n]) => n);
    const off = Object.entries(r.statuses).filter(([, s]) => s !== "sent").map(([n]) => n);
    log(`${title}: отправлено ${sent.length ? "на " + sent.join(", ") : "никому"}` + (off.length ? `; не на связи: ${off.join(", ")}` : ""));
  } catch (e) { log(`${title}: ${e.message}`, true); }
}
App.sendCommand = sendCommand;

// ---------- Ноутбуки ----------
function fmtAgo(ts) {
  if (!ts) return "ещё не подключался";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "был только что";
  if (s < 3600) return `был ${Math.round(s / 60)} мин назад`;
  return "был " + new Date(ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
const BROWSER_NAMES = { chrome: "Chrome", yandex: "Яндекс", edge: "Edge" };
const SCREEN_BADGES = { lock: "🔒 заставка", message: "💬 сообщение", countdown: "⏻ отсчёт" };

function renderLaptops() {
  const grid = $("grid");
  grid.innerHTML = "";
  const numbers = new Set(state.laptops.map((l) => l.number));
  for (const n of [...selected]) if (!numbers.has(n)) selected.delete(n);

  for (const l of state.laptops) {
    const cb = el("input", { type: "checkbox", checked: selected.has(l.number) });
    cb.onchange = () => { cb.checked ? selected.add(l.number) : selected.delete(l.number); renderLaptops(); };
    const badges = el("div", { className: "badges" });
    if (l.online && l.screen) badges.append(el("span", { className: "pill" }, SCREEN_BADGES[l.screen] || l.screen));
    else if (l.locked) badges.append(el("span", { className: "pill" }, "🔒 включится при подключении"));
    if (l.shutdown) badges.append(el("span", { className: "pill warn" }, l.shutdown === "reboot" ? "перезагрузка…" : "выключение…"));

    const unpair = el("button", { className: "btn small danger", textContent: "Отвязать" });
    unpair.onclick = async () => {
      if (!confirm(`Отвязать ноутбук ${l.number}? Чтобы подключить его снова, запустите на нём agent.exe и нажмите «Принять».`)) return;
      try { await api("DELETE", `/ui/api/laptops/${encodeURIComponent(l.number)}`); log(`Ноутбук ${l.number} отвязан`); }
      catch (e) { log(e.message, true); }
    };
    const card = el("div", { className: "card" + (selected.has(l.number) ? " selected" : "") + (l.online ? "" : " offline") },
      el("div", { className: "head" },
        el("span", { className: "dot" + (l.online ? " on" : ""), title: l.online ? "на связи" : "не на связи" }),
        el("span", { className: "num" }, l.number), cb),
      el("div", { className: "meta" }, l.online ? "на связи" : fmtAgo(l.last_seen)),
      el("div", { className: "meta" }, "Пользователь: " + (l.username || "—")),
      el("div", { className: "meta" }, "Ждут отправки: " + (l.pending_files == null ? "—" : l.pending_files)),
      el("div", { className: "meta" }, "Браузеры: " + (l.browsers && l.browsers.length ? l.browsers.map((b) => BROWSER_NAMES[b] || b).join(", ") : "—")),
      badges,
      el("div", { className: "card-actions" }, unpair));
    card.onclick = (e) => {
      if (["INPUT", "BUTTON"].includes(e.target.tagName)) return;
      selected.has(l.number) ? selected.delete(l.number) : selected.add(l.number);
      if (selected.size) document.querySelector('input[name="target"][value="selected"]').checked = true;
      renderLaptops();
    };
    grid.append(card);
  }
  const online = state.laptops.filter((l) => l.online).length;
  $("summary").textContent = `на связи ${online} из ${state.laptops.length}` + (selected.size ? ` · выбрано ${selected.size}` : "");
  $("selCount").textContent = `(${selected.size})`;
  $("empty").hidden = state.laptops.length > 0;
  $("selectAll").checked = state.laptops.length > 0 && selected.size === state.laptops.length;
  const t = state.teacher || {};
  $("macAddr").textContent = t.ip ? `Адрес Mac: ${t.ip}` : "";
}

$("selectAll").onchange = (e) => {
  selected.clear();
  if (e.target.checked) state.laptops.forEach((l) => selected.add(l.number));
  renderLaptops();
};

// ---------- Новые ноутбуки (запросы на подключение) ----------
// Блок перерисовываем только при изменении списка, чтобы не стирать вводимый номер.
let shownRequestIds = null;
function renderRequests() {
  const reqs = state.pair_requests || [];
  const ids = reqs.map((r) => r.id).join(",");
  $("requestsPanel").hidden = reqs.length === 0;
  if (ids === shownRequestIds) return;
  shownRequestIds = ids;
  const box = $("requests");
  box.innerHTML = "";
  const taken = new Set(state.laptops.map((l) => l.number));
  for (const r of reqs) {
    let n = 1;
    while (taken.has(String(n))) n++;
    taken.add(String(n));
    const num = el("input", { type: "text", maxLength: 10, placeholder: "номер", className: "req-num", value: String(n) });
    const ok = el("button", { className: "btn primary", textContent: "Принять" });
    ok.onclick = async () => {
      const number = num.value.trim();
      if (!number) { num.focus(); return; }
      if (state.laptops.some((l) => l.number === number) &&
          !confirm(`Номер ${number} уже занят другим ноутбуком. Заменить (старый ноутбук отключится)?`)) return;
      try { await api("POST", `/ui/api/pair-requests/${r.id}/approve`, { number }); log(`Ноутбук ${number} подключён (${r.hostname})`); }
      catch (e) { log(e.message, true); }
    };
    num.onkeydown = (e) => { if (e.key === "Enter") ok.click(); };
    const no = el("button", { className: "btn", textContent: "Отклонить" });
    no.onclick = () => api("POST", `/ui/api/pair-requests/${r.id}/reject`).catch((e) => log(e.message, true));
    box.append(el("div", { className: "req" },
      el("div", { className: "match", title: "Число для сверки — такое же на экране ноутбука" }, r.match_code),
      el("div", { className: "req-info" }, el("b", {}, r.hostname || "Ноутбук"),
        el("div", { className: "meta muted" }, `пользователь ${r.username || "—"} · ${r.ip}`)),
      num, ok, no));
  }
}

// ---------- Открыть сайт / наборы урока ----------
function lines(text) { return text.split("\n").map((s) => s.trim()).filter(Boolean); }

$("btnOpen").onclick = () => {
  const items = lines($("urls").value);
  if (!items.length) { log("Введите адрес сайта или что найти", true); return; }
  sendCommand("open_urls", { urls: items, browser: $("browser").value, engine: $("engine").value }, "Открыть");
};
$("urls").onkeydown = (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("btnOpen").click(); };

async function saveSettings(patch, okText) {
  try { await api("PUT", "/ui/api/settings", patch); if (okText) log(okText); return true; }
  catch (e) { log(e.message, true); alert(e.message); return false; }
}

$("btnSaveSet").onclick = async () => {
  const items = lines($("urls").value);
  if (!items.length) { log("Сначала введите адреса или запросы для набора", true); return; }
  const name = prompt("Название набора урока:", "Урок " + new Date().toLocaleDateString("ru-RU"));
  if (!name) return;
  const sets = [...(state.settings.lesson_sets || []), { name, items, browser: $("browser").value }];
  saveSettings({ lesson_sets: sets }, `Набор «${name}» сохранён`);
};

function renderSets() {
  const box = $("sets");
  box.innerHTML = "";
  const sets = state.settings.lesson_sets || [];
  if (!sets.length) return;
  box.append(el("div", { className: "muted small" }, "Наборы урока:"));
  for (const s of sets) {
    const open = el("button", { className: "btn primary small", textContent: "Открыть", title: s.items.join("\n") });
    open.onclick = () => sendCommand("open_urls", { urls: s.items, browser: s.browser }, `Набор «${s.name}»`);
    const edit = el("button", { className: "btn small", textContent: "Изменить" });
    edit.onclick = () => {
      const text = prompt(`Адреса и запросы набора «${s.name}» через « | »:`, s.items.join(" | "));
      if (text == null) return;
      const items = text.split("|").map((x) => x.trim()).filter(Boolean);
      const sets2 = sets.map((x) => (x.id === s.id ? { ...x, items } : x));
      saveSettings({ lesson_sets: sets2 }, `Набор «${s.name}» изменён`);
    };
    const load = el("button", { className: "btn small", textContent: "В поле" , title: "Скопировать адреса в поле выше"});
    load.onclick = () => { $("urls").value = s.items.join("\n"); $("browser").value = s.browser; };
    const del = el("button", { className: "btn small danger", textContent: "✕", title: "Удалить набор" });
    del.onclick = () => {
      if (confirm(`Удалить набор «${s.name}»?`)) saveSettings({ lesson_sets: sets.filter((x) => x.id !== s.id) }, `Набор «${s.name}» удалён`);
    };
    box.append(el("div", { className: "set" },
      el("b", {}, s.name), el("span", { className: "muted small" }, ` ${s.items.length} вкл.`), open, load, edit, del));
  }
}

// ---------- Кнопки управления ----------
const BROWSER_PROCESSES = ["chrome.exe", "browser.exe", "msedge.exe"];
document.querySelectorAll("[data-cmd]").forEach((b) => {
  b.onclick = () => {
    const cmd = b.dataset.cmd;
    if (cmd === "lock") sendCommand("lock_screen", { text: $("lockText").value || "Внимание на учителя!" }, "Внимание на учителя");
    if (cmd === "unlock") sendCommand("unlock_screen", {}, "Снять заставку");
    if (cmd === "message") {
      const text = $("msgText").value.trim();
      if (!text) { log("Введите текст сообщения", true); $("msgText").focus(); return; }
      sendCommand("show_message", { text, seconds: Number($("msgSeconds").value) }, "Сообщение");
    }
    if (cmd === "hide-message") sendCommand("hide_message", {}, "Убрать сообщение");
    if (cmd === "close-app") {
      const p = $("appSelect").value;
      if (p) sendCommand("close_apps", { process_names: [p] }, `Закрыть ${p}`);
    }
    if (cmd === "close-browsers") sendCommand("close_apps", { process_names: BROWSER_PROCESSES }, "Закрыть браузеры");
    if (cmd === "shutdown" || cmd === "reboot") {
      const word = cmd === "shutdown" ? "Выключить" : "Перезагрузить";
      if (!confirm(`${word} — ${targetTitle()}?\nНа экранах будет отсчёт 30 секунд, за это время можно нажать «Отменить».`)) return;
      sendCommand(cmd, { seconds: 30 }, word);
    }
    if (cmd === "cancel-shutdown") sendCommand("cancel_shutdown", {}, "Отменить выключение");
  };
});

function renderControls() {
  const st = state.settings || {};
  const engines = st.search_engines || {};
  for (const id of ["engine", "setEngine"]) {
    const sel = $(id);
    const keep = sel.value;
    sel.innerHTML = "";
    for (const [k, title] of Object.entries(engines)) sel.append(el("option", { value: k }, title));
    sel.value = id === "setEngine" || !keep ? st.search_engine : keep;
  }
  const appSel = $("appSelect");
  const keepApp = appSel.value;
  appSel.innerHTML = "";
  for (const a of st.apps || []) appSel.append(el("option", { value: a.process }, `${a.title} (${a.process})`));
  if (keepApp) appSel.value = keepApp;
}

// ---------- Настройки: программы ----------
let appsDraft = null;
function renderAppsEditor(force = false) {
  if (appsDraft && !force) return;  // не мешаем, пока учитель редактирует
  appsDraft = (state.settings.apps || []).map((a) => ({ ...a }));
  drawAppsDraft();
}
function drawAppsDraft() {
  const box = $("appsEditor");
  box.innerHTML = "";
  appsDraft.forEach((a, i) => {
    const title = el("input", { type: "text", value: a.title, placeholder: "Название" });
    title.oninput = () => (a.title = title.value);
    const proc = el("input", { type: "text", value: a.process, placeholder: "имя.exe" });
    proc.oninput = () => (a.process = proc.value);
    const del = el("button", { className: "btn small danger", textContent: "✕" });
    del.onclick = () => { appsDraft.splice(i, 1); drawAppsDraft(); };
    box.append(el("div", { className: "row" }, title, proc, del));
  });
}
$("btnAddApp").onclick = () => { appsDraft.push({ title: "", process: "" }); drawAppsDraft(); };
$("btnSaveApps").onclick = async () => {
  const apps = appsDraft.filter((a) => a.process.trim());
  if (await saveSettings({ apps }, "Список программ сохранён")) { appsDraft = null; renderAppsEditor(true); }
};
$("setEngine").onchange = () => saveSettings({ search_engine: $("setEngine").value }, "Поисковая система сохранена");

// ---------- Живые обновления ----------
function onState() {
  renderLaptops();
  renderRequests();
  renderSets();
  renderControls();
  renderAppsEditor();
  for (const fn of stateListeners) { try { fn(state); } catch (e) { console.error(e); } }
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ui/ws?t=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => { $("conn").textContent = "работает"; $("conn").className = "conn ok"; };
  ws.onclose = () => {
    $("conn").textContent = "нет связи с сервером…"; $("conn").className = "conn";
    setTimeout(connect, 2000);
  };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.kind === "state") { state = m; onState(); }
    else if (m.kind === "result") {
      log(`Ноутбук ${m.number}: ${m.ok ? "" : "ошибка — "}${m.message || "выполнено"}`, !m.ok);
    } else if (m.kind === "log") {
      log(m.message, !!m.error);
    }
    for (const fn of stateListeners) { if (fn.onEvent) fn.onEvent(m); }
  };
}
setInterval(renderLaptops, 30000);  // обновлять «был N мин назад»
connect();
