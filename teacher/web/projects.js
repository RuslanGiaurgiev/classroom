// Экран «Работы»: группы → ученики → их работы, разбор «Неразобранного»,
// выбор текущей группы на экране «Класс» и окно «Конец урока».
"use strict";
(function () {
  const { api, el, $ } = App;
  const ok = (t) => App.toast(t, "ok");
  const fail = (t) => App.toast(t, "error");
  const COLORS = ["var(--teal)", "var(--orange)", "var(--purple)", "var(--blue)", "var(--red)", "var(--mint)"];

  let proj = { groups: [], unsorted: [], recent: [], active_groups: [], root: "" };
  const activeIds = () => proj.active_groups || [];
  const activeGroups = () => proj.groups.filter((g) => activeIds().includes(g.id));
  let disk = { counts: {}, state: "", has_token: false };
  // Что открыто справа: {mode: "group"|"student"|"unsorted"|"recent", groupId, studentId}
  let view = { mode: "group", groupId: null, studentId: null };
  let diskStart = null;   // с какой папки открыть «Яндекс Диск» (null — где были)

  const fmtSize = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + " МБ" : Math.max(1, Math.round(b / 1024)) + " КБ");
  const fmtTime = (ts) => new Date(ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const fmtDate = (d) => new Date(d + "T12:00").toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
  const ext = (n) => (n.includes(".") ? n.split(".").pop().toLowerCase() : "");
  const ICON = { sb3: "🐱", sb2: "🐱", py: "🐍", png: "🖼️", jpg: "🖼️", jpeg: "🖼️", gif: "🖼️", pptx: "📊", ppt: "📊", docx: "📄", doc: "📄",
    pdf: "📕", mp4: "🎬", mov: "🎬", mp3: "🎵", wav: "🎵", zip: "🗜️", html: "🌐", txt: "📝", stl: "🧊", blend: "🧊" };
  const icon = (n) => ICON[ext(n)] || "📄";
  const plural = (n, one, few, many) => {
    const m10 = n % 10, m100 = n % 100;
    return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
  };

  // ---------- Данные ----------
  let timer = null;
  function reload() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        [proj, disk] = await Promise.all([api("GET", "/ui/api/projects"), api("GET", "/ui/api/disk")]);
        App.proj = proj;
        App.disk = disk;
        if (view.groupId == null || !proj.groups.some((g) => g.id === view.groupId)) view.groupId = activeIds()[0] || (proj.groups[0] || {}).id || null;
        render();
        renderGroupPick();
        if (App.onProjects) App.onProjects();
      } catch (e) { console.error(e); }
    }, 200);
  }
  App.reloadProjects = reload;

  // ---------- «Сейчас занимаются» на экране «Класс» (можно несколько групп) ----------
  function renderGroupPick() {
    const names = activeGroups().map((g) => g.name);
    $("groupPickText").textContent = !proj.groups.length ? "сначала добавьте группы" : names.length ? names.join(", ") : "выберите группы";
  }
  async function setActiveGroups(ids, quiet = false) {
    try {
      await api("PUT", "/ui/api/settings", { active_groups: ids });
      const names = proj.groups.filter((g) => ids.includes(g.id)).map((g) => g.name);
      if (!quiet) ok(names.length ? `Сейчас занимаются: ${names.join(", ")} — работы раскладываются по их ученикам` : "Группы урока не выбраны");
    } catch (e) { fail(e.message); }
    reload();
  }
  async function groupPickDialog() {
    if (!proj.groups.length) { App.showTab("projects"); importDialog(); return; }
    const chosen = new Set(activeIds());
    const grid = el("div", { className: "toggle-grid" });
    for (const g of proj.groups) {
      const t = el("button", { className: "toggle" + (chosen.has(g.id) ? " on" : "") }, g.name, el("small", {}, `${g.students.length} уч.`));
      t.onclick = () => { chosen.has(g.id) ? chosen.delete(g.id) : chosen.add(g.id); t.classList.toggle("on"); };
      grid.append(t);
    }
    const v = await App.dialog({
      title: "Кто сейчас на уроке?", wide: true,
      body: [el("p", { className: "muted small" }, "Отметьте одну или несколько групп (например, 6-1 и 6-1(2), если они занимаются вместе). Работы будут раскладываться по их ученикам."), grid],
      buttons: [{ text: "Снять все", value: "none" }, { text: "Отмена", value: null }, { text: "Готово", kind: "primary", value: "ok" }],
    });
    if (v === "ok") setActiveGroups([...chosen]);
    if (v === "none") setActiveGroups([]);
  }
  $("groupPick").onclick = groupPickDialog;

  // ---------- Импорт групп и учеников из папки ----------
  async function importDialog() {
    let r;
    try {
      App.toast("Выберите папку, где лежат работы по группам (например, «Проекты Детей» или папка Яндекс Диска)");
      r = await api("POST", "/ui/api/import/pick", {});
    } catch (e) { fail(e.message); return; }
    if (!r.path) return;
    if (!r.groups.length) {
      App.dialog({ title: "Группы не найдены", body: [el("p", {}, "В выбранной папке нет папок с номерами групп (например, «М6-1(2)» или «6-3»)."),
        el("p", { className: "muted small" }, "Выберите папку, внутри которой лежат папки групп.")], buttons: [{ text: "Понятно", kind: "primary", value: null }] });
      return;
    }
    const groups = r.groups.map((g) => ({ ...g, on: true, students: g.students.map((s) => ({ ...s })) }));
    const list = el("div", { style: "display:grid;gap:10px" });
    const draw = () => {
      list.innerHTML = "";
      for (const g of groups) {
        const on = el("input", { type: "checkbox", checked: g.on });
        on.onchange = () => { g.on = on.checked; draw(); };
        const gname = el("input", { type: "text", value: g.name, className: "gname" });
        gname.oninput = () => (g.name = gname.value);
        const kids = el("div", {});
        g.students.forEach((s, i) => {
          const rm = el("button", { textContent: "✕", title: "Убрать" });
          rm.onclick = () => { g.students.splice(i, 1); draw(); };
          kids.append(el("span", { className: "kid" + (s.exists ? " exists" : ""), title: s.exists ? "уже есть" : `найдено в ${s.files} файл.` }, s.name, rm));
        });
        const add = el("input", { type: "text", placeholder: "+ имя и Enter", style: "width:140px;padding:5px 10px" });
        add.onkeydown = (e) => { if (e.key === "Enter" && add.value.trim()) { g.students.push({ name: add.value.trim(), files: 0 }); draw(); } };
        kids.append(add);
        list.append(el("div", { className: "import-group" + (g.on ? "" : " off") },
          el("label", { className: "check" }, on, gname, el("span", { className: "muted small" }, g.exists ? "группа уже есть — добавятся только новые дети" : "новая группа")),
          kids));
      }
    };
    draw();
    const v = await App.dialog({
      title: "Импорт групп", wide: true,
      body: [el("p", { className: "muted small" }, `Папка: ${r.path}. Проверьте, что всё верно: уберите лишнее (✕), допишите недостающих детей, снимите галочку с ненужных групп.`), list],
      buttons: [{ text: "Отмена", value: null }, { text: "Создать", kind: "primary", value: "ok" }],
    });
    if (v !== "ok") return;
    try {
      const res = await api("POST", "/ui/api/import/apply", { groups: groups.filter((g) => g.on).map((g) => ({ name: g.name, students: g.students })) });
      ok(`Создано групп: ${res.groups}, добавлено детей: ${res.students}`);
      if (res.errors.length) fail(res.errors.join("; "));
    } catch (e) { fail(e.message); }
    reload();
  }
  App.importGroups = importDialog;

  // ---------- Действия с работами ----------
  const reveal = (id) => api("POST", `/ui/api/files/${id}/reveal`).catch((e) => fail(e.message));
  const openFile = (id) => api("POST", `/ui/api/files/${id}/open`).catch((e) => fail(e.message));
  const openFolder = (which) => api("POST", "/ui/api/open-folder", { which }).catch((e) => fail(e.message));

  function allStudents() {
    const out = [];
    for (const g of proj.groups) for (const s of g.students) out.push({ ...s, group: g.name });
    return out;
  }

  async function assign(file, studentId) {
    try {
      const r = await api("POST", `/ui/api/files/${file.id}/assign`, { student_id: studentId });
      ok(`«${file.name}» → ${r.student}`);
      if (r.suggest_alias) {
        const alias = await App.prompt(`Запомнить «${r.suggest_alias}»?`, r.suggest_alias,
          { label: `Тогда файлы с этим словом будут сами попадать к ученику «${r.student}». Можно исправить слово:`, okText: "Запомнить" });
        if (alias) {
          const st = allStudents().find((s) => s.id === studentId);
          await api("PUT", `/ui/api/students/${studentId}`, { aliases: [...(st ? st.aliases : []), alias] });
          ok(`Запомнено: «${alias}» — это ${r.student}`);
        }
      }
    } catch (e) { fail(e.message); }
    reload();
  }

  // Окно выбора ученика (для «Другому ученику» и «Выбрать ученика»)
  function pickStudent(title) {
    return new Promise((resolve) => {
      const list = el("div", {});
      const groups = proj.groups.filter((g) => g.students.length);
      if (!groups.length) list.append(el("p", { className: "muted" }, "Учеников пока нет. Добавьте их в разделе «Работы»."));
      for (const g of groups) {
        const row = el("div", { className: "assign", style: "margin-bottom:12px" });
        for (const s of g.students) {
          const b = el("button", { className: "btn ghost small", textContent: s.name });
          b.onclick = () => { b.closest(".modal")._close("picked"); resolve(s.id); };
          row.append(b);
        }
        list.append(el("div", { className: "eyebrow", style: "margin:6px 0" }, g.name), row);
      }
      App.dialog({ title, body: [list], buttons: [{ text: "Отмена", value: null }] }).then((v) => { if (v !== "picked") resolve(null); });
    });
  }

  // ---------- Отрисовка ----------
  function render() {
    const root = $("projectsRoot");
    root.innerHTML = "";
    const badge = $("unsortedBadge");
    badge.hidden = !proj.unsorted.length;
    badge.textContent = proj.unsorted.length;

    if (proj.unsorted.length && view.mode !== "unsorted") {
      const go = el("button", { className: "btn ghost big", textContent: "Разобрать" });
      go.onclick = () => { view.mode = "unsorted"; render(); };
      root.append(el("div", { className: "banner" }, el("div", { className: "ico" }, "📥"),
        el("div", {}, el("h2", {}, `${proj.unsorted.length} ${plural(proj.unsorted.length, "работа ждёт", "работы ждут", "работ ждут")} разбора`),
          el("div", {}, "Не удалось понять, чьи это работы. Нажмите «Разобрать» и укажите ученика.")),
        el("div", { className: "spacer" }), go));
    }
    const layout = el("div", { className: "proj-layout" });
    layout.append(renderSide(), renderMain());
    root.append(layout);
  }

  function renderSide() {
    const side = el("aside", { className: "side-list" }, el("div", { className: "eyebrow" }, "Группы"));
    for (const g of proj.groups) {
      const active = (view.mode === "group" || view.mode === "student") && view.groupId === g.id;
      const b = el("button", { className: "side-item" + (active ? " active" : "") + (activeIds().includes(g.id) ? " now" : "") },
        el("b", {}, g.name), el("span", { className: "cnt" }, String(g.students.length)));
      b.onclick = () => { view = { mode: "group", groupId: g.id, studentId: null }; render(); };
      side.append(b);
    }
    const add = el("button", { className: "btn ghost", textContent: "+ Новая группа" });
    add.onclick = newGroup;
    const imp = el("button", { className: "btn orange", textContent: "⬇ Импорт из папки", title: "Создать группы и детей по папке с работами" });
    imp.onclick = importDialog;
    side.append(add, imp, el("div", { className: "eyebrow", style: "margin-top:10px" }, "Ещё"));
    for (const [mode, ic, title, cnt] of [["unsorted", "📥", "Неразобранное", proj.unsorted.length], ["recent", "🕘", "Последние", null]]) {
      const b = el("button", { className: "side-item" + (view.mode === mode ? " active" : "") }, el("span", {}, ic), el("b", {}, title),
        cnt != null ? el("span", { className: "cnt" }, String(cnt)) : null);
      b.onclick = () => { view = { ...view, mode }; render(); };
      side.append(b);
    }
    const finder = el("button", { className: "side-item" }, el("span", {}, "📂"), el("b", {}, "Папка в Finder"));
    finder.onclick = () => openFolder("projects");
    side.append(finder);
    const c = disk.counts || {};
    const dtext = !disk.has_token ? "не подключён" : disk.state === "offline" ? `нет интернета, ждут ${c.pending || 0}`
      : disk.state === "auth_error" ? "ошибка токена" : disk.state === "working" ? `отправляю, осталось ${c.pending || 0}`
      : `отправлено ${c.done || 0}` + (c.pending ? `, в очереди ${c.pending}` : "");
    const dbtn = el("button", { className: "side-item" + (view.mode === "disk" ? " active" : ""), title: "Папки детей на Яндекс Диске, связь с Диском" },
      el("span", {}, "☁️"), el("span", { className: "small" }, el("b", {}, "Яндекс Диск"), el("br"), dtext));
    dbtn.onclick = () => { view = { ...view, mode: "disk" }; diskStart = ""; render(); };
    side.append(dbtn);
    return side;
  }

  function renderMain() {
    if (view.mode === "disk") { const b = App.diskBrowser(diskStart); diskStart = null; return b; }
    if (view.mode === "unsorted") return renderUnsorted();
    if (view.mode === "recent") return renderRecent();
    if (view.mode === "student") {
      const st = allStudents().find((s) => s.id === view.studentId);
      if (st) return renderStudent(st);
    }
    const g = proj.groups.find((x) => x.id === view.groupId);
    return g ? renderGroup(g) : renderNoGroups();
  }

  function renderNoGroups() {
    const imp = el("button", { className: "btn orange big", textContent: "⬇ Импорт из папки с работами" });
    imp.onclick = importDialog;
    const add = el("button", { className: "btn ghost big", textContent: "+ Создать вручную" });
    add.onclick = newGroup;
    return el("section", { className: "box empty" }, el("div", { className: "big" }, "👩‍🎓"),
      el("h2", {}, "Добавьте группы и учеников"),
      el("p", { className: "muted" }, "Быстрее всего — импорт: выберите папку, где работы лежат по группам (например, «Проекты Детей» с папками М6-1(2), М6-3…).", el("br"),
        "Приложение само найдёт группы и имена детей в названиях файлов, а вы проверите список."),
      el("div", { className: "row", style: "justify-content:center" }, imp, add));
  }

  function renderGroup(g) {
    const box = el("section", { className: "box" });
    const isNow = activeIds().includes(g.id);
    const now = el("button", { className: "btn " + (isNow ? "" : "orange") + " small", textContent: isNow ? "✓ На уроке сейчас" : "Отметить: на уроке сейчас",
      title: isNow ? "Нажмите, чтобы снять отметку" : "Можно отметить несколько групп" });
    now.onclick = () => setActiveGroups(isNow ? activeIds().filter((x) => x !== g.id) : [...activeIds(), g.id]);
    const ren = el("button", { className: "btn ghost small", textContent: "Переименовать" });
    ren.onclick = async () => {
      const name = await App.prompt("Новое название группы", g.name);
      if (name) { try { await api("PUT", `/ui/api/groups/${g.id}`, { name }); ok("Группа переименована"); } catch (e) { fail(e.message); } reload(); }
    };
    const del = el("button", { className: "btn red small", textContent: "Удалить" });
    del.onclick = async () => {
      if (!(await App.confirm(`Удалить группу «${g.name}» и её список учеников?\nРаботы на диске останутся.`, { okText: "Удалить", danger: true }))) return;
      await api("DELETE", `/ui/api/groups/${g.id}`); ok(`Группа «${g.name}» удалена`); view.groupId = null; reload();
    };
    const toDisk = el("button", { className: "btn small", textContent: "☁️ На Диск", title: "Отправить работы урока в папки детей на Яндекс Диске" });
    toDisk.onclick = () => App.diskSendDialog(g.id);
    box.append(el("div", { className: "box-title" }, el("h2", {}, g.name), now, el("div", { className: "spacer" }), toDisk, ren, del));

    const grid = el("div", { className: "students-grid" });
    g.students.forEach((s, i) => {
      const card = el("button", { className: "scard", style: `--c:${COLORS[i % COLORS.length]}` },
        el("div", { className: "ava" }, s.name.slice(0, 1).toUpperCase()),
        el("b", {}, s.name),
        s.aliases.length ? el("div", { className: "aka" }, s.aliases.join(", ")) : null,
        el("div", { className: "counts" }, s.files_today ? el("span", { className: "today" }, `+${s.files_today} сегодня`) : null,
          ` ${s.files_total} ${plural(s.files_total, "работа", "работы", "работ")}`));
      card.onclick = () => { view = { mode: "student", groupId: g.id, studentId: s.id }; render(); };
      grid.append(card);
    });
    const add = el("button", { className: "scard add" }, el("div", { style: "font-size:30px" }, "+"), "Добавить учеников");
    add.onclick = () => addStudents(g);
    grid.append(add);
    box.append(grid);
    return box;
  }

  async function addStudents(g) {
    const ta = el("textarea", { rows: 7, placeholder: "Маша, Masha, Машуня\nСаша, Sasha, Александр\nОля" });
    const v = await App.dialog({
      title: `Ученики группы «${g.name}»`,
      body: [el("p", { className: "muted small" }, "По одному ученику в строке. После запятой можно написать, как ребёнок подписывает файлы: латиницей, уменьшительно."), ta],
      buttons: [{ text: "Отмена", value: null }, { text: "Добавить", kind: "primary", value: "ok" }],
      onOpen: () => setTimeout(() => ta.focus(), 30),
    });
    if (v !== "ok" || !ta.value.trim()) return;
    try {
      const r = await api("POST", "/ui/api/students/bulk", { group_id: g.id, text: ta.value });
      ok(`Добавлено учеников: ${r.added}`);
      if (r.errors.length) fail(r.errors.join("; "));
    } catch (e) { fail(e.message); }
    reload();
  }

  async function newGroup() {
    const name = await App.prompt("Новая группа", "", { label: "Например: «Пн 10.00» или «Скретч младшие»", okText: "Создать" });
    if (!name) return;
    try {
      const r = await api("POST", "/ui/api/groups", { name });
      view = { mode: "group", groupId: r.id, studentId: null };
      ok(`Группа «${name}» создана — теперь добавьте учеников`);
    } catch (e) { fail(e.message); }
    reload();
  }

  function renderStudent(st) {
    const box = el("section", { className: "box" });
    const back = el("button", { className: "btn link", textContent: `← ${st.group}` });
    back.onclick = () => { view = { mode: "group", groupId: view.groupId, studentId: null }; render(); };
    const edit = el("button", { className: "btn ghost small", textContent: "Имя и варианты" });
    edit.onclick = async () => {
      const name = el("input", { type: "text", value: st.name });
      const aka = el("input", { type: "text", value: st.aliases.join(", "), placeholder: "Masha, Машуня" });
      const v = await App.dialog({ title: "Ученик", body: [el("label", { className: "field" }, el("span", {}, "Имя"), name),
        el("label", { className: "field" }, el("span", {}, "Как ещё подписывает файлы (через запятую)"), aka)],
        buttons: [{ text: "Отмена", value: null }, { text: "Сохранить", kind: "primary", value: "ok" }] });
      if (v !== "ok") return;
      try { await api("PUT", `/ui/api/students/${st.id}`, { name: name.value, aliases: aka.value.split(",").map((x) => x.trim()).filter(Boolean) }); ok("Сохранено"); }
      catch (e) { fail(e.message); }
      reload();
    };
    const onDisk = el("button", { className: "btn small", textContent: "☁️ Папка на Диске", title: "Посмотреть, удалить или добавить работы ребёнка на Яндекс Диске" });
    onDisk.onclick = async () => {
      try {
        const r = await api("GET", `/ui/api/disk/student-folder?student_id=${st.id}`);
        if (!r.folder) { fail(r.options && r.options.length ? `Подходят несколько папок: ${r.options.join(", ")} — откройте нужную в «Яндекс Диске»` : "Папку ребёнка на Диске не нашёл — она появится после первой отправки"); }
        view = { ...view, mode: "disk" }; diskStart = r.folder || ""; render();
      } catch (e) { fail(e.message); }
    };
    const del = el("button", { className: "btn red small", textContent: "Удалить ученика" });
    del.onclick = async () => {
      if (!(await App.confirm(`Удалить ученика «${st.name}» из списка? Его работы на диске останутся.`, { okText: "Удалить", danger: true }))) return;
      await api("DELETE", `/ui/api/students/${st.id}`); view.mode = "group"; ok("Ученик удалён"); reload();
    };
    box.append(back, el("div", { className: "box-title", style: "margin-top:8px" },
      el("div", { className: "scard", style: "--c:var(--teal);padding:0;outline:0;box-shadow:none;background:none" }, el("div", { className: "ava" }, st.name.slice(0, 1).toUpperCase())),
      el("div", {}, el("h2", {}, st.name), st.aliases.length ? el("div", { className: "muted small" }, "также подписывает: " + st.aliases.join(", ")) : null),
      el("div", { className: "spacer" }), onDisk, edit, del));
    const list = el("div", {}, el("p", { className: "muted" }, "Загрузка…"));
    box.append(list);
    api("GET", `/ui/api/students/${st.id}/files`).then((r) => {
      list.innerHTML = "";
      if (!r.files.length) { list.append(el("p", { className: "muted" }, "Работ пока нет.")); return; }
      let lastDate = null;
      for (const f of r.files) {
        if (f.lesson_date !== lastDate) { lastDate = f.lesson_date; list.append(el("div", { className: "eyebrow", style: "margin-top:14px" }, fmtDate(f.lesson_date))); }
        const open = el("button", { className: "btn small", textContent: "Открыть" });
        open.onclick = () => openFile(f.id);
        const show = el("button", { className: "btn ghost small", textContent: "В Finder" });
        show.onclick = () => reveal(f.id);
        const move = el("button", { className: "btn quiet small", textContent: "Другому ученику" });
        move.onclick = async () => { const sid = await pickStudent(`Чья работа «${f.name}»?`); if (sid) assign(f, sid); };
        list.append(el("div", { className: "file-row" }, el("span", { className: "file-ico" }, icon(f.name)),
          el("div", { className: "file-info" }, el("b", {}, f.name), el("span", { className: "muted small" }, `ноутбук ${f.laptop} · ${fmtTime(f.received_at)} · ${fmtSize(f.size)}`)),
          f.exists ? open : el("span", { className: "muted small" }, "файл удалён с Mac"), show, move));
      }
    }).catch((e) => fail(e.message));
    return box;
  }

  function renderUnsorted() {
    const box = el("section", { className: "box" }, el("div", { className: "box-title" }, el("h2", {}, "📥 Неразобранное"),
      el("span", { className: "muted small" }, "Нажмите на имя ученика — работа переедет в его папку")));
    if (!proj.unsorted.length) {
      box.append(el("div", { className: "empty" }, el("div", { className: "big" }, "👍"), el("p", {}, "Всё разобрано!")));
      return box;
    }
    const active = activeGroups();
    const sameName = {};
    for (const f of proj.unsorted) sameName[f.name] = (sameName[f.name] || 0) + 1;
    const wrap = el("div", { style: "display:grid;gap:16px" });
    for (const f of proj.unsorted) {
      const chips = el("div", { className: "assign" });
      const manyGroups = active.length > 1;
      for (const g of active) {
        if (manyGroups) chips.append(el("span", { className: "pill", style: "align-self:center" }, g.name));
        for (const s of g.students) {
          const b = el("button", { className: "btn small", textContent: s.name, title: g.name });
          b.onclick = () => assign(f, s.id);
          chips.append(b);
        }
      }
      const students = active.flatMap((g) => g.students);
      const other = el("button", { className: "btn ghost small", textContent: students.length ? "Другой ученик…" : "Выбрать ученика…" });
      other.onclick = async () => { const sid = await pickStudent(`Чья работа «${f.name}»?`); if (sid) assign(f, sid); };
      chips.append(other);
      const show = el("button", { className: "btn quiet small", textContent: "Посмотреть" });
      show.onclick = () => openFile(f.id);
      const ign = el("button", { className: "btn quiet small", textContent: "Это мусор", title: "Больше никогда не принимать такие файлы (например, журналы программ)" });
      ign.onclick = async () => {
        const e = f.name.includes(".") ? "*." + ext(f.name) : f.name.toLowerCase();
        const pattern = await App.prompt("Больше не принимать такие файлы", e,
          { label: "*.dpapi — все файлы с таким расширением; agent-errors.dpapi — только это имя; logs/ — всё из папки logs", okText: "Не принимать" });
        if (!pattern) return;
        try { const r = await api("POST", "/ui/api/ignore", { pattern }); ok(`Файлы «${r.pattern}» больше не принимаются. Убрано: ${r.removed}`); }
        catch (err) { fail(err.message); }
        reload();
      };
      const many = sameName[f.name] > 1;
      const del = el("button", { className: "btn red small", textContent: many ? `Удалить все (${sameName[f.name]})` : "Удалить" });
      del.onclick = async () => {
        if (!(await App.confirm(many ? `Удалить все файлы «${f.name}» (${sameName[f.name]} шт.)?\nОни уйдут в Корзину Mac.` : `Удалить «${f.name}»?\nФайл уйдёт в Корзину Mac.`, { okText: "Удалить", danger: true }))) return;
        try {
          if (many) await api("POST", "/ui/api/files/delete-unsorted-by-name", { name: f.name });
          else await api("POST", `/ui/api/files/${f.id}/delete`);
          ok("Удалено в Корзину");
        } catch (err) { fail(err.message); }
        reload();
      };
      wrap.append(el("div", { className: "unsorted-card" },
        el("div", { className: "head" }, el("span", { className: "file-ico" }, icon(f.name)),
          el("div", { className: "file-info" }, el("b", {}, f.name), el("span", { className: "muted small" }, `ноутбук ${f.laptop} · ${fmtTime(f.received_at)} · ${fmtSize(f.size)}`)),
          show, ign, del),
        el("div", { className: "muted small" }, active.length ? `Чья это работа? (${active.map((g) => g.name).join(", ")})` : "Чья это работа? (Отметьте на экране «Класс», какие группы сейчас на уроке, — здесь появятся кнопки с именами.)"),
        chips));
    }
    box.append(wrap);
    return box;
  }

  function renderRecent() {
    const box = el("section", { className: "box" }, el("div", { className: "box-title" }, el("h2", {}, "🕘 Последние работы")));
    if (!proj.recent.length) box.append(el("p", { className: "muted" }, "Пока ничего не получено."));
    for (const f of proj.recent.slice(0, 100)) {
      const where = f.status === "sorted" ? `${f.group_name} · ${f.student}` : f.status === "unsorted" ? "неразобранное" : f.status === "deleted" ? "удалено" : "…";
      const show = el("button", { className: "btn ghost small", textContent: "В Finder" });
      show.onclick = () => reveal(f.id);
      box.append(el("div", { className: "file-row" }, el("span", { className: "file-ico" }, icon(f.name)),
        el("div", { className: "file-info" }, el("b", {}, f.name), el("span", { className: "muted small" }, `ноутбук ${f.laptop} · ${fmtTime(f.received_at)}`)),
        el("span", { className: "pill" + (f.status === "unsorted" ? " info" : "") }, where), f.status !== "deleted" ? show : null));
    }
    return box;
  }

  // ---------- «Конец урока» ----------
  const STAGE = { wait: "⏳", collect: "📥", cleanup: "🧹", done: "✅", warning: "⚠️", error: "❌", skip: "⏸" };
  let elBox = null;
  function endLessonWindow() {
    if (elBox && document.body.contains(elBox)) return elBox;
    elBox = el("div", {}, el("p", { className: "muted" }, "Начинаю…"));
    App.dialog({ title: "🏁 Конец урока", wide: true, body: [elBox], buttons: [{ text: "Закрыть", kind: "primary", value: null }] });
    return elBox;
  }
  function renderEndLesson(ev) {
    const box = endLessonWindow();
    box.innerHTML = "";
    const nums = Object.keys(ev.progress).sort((a, b) => Number(a) - Number(b));
    const done = nums.filter((n) => ev.progress[n].stage === "done").length;
    box.append(el("p", {}, ev.running ? el("b", {}, `Собираю работы… готово ${done} из ${nums.length}. Не выключайте ноутбуки.`)
      : el("b", { className: done === nums.length ? "ok-text" : "error" }, done === nums.length ? "Готово! Все работы собраны, ноутбуки очищены." : `Готово ${done} из ${nums.length}. Причины — ниже.`)));
    for (const n of nums) {
      const p = ev.progress[n];
      box.append(el("div", { className: "el-row " + p.stage }, el("b", {}, `${STAGE[p.stage] || ""} Ноутбук ${n}`), el("span", {}, p.text)));
    }
    if (!ev.running) {
      const b = el("button", { className: "btn", textContent: "☁️ Отправить работы на Яндекс Диск" });
      b.onclick = () => { b.closest(".modal")._close(); App.diskSendDialog(); };
      box.append(el("div", { className: "row", style: "margin-top:12px" }, b));
    }
  }
  $("btnEndLesson").onclick = async () => {
    if (!App.state.laptops.length) { fail("Сначала подключите ноутбуки"); return; }
    const targets = App.targets();
    if (!(await App.confirm("Для: " + App.targetTitle() + "\n\n1) С ноутбуков соберутся все оставшиеся работы.\n2) Когда Mac подтвердит получение КАЖДОГО файла, папки на ноутбуках очистятся (файлы на 7 дней уйдут в корзину ноутбука).\n\nЕсли какой-то файл не удалось получить — на этом ноутбуке ничего не удаляется.",
      { title: "🏁 Конец урока", okText: "Начать" }))) return;
    try { await api("POST", "/ui/api/end-lesson", { targets }); endLessonWindow(); }
    catch (e) { fail(e.message); }
  };

  // ---------- Подписки ----------
  const listener = () => {};
  listener.onEvent = (m) => {
    if (["projects_changed", "file", "disk_changed"].includes(m.kind)) reload();
    if (m.kind === "file") App.log(`Получена работа от ноутбука ${m.number}: ${m.name}`);
    if (m.kind === "end_lesson") {
      renderEndLesson(m);
      if (!m.running) {
        const bad = Object.keys(m.progress).filter((n) => m.progress[n].stage !== "done");
        App.toast(bad.length ? `Конец урока: на ноутбуках ${bad.join(", ")} очистка не выполнена` : "Конец урока: все работы собраны, ноутбуки очищены", bad.length ? "error" : "ok");
      }
    }
  };
  App.onState(listener);
  reload();
})();
