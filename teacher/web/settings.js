// Экран «Настройки»: слева разделы, справа — один раздел с понятными пояснениями.
"use strict";
(function () {
  const { api, el, $ } = App;
  const ok = (t) => App.toast(t, "ok");
  const fail = (t) => App.toast(t, "error");
  const save = (patch, text) => App.saveSettings(patch, text);
  const BASES = { downloads: "Загрузки", desktop: "Рабочий стол", documents: "Документы" };

  const SECTIONS = [
    ["laptops", "💻", "Ноутбуки"],
    ["collect", "📥", "Сбор работ"],
    ["materials", "📁", "Материалы"],
    ["sites", "🌐", "Сайты и заставка"],
    ["apps", "✖️", "Программы"],
    ["disk", "☁️", "Яндекс Диск"],
    ["updates", "🔄", "Обновления"],
    ["backups", "💾", "Резервные копии"],
  ];
  let current = "laptops";
  let dirty = false;   // учитель что-то вводит — не перерисовываем раздел под ним

  function renderNav() {
    const nav = $("settingsNav");
    nav.innerHTML = "";
    for (const [id, ic, title] of SECTIONS) {
      const b = el("button", { className: "side-item" + (id === current ? " active" : "") }, el("span", { className: "ic" }, ic), el("b", {}, title));
      b.onclick = () => openSection(id);
      nav.append(b);
    }
  }
  function openSection(id) { current = id; dirty = false; renderNav(); renderBody(); }
  App.openSettings = openSection;

  function renderBody() {
    const body = $("settingsBody");
    body.innerHTML = "";
    body.oninput = () => (dirty = true);
    ({ laptops, collect, materials, sites, apps, disk, updates, backups }[current])(body);
  }
  const head = (body, title, lead) => body.append(el("h2", {}, title), el("p", { className: "lead" }, lead));
  const group = (title, ...children) => el("div", { className: "set-group" }, title ? el("h3", {}, title) : null, ...children);

  // ---------- 💻 Ноутбуки ----------
  function laptops(body) {
    head(body, "💻 Ноутбуки", "Все подключённые ноутбуки. Здесь можно поменять номер (если перепутали наклейки) или отвязать ноутбук.");
    const st = App.state;
    const g = group(null);
    if (!st.laptops.length) g.append(el("p", { className: "muted" }, "Пока нет подключённых ноутбуков."));
    for (const l of st.laptops) {
      const ren = el("button", { className: "btn orange small", textContent: "Изменить номер" });
      ren.onclick = () => App.renameLaptop(l.number);
      const more = el("button", { className: "btn ghost small", textContent: "Подробнее" });
      more.onclick = () => App.laptopDialog(l);
      g.append(el("div", { className: "list-row" },
        el("span", { className: "dot" + (l.online ? " on" : "") }),
        el("b", { style: "font-family:var(--font-head);font-size:22px;min-width:44px" }, l.number),
        el("div", { className: "file-info" }, el("b", {}, l.username || l.hostname || "—"),
          el("span", { className: "muted small" }, `${l.hostname || ""} · агент ${l.agent_version || "—"}`)),
        ren, more));
    }
    body.append(g, group("Как подключить новый ноутбук",
      el("ol", { className: "steps" },
        el("li", {}, "На ноутбуке откройте в браузере ссылку: ", el("code", {}, "github.com/RuslanGiaurgiev/classroom/releases/latest/download/agent.exe")),
        el("li", {}, "Запустите скачанный agent.exe под учётной записью ученика."),
        el("li", {}, "На экране «Класс» появится «Новые ноутбуки» — сверьте число и нажмите «Принять».")))); 
  }

  // ---------- 📥 Сбор работ ----------
  function collect(body) {
    const st = App.state.settings || {};
    head(body, "📥 Сбор работ", "Откуда на ноутбуках забирать работы детей. Всё новое в этих папках (и во вложенных) само приходит на Mac.");
    const folders = (st.watch_folders || []).map((f) => ({ ...f }));
    const list = el("div");
    const draw = () => {
      list.innerHTML = "";
      folders.forEach((f, i) => {
        const base = el("select", {}, ...Object.entries(BASES).map(([k, t]) => el("option", { value: k, selected: k === f.base }, t)));
        base.onchange = () => (f.base = base.value);
        const sub = el("input", { type: "text", value: f.sub, placeholder: "вся папка" });
        sub.oninput = () => (f.sub = sub.value);
        const del = el("button", { className: "btn red small", textContent: "✕" });
        del.onclick = () => { folders.splice(i, 1); draw(); };
        list.append(el("div", { className: "list-row" }, base, el("b", {}, "\\"), sub, del));
      });
    };
    draw();
    const add = el("button", { className: "btn ghost small", textContent: "+ Папка" });
    add.onclick = () => { folders.push({ base: "desktop", sub: "" }); draw(); };
    const maxMb = el("input", { type: "number", min: 1, max: 4096, value: st.max_file_mb, style: "width:110px" });
    const root = el("input", { type: "text", value: (st.projects_root || "").includes("Компьютерный класс") ? "" : (st.projects_root || ""), placeholder: "Документы/Компьютерный класс", style: "flex:1" });
    const saveBtn = el("button", { className: "btn", textContent: "Сохранить" });
    saveBtn.onclick = async () => {
      if (await save({ watch_folders: folders, max_file_mb: Number(maxMb.value), projects_root: root.value }, "Сохранено и отправлено на ноутбуки")) { dirty = false; }
    };
    const ign = el("div");
    for (const p of st.ignore_patterns || []) {
      const rm = el("button", { className: "btn red small", textContent: "✕", title: "Снова принимать такие файлы" });
      rm.onclick = async () => { if (await save({ ignore_patterns: st.ignore_patterns.filter((x) => x !== p) }, `Правило «${p}» удалено`)) setTimeout(renderBody, 300); };
      ign.append(el("span", { className: "chip" }, el("code", {}, p), rm));
    }
    if (!(st.ignore_patterns || []).length) ign.append(el("p", { className: "muted small" }, "Своих правил нет. Их добавляет кнопка «Это мусор» в «Неразобранном»."));
    const openRoot = el("button", { className: "btn ghost small", textContent: "Открыть в Finder" });
    openRoot.onclick = () => api("POST", "/ui/api/open-folder", { which: "root" });
    body.append(
      group("Папки на ноутбуках", list, el("div", { className: "row", style: "margin-top:8px" }, add)),
      group("Не присылать", el("div", { className: "row" }, el("span", {}, "файлы больше"), maxMb, el("span", {}, "МБ")),
        el("p", { className: "muted small" }, "Журналы программ, кэш, недокачанные файлы не присылаются никогда. Свои правила:"), ign),
      group("Где хранить работы на Mac", el("div", { className: "row" }, root, openRoot),
        el("p", { className: "muted small" }, "Сейчас: " + (st.projects_root || "") + ". Пусто — Документы/Компьютерный класс.")),
      el("div", { className: "row", style: "margin-top:16px" }, el("div", { className: "spacer" }), saveBtn));
  }

  // ---------- 📁 Материалы ----------
  function materials(body) {
    const st = App.state.settings || {};
    const d = st.materials_dest || { base: "desktop", sub: "Материалы" };
    head(body, "📁 Материалы", "Куда на ноутбуках складывать файлы, которые вы отправляете кнопкой «Материалы». Эта папка не собирается как работы и не очищается «Концом урока».");
    const base = el("select", {}, ...Object.entries(BASES).map(([k, t]) => el("option", { value: k, selected: k === d.base }, t)));
    const sub = el("input", { type: "text", value: d.sub, placeholder: "Материалы" });
    const b = el("button", { className: "btn", textContent: "Сохранить" });
    b.onclick = async () => { if (await save({ materials_dest: { base: base.value, sub: sub.value } }, "Папка материалов сохранена")) dirty = false; };
    body.append(group("Папка на ноутбуках", el("div", { className: "row" }, base, el("b", {}, "\\"), sub, b)));
  }

  // ---------- 🌐 Сайты и заставка ----------
  function sites(body) {
    const st = App.state.settings || {};
    head(body, "🌐 Сайты и заставка", "Как открывается поиск и что написано на экране «Внимание».");
    const engine = el("select", {}, ...Object.entries(st.search_engines || {}).map(([k, t]) => el("option", { value: k, selected: k === st.search_engine }, t)));
    engine.onchange = () => save({ search_engine: engine.value }, "Поисковая система сохранена");
    const lock = el("input", { type: "text", value: st.lock_text || "Внимание на учителя!", maxLength: 200, style: "flex:1" });
    const lb = el("button", { className: "btn", textContent: "Сохранить" });
    lb.onclick = async () => { if (await save({ lock_text: lock.value }, "Текст заставки сохранён")) dirty = false; };
    const sets = (st.lesson_sets || []);
    body.append(
      group("Поиск", el("div", { className: "row" }, el("span", {}, "Искать в:"), engine),
        el("p", { className: "muted small" }, "Яндекс — с семейным фильтром, Google — с безопасным поиском.")),
      group("Экран «Внимание»", el("div", { className: "row" }, lock, lb)),
      group("Наборы урока", el("p", { className: "muted small" }, sets.length ? `Сохранено наборов: ${sets.length} (${sets.map((x) => x.name).join(", ")}). Открывать и менять — в окне «Сайт» на экране «Класс».` : "Наборов пока нет. Создаются в окне «Сайт» на экране «Класс».")));
  }

  // ---------- ✖️ Программы ----------
  function apps(body) {
    const st = App.state.settings || {};
    head(body, "✖️ Программы", "Какие программы можно закрыть кнопкой «Закрыть». Имя процесса видно в Диспетчере задач Windows → «Подробности» (например, chrome.exe).");
    const draft = (st.apps || []).map((a) => ({ ...a }));
    const list = el("div");
    const draw = () => {
      list.innerHTML = "";
      draft.forEach((a, i) => {
        const title = el("input", { type: "text", value: a.title, placeholder: "Название (как видит учитель)" });
        title.oninput = () => (a.title = title.value);
        const proc = el("input", { type: "text", value: a.process, placeholder: "имя.exe" });
        proc.oninput = () => (a.process = proc.value);
        const del = el("button", { className: "btn red small", textContent: "✕" });
        del.onclick = () => { draft.splice(i, 1); draw(); dirty = true; };
        list.append(el("div", { className: "list-row" }, title, proc, del));
      });
    };
    draw();
    const add = el("button", { className: "btn ghost small", textContent: "+ Программа" });
    add.onclick = () => { draft.push({ title: "", process: "" }); draw(); dirty = true; };
    const b = el("button", { className: "btn", textContent: "Сохранить" });
    b.onclick = async () => { if (await save({ apps: draft.filter((a) => a.process.trim()) }, "Список программ сохранён")) { dirty = false; setTimeout(renderBody, 300); } };
    body.append(group(null, list, el("div", { className: "row", style: "margin-top:8px" }, add, el("div", { className: "spacer" }), b)));
  }

  // ---------- ☁️ Яндекс Диск ----------
  async function disk(body) {
    head(body, "☁️ Яндекс Диск", "Работы урока отправляются в папки детей кнопкой «☁️ На Диск» (вкладка «Работы» или после «Конца урока»): Ребёнок / Программа / Урок N — тема (дата). Если интернета нет — ждут и уходят позже. С Диска ничего не удаляется.");
    let d = App.disk || {};
    try { d = await api("GET", "/ui/api/disk"); } catch (e) { /* покажем то, что есть */ }
    const st = App.state.settings || {};
    const c = d.counts || {};
    const stateText = { no_token: "не подключён", auth_error: "❌ токен не подходит — получите новый (шаги ниже)", offline: "нет интернета — работы ждут в очереди",
      working: "выгружаю…", idle: "✅ подключён" }[d.has_token ? d.state : "no_token"] || d.state;
    const result = el("p", { className: "muted" });
    const check = el("button", { className: "btn", textContent: "Проверить подключение" });
    check.onclick = async () => { result.textContent = "проверяю…"; try { const r = await api("POST", "/ui/api/disk/check"); result.textContent = (r.ok ? "✅ " : "❌ ") + r.message; } catch (e) { result.textContent = e.message; } };
    const retry = el("button", { className: "btn ghost", textContent: "Повторить выгрузку" });
    retry.onclick = () => api("POST", "/ui/api/disk/retry").then(() => ok("Выгрузка запущена"));
    const cid = el("input", { type: "text", value: d.client_id || "", placeholder: "ClientID (32 символа)", style: "flex:1;min-width:260px" });
    const tok = el("input", { type: "password", placeholder: d.has_token ? "токен сохранён — можно вставить новый" : "вставьте токен", style: "flex:1;min-width:260px" });
    const createApp = el("button", { className: "btn ghost small", textContent: "Открыть oauth.yandex.ru" });
    createApp.onclick = () => api("POST", "/ui/api/open-url", { url: "https://oauth.yandex.ru/client/new" }).catch((e) => fail(e.message));
    const getTok = el("button", { className: "btn orange small", textContent: "Получить токен" });
    getTok.onclick = async () => {
      const id = cid.value.trim();
      if (!/^[0-9a-fA-F]{32}$/.test(id)) { fail("Сначала вставьте ClientID (шаг 3)"); return; }
      try { await api("PUT", "/ui/api/settings", { disk_client_id: id }); await api("POST", "/ui/api/open-url", { url: `https://oauth.yandex.ru/authorize?response_type=token&client_id=${id}` }); }
      catch (e) { fail(e.message); }
    };
    const saveTok = el("button", { className: "btn small", textContent: "Сохранить токен" });
    saveTok.onclick = async () => {
      try { const r = await api("POST", "/ui/api/disk/token", { token: tok.value }); (r.ok ? ok : fail)(r.message); tok.value = ""; dirty = false; setTimeout(renderBody, 300); }
      catch (e) { fail(e.message); }
    };
    const delTok = el("button", { className: "btn red small", textContent: "Отключить Диск" });
    delTok.onclick = async () => { if (await App.confirm("Удалить токен Яндекс Диска с этого Mac?", { okText: "Удалить", danger: true })) { await api("DELETE", "/ui/api/disk/token"); setTimeout(renderBody, 300); } };
    const rootName = el("input", { type: "text", value: st.disk_root || "", placeholder: "например, Марьино/2026-2027", style: "flex:1" });
    const saveRoot = el("button", { className: "btn small", textContent: "Сохранить" });
    saveRoot.onclick = () => save({ disk_root: rootName.value }, "Папка на Диске сохранена").then(() => (dirty = false));
    const cleanLocal = el("input", { type: "checkbox", checked: st.disk_clean_local !== false });
    cleanLocal.onchange = () => save({ disk_clean_local: cleanLocal.checked }, cleanLocal.checked ? "Работы будут удаляться с Mac после отправки на Диск" : "Работы останутся на Mac");
    const errors = (d.items || []).filter((i) => i.status !== "done" && i.last_error).slice(0, 8);
    body.append(
      group("Состояние", el("p", {}, el("b", {}, stateText)), el("p", { className: "muted small" }, `Выгружено: ${c.done || 0} · в очереди: ${c.pending || 0}`),
        ...errors.map((i) => el("div", { className: "muted small" }, `${i.remote_path}: ${i.last_error}`)),
        el("div", { className: "row" }, check, c.pending ? retry : null, d.has_token ? delTok : null), result),
      group(d.has_token ? "Подключить заново" : "Как подключить (один раз, ~5 минут)",
        el("ol", { className: "steps" },
          el("li", {}, "Войдите в браузере в Яндекс-аккаунт, на чей Диск складывать работы."),
          el("li", {}, "Нажмите ", createApp, " и создайте приложение: название «Компьютерный класс», платформа «Веб-сервисы», Redirect URI: ",
            el("code", {}, "https://oauth.yandex.ru/verification_code"), ". В доступах отметьте «Яндекс Диск REST API» → «Чтение всего Диска» и «Запись в любом месте на Диске»."),
          el("li", {}, "Скопируйте ClientID приложения сюда: ", cid),
          el("li", {}, "Нажмите ", getTok, ", разрешите доступ и скопируйте показанный токен."),
          el("li", {}, "Вставьте токен: ", tok, " ", saveTok)),
        el("p", { className: "muted small" }, "Токен хранится только в Связке ключей этого Mac.")),
      group("Папка с папками детей на Диске", el("p", { className: "muted small" }, "Путь от корня Диска, через «/». Каждый учебный год — меняйте здесь."), el("div", { className: "row" }, rootName, saveRoot)),
      group("Место на Mac", el("label", { className: "check" }, cleanLocal, " Удалять работы с Mac, когда они уже на Диске"),
        el("p", { className: "muted small" }, "Удаляется только после проверки: контрольная сумма файла на Диске должна совпасть с полученным с ноутбука. В «Работах» такие файлы открываются кнопкой «☁️ На Диске». Неразобранное и неотправленное не трогается.")));
  }

  // ---------- ⟳ Обновления ----------
  function updates(body) {
    const st = App.state;
    const u = st.updates || {};
    head(body, "🔄 Обновления", "Агенты на ноутбуках обновляются сами — только в спокойный момент (нет заставки, сообщения, отсчёта и отправки файлов) и только версиями с цифровой подписью.");
    const versions = {};
    for (const l of st.laptops) if (l.agent_version) versions[l.agent_version] = (versions[l.agent_version] || 0) + 1;
    const outdated = st.laptops.filter((l) => l.outdated).map((l) => l.number);
    const auto = el("input", { type: "checkbox", checked: !!u.auto });
    auto.onchange = () => save({ auto_update: auto.checked }, auto.checked ? "Автообновление включено" : "Автообновление выключено");
    const check = el("button", { className: "btn ghost", textContent: "Проверить сейчас" });
    check.onclick = async () => {
      check.disabled = true;
      try { const r = await api("POST", "/ui/api/updates/check"); ok(r.latest ? `Последняя версия: ${r.latest}` : "Обновлений не найдено"); } catch (e) { fail(e.message); }
      check.disabled = false;
    };
    const push = el("button", { className: "btn", textContent: "Обновить ноутбуки сейчас" });
    push.onclick = async () => {
      push.disabled = true;
      try { const r = await api("POST", "/ui/api/updates/push", { targets: "all" }); const res = Object.entries(r.results);
        ok(res.length ? res.map(([n, m]) => `${n}: ${m}`).join("; ") : "Все ноутбуки уже обновлены"); } catch (e) { fail(e.message); }
      push.disabled = false;
    };
    body.append(
      group("Версии", el("p", {}, u.latest ? `Последняя версия агента: ${u.latest}` : "Новых версий пока не скачано."),
        el("p", { className: "muted small" }, "На ноутбуках: " + (Object.entries(versions).map(([v, n]) => `${v} — ${n} шт.`).join(", ") || "—")
          + (outdated.length ? `. Ждут обновления: ${outdated.join(", ")}` : "")),
        u.error ? el("p", { className: "error" }, u.error) : null),
      group("Управление", el("label", { className: "check" }, auto, el("span", {}, "Обновлять автоматически")),
        el("div", { className: "row", style: "margin-top:12px" }, check, push)));
  }

  // ---------- 💾 Резервные копии ----------
  async function backups(body) {
    head(body, "💾 Резервные копии", "Группы, ученики, история работ и настройки сохраняются автоматически: при каждом запуске приложения и каждые 3 часа. Хранятся последние 30 копий.");
    let info = { root: "", copies: [] };
    try { info = await api("GET", "/ui/api/backup"); } catch (e) { /* покажем пустой список */ }
    const now = el("button", { className: "btn", textContent: "Сделать копию сейчас" });
    now.onclick = async () => {
      now.disabled = true;
      try { await api("POST", "/ui/api/backup"); ok("Резервная копия сохранена"); setTimeout(renderBody, 300); } catch (e) { fail(e.message); }
      now.disabled = false;
    };
    const open = el("button", { className: "btn ghost", textContent: "Открыть папку" });
    open.onclick = () => api("POST", "/ui/api/open-folder", { which: "backups" });
    body.append(group(null, el("div", { className: "row" }, now, open),
      el("p", { className: "muted small" }, "Папка: " + info.root)),
      group("Последние копии", info.copies.length ? el("div", {}, ...info.copies.slice(0, 10).map((c) => el("div", { className: "small" }, "• " + c.replace("_", " в ").replace(/-(\d\d)-(\d\d)$/, ":$1:$2"))))
        : el("p", { className: "muted small" }, "Копий пока нет.")),
      group("Если что-то пропало", el("p", { className: "small" }, "Закройте приложение и напишите разработчику — данные восстанавливаются из любой копии за минуту. Сами копии ничего не перезаписывают.")));
  }

  // Перерисовка при изменениях (кроме момента, когда учитель что-то вводит)
  App.onState(() => {
    renderNav();
    const body = $("settingsBody");
    if (current !== "disk" && current !== "backups" && !dirty && !body.contains(document.activeElement)) renderBody();
  });
  App.onProjects = () => { if (current === "disk" && !dirty) renderBody(); };
  renderNav();
})();
