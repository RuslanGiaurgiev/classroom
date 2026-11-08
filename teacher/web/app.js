// Панель учителя. Получает состояние по WebSocket, отправляет команды через API.
"use strict";

// Токен панели передаётся в адресе страницы (?t=...). Сервер пускает в API только с ним.
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);

let state = { laptops: [], teacher: {} };
const selected = new Set();

// ---------- Запросы к серверу ----------
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-UI-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Ошибка ${res.status}`);
  return data;
}

// ---------- Журнал ----------
function log(text, isError = false) {
  const li = document.createElement("li");
  if (isError) li.className = "err";
  const t = document.createElement("time");
  t.textContent = new Date().toLocaleTimeString("ru-RU");
  li.append(t, document.createTextNode(text));
  $("log").prepend(li);
  while ($("log").children.length > 200) $("log").lastChild.remove();
}

// ---------- Отрисовка ноутбуков ----------
function fmtAgo(ts) {
  if (!ts) return "ещё не подключался";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "был только что";
  if (s < 3600) return `был ${Math.round(s / 60)} мин назад`;
  return "был " + new Date(ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

const BROWSER_NAMES = { chrome: "Chrome", yandex: "Яндекс", edge: "Edge" };

function render() {
  const grid = $("grid");
  grid.innerHTML = "";
  const numbers = new Set(state.laptops.map((l) => l.number));
  for (const n of [...selected]) if (!numbers.has(n)) selected.delete(n);

  for (const l of state.laptops) {
    const card = document.createElement("div");
    card.className = "card" + (selected.has(l.number) ? " selected" : "");

    const head = document.createElement("div");
    head.className = "head";
    const dot = document.createElement("span");
    dot.className = "dot" + (l.online ? " on" : "");
    dot.title = l.online ? "на связи" : "не на связи";
    const num = document.createElement("span");
    num.className = "num";
    num.textContent = l.number;
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = selected.has(l.number);
    cb.onchange = () => { cb.checked ? selected.add(l.number) : selected.delete(l.number); render(); };
    head.append(dot, num, cb);

    const status = document.createElement("div");
    status.className = "meta";
    status.textContent = l.online ? "на связи" : fmtAgo(l.last_seen);

    const user = document.createElement("div");
    user.className = "meta";
    user.textContent = "Пользователь: " + (l.username || "—");

    const files = document.createElement("div");
    files.className = "meta";
    files.textContent = "Ждут отправки: " + (l.pending_files == null ? "—" : l.pending_files);

    const br = document.createElement("div");
    br.className = "meta";
    br.textContent = "Браузеры: " + (l.browsers && l.browsers.length ? l.browsers.map((b) => BROWSER_NAMES[b] || b).join(", ") : "—");

    const actions = document.createElement("div");
    const unpair = document.createElement("button");
    unpair.className = "btn small danger";
    unpair.textContent = "Отвязать";
    unpair.onclick = async () => {
      if (!confirm(`Отвязать ноутбук ${l.number}? Чтобы снова подключить его, понадобится новый код.`)) return;
      try { await api("DELETE", `/ui/api/laptops/${encodeURIComponent(l.number)}`); log(`Ноутбук ${l.number} отвязан`); }
      catch (e) { log(e.message, true); }
    };
    actions.append(unpair);

    card.append(head, status, user, files, br, actions);
    card.onclick = (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "BUTTON") return;
      selected.has(l.number) ? selected.delete(l.number) : selected.add(l.number);
      render();
    };
    grid.append(card);
  }
  const online = state.laptops.filter((l) => l.online).length;
  $("summary").textContent = `на связи ${online} из ${state.laptops.length}` + (selected.size ? ` · выбрано ${selected.size}` : "");
  $("empty").hidden = state.laptops.length > 0;
  $("selectAll").checked = state.laptops.length > 0 && selected.size === state.laptops.length;
  const t = state.teacher || {};
  $("macAddr").textContent = t.ip ? `Адрес Mac: ${t.ip}:${t.port}` : "";
}

$("selectAll").onchange = (e) => {
  selected.clear();
  if (e.target.checked) state.laptops.forEach((l) => selected.add(l.number));
  render();
};

// ---------- Команды ----------
async function sendCommand(targets, action, params, title) {
  if (Array.isArray(targets) && targets.length === 0) { log("Сначала выберите ноутбуки", true); return; }
  try {
    const r = await api("POST", "/ui/api/commands", { targets, action, params });
    const sent = Object.entries(r.statuses).filter(([, s]) => s === "sent").map(([n]) => n);
    const off = Object.entries(r.statuses).filter(([, s]) => s !== "sent").map(([n]) => n);
    log(`${title}: отправлено на ${sent.length ? sent.join(", ") : "—"}` + (off.length ? `; не на связи: ${off.join(", ")}` : ""));
  } catch (e) { log(e.message, true); }
}

function openSites(targets) {
  const urls = $("urls").value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (!urls.length) { log("Введите адрес сайта", true); return; }
  sendCommand(targets, "open_urls", { urls, browser: $("browser").value }, "Открыть сайт");
}
$("btnOpenAll").onclick = () => openSites("all");
$("btnOpenSelected").onclick = () => openSites([...selected]);

// ---------- Добавление ноутбука ----------
let codeTimer = null;
function closeModal() {
  $("modalAdd").hidden = true;
  clearInterval(codeTimer);
}
$("btnAdd").onclick = () => {
  $("addStep1").hidden = false; $("addStep2").hidden = true; $("addError").textContent = "";
  $("laptopNumber").value = "";
  $("modalAdd").hidden = false;
  $("laptopNumber").focus();
};
document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = closeModal));
$("laptopNumber").onkeydown = (e) => { if (e.key === "Enter") $("btnMakeCode").click(); };
$("btnMakeCode").onclick = async () => {
  const number = $("laptopNumber").value.trim();
  if (!number) { $("addError").textContent = "Введите номер"; return; }
  if (state.laptops.some((l) => l.number === number) &&
      !confirm(`Ноутбук ${number} уже привязан. Привязать заново (старая привязка перестанет работать)?`)) return;
  try {
    const r = await api("POST", "/ui/api/pairing-codes", { number });
    $("codeNumber").textContent = r.number;
    $("codeText").textContent = r.code;
    $("addStep1").hidden = true; $("addStep2").hidden = false; $("addError").textContent = "";
    const tick = () => {
      const left = Math.max(0, Math.round(r.expires_at - Date.now() / 1000));
      $("codeTimer").textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
      if (left === 0) { clearInterval(codeTimer); $("codeText").textContent = "Код устарел — получите новый"; }
    };
    tick(); clearInterval(codeTimer); codeTimer = setInterval(tick, 1000);
  } catch (e) { $("addError").textContent = e.message; }
};

// ---------- Живые обновления ----------
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ui/ws?t=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => { $("conn").textContent = "работает"; $("conn").className = "conn ok"; };
  ws.onclose = () => {
    $("conn").textContent = "нет связи с сервером, переподключаюсь…"; $("conn").className = "conn";
    setTimeout(connect, 2000);
  };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.kind === "state") { state = m; render(); }
    else if (m.kind === "result") {
      log(`Ноутбук ${m.number}: ${m.ok ? "выполнено" : "ошибка"}${m.message ? " — " + m.message : ""}`, !m.ok);
    } else if (m.kind === "paired") {
      log(`Ноутбук ${m.number} привязан`);
      if ($("codeNumber").textContent === m.number) closeModal();
    }
  };
}
// Обновляем «был N мин назад» раз в 30 секунд
setInterval(render, 30000);
connect();
