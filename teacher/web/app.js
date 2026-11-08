// Панель учителя: вкладка «Класс», окна, уведомления и общие помощники.
// Состояние приходит по WebSocket, команды уходят через API.
"use strict";

// Токен панели передаётся в адресе страницы (?t=...). Сервер пускает в API только с ним.
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);

let state = { laptops: [], pair_requests: [], teacher: {}, settings: {}, materials: [] };
const selected = new Set();
const stateListeners = [];

// ================= Помощники =================

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-UI-Token": TOKEN },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.error || `Ошибка ${res.status}`); e.data = data; e.status = res.status; throw e; }
  return data;
}

// el("button", {className: "btn", onclick}, "текст", дочерние…)
function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "dataset") Object.assign(e.dataset, v);
    else if (k === "style" && typeof v === "string") e.setAttribute("style", v);
    else if (k in e) e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null && c !== false) e.append(c);
  return e;
}

function log(text, isError = false) {
  $("log").prepend(el("li", { className: isError ? "err" : "" }, el("time", {}, new Date().toLocaleTimeString("ru-RU")), text));
  while ($("log").children.length > 300) $("log").lastChild.remove();
}

function toast(text, kind = "") {
  const t = el("div", { className: "toast " + kind }, text);
  $("toasts").append(t);
  setTimeout(() => t.remove(), kind === "error" ? 9000 : 5000);
  log(text, kind === "error");
}

// ---------- Окна ----------
// dialog({title, body: [элементы], buttons: [{text, kind, value, onClick}], wide}) → Promise(value)
function dialog({ title, body = [], buttons = [{ text: "Закрыть", value: null }], wide = false, onOpen }) {
  return new Promise((resolve) => {
    const overlay = el("div", { className: "modal" });
    const close = (v) => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") close(null); };
    const acts = el("div", { className: "modal-actions" });
    for (const b of buttons) {
      const btn = el("button", { className: "btn " + (b.kind || ""), textContent: b.text });
      btn.onclick = async () => {
        if (b.onClick) { const r = await b.onClick(btn); if (r === false) return; }
        close(b.value);
      };
      acts.append(btn);
    }
    overlay.append(el("div", { className: "modal-box" + (wide ? " wide" : "") }, el("h2", {}, title), ...body, acts));
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(null); });
    document.addEventListener("keydown", onKey);
    document.body.append(overlay);
    overlay._close = close;
    if (onOpen) onOpen(overlay);
  });
}

function confirmBox(text, { okText = "Да", danger = false, title = "Подтверждение" } = {}) {
  return dialog({ title, body: [el("p", { style: "white-space:pre-line" }, text)],
    buttons: [{ text: "Отмена", value: false }, { text: okText, kind: danger ? "danger solid" : "primary", value: true }] });
}

function promptBox(title, value = "", { label = "", okText = "Сохранить", placeholder = "" } = {}) {
  const input = el("input", { type: "text", value, placeholder });
  input.onkeydown = (e) => { if (e.key === "Enter") input.closest(".modal")._close(input.value.trim()); };
  return dialog({
    title, body: [el("label", { className: "field" }, label ? el("span", {}, label) : null, input)],
    buttons: [{ text: "Отмена", value: null }, { text: okText, kind: "primary", onClick: () => { input.closest(".modal")._close(input.value.trim()); return false; } }],
    onOpen: () => setTimeout(() => { input.focus(); input.select(); }, 30),
  });
}

window.App = {
  api, el, log, toast, $, dialog, confirm: confirmBox, prompt: promptBox,
  get state() { return state; }, onState: (fn) => stateListeners.push(fn),
  targets: () => targets(), targetTitle: () => targetTitle(),
};

// ================= Вкладки =================
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
    document.querySelectorAll(".tabpage").forEach((p) => (p.hidden = p.id !== "tab-" + t.dataset.tab));
  };
});
{
  const t = new URLSearchParams(location.search).get("tab");
  const btn = t && document.querySelector(`.tab[data-tab="${t}"]`);
  if (btn) btn.click();
}

// ================= Кому отправлять =================
const sortNum = (a, b) => (Number(a) - Number(b)) || String(a).localeCompare(String(b));
function targets() { return selected.size ? [...selected].sort(sortNum) : "all"; }
function targetTitle() {
  const t = targets();
  return t === "all" ? "всем ноутбукам" : t.length === 1 ? `ноутбуку ${t[0]}` : `ноутбукам ${t.join(", ")}`;
}
function renderTarget() {
  const chip = $("targetChip");
  if (selected.size) {
    chip.textContent = `выбранных: ${[...selected].sort(sortNum).join(", ")}`;
    chip.classList.add("selected");
  } else {
    const online = state.laptops.filter((l) => l.online).length;
    chip.textContent = `всех ноутбуков (на связи ${online} из ${state.laptops.length})`;
    chip.classList.remove("selected");
  }
  $("btnClearSel").hidden = !selected.size;
  $("selHint").hidden = !!selected.size || !state.laptops.length;
  $("btnSelectAll").textContent = selected.size === state.laptops.length && selected.size ? "Снять выбор" : "Выбрать все";
}
$("btnClearSel").onclick = () => { selected.clear(); renderLaptops(); };
$("btnSelectAll").onclick = () => {
  if (selected.size === state.laptops.length) selected.clear();
  else state.laptops.forEach((l) => selected.add(l.number));
  renderLaptops();
};

// ================= Команды и сводка результатов =================
const pendingResults = {};   // command_id → {title, sent, ok, err}

async function sendCommand(action, params, title) {
  try {
    const r = await api("POST", "/ui/api/commands", { targets: targets(), action, params });
    const sent = Object.entries(r.statuses).filter(([, s]) => s === "sent").map(([n]) => n);
    const off = Object.entries(r.statuses).filter(([, s]) => s !== "sent").map(([n]) => n);
    if (!sent.length) { toast(`${title}: ни один из ноутбуков не на связи`, "error"); return; }
    pendingResults[r.command_id] = { title, sent, off, ok: [], err: [], timer: null };
    // Если ответы не пришли за 15 секунд — показываем то, что есть
    pendingResults[r.command_id].timer = setTimeout(() => flushResult(r.command_id), 15000);
  } catch (e) { toast(`${title}: ${e.message}`, "error"); }
}
App.sendCommand = sendCommand;

function onResult(m) {
  const p = pendingResults[m.command_id];
  if (!p) { log(`Ноутбук ${m.number}: ${m.message || (m.ok ? "выполнено" : "ошибка")}`, !m.ok); return; }
  (m.ok ? p.ok : p.err).push(m);
  if (p.ok.length + p.err.length >= p.sent.length) { clearTimeout(p.timer); setTimeout(() => flushResult(m.command_id), 200); }
}
function flushResult(id) {
  const p = pendingResults[id];
  if (!p) return;
  delete pendingResults[id];
  let text = `${p.title}: выполнено на ${p.ok.length} из ${p.sent.length}`;
  if (p.ok.length === 1 && p.sent.length === 1 && p.ok[0].message) text = `${p.title} (ноутбук ${p.ok[0].number}): ${p.ok[0].message}`;
  if (p.err.length) text += `. Ошибки: ` + p.err.map((m) => `${m.number} — ${m.message}`).join("; ");
  const silent = p.sent.filter((n) => !p.ok.concat(p.err).some((m) => m.number === n));
  if (silent.length) text += `. Нет ответа: ${silent.join(", ")}`;
  if (p.off.length) text += `. Не на связи: ${p.off.join(", ")}`;
  toast(text, p.err.length || silent.length ? "error" : "ok");
  for (const m of p.ok) if (m.message) log(`Ноутбук ${m.number}: ${m.message}`);
}

// ================= Ноутбуки =================
function fmtAgo(ts) {
  if (!ts) return "ещё не подключался";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "был только что";
  if (s < 3600) return `был ${Math.round(s / 60)} мин назад`;
  if (s < 86400) return `был ${Math.round(s / 3600)} ч назад`;
  return "был " + new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}
const BROWSER_NAMES = { chrome: "Chrome", yandex: "Яндекс", edge: "Edge" };

function laptopBadges(l) {
  const out = [];
  if (l.online && l.screen === "lock") out.push(el("span", { className: "pill lock" }, "🔒 внимание"));
  else if (l.online && l.screen === "message") out.push(el("span", { className: "pill accent" }, "💬 сообщение"));
  else if (l.online && l.screen === "countdown") out.push(el("span", { className: "pill warn" }, "⏻ отсчёт"));
  else if (l.locked) out.push(el("span", { className: "pill lock" }, "🔒 включится при подключении"));
  if (l.shutdown) out.push(el("span", { className: "pill warn" }, l.shutdown === "reboot" ? "перезагрузка…" : "выключение…"));
  if (l.pending_files) out.push(el("span", { className: "pill accent" }, `📤 ${l.pending_files} ждут отправки`));
  for (const p of state.materials || []) {
    const t = p.targets[l.number];
    if (t && (t.state === "downloading" || t.state === "sent")) {
      const pct = p.total_size ? Math.round((t.done || 0) * 100 / p.total_size) : 0;
      out.push(el("span", { className: "pill accent" }, `⬇️ ${pct}%`));
    }
  }
  if (l.update_status) out.push(el("span", { className: "pill" + (l.update_status.startsWith("ошибка") ? " warn" : ""), title: l.update_status }, "⟳ " + l.update_status.slice(0, 40)));
  return out;
}

function renderLaptops() {
  const grid = $("grid");
  grid.innerHTML = "";
  const numbers = new Set(state.laptops.map((l) => l.number));
  for (const n of [...selected]) if (!numbers.has(n)) selected.delete(n);

  for (const l of state.laptops) {
    const menu = el("button", { className: "btn small menu", textContent: "⋯", title: "Подробнее, изменить номер, отвязать" });
    menu.onclick = (e) => { e.stopPropagation(); laptopDialog(l); };
    const sub = l.online
      ? [l.username || "—", l.browsers && l.browsers.length ? l.browsers.map((b) => BROWSER_NAMES[b] || b).join(", ") : null].filter(Boolean).join(" · ")
      : "не в сети · " + fmtAgo(l.last_seen);
    const tile = el("div", { className: "tile" + (selected.has(l.number) ? " selected" : "") + (l.online ? "" : " offline"), title: "Нажмите, чтобы выбрать" },
      el("div", { className: "top" },
        el("span", { className: "dot" + (l.online ? " on" : "") }),
        el("span", { className: "num" }, l.number),
        el("span", { className: "check" }, selected.has(l.number) ? "✓" : "")),
      el("div", { className: "meta" }, sub),
      el("div", { className: "badges" }, laptopBadges(l)),
      menu);
    tile.onclick = () => { selected.has(l.number) ? selected.delete(l.number) : selected.add(l.number); renderLaptops(); };
    grid.append(tile);
  }
  const online = state.laptops.filter((l) => l.online).length;
  $("summary").textContent = state.laptops.length ? `на связи ${online} из ${state.laptops.length}` : "";
  $("empty").hidden = state.laptops.length > 0;
  $("btnSelectAll").hidden = !state.laptops.length;
  const t = state.teacher || {};
  const chip = $("conn");
  chip.lastChild.textContent = chip.dataset.ok ? `работает · Mac ${t.ip || ""}` : "нет связи с сервером…";
  renderTarget();
  renderLockButton();
}

async function laptopDialog(l) {
  const info = el("div", { className: "kv" },
    el("span", {}, "Состояние"), el("span", {}, l.online ? "🟢 на связи" : "⚪ не в сети, " + fmtAgo(l.last_seen)),
    el("span", {}, "Пользователь"), el("span", {}, l.username || "—"),
    el("span", {}, "Имя компьютера"), el("span", {}, l.hostname || "—"),
    el("span", {}, "IP-адрес"), el("span", {}, l.ip || "—"),
    el("span", {}, "Версия агента"), el("span", {}, l.agent_version || "—"),
    el("span", {}, "Браузеры"), el("span", {}, (l.browsers || []).map((b) => BROWSER_NAMES[b] || b).join(", ") || "—"),
    el("span", {}, "Ждут отправки"), el("span", {}, l.pending_files == null ? "—" : String(l.pending_files)));
  const v = await dialog({
    title: `💻 Ноутбук ${l.number}`, body: [info],
    buttons: [
      { text: "Отвязать", kind: "danger", value: "unpair" },
      { text: "Изменить номер", value: "rename" },
      { text: "Готово", kind: "primary", value: null },
    ],
  });
  if (v === "rename") renameLaptop(l.number);
  if (v === "unpair") {
    if (!(await confirmBox(`Отвязать ноутбук ${l.number}?\nЧтобы подключить его снова, запустите на нём agent.exe и нажмите «Принять».`, { okText: "Отвязать", danger: true }))) return;
    try { await api("DELETE", `/ui/api/laptops/${encodeURIComponent(l.number)}`); toast(`Ноутбук ${l.number} отвязан`, "ok"); }
    catch (e) { toast(e.message, "error"); }
  }
}

async function renameLaptop(old) {
  const n = await promptBox(`Новый номер для ноутбука ${old}`, old, { label: "Номер с наклейки на мониторе" });
  if (!n || n === old) return;
  try {
    await api("POST", `/ui/api/laptops/${encodeURIComponent(old)}/rename`, { new_number: n });
    if (selected.delete(old)) selected.add(n);
    toast(`Ноутбук ${old} теперь — ${n}`, "ok");
  } catch (e) {
    if (e.data && e.data.busy) {
      if (await confirmBox(`Номер ${n} уже занят другим ноутбуком.\nПоменять номера местами (${old} ⇄ ${n})?`, { okText: "Поменять местами" })) {
        try { await api("POST", `/ui/api/laptops/${encodeURIComponent(old)}/rename`, { new_number: n, swap: true }); toast(`Номера ${old} и ${n} поменялись местами`, "ok"); }
        catch (e2) { toast(e2.message, "error"); }
      }
    } else toast(e.message, "error");
  }
}

// ================= Новые ноутбуки =================
let shownRequestIds = null;
function renderRequests() {
  const reqs = state.pair_requests || [];
  const ids = reqs.map((r) => r.id).join(",");
  $("requestsPanel").hidden = reqs.length === 0;
  if (ids === shownRequestIds) return;   // не стираем номер, который учитель сейчас вводит
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
          !(await confirmBox(`Номер ${number} уже занят другим ноутбуком. Заменить (старый ноутбук отключится)?`, { okText: "Заменить", danger: true }))) return;
      try { await api("POST", `/ui/api/pair-requests/${r.id}/approve`, { number }); toast(`Ноутбук ${number} подключён`, "ok"); }
      catch (e) { toast(e.message, "error"); }
    };
    num.onkeydown = (e) => { if (e.key === "Enter") ok.click(); };
    const no = el("button", { className: "btn", textContent: "Отклонить" });
    no.onclick = () => api("POST", `/ui/api/pair-requests/${r.id}/reject`).catch((e) => toast(e.message, "error"));
    box.append(el("div", { className: "req" },
      el("div", { className: "match", title: "Число для сверки — такое же на экране ноутбука" }, r.match_code),
      el("div", { className: "req-info" }, el("b", {}, r.hostname || "Ноутбук"),
        el("div", { className: "muted small" }, `пользователь ${r.username || "—"} · ${r.ip}`)),
      el("span", { className: "muted small" }, "номер:"), num, ok, no));
  }
}

// ================= Действия =================
const lines = (text) => text.split("\n").map((s) => s.trim()).filter(Boolean);

async function saveSettings(patch, okText) {
  try { await api("PUT", "/ui/api/settings", patch); if (okText) toast(okText, "ok"); return true; }
  catch (e) { toast(e.message, "error"); return false; }
}

// ---------- 🌐 Открыть сайт ----------
function openSiteDialog() {
  const st = state.settings || {};
  const urls = el("textarea", { rows: 3, placeholder: "scratch.mit.edu\nраскраски динозавры\n\nКаждая строка — отдельная вкладка. Можно писать адрес или просто что найти." });
  const browser = el("select", {},
    el("option", { value: "default" }, "по умолчанию на ноутбуке"), el("option", { value: "chrome" }, "Google Chrome"),
    el("option", { value: "yandex" }, "Яндекс Браузер"), el("option", { value: "edge" }, "Microsoft Edge"));
  const engine = el("select", {});
  for (const [k, title] of Object.entries(st.search_engines || {})) engine.append(el("option", { value: k }, title));
  engine.value = st.search_engine;
  const setsBox = el("div", { className: "sets" });
  const drawSets = () => {
    setsBox.innerHTML = "";
    const sets = (state.settings || {}).lesson_sets || [];
    if (!sets.length) { setsBox.append(el("p", { className: "muted small" }, "Наборов урока пока нет. Введите адреса и нажмите «Сохранить как набор» — потом их можно открывать одной кнопкой.")); return; }
    setsBox.append(el("div", { className: "muted small" }, "Наборы урока:"));
    for (const s of sets) {
      const open = el("button", { className: "btn primary small", textContent: "Открыть", title: s.items.join("\n") });
      open.onclick = () => { sendCommand("open_urls", { urls: s.items, browser: s.browser }, `Набор «${s.name}»`); open.closest(".modal")._close(); };
      const load = el("button", { className: "btn small", textContent: "Изменить" });
      load.onclick = async () => {
        const text = await promptBox(`Набор «${s.name}»`, s.items.join(" | "), { label: "Адреса и запросы через « | »" });
        if (text == null) return;
        const items = text.split("|").map((x) => x.trim()).filter(Boolean);
        if (await saveSettings({ lesson_sets: sets.map((x) => (x.id === s.id ? { ...x, items } : x)) }, `Набор «${s.name}» изменён`)) setTimeout(drawSets, 300);
      };
      const del = el("button", { className: "btn small danger", textContent: "Удалить" });
      del.onclick = async () => {
        if (await confirmBox(`Удалить набор «${s.name}»?`, { okText: "Удалить", danger: true })
            && await saveSettings({ lesson_sets: sets.filter((x) => x.id !== s.id) }, `Набор «${s.name}» удалён`)) setTimeout(drawSets, 300);
      };
      setsBox.append(el("div", { className: "set" }, el("b", {}, s.name), el("span", { className: "muted small" }, `${s.items.length} вкл.`), open, load, del));
    }
  };
  drawSets();
  dialog({
    title: "🌐 Открыть сайт или найти", wide: true,
    body: [el("p", { className: "muted small" }, `Для: ${targetTitle()}`), urls,
      el("div", { className: "row" }, el("span", {}, "Браузер:"), browser, el("span", {}, "Поиск в:"), engine), setsBox],
    buttons: [
      { text: "Сохранить как набор", onClick: async () => {
        const items = lines(urls.value);
        if (!items.length) { toast("Сначала введите адреса или запросы", "error"); return false; }
        const name = await promptBox("Название набора урока", "Урок " + new Date().toLocaleDateString("ru-RU"));
        if (name) await saveSettings({ lesson_sets: [...((state.settings || {}).lesson_sets || []), { name, items, browser: browser.value }] }, `Набор «${name}» сохранён`);
        setTimeout(drawSets, 300);
        return false;
      } },
      { text: "Отмена", value: null },
      { text: "Открыть", kind: "primary", onClick: () => {
        const items = lines(urls.value);
        if (!items.length) { urls.focus(); return false; }
        sendCommand("open_urls", { urls: items, browser: browser.value, engine: engine.value }, "Открыть сайт");
      } },
    ],
    onOpen: () => setTimeout(() => urls.focus(), 30),
  });
}

// ---------- 📁 Материалы ----------
const fmtMB = (b) => (b >= 1073741824 ? (b / 1073741824).toFixed(1) + " ГБ" : b >= 1048576 ? (b / 1048576).toFixed(1) + " МБ" : Math.max(1, Math.round(b / 1024)) + " КБ");
const BASE_NAMES = { desktop: "Рабочий стол", documents: "Документы", downloads: "Загрузки" };

function materialsDialog() {
  let chosen = [];
  const list = el("div", { className: "chosen" });
  const draw = () => {
    list.innerHTML = "";
    if (!chosen.length) { list.append(el("p", { className: "muted" }, "Ничего не выбрано.")); return; }
    for (const it of chosen) {
      const rm = el("button", { className: "btn small danger", textContent: "✕" });
      rm.onclick = () => { chosen = chosen.filter((x) => x !== it); draw(); };
      list.append(el("div", { className: "file-row" }, el("span", {}, it.is_dir ? "📁" : "📄"), el("b", { className: "file-info" }, it.name), el("span", { className: "muted" }, fmtMB(it.size)), rm));
    }
    list.append(el("div", { className: "muted small" }, "Всего: " + fmtMB(chosen.reduce((a, x) => a + x.size, 0))));
  };
  draw();
  const pick = (kind) => async () => {
    try {
      const r = await api("POST", "/ui/api/materials/pick", { kind });
      for (const it of r.items) if (!chosen.some((x) => x.path === it.path)) chosen.push(it);
      draw();
    } catch (e) { toast(e.message, "error"); }
  };
  const d = (state.settings || {}).materials_dest || { base: "desktop", sub: "Материалы" };
  dialog({
    title: "📁 Отправить материалы", wide: true,
    body: [
      el("p", { className: "muted small" }, `Для: ${targetTitle()}. На ноутбуках файлы появятся в «${BASE_NAMES[d.base] || d.base}${d.sub ? "\\" + d.sub : ""}» (меняется в «Настройках»).`),
      el("div", { className: "row" },
        Object.assign(el("button", { className: "btn", textContent: "📄 Выбрать файлы…" }), { onclick: pick("files") }),
        Object.assign(el("button", { className: "btn", textContent: "📁 Выбрать папку…" }), { onclick: pick("folders") })),
      list,
    ],
    buttons: [
      { text: "Отмена", value: null },
      { text: "Отправить", kind: "primary", onClick: async (btn) => {
        if (!chosen.length) { toast("Сначала выберите файлы или папку", "error"); return false; }
        btn.disabled = true; btn.textContent = "готовлю…";
        try {
          const r = await api("POST", "/ui/api/materials/send", { paths: chosen.map((x) => x.path), targets: targets() });
          toast(`Материалы «${r.name}» (${r.count} файл., ${fmtMB(r.total_size)}) отправляются ${targetTitle()}`, "ok");
        } catch (e) { toast(e.message, "error"); btn.disabled = false; btn.textContent = "Отправить"; return false; }
      } },
    ],
  });
}

const MAT_ICONS = { pending: "⏸", sent: "⏳", downloading: "⬇️", done: "✅", error: "❌" };
function renderMaterials() {
  const box = $("matStrip");
  box.innerHTML = "";
  const now = Date.now() / 1000;
  // Показываем пакеты, которые ещё доставляются, и недавно доставленные (5 минут)
  const active = (state.materials || []).filter((p) => p.state === "preparing" ||
    Object.values(p.targets).some((t) => t.state !== "done") || now - p.created < 300).slice(0, 3);
  for (const p of active) {
    const nums = Object.keys(p.targets).sort(sortNum);
    const done = nums.filter((n) => p.targets[n].state === "done").length;
    const notDone = nums.filter((n) => p.targets[n].state !== "done");
    const again = el("button", { className: "btn small", textContent: "Отправить ещё раз", title: "Тем, кто ещё не получил" });
    again.onclick = () => api("POST", `/ui/api/materials/${p.id}/resend`, {}).then(() => toast(`«${p.name}»: отправлено повторно`, "ok")).catch((e) => toast(e.message, "error"));
    const grid = el("div", { className: "mat-grid" });
    for (const n of nums) {
      const t = p.targets[n];
      const pct = p.total_size ? Math.min(100, Math.round((t.done || 0) * 100 / p.total_size)) : 100;
      grid.append(el("div", { className: "mat-cell " + t.state, title: t.message || "" },
        el("span", {}, `${MAT_ICONS[t.state] || ""} ${n}`),
        el("div", { className: "bar" }, el("div", { style: `width:${t.state === "done" ? 100 : pct}%` })),
        el("span", { className: "muted small" }, t.state === "downloading" ? `${pct}%` : t.state === "error" ? "ошибка" : t.state === "pending" ? "не в сети" : "")));
    }
    box.append(el("div", { className: "mat-pkg" },
      el("div", { className: "row" }, el("b", {}, p.state === "preparing" ? `⌛ ${p.name} — подготовка…` : `📁 ${p.name}`),
        el("span", { className: "muted small" }, `${p.count} файл., ${fmtMB(p.total_size)} · получили ${done} из ${nums.length}`),
        el("div", { className: "spacer" }), notDone.length ? again : null),
      grid));
  }
}

// ---------- 🔒 Внимание ----------
function lockActiveForTargets() {
  const t = targets();
  const list = t === "all" ? state.laptops : state.laptops.filter((l) => t.includes(l.number));
  return list.length > 0 && list.some((l) => l.locked);
}
function renderLockButton() {
  const on = lockActiveForTargets();
  const b = $("actLock");
  b.classList.toggle("on", on);
  b.querySelector(".ic").textContent = on ? "🔓" : "🔒";
  b.lastChild.textContent = on ? "Снять внимание" : "Внимание";
}
function toggleLock() {
  if (lockActiveForTargets()) sendCommand("unlock_screen", {}, "Снять «Внимание»");
  else sendCommand("lock_screen", { text: (state.settings || {}).lock_text || "Внимание на учителя!" }, "Внимание на учителя");
}

// ---------- 💬 Сообщение ----------
function messageDialog() {
  const text = el("textarea", { rows: 3, maxLength: 500, placeholder: "Например: Сохраните проект и поднимите руку 🙂" });
  const secs = el("select", {}, ...[["10", "10 секунд"], ["30", "30 секунд"], ["60", "1 минута"], ["300", "5 минут"], ["0", "пока не уберу"]]
    .map(([v, t]) => el("option", { value: v, selected: v === "30" }, t)));
  dialog({
    title: "💬 Сообщение на весь экран",
    body: [el("p", { className: "muted small" }, `Для: ${targetTitle()}`), text, el("div", { className: "row" }, el("span", {}, "Показывать:"), secs)],
    buttons: [
      { text: "Убрать сообщение", onClick: () => { sendCommand("hide_message", {}, "Убрать сообщение"); } },
      { text: "Отмена", value: null },
      { text: "Показать", kind: "primary", onClick: () => {
        if (!text.value.trim()) { text.focus(); return false; }
        sendCommand("show_message", { text: text.value.trim(), seconds: Number(secs.value) }, "Сообщение");
      } },
    ],
    onOpen: () => setTimeout(() => text.focus(), 30),
  });
}

// ---------- ✖️ Закрыть программу ----------
const BROWSER_PROCESSES = ["chrome.exe", "browser.exe", "msedge.exe"];
function closeDialog() {
  const apps = (state.settings || {}).apps || [];
  const btns = apps.map((a) => {
    const b = el("button", { className: "btn", textContent: a.title, title: a.process });
    b.onclick = () => { sendCommand("close_apps", { process_names: [a.process] }, `Закрыть ${a.title}`); b.closest(".modal")._close(); };
    return b;
  });
  const all = el("button", { className: "btn danger", textContent: "Все браузеры" });
  all.onclick = () => { sendCommand("close_apps", { process_names: BROWSER_PROCESSES }, "Закрыть браузеры"); all.closest(".modal")._close(); };
  dialog({
    title: "✖️ Закрыть программу",
    body: [el("p", { className: "muted small" }, `Для: ${targetTitle()}. Несохранённая работа в программе пропадёт.`),
      el("div", { className: "big-choice" }, ...btns, all),
      el("p", { className: "muted small" }, "Список программ меняется в «Настройках».")],
    buttons: [{ text: "Отмена", value: null }],
  });
}

// ---------- ⏻ Питание ----------
function powerDialog() {
  const act = async (btn, cmd, word) => {
    const m = btn.closest(".modal");
    if (await confirmBox(`${word} — ${targetTitle()}?\nНа экранах будет отсчёт 30 секунд; в это время можно нажать «Отменить».`, { okText: word, danger: true })) {
      sendCommand(cmd, { seconds: 30 }, word);
      m._close();
    }
  };
  const b1 = el("button", { className: "btn danger solid", textContent: "⏻ Выключить" });
  const b2 = el("button", { className: "btn danger", textContent: "↻ Перезагрузить" });
  const b3 = el("button", { className: "btn", textContent: "Отменить выключение" });
  b1.onclick = () => act(b1, "shutdown", "Выключить");
  b2.onclick = () => act(b2, "reboot", "Перезагрузить");
  b3.onclick = () => { sendCommand("cancel_shutdown", {}, "Отменить выключение"); b3.closest(".modal")._close(); };
  dialog({
    title: "⏻ Питание",
    body: [el("p", { className: "muted small" }, `Для: ${targetTitle()}. Перед выключением ноутбуки успеют отправить новые файлы.`),
      el("div", { className: "big-choice" }, b1, b2, b3)],
    buttons: [{ text: "Закрыть", value: null }],
  });
}

document.querySelectorAll("[data-act]").forEach((b) => {
  b.addEventListener("click", () => {
    const a = b.dataset.act;
    if (a === "endlesson") return;   // обрабатывается в projects.js
    if (!state.laptops.length) { toast("Сначала подключите ноутбуки", "error"); return; }
    ({ open: openSiteDialog, materials: materialsDialog, lock: toggleLock, message: messageDialog,
       close: closeDialog, power: powerDialog }[a])();
  });
});

// ================= Настройки (сайты, программы, обновления) =================
function renderSettingsBasics() {
  const st = state.settings || {};
  const sel = $("setEngine");
  if (!sel.options.length) for (const [k, title] of Object.entries(st.search_engines || {})) sel.append(el("option", { value: k }, title));
  sel.value = st.search_engine;
  if (document.activeElement !== $("setLockText")) $("setLockText").value = st.lock_text || "Внимание на учителя!";
}
$("setEngine").onchange = () => saveSettings({ search_engine: $("setEngine").value }, "Поисковая система сохранена");
$("btnSaveLockText").onclick = () => saveSettings({ lock_text: $("setLockText").value }, "Текст заставки сохранён");

let appsDraft = null;
function renderAppsEditor(force = false) {
  if (appsDraft && !force) return;
  appsDraft = ((state.settings || {}).apps || []).map((a) => ({ ...a }));
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
  if (await saveSettings({ apps: appsDraft.filter((a) => a.process.trim()) }, "Список программ сохранён")) { appsDraft = null; setTimeout(() => renderAppsEditor(true), 300); }
};

function renderUpdates() {
  let sec = $("updatesSettings");
  if (!sec) {
    sec = el("section", { className: "panel", id: "updatesSettings" });
    $("tab-settings").insertBefore(sec, $("settingsExtra"));
  }
  const u = state.updates || {};
  const versions = {};
  for (const l of state.laptops) if (l.agent_version) versions[l.agent_version] = (versions[l.agent_version] || 0) + 1;
  const auto = el("input", { type: "checkbox", checked: !!u.auto });
  auto.onchange = () => saveSettings({ auto_update: auto.checked }, auto.checked ? "Автообновление включено" : "Автообновление выключено");
  const check = el("button", { className: "btn", textContent: "Проверить обновления" });
  check.onclick = async () => {
    check.disabled = true; check.textContent = "проверяю…";
    try { const r = await api("POST", "/ui/api/updates/check"); toast(r.latest ? `Последняя версия агента: ${r.latest}` : "Обновлений не найдено", "ok"); }
    catch (e) { toast(e.message, "error"); }
    check.disabled = false; check.textContent = "Проверить обновления";
  };
  const push = el("button", { className: "btn primary", textContent: "Обновить ноутбуки сейчас" });
  push.onclick = async () => {
    push.disabled = true;
    try {
      const r = await api("POST", "/ui/api/updates/push", { targets: "all" });
      const res = Object.entries(r.results);
      toast(res.length ? res.map(([n, m]) => `${n}: ${m}`).join("; ") : "Все ноутбуки уже обновлены", "ok");
    } catch (e) { toast(e.message, "error"); }
    push.disabled = false;
  };
  const outdated = state.laptops.filter((l) => l.outdated).map((l) => l.number);
  sec.innerHTML = "";
  sec.append(el("h2", {}, "⟳ Обновления ноутбуков"),
    el("p", {}, u.latest ? `Последняя версия агента: ${u.latest}` : "Новых версий пока не скачано.",
      u.last_check ? el("span", { className: "muted small" }, ` (проверено в ${new Date(u.last_check * 1000).toLocaleTimeString("ru-RU")})`) : null),
    u.error ? el("p", { className: "error" }, u.error) : null,
    el("p", { className: "muted small" }, "На ноутбуках: " + (Object.entries(versions).map(([v, n]) => `версия ${v} — ${n} шт.`).join(", ") || "—") +
      (outdated.length ? `. Ждут обновления: ${outdated.join(", ")}` : "")),
    el("label", { className: "row" }, auto, el("span", {}, "Обновлять автоматически (только когда на ноутбуке нет заставки, сообщения, отсчёта и отправки файлов)")),
    el("div", { className: "row" }, check, push),
    el("p", { className: "muted small" }, "Устанавливаются только версии с цифровой подписью. Если новая версия не запустится, ноутбук сам вернёт прежнюю."));
}

// ================= Живые обновления =================
function onState() {
  renderLaptops();
  renderRequests();
  renderMaterials();
  renderSettingsBasics();
  renderAppsEditor();
  renderUpdates();
  for (const fn of stateListeners) { try { fn(state); } catch (e) { console.error(e); } }
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ui/ws?t=${encodeURIComponent(TOKEN)}`);
  const chip = $("conn");
  ws.onopen = () => { chip.dataset.ok = "1"; chip.querySelector(".dot").className = "dot on"; };
  ws.onclose = () => {
    delete chip.dataset.ok;
    chip.querySelector(".dot").className = "dot";
    chip.lastChild.textContent = "нет связи с сервером…";
    setTimeout(connect, 2000);
  };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.kind === "state") { state = m; onState(); }
    else if (m.kind === "result") onResult(m);
    else if (m.kind === "materials") { state.materials = m.packages; renderMaterials(); renderLaptops(); }
    else if (m.kind === "log") log(m.message, !!m.error);
    for (const fn of stateListeners) { if (fn.onEvent) fn.onEvent(m); }
  };
}
setInterval(renderLaptops, 30000);   // обновлять «был N мин назад»
connect();
