// Вкладка «Проекты» (неразобранное, группы и ученики, Яндекс Диск),
// настройки сбора и Диска, окно «Конец урока».
"use strict";
(function () {
  const { api, el, log, $ } = App;
  let proj = { groups: [], unsorted: [], recent: [], active_group: null, root: "" };
  let disk = { has_token: false, state: "", counts: {}, items: [] };

  // ---------- Загрузка данных (с задержкой, чтобы не дёргать сервер на каждое событие) ----------
  let reloadTimer = null;
  function reload() {
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(async () => {
      try {
        [proj, disk] = await Promise.all([api("GET", "/ui/api/projects"), api("GET", "/ui/api/disk")]);
        renderProjects();
        renderDiskSettings();
      } catch (e) { console.error(e); }
    }, 250);
  }

  const fmtSize = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + " МБ" : Math.max(1, Math.round(b / 1024)) + " КБ");
  const fmtTime = (ts) => new Date(ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  function allStudents() {
    const out = [];
    for (const g of proj.groups) for (const s of g.students) out.push({ ...s, group: g.name });
    return out;
  }

  // ---------- Назначить файл ученику ----------
  async function assign(file, studentId) {
    try {
      const r = await api("POST", `/ui/api/files/${file.id}/assign`, { student_id: studentId });
      log(`«${file.name}» → ${r.student}`);
      if (r.suggest_alias) {
        const alias = prompt(`Запомнить написание «${r.suggest_alias}» как вариант имени «${r.student}»?\n` +
          "Тогда такие файлы будут разбираться сами. Можно исправить слово:", r.suggest_alias);
        if (alias && alias.trim()) {
          const st = allStudents().find((s) => s.id === studentId);
          await api("PUT", `/ui/api/students/${studentId}`, { aliases: [...(st ? st.aliases : []), alias.trim()] });
          log(`Запомнено: «${alias.trim()}» — это ${r.student}`);
        }
      }
    } catch (e) { log(e.message, true); }
    reload();
  }

  function studentPicker(file) {
    const box = el("div", { className: "assign" });
    const active = proj.groups.find((g) => g.id === proj.active_group);
    const list = active ? active.students.map((s) => ({ ...s, group: active.name })) : allStudents();
    if (active && list.length && list.length <= 24) {
      // Один клик — кнопки с именами учеников текущей группы
      for (const s of list) {
        const b = el("button", { className: "btn small chip", textContent: s.name });
        b.onclick = () => assign(file, s.id);
        box.append(b);
      }
    } else if (list.length) {
      const sel = el("select", {}, el("option", { value: "" }, "— выберите ученика —"));
      for (const s of list) sel.append(el("option", { value: s.id }, `${s.name} (${s.group})`));
      sel.onchange = () => { if (sel.value) assign(file, Number(sel.value)); };
      box.append(sel);
    } else {
      box.append(el("span", { className: "muted small" }, "Сначала добавьте учеников ниже"));
    }
    return box;
  }

  const reveal = (id) => api("POST", `/ui/api/files/${id}/reveal`).catch((e) => log(e.message, true));
  const openFolder = (which) => api("POST", "/ui/api/open-folder", { which }).catch((e) => log(e.message, true));

  // ---------- Отрисовка вкладки ----------
  function renderProjects() {
    const root = $("projectsRoot");
    root.innerHTML = "";
    const badge = $("unsortedBadge");
    badge.hidden = !proj.unsorted.length;
    badge.textContent = proj.unsorted.length;

    // Папки
    const folders = el("section", { className: "panel" },
      el("h2", {}, "Папка проектов на Mac"),
      el("div", { className: "row" }, el("code", {}, proj.root), el("div", { className: "spacer" }),
        ...[["projects", "Проекты"], ["unsorted", "Неразобранное"], ["inbox", "Входящие"]].map(([w, t]) => {
          const b = el("button", { className: "btn small", textContent: "Открыть «" + t + "»" });
          b.onclick = () => openFolder(w);
          return b;
        })));
    root.append(folders);

    // Неразобранное
    const uns = el("section", { className: "panel" + (proj.unsorted.length ? " attention" : "") },
      el("h2", {}, `Неразобранное (${proj.unsorted.length})`));
    if (!proj.unsorted.length) uns.append(el("p", { className: "muted" }, "Всё разобрано 👍"));
    else uns.append(el("p", { className: "muted small" },
      "Эти файлы не удалось отнести к ученику (имя не найдено или подходит нескольким). Нажмите на имя ученика."));
    const sameName = {};
    for (const f of proj.unsorted) sameName[f.name] = (sameName[f.name] || 0) + 1;
    for (const f of proj.unsorted) {
      const show = el("button", { className: "btn small", textContent: "Показать", title: "Показать в Finder" });
      show.onclick = () => reveal(f.id);
      const del = el("button", { className: "btn small danger", textContent: "Удалить", title: "Это мусор — убрать в Корзину Mac" });
      del.onclick = async () => {
        if (sameName[f.name] > 1 && confirm(`Удалить ВСЕ файлы «${f.name}» из Неразобранного (${sameName[f.name]} шт.)?\nОни уйдут в Корзину Mac.`)) {
          const r = await api("POST", "/ui/api/files/delete-unsorted-by-name", { name: f.name });
          log(`Удалено в Корзину: ${r.deleted} × «${f.name}»`);
        } else if (sameName[f.name] === 1 && confirm(`Удалить «${f.name}»? Файл уйдёт в Корзину Mac.`)) {
          await api("POST", `/ui/api/files/${f.id}/delete`);
          log(`«${f.name}» удалён в Корзину`);
        } else return;
        reload();
      };
      const ign = el("button", { className: "btn small", textContent: "Игнорировать такие",
        title: "Больше никогда не принимать такие файлы (например, журналы программ)" });
      ign.onclick = async () => {
        const ext = f.name.includes(".") ? "*." + f.name.split(".").pop().toLowerCase() : f.name.toLowerCase();
        const pattern = prompt("Больше не принимать файлы по шаблону (можно исправить):\n" +
          "*.dpapi — все файлы с таким расширением; agent-errors.dpapi — только это имя; logs/ — всё из папки logs", ext);
        if (!pattern) return;
        try {
          const r = await api("POST", "/ui/api/ignore", { pattern });
          log(`Игнорируются файлы «${r.pattern}». Убрано из Неразобранного: ${r.removed}`);
        } catch (e) { alert(e.message); }
        reload();
      };
      uns.append(el("div", { className: "file-row" },
        el("div", { className: "file-info" }, el("b", {}, f.name),
          el("div", { className: "muted small" }, `ноутбук ${f.laptop} · ${fmtTime(f.received_at)} · ${fmtSize(f.size)}` +
            (sameName[f.name] > 1 ? ` · таких файлов: ${sameName[f.name]}` : ""))),
        studentPicker(f), show, ign, del));
    }
    root.append(uns);

    // Группы и ученики
    root.append(renderGroups());

    // Яндекс Диск — кратко
    const c = disk.counts || {};
    const stateText = {
      no_token: "не подключён (см. «Настройки»)", auth_error: "ошибка токена — получите новый в «Настройках»",
      offline: "нет интернета — файлы ждут в очереди", working: "выгружаю…", idle: "работает",
    }[disk.state] || disk.state;
    const retry = el("button", { className: "btn small", textContent: "Повторить сейчас" });
    retry.onclick = () => api("POST", "/ui/api/disk/retry").then(reload);
    const dsk = el("section", { className: "panel" }, el("h2", {}, "Яндекс Диск"),
      el("div", { className: "row" }, el("span", {}, "Состояние: " + stateText),
        el("span", { className: "muted" }, ` · выгружено ${c.done || 0}, в очереди ${c.pending || 0}`),
        el("div", { className: "spacer" }), retry));
    const failed = (disk.items || []).filter((i) => i.status !== "done" && i.last_error);
    for (const i of failed.slice(0, 20)) {
      dsk.append(el("div", { className: "muted small" }, `${i.remote_path}: ${i.last_error} (попыток ${i.attempts})`));
    }
    root.append(dsk);

    // Последние полученные
    const rec = el("section", { className: "panel" }, el("h2", {}, "Последние полученные файлы"));
    if (!proj.recent.length) rec.append(el("p", { className: "muted" }, "Пока ничего не получено."));
    for (const f of proj.recent.slice(0, 50)) {
      const where = f.status === "sorted" ? `${f.group_name} / ${f.student}` : f.status === "unsorted" ? "Неразобранное" : "…";
      const row = el("div", { className: "file-row small" },
        el("span", { className: "muted" }, fmtTime(f.received_at)), el("span", {}, `ноут ${f.laptop}`),
        el("b", { className: "file-info" }, f.name), el("span", {}, "→ " + where));
      row.ondblclick = () => reveal(f.id);
      row.title = "Двойной щелчок — показать в Finder";
      rec.append(row);
    }
    root.append(rec);
  }

  function renderGroups() {
    const sec = el("section", { className: "panel" }, el("h2", {}, "Группы и ученики"));
    // Текущая группа
    const sel = el("select", {}, el("option", { value: "" }, "все группы"));
    for (const g of proj.groups) sel.append(el("option", { value: g.id }, g.name));
    sel.value = proj.active_group || "";
    sel.onchange = async () => {
      try { await api("PUT", "/ui/api/settings", { active_group: sel.value ? Number(sel.value) : null }); log("Текущая группа сохранена"); }
      catch (e) { log(e.message, true); }
      reload();
    };
    sec.append(el("div", { className: "row" }, el("b", {}, "Сейчас занимается:"), sel,
      el("span", { className: "muted small" }, "файлы сортируются только по ученикам этой группы")));

    for (const g of proj.groups) {
      const box = el("div", { className: "group" + (g.id === proj.active_group ? " active" : "") });
      const ren = el("button", { className: "btn small", textContent: "Переименовать" });
      ren.onclick = async () => {
        const name = prompt("Новое название группы:", g.name);
        if (name) { try { await api("PUT", `/ui/api/groups/${g.id}`, { name }); } catch (e) { alert(e.message); } reload(); }
      };
      const del = el("button", { className: "btn small danger", textContent: "Удалить группу" });
      del.onclick = async () => {
        if (!confirm(`Удалить группу «${g.name}» и её список учеников? Файлы на диске останутся.`)) return;
        await api("DELETE", `/ui/api/groups/${g.id}`); reload();
      };
      box.append(el("div", { className: "row" }, el("h3", {}, g.name), el("span", { className: "muted small" }, `${g.students.length} уч.`),
        el("div", { className: "spacer" }), ren, del));
      const list = el("div", { className: "students" });
      for (const s of g.students) {
        const edit = el("button", { className: "btn small", textContent: "✎", title: "Изменить имя и варианты написания" });
        edit.onclick = async () => {
          const name = prompt("Имя ученика:", s.name);
          if (name == null) return;
          const al = prompt("Варианты написания через запятую (например: Masha, Машуня):", s.aliases.join(", "));
          if (al == null) return;
          try { await api("PUT", `/ui/api/students/${s.id}`, { name, aliases: al.split(",").map((x) => x.trim()).filter(Boolean) }); }
          catch (e) { alert(e.message); }
          reload();
        };
        const rm = el("button", { className: "btn small danger", textContent: "✕", title: "Удалить ученика" });
        rm.onclick = async () => { if (confirm(`Удалить ученика «${s.name}»?`)) { await api("DELETE", `/ui/api/students/${s.id}`); reload(); } };
        list.append(el("div", { className: "student" }, el("b", {}, s.name),
          s.aliases.length ? el("span", { className: "muted small" }, " (" + s.aliases.join(", ") + ")") : null, edit, rm));
      }
      box.append(list);
      const ta = el("textarea", { rows: 2, placeholder: "Добавить учеников: по одному в строке. Варианты написания — через запятую:\nМаша, Masha, Машуня\nСаша, Sasha, Александр" });
      const add = el("button", { className: "btn small primary", textContent: "Добавить" });
      add.onclick = async () => {
        if (!ta.value.trim()) return;
        try {
          const r = await api("POST", "/ui/api/students/bulk", { group_id: g.id, text: ta.value });
          log(`Добавлено учеников: ${r.added}`);
          if (r.errors.length) alert(r.errors.join("\n"));
        } catch (e) { alert(e.message); }
        reload();
      };
      box.append(el("div", { className: "row" }, ta, add));
      sec.append(box);
    }
    const gname = el("input", { type: "text", placeholder: "Название новой группы (например, Пн 10.00)" });
    const gadd = el("button", { className: "btn primary", textContent: "+ Группа" });
    gadd.onclick = async () => {
      if (!gname.value.trim()) return;
      try { await api("POST", "/ui/api/groups", { name: gname.value }); } catch (e) { alert(e.message); }
      reload();
    };
    sec.append(el("div", { className: "row" }, gname, gadd));
    return sec;
  }

  // ---------- Настройки: сбор файлов и Яндекс Диск ----------
  const BASES = { downloads: "Загрузки", desktop: "Рабочий стол", documents: "Документы" };
  let folderDraft = null;

  function renderCollectSettings(state) {
    const st = state.settings || {};
    if (!st.watch_folders) return;
    let sec = $("collectSettings");
    if (!sec) {
      sec = el("section", { className: "panel", id: "collectSettings" });
      $("settingsExtra").append(sec);
    } else if (folderDraft && JSON.stringify(st.ignore_patterns || []) === sec.dataset.ign) return;  // не мешаем редактированию
    sec.dataset.ign = JSON.stringify(st.ignore_patterns || []);
    folderDraft = st.watch_folders.map((f) => ({ ...f }));
    const maxMb = el("input", { type: "number", min: 1, max: 4096, value: st.max_file_mb, style: "width:90px" });
    const rootIn = el("input", { type: "text", value: st.projects_root || "", style: "flex:1" });
    const list = el("div");
    const draw = () => {
      list.innerHTML = "";
      folderDraft.forEach((f, i) => {
        const base = el("select", {});
        for (const [k, t] of Object.entries(BASES)) base.append(el("option", { value: k }, t));
        base.value = f.base;
        base.onchange = () => (f.base = base.value);
        const sub = el("input", { type: "text", value: f.sub, placeholder: "(сама папка) или подпапка, например Проекты" });
        sub.oninput = () => (f.sub = sub.value);
        const del = el("button", { className: "btn small danger", textContent: "✕" });
        del.onclick = () => { folderDraft.splice(i, 1); draw(); };
        list.append(el("div", { className: "row" }, base, el("span", {}, "\\"), sub, del));
      });
    };
    draw();
    const addF = el("button", { className: "btn small", textContent: "+ Папка" });
    addF.onclick = () => { folderDraft.push({ base: "desktop", sub: "" }); draw(); };
    const save = el("button", { className: "btn primary", textContent: "Сохранить" });
    save.onclick = async () => {
      try {
        await api("PUT", "/ui/api/settings", { watch_folders: folderDraft, max_file_mb: Number(maxMb.value), projects_root: rootIn.value });
        log("Настройки сбора сохранены и отправлены на ноутбуки");
        folderDraft = null;
      } catch (e) { alert(e.message); }
    };
    sec.innerHTML = "";
    sec.append(el("h2", {}, "Сбор проектов"),
      el("p", { className: "muted small" }, "Какие папки на ноутбуках отслеживать (вместе с вложенными папками). Подпапки создаются автоматически."),
      list, el("div", { className: "row" }, addF),
      el("div", { className: "row" }, el("span", {}, "Не отправлять файлы больше"), maxMb, el("span", {}, "МБ")),
      ignoreList(st),
      materialsDest(st),
      el("div", { className: "row" }, el("span", {}, "Папка проектов на Mac:"), rootIn),
      el("p", { className: "muted small" }, "Пусто — Документы/Компьютерный класс."),
      el("div", { className: "row" }, el("div", { className: "spacer" }), save));
  }

  function materialsDest(st) {
    const d = st.materials_dest || { base: "desktop", sub: "Материалы" };
    const base = el("select", {});
    for (const [k, t] of Object.entries(BASES)) base.append(el("option", { value: k }, t));
    base.value = d.base;
    const sub = el("input", { type: "text", value: d.sub, placeholder: "например, Материалы" });
    const save = el("button", { className: "btn small", textContent: "Сохранить" });
    save.onclick = async () => {
      try { await api("PUT", "/ui/api/settings", { materials_dest: { base: base.value, sub: sub.value } }); log("Папка материалов сохранена"); folderDraft = null; }
      catch (e) { alert(e.message); }
    };
    return el("div", {}, el("div", { className: "row" }, el("b", {}, "Материалы урока на ноутбуках:"), base, el("span", {}, "\\"), sub, save),
      el("p", { className: "muted small" }, "Эта папка не собирается как проекты и не очищается «Концом урока»."));
  }

  function ignoreList(st) {
    const box = el("div", {}, el("div", { className: "row" }, el("b", {}, "Не принимать файлы:"),
      el("span", { className: "muted small" }, "кроме встроенных правил (журналы, кэш, недокачанные файлы)")));
    const pats = st.ignore_patterns || [];
    if (!pats.length) box.append(el("div", { className: "muted small" }, "Своих правил нет. Их добавляет кнопка «Игнорировать такие» в «Неразобранном»."));
    const chips = el("div", { className: "students" });
    for (const p of pats) {
      const rm = el("button", { className: "btn small danger", textContent: "✕", title: "Снова принимать такие файлы" });
      rm.onclick = async () => {
        try { await api("PUT", "/ui/api/settings", { ignore_patterns: pats.filter((x) => x !== p) }); log(`Правило «${p}» удалено`); folderDraft = null; }
        catch (e) { alert(e.message); }
      };
      chips.append(el("div", { className: "student" }, el("code", {}, p), rm));
    }
    box.append(chips);
    return box;
  }

  function renderDiskSettings() {
    let sec = $("diskSettings");
    if (!sec) {
      sec = el("section", { className: "panel", id: "diskSettings" });
      $("settingsExtra").append(sec);
    }
    if (sec.contains(document.activeElement)) return;  // учитель вводит данные — не перерисовываем
    const st = App.state.settings || {};
    const cid = el("input", { type: "text", value: disk.client_id || "", placeholder: "ClientID (32 символа)", style: "flex:1" });
    const openAuth = el("button", { className: "btn", textContent: "Получить токен" });
    openAuth.onclick = async () => {
      const id = cid.value.trim();
      if (!/^[0-9a-fA-F]{32}$/.test(id)) { alert("Сначала вставьте ClientID (шаг 3)"); return; }
      try {
        await api("PUT", "/ui/api/settings", { disk_client_id: id });
        await api("POST", "/ui/api/open-url", { url: `https://oauth.yandex.ru/authorize?response_type=token&client_id=${id}` });
      } catch (e) { alert(e.message); }
    };
    const createApp = el("button", { className: "btn", textContent: "Открыть oauth.yandex.ru" });
    createApp.onclick = () => api("POST", "/ui/api/open-url", { url: "https://oauth.yandex.ru/client/new" }).catch((e) => alert(e.message));
    const tok = el("input", { type: "password", placeholder: disk.has_token ? "токен сохранён в Связке ключей" : "вставьте токен", style: "flex:1" });
    const saveTok = el("button", { className: "btn primary", textContent: "Сохранить токен" });
    const result = el("div", { className: "muted" });
    saveTok.onclick = async () => {
      try { const r = await api("POST", "/ui/api/disk/token", { token: tok.value }); result.textContent = r.message; tok.value = ""; }
      catch (e) { result.textContent = e.message; }
      reload();
    };
    const check = el("button", { className: "btn", textContent: "Проверить подключение" });
    check.onclick = async () => {
      result.textContent = "проверяю…";
      try { const r = await api("POST", "/ui/api/disk/check"); result.textContent = (r.ok ? "✅ " : "❌ ") + r.message; }
      catch (e) { result.textContent = e.message; }
    };
    const delTok = el("button", { className: "btn small danger", textContent: "Удалить токен" });
    delTok.onclick = async () => { if (confirm("Удалить токен Яндекс Диска с этого Mac?")) { await api("DELETE", "/ui/api/disk/token"); reload(); } };
    const rootName = el("input", { type: "text", value: st.disk_root || "Компьютерный класс", style: "flex:1" });
    const saveRoot = el("button", { className: "btn small", textContent: "Сохранить" });
    saveRoot.onclick = () => api("PUT", "/ui/api/settings", { disk_root: rootName.value }).then(() => log("Папка на Диске сохранена")).catch((e) => alert(e.message));

    sec.innerHTML = "";
    sec.append(el("h2", {}, "Яндекс Диск"),
      el("p", {}, disk.has_token ? "✅ Токен сохранён в Связке ключей Mac." : "Токен ещё не введён — проекты сохраняются только на Mac."),
      el("ol", { className: "steps" },
        el("li", {}, "Войдите в браузере в Яндекс-аккаунт, на чей Диск складывать проекты."),
        el("li", {}, "Нажмите ", createApp, ". Создайте приложение: название «Компьютерный класс», платформа «Веб-сервисы», Redirect URI: ",
          el("code", {}, "https://oauth.yandex.ru/verification_code"),
          ". В доступах отметьте «Яндекс Диск REST API → Запись в любом месте на Диске» (и, по желанию, «Доступ к информации о Диске» — для показа свободного места)."),
        el("li", {}, "Скопируйте ClientID приложения сюда: ", cid),
        el("li", {}, "Нажмите ", openAuth, ", разрешите доступ и скопируйте показанный токен."),
        el("li", {}, "Вставьте токен: ", tok, " ", saveTok)),
      el("div", { className: "row" }, check, delTok), result,
      el("div", { className: "row" }, el("span", {}, "Папка школы на Диске:"), rootName, saveRoot),
      el("p", { className: "muted small" }, "На Диске: <папка школы>/<группа>/<ученик>/<дата>/. На Диск попадают только разобранные по ученикам файлы."));
  }

  // ---------- «Конец урока» ----------
  const modal = el("div", { className: "modal", hidden: true },
    el("div", { className: "modal-box wide" }, el("h2", {}, "Конец урока"), el("div", { id: "elProgress" }),
      el("div", { className: "row" }, el("div", { className: "spacer" }),
        Object.assign(el("button", { className: "btn primary", textContent: "Закрыть" }), { onclick: () => (modal.hidden = true) }))));
  document.body.append(modal);
  const STAGE_ICONS = { wait: "⏳", collect: "📥", cleanup: "🧹", done: "✅", warning: "⚠️", error: "❌", skip: "⏸" };

  function renderEndLesson(ev) {
    const box = $("elProgress");
    box.innerHTML = "";
    box.append(el("p", { className: "muted" }, ev.running ? "Идёт сбор… не выключайте ноутбуки." : "Завершено."));
    const nums = Object.keys(ev.progress).sort((a, b) => Number(a) - Number(b));
    for (const n of nums) {
      const p = ev.progress[n];
      box.append(el("div", { className: "el-row " + p.stage }, el("b", {}, `${STAGE_ICONS[p.stage] || ""} Ноутбук ${n}`), el("span", {}, p.text)));
    }
    if (!ev.running) {
      const bad = nums.filter((n) => !["done"].includes(ev.progress[n].stage));
      log(bad.length ? `Конец урока: на ноутбуках ${bad.join(", ")} очистка не выполнена` : "Конец урока: все ноутбуки собраны и очищены", bad.length > 0);
    }
  }

  $("btnEndLesson").onclick = async () => {
    const mode = document.querySelector('input[name="target"]:checked').value;
    const targets = mode === "all" ? "all" : [...document.querySelectorAll(".card.selected .num")].map((x) => x.textContent);
    if (Array.isArray(targets) && !targets.length) { log("Выберите ноутбуки", true); return; }
    if (!confirm("Конец урока:\n\n1) с ноутбуков будут собраны все оставшиеся проекты;\n2) после подтверждения получения КАЖДОГО файла " +
      "отслеживаемые папки будут очищены (файлы уйдут в корзину агента на 7 дней).\n\nЕсли какой-то файл не удалось получить — на этом ноутбуке ничего не удаляется.\n\nПродолжить?")) return;
    try {
      await api("POST", "/ui/api/end-lesson", { targets });
      modal.hidden = false;
    } catch (e) { log(e.message, true); }
  };

  // ---------- Подписки ----------
  const listener = (state) => renderCollectSettings(state);
  listener.onEvent = (m) => {
    if (["projects_changed", "file", "disk_changed"].includes(m.kind)) reload();
    if (m.kind === "file") log(`Получен файл от ноутбука ${m.number}: ${m.name}`);
    if (m.kind === "end_lesson") { modal.hidden = false; renderEndLesson(m); }
  };
  App.onState(listener);
  reload();
})();
