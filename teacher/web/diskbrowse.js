// «☁️ Яндекс Диск» на вкладке «Работы»: связь с Диском и папки детей —
// просмотр, удаление (в Корзину Диска), добавление файлов с Mac, новые папки.
"use strict";
(() => {
  const { api, el, toast } = App;
  const ok = (t) => toast(t, "ok");
  const fail = (t) => toast(t, "error");
  const fmtSize = (b) => (b == null ? "" : b > 1048576 ? (b / 1048576).toFixed(1) + " МБ" : Math.max(1, Math.round(b / 1024)) + " КБ");
  const fmtTime = (s) => (s ? new Date(s).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
  const ICON = { stl: "🧊", sb3: "🐱", hex: "🔌", png: "🖼️", jpg: "🖼️", jpeg: "🖼️", heic: "🖼️", mp4: "🎬", mov: "🎬", pdf: "📕", pptx: "📊", docx: "📄", zip: "🗜️" };
  const icon = (n) => ICON[(n.split(".").pop() || "").toLowerCase()] || "📄";
  const STATE = { no_token: "❌ не подключён", auth_error: "❌ токен не подходит", offline: "⚠️ нет интернета", working: "⏳ отправляю…", idle: "✅ подключён" };

  let path = "";          // путь внутри папки школы
  let filter = "";
  let box = null;

  function open(startPath) {
    if (startPath != null) { path = startPath; filter = ""; }
    box = el("section", { className: "box disk-box" });
    render();
    return box;
  }

  async function render() {
    if (!box) return;
    const target = box;
    let st = {};
    try { st = await api("GET", "/ui/api/disk"); } catch (e) { /* покажем, что есть */ }
    const c = st.counts || {};
    const check = el("button", { className: "btn ghost small", textContent: "🔌 Проверить связь" });
    check.onclick = async () => {
      check.disabled = true;
      try { const r = await api("POST", "/ui/api/disk/check"); (r.ok ? ok : fail)(r.message); } catch (e) { fail(e.message); }
      check.disabled = false; render();
    };
    const retry = el("button", { className: "btn orange small", textContent: `↻ Отправить ожидающие (${c.pending || 0})` });
    retry.onclick = () => api("POST", "/ui/api/disk/retry").then(() => { ok("Отправка запущена"); setTimeout(render, 1500); });
    const settings = el("button", { className: "btn ghost small", textContent: "⚙️ Настройки Диска" });
    settings.onclick = () => { App.showTab("settings"); if (App.openSettings) App.openSettings("disk"); };
    const head = el("div", { className: "box-title" }, el("h2", {}, "☁️ Яндекс Диск"), el("div", { className: "spacer" }), check, c.pending ? retry : null, settings);
    const status = el("p", { className: "small" }, el("b", {}, STATE[st.has_token ? st.state : "no_token"] || st.state || "…"),
      ` · отправлено ${c.done || 0}` + (c.pending ? ` · ждут ${c.pending}` : "") + (st.last_error && st.state !== "idle" ? ` · ${st.last_error}` : ""));
    const body = el("div", {}, el("p", { className: "muted" }, "Загружаю…"));
    target.replaceChildren(head, status, body);
    if (!st.has_token) { body.replaceChildren(el("p", {}, "Диск не подключён — «Настройки» → «Яндекс Диск».")); return; }
    await renderFolder(body);
  }

  async function renderFolder(body) {
    let data;
    try { data = await api("GET", "/ui/api/disk/ls?path=" + encodeURIComponent(path)); }
    catch (e) {
      const up = el("button", { className: "btn ghost small", textContent: "← В папку школы" });
      up.onclick = () => { path = ""; render(); };
      body.replaceChildren(...[el("p", { className: "error" }, "❌ " + e.message), path ? up : null].filter(Boolean));
      return;
    }
    // Хлебные крошки: Марьино/2026-2027 › Ребёнок › Программа › Урок
    const parts = data.path ? data.path.split("/") : [];
    const crumbs = el("div", { className: "crumbs" });
    const crumb = (title, p) => { const b = el("button", { className: "crumb", textContent: title }); b.onclick = () => { path = p; filter = ""; render(); }; return b; };
    crumbs.append(crumb("🏫 " + data.root, ""));
    parts.forEach((name, i) => crumbs.append(el("span", { className: "muted" }, " › "), crumb(name, parts.slice(0, i + 1).join("/"))));

    const search = el("input", { type: "search", placeholder: parts.length ? "Найти в папке" : "Найти ребёнка", value: filter, style: "flex:1;min-width:180px" });
    const mk = el("button", { className: "btn ghost small", textContent: "+ Папка" });
    mk.onclick = async () => {
      const name = await App.prompt("Новая папка", "", { label: `В «${data.path || data.root}»`, okText: "Создать", placeholder: "например, Урок 1" });
      if (!name) return;
      try { await api("POST", "/ui/api/disk/mkdir", { path: data.path, name }); ok(`Папка «${name}» создана`); render(); } catch (e) { fail(e.message); }
    };
    const fileIn = el("input", { type: "file", multiple: true, hidden: true });
    const upBtn = el("button", { className: "btn small", textContent: "⬆ Добавить файлы" });
    upBtn.onclick = () => fileIn.click();
    fileIn.onchange = async () => {
      if (!fileIn.files.length) return;
      const form = new FormData();
      for (const f of fileIn.files) form.append("file", f, f.name);
      upBtn.disabled = true; upBtn.textContent = "Загружаю…";
      try {
        const res = await fetch("/ui/api/disk/upload?path=" + encodeURIComponent(data.path), { method: "POST", body: form, headers: { "X-UI-Token": new URLSearchParams(location.search).get("t") || "" } });
        const r = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(r.error || `Ошибка ${res.status}`);
        ok(`Добавлено: ${r.names.join(", ")}`);
      } catch (e) { fail(e.message); }
      render();
    };
    const tools = el("div", { className: "row" }, search, mk, parts.length ? upBtn : null, fileIn);

    const list = el("div", { className: "disk-list" });
    const draw = () => {
      list.innerHTML = "";
      const q = filter.trim().toLowerCase().replace(/ё/g, "е");
      const items = data.items.filter((i) => !q || i.name.toLowerCase().replace(/ё/g, "е").includes(q));
      if (!items.length) list.append(el("p", { className: "muted" }, data.items.length ? "Ничего не найдено" : "Папка пустая"));
      for (const it of items) {
        const full = (data.path ? data.path + "/" : "") + it.name;
        const isDir = it.type === "dir";
        const name = el("button", { className: "disk-name", textContent: (isDir ? "📁 " : icon(it.name) + " ") + it.name, title: isDir ? "Открыть папку" : "Открыть файл" });
        name.onclick = async () => {
          if (isDir) { path = full; filter = ""; render(); return; }
          try { const r = await api("GET", "/ui/api/disk/link?path=" + encodeURIComponent(full)); window.open(r.href, "_blank"); } catch (e) { fail(e.message); }
        };
        const del = el("button", { className: "ow-x", textContent: "🗑", title: "Удалить (в Корзину Диска)" });
        del.onclick = async () => {
          const what = isDir ? `папку «${it.name}» со всем содержимым` : `файл «${it.name}»`;
          if (!(await App.confirm(`Удалить ${what}?\n\nОно попадёт в Корзину Яндекс Диска — оттуда можно восстановить.\nРаботы из удалённой папки можно будет отправить заново кнопкой «☁️ На Диск».`, { okText: "Удалить", danger: true }))) return;
          try { await api("POST", "/ui/api/disk/delete", { path: full }); ok(`Удалено: ${it.name}`); render(); } catch (e) { fail(e.message); }
        };
        list.append(el("div", { className: "disk-row" }, name,
          el("span", { className: "muted small" }, isDir ? "" : fmtSize(it.size)),
          el("span", { className: "muted small disk-time" }, fmtTime(it.modified)),
          parts.length || !isDir ? del : el("span")));   // папки детей целиком с главного уровня не удаляем случайно
      }
    };
    search.oninput = () => { filter = search.value; draw(); };
    draw();
    const hint = parts.length ? null : el("p", { className: "muted small" }, "Откройте папку ребёнка, чтобы посмотреть, удалить или добавить его работы.");
    body.replaceChildren(...[crumbs, tools, hint, list].filter(Boolean));
  }

  App.diskBrowser = open;
  App.diskBrowserRefresh = () => { if (box && document.body.contains(box)) render(); };
})();
