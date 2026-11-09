// «Что открыто»: окна программ и вкладки браузеров на ноутбуках. Можно закрыть любое окно или вкладку.
"use strict";
(() => {
  const { api, el, toast, dialog } = App;
  const REFRESH_MS = 10000;
  const APP_ICONS = { "chrome.exe": "🌐", "msedge.exe": "🌐", "browser.exe": "🌐", "firefox.exe": "🦊", "opera.exe": "🌐",
    "explorer.exe": "📁", "mspaint.exe": "🎨", "notepad.exe": "📝", "winword.exe": "📘", "powerpnt.exe": "📙", "excel.exe": "📗" };

  // Какие ноутбуки показывать: выбранные или все на связи
  function laptopsFor(numbers) {
    const all = App.state.laptops || [];
    if (numbers) return all.filter((l) => numbers.includes(l.number));
    const t = App.targets();
    return t === "all" ? all.filter((l) => l.online) : all.filter((l) => t.includes(l.number));
  }

  function openedDialog(numbers) {
    const laptops = laptopsFor(numbers);
    if (!laptops.length) { toast("Нет ноутбуков на связи", "error"); return; }
    const filter = el("input", { type: "search", placeholder: "Найти: например, youtube или игра", className: "ow-filter" });
    const refreshBtn = el("button", { className: "btn ghost small", textContent: "↻ Обновить" });
    const root = el("div", { className: "ow-root" });
    const sections = {};   // номер → {box, body, status, data, busy}

    for (const l of laptops) {
      const status = el("span", { className: "muted small" });
      const body = el("div", { className: "ow-body" });
      const box = el("div", { className: "ow-laptop" },
        el("div", { className: "ow-head" }, el("b", {}, `💻 ${l.number}`), el("span", { className: "muted small" }, l.username || ""), el("span", { className: "spacer" }), status),
        body);
      root.append(box);
      sections[l.number] = { l, box, body, status, data: null, busy: false };
    }

    const matches = (text) => !filter.value.trim() || text.toLowerCase().includes(filter.value.trim().toLowerCase());

    function render(n) {
      const s = sections[n];
      s.body.innerHTML = "";
      if (!s.data) return;
      const windows = s.data.windows || [];
      const shown = windows.filter((w) => matches(w.app + " " + w.title + " " + (w.tabs || []).map((t) => t.title + " " + t.url).join(" ")));
      if (!windows.length) { s.body.append(el("p", { className: "muted small" }, "Ничего не открыто — только рабочий стол.")); return; }
      if (!shown.length) { s.body.append(el("p", { className: "muted small" }, "Ничего не найдено.")); return; }
      for (const w of shown) s.body.append(windowRow(n, w));
      if (s.data.tabs_error) s.body.append(el("p", { className: "muted small" }, "⚠️ " + s.data.tabs_error));
    }

    function windowRow(n, w) {
      const close = el("button", { className: "btn red small", textContent: "✖ Закрыть", title: w.browser ? "Закрыть окно браузера со всеми вкладками" : "Закрыть программу (как крестиком)" });
      close.onclick = () => closeWindow(n, w, close);
      const head = el("div", { className: "ow-win" + (w.minimized ? " min" : "") },
        el("span", { className: "ow-ico" }, APP_ICONS[w.process] || "🪟"),
        el("div", { className: "ow-text" },
          el("b", {}, w.app || w.process),
          w.browser && w.tabs.length ? el("span", { className: "muted small" }, ` · вкладок: ${w.tabs.length}`) : null,
          w.minimized ? el("span", { className: "muted small" }, " · свёрнуто") : null,
          (!w.browser || !w.tabs.length) ? el("div", { className: "ow-title" }, w.title) : null),
        close);
      const wrap = el("div", { className: "ow-window" }, head);
      if (w.browser && w.tabs.length) {
        const list = el("div", { className: "ow-tabs" });
        const found = w.tabs.filter((t) => matches(t.title + " " + t.url + " " + w.app));
        const tabs = found.length ? found : w.tabs;
        for (const t of tabs) {
          const x = el("button", { className: "ow-x", textContent: "✖", title: "Закрыть вкладку" });
          x.onclick = () => closeTab(n, w, t, x);
          list.append(el("div", { className: "ow-tab" + (t.active ? " active" : "") },
            el("span", { className: "ow-dot", title: t.active ? "Эта вкладка сейчас на экране" : "" }, t.active ? "●" : ""),
            el("div", { className: "ow-text" }, el("div", { className: "ow-title" }, t.title || "(без названия)"), t.url ? el("div", { className: "ow-url" }, t.url) : null),
            x));
        }
        wrap.append(list);
      }
      return wrap;
    }

    async function load(n) {
      const s = sections[n];
      if (s.busy) return;
      s.busy = true;
      if (!s.data) s.status.textContent = "загружаю…";
      try {
        s.data = await api("GET", `/ui/api/laptops/${encodeURIComponent(n)}/open`);
        s.status.textContent = "обновлено " + new Date().toLocaleTimeString("ru-RU");
        s.box.classList.remove("err");
      } catch (e) {
        s.status.textContent = e.message;
        s.box.classList.add("err");
        if (!s.data) s.body.innerHTML = "";
      } finally { s.busy = false; }
      render(n);
    }
    const loadAll = () => Object.keys(sections).forEach(load);

    async function act(n, body, btn, title) {
      const row = btn.closest(".ow-tab, .ow-window");
      row.classList.add("closing");
      btn.disabled = true;
      try {
        const r = await api("POST", `/ui/api/laptops/${encodeURIComponent(n)}/close`, body);
        toast(`Ноутбук ${n}: ${r.message}`, "ok");
        return r;
      } catch (e) {
        toast(`Ноутбук ${n}: ${title} — ${e.message}`, "error");
        row.classList.remove("closing");
        btn.disabled = false;
        return null;
      } finally { setTimeout(() => load(n), 400); }
    }

    const closeTab = (n, w, t, btn) => act(n, { kind: "tab", window: w.id, title: t.title, index: t.index }, btn, "вкладка");

    async function closeWindow(n, w, btn) {
      const r = await act(n, { kind: "window", window: w.id }, btn, w.app);
      // Программа не закрылась (спрашивает «Сохранить?») — предложить завершить принудительно
      if (r && /не закрылась/.test(r.message)) {
        if (await App.confirm(`${w.app} на ноутбуке ${n} не закрывается — наверное, спрашивает, сохранить ли работу.\nЗавершить программу принудительно? Несохранённое пропадёт.`, { okText: "Завершить", danger: true })) {
          await act(n, { kind: "window", window: w.id, force: true }, btn, w.app);
        }
      }
    }

    filter.oninput = () => Object.keys(sections).forEach(render);
    refreshBtn.onclick = loadAll;
    const timer = setInterval(loadAll, REFRESH_MS);
    loadAll();
    dialog({
      title: "👁 Что открыто",
      wide: true,
      body: [
        el("div", { className: "ow-top" }, filter, refreshBtn),
        el("p", { className: "muted small" }, "Список обновляется сам каждые 10 секунд. ✖ закрывает окно или вкладку сразу, без вопросов ученику. Адрес сайта виден только у вкладки, которая сейчас на экране (●)."),
        root,
      ],
      onOpen: () => setTimeout(() => filter.focus(), 30),
    }).then(() => clearInterval(timer));
  }

  App.openedDialog = openedDialog;
})();
