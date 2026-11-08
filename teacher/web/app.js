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

// ---------- Новые ноутбуки (запросы на подключение) ----------
// Перерисовываем блок только когда меняется список запросов,
// чтобы не стирать номер, который учитель сейчас вводит.
let shownRequestIds = "";

function suggestNumber(taken) {
  for (let n = 1; n < 1000; n++) if (!taken.has(String(n))) return String(n);
  return "";
}

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
    const row = document.createElement("div");
    row.className = "req";
    const code = document.createElement("div");
    code.className = "match";
    code.textContent = r.match_code;
    code.title = "Число для сверки — такое же на экране ноутбука";
    const info = document.createElement("div");
    info.className = "req-info";
    info.innerHTML = "";
    const b = document.createElement("b");
    b.textContent = r.hostname || "Ноутбук";
    const meta = document.createElement("div");
    meta.className = "meta muted";
    meta.textContent = `пользователь ${r.username || "—"} · ${r.ip}`;
    info.append(b, meta);
    const num = document.createElement("input");
    num.type = "text";
    num.maxLength = 10;
    num.placeholder = "номер";
    num.className = "req-num";
    num.value = suggestNumber(taken);
    taken.add(num.value);
    const ok = document.createElement("button");
    ok.className = "btn primary";
    ok.textContent = "Принять";
    ok.onclick = async () => {
      const number = num.value.trim();
      if (!number) { num.focus(); return; }
      if (state.laptops.some((l) => l.number === number) &&
          !confirm(`Номер ${number} уже занят другим ноутбуком. Заменить (старый ноутбук отключится)?`)) return;
      try { await api("POST", `/ui/api/pair-requests/${r.id}/approve`, { number }); log(`Ноутбук ${number} подключён (${r.hostname})`); }
      catch (e) { log(e.message, true); }
    };
    num.onkeydown = (e) => { if (e.key === "Enter") ok.click(); };
    const no = document.createElement("button");
    no.className = "btn";
    no.textContent = "Отклонить";
    no.onclick = async () => {
      try { await api("POST", `/ui/api/pair-requests/${r.id}/reject`); } catch (e) { log(e.message, true); }
    };
    row.append(code, info, num, ok, no);
    box.append(row);
  }
}

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
    if (m.kind === "state") { state = m; render(); renderRequests(); }
    else if (m.kind === "result") {
      log(`Ноутбук ${m.number}: ${m.ok ? "выполнено" : "ошибка"}${m.message ? " — " + m.message : ""}`, !m.ok);
    }
  };
}
// Обновляем «был N мин назад» раз в 30 секунд
setInterval(render, 30000);
connect();
