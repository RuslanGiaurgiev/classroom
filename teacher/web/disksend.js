// «☁️ На Диск»: отправка работ урока в папки детей на Яндекс Диске.
// Учитель выбирает программу, номер урока и тему, смотрит, куда что попадёт, и подтверждает новые папки.
"use strict";
(() => {
  const { api, el, toast } = App;
  const today = () => new Date().toLocaleDateString("sv-SE");   // ГГГГ-ММ-ДД по местному времени
  const ruDate = (d) => d.split("-").reverse().join(".");
  const safe = (s) => s.replace(/[\\/:*?"<>|\x00-\x1f]/g, "_").trim();

  const LESSON_RE = /^\s*урок\s*(\d{1,3})(?!\d)/i;
  // Куда у ребёнка попадёт урок: папка с таким же названием, иначе с тем же номером урока, иначе новая
  function resolveLessonDir(wanted, st) {
    const w = wanted.trim().toLowerCase();
    const same = (st.lesson_dirs || []).find((d) => d.trim().toLowerCase() === w);
    if (same) return { name: same, exists: true };
    const m = wanted.match(LESSON_RE);
    const byNum = m && st.lessons && st.lessons[String(Number(m[1]))];
    return byNum ? { name: byNum, exists: true } : { name: wanted.trim().replace(/\s+/g, " "), exists: false };
  }

  async function diskSendDialog(groupId, date) {
    const groups = (App.proj && App.proj.groups) || [];
    if (!groups.length) { toast("Сначала добавьте группы", "error"); return; }
    groupId = groupId || ((App.proj.active_groups || [])[0]) || groups[0].id;
    const gSel = el("select", {}, ...groups.map((g) => el("option", { value: g.id, textContent: g.name, selected: g.id === groupId })));
    const dIn = el("input", { type: "date", value: date || today() });
    const prog = el("input", { type: "text", placeholder: "например, Make Code" });
    prog.setAttribute("list", "dsPrograms");
    const progList = el("datalist", { id: "dsPrograms" });
    const lesson = el("input", { type: "text", placeholder: "например, Урок 1", maxLength: 100 });
    lesson.setAttribute("list", "dsTopics");
    const topic = lesson;   // одно поле: «тема» — это и есть название папки урока
    const topicList = el("datalist", { id: "dsTopics" });
    const info = el("p", { className: "muted small" });
    const list = el("div", { className: "ds-list" });
    const sendBtn = el("button", { className: "btn", textContent: "☁️ Отправить" });
    let plan = null, rows = [], loadSeq = 0, lessonTouched = false;

    const field = (label, input) => el("label", { className: "field" }, el("span", {}, label), input);

    async function load() {
      const seq = ++loadSeq;
      list.innerHTML = "";
      info.textContent = "Смотрю папки на Диске…";
      sendBtn.disabled = true;
      try {
        const q = new URLSearchParams({ group_id: gSel.value, date: dIn.value, program: prog.value.trim() });
        const p = await api("GET", "/ui/api/disk/plan?" + q);
        if (seq !== loadSeq) return;
        plan = p;
        progList.replaceChildren(...p.programs.map((n) => el("option", { value: n })));
        topicList.replaceChildren(...[...new Set([`Урок ${p.lesson_suggest}`, ...p.topics])].map((n) => el("option", { value: n })));
        if (!prog.value.trim() && p.group_program) { prog.value = p.group_program; return load(); }
        if (!lessonTouched && p.program) lesson.value = `Урок ${p.lesson_suggest}`;
        render();
      } catch (e) {
        if (seq !== loadSeq) return;
        info.textContent = "❌ " + e.message;
      }
    }

    function render() {
      list.innerHTML = "";
      rows = [];
      const p = plan;
      if (!p.students.length) { info.textContent = `За ${ruDate(p.date)} у группы «${p.group.name}» нет разобранных работ.`; return; }
      if (!p.program) { info.textContent = "Выберите программу — покажу, куда попадут работы."; }
      else info.textContent = `Папка на Диске: ${p.root}`;
      if (p.program && p.program !== prog.value.trim() && document.activeElement !== prog) prog.value = p.program;
      if (p.root_suspicious) {
        list.append(el("div", { className: "ds-alert" }, `⚠️ В папке «${p.root}» не нашлись папки большинства учеников. Возможно, в настройках указана не та папка (нужна та, где лежат папки детей, например «Марьино/2026-2027»). `,
          el("button", { className: "btn small orange", textContent: "Открыть настройки", onclick: () => { document.querySelector(".modal")._close(); App.showTab("settings"); if (App.openSettings) App.openSettings("disk"); } })));
      }
      for (const st of p.students) {
        const unsent = st.files.filter((f) => !f.sent);
        const row = { st, include: el("input", { type: "checkbox", checked: unsent.length > 0, disabled: !unsent.length }), folder: null, create: null };
        let where;
        if (st.status === "found") {
          row.folder = () => st.folder;
          where = el("span", {}, "📁 ", st.folder);
        } else if (st.status === "ambiguous") {
          const sel = el("select", {}, el("option", { value: "", textContent: "— какая папка? —" }), ...st.options.map((o) => el("option", { value: o, textContent: o })));
          row.folder = () => sel.value;
          where = el("span", { className: "ds-warn" }, "❓ Подходят несколько папок: ", sel);
        } else {
          const name = el("input", { type: "text", value: st.new_name, style: "width:200px" });
          const pick = el("select", { title: "Если папка ребёнка на Диске названа иначе" },
            el("option", { value: "", textContent: "или выбрать существующую…" }), ...p.folders.map((f) => el("option", { value: f, textContent: f })));
          row.create = el("input", { type: "checkbox", disabled: !!p.root_suspicious, title: p.root_suspicious ? "Сначала проверьте папку в настройках" : "" });
          const createLabel = el("label", {}, row.create, " создать ");
          row.folder = () => pick.value || name.value.trim();
          row.isNew = () => !pick.value;
          where = el("span", { className: "ds-warn" }, "🆕 Папки не нашёл. ", createLabel, name, pick);
          row.create.onchange = upd;
          pick.onchange = () => { name.disabled = createLabel.hidden = !!pick.value; upd(); };
        }
        const dest = el("div", { className: "ds-dest muted small" });
        row.dest = dest;
        const files = el("div", { className: "muted small" },
          unsent.length ? `${unsent.length} ${unsent.length === 1 ? "работа" : unsent.length < 5 ? "работы" : "работ"}: ${unsent.map((f) => f.name).join(", ")}` : "всё уже на Диске",
          st.files.length > unsent.length && unsent.length ? ` (ещё ${st.files.length - unsent.length} уже на Диске)` : "");
        row.include.onchange = upd;
        list.append(el("div", { className: "ds-row" + (unsent.length ? "" : " done") }, row.include,
          el("div", { className: "ds-main" }, el("b", {}, st.name), files, el("div", {}, where), dest)));
        rows.push(row);
      }
      upd();
    }

    function upd() {
      if (!plan) return;
      const wanted = lesson.value.trim();
      const bad = /[\\/:*?"<>|]/.test(wanted);
      let count = 0;
      for (const r of rows) {
        const folder = r.folder();
        const progPath = r.st.program_path || safe(prog.value.trim());
        const ld = resolveLessonDir(wanted, r.st);
        r.dest.textContent = plan.program && wanted ? `→ ${folder || "…"} / ${progPath} / ${ld.name}${ld.exists ? " (уже есть — добавлю туда)" : ""}` : "";
        const ready = r.include.checked && folder && (!r.create || !r.isNew() || r.create.checked);
        if (ready) count += r.st.files.filter((f) => !f.sent).length;
      }
      sendBtn.disabled = !count || !prog.value.trim() || !wanted || bad;
      lesson.classList.toggle("bad", bad);
      sendBtn.textContent = count ? `☁️ Отправить (${count})` : "☁️ Отправить";
    }

    async function send() {
      const isNew = (r) => r.create && r.isNew();
      const missingNoConfirm = rows.filter((r) => r.include.checked && isNew(r) && !r.create.checked);
      const chosen = rows.filter((r) => r.include.checked && r.folder() && (!isNew(r) || r.create.checked));
      const created = chosen.filter(isNew).map((r) => r.folder());
      let text = `Отправить работы на Диск?\nПрограмма: ${prog.value.trim()}, папка урока: «${lesson.value.trim()}».`;
      if (created.length) text += `\n\nБудут созданы новые папки детей:\n• ${created.join("\n• ")}`;
      if (missingNoConfirm.length) text += `\n\nНе отправляются (папка не подтверждена): ${missingNoConfirm.map((r) => r.st.name).join(", ")}`;
      if (!(await App.confirm(text, { okText: "Отправить", title: "☁️ На Диск" }))) return false;
      sendBtn.disabled = true;
      try {
        const r = await api("POST", "/ui/api/disk/send", {
          group_id: Number(gSel.value), date: dIn.value, program: prog.value.trim(), folder_name: lesson.value.trim(),
          students: chosen.map((r) => ({ student_id: r.st.student_id, folder: r.folder(), create: isNew(r) })),
        });
        toast(`На Диск: отправляю ${r.queued} ${r.queued === 1 ? "работу" : "работ"}` + (r.created.length ? `, новых папок: ${r.created.length}` : "") + (r.skipped ? `, уже были: ${r.skipped}` : ""), "ok");
        return true;
      } catch (e) { toast(e.message, "error"); sendBtn.disabled = false; return false; }
    }

    let progTimer = null;
    prog.oninput = () => { clearTimeout(progTimer); lessonTouched = false; progTimer = setTimeout(load, 500); };
    gSel.onchange = () => { prog.value = ""; lessonTouched = false; load(); };
    dIn.onchange = load;
    lesson.oninput = () => { lessonTouched = true; upd(); };

    const done = dialog();
    function dialog() {
      return App.dialog({
        title: "☁️ Работы на Яндекс Диск",
        wide: true,
        body: [
          el("div", { className: "ds-form" }, field("Группа", gSel), field("Дата урока", dIn), field("Программа", prog), field("Папка урока (тема)", lesson)),
          progList, topicList, info, list,
          el("p", { className: "muted small" }, "На Диске ничего не удаляется и не перезаписывается. Новые папки детей создаются только с вашей галочкой."),
        ],
        buttons: [{ text: "Отмена", value: null }, { text: "☁️ Отправить", kind: "primary", onClick: async () => send() }],
        onOpen: (overlay) => {
          // Заменяем стандартную кнопку на свою (с числом работ и блокировкой)
          const std = overlay.querySelector(".modal-actions .btn:last-child");
          sendBtn.onclick = async () => { if (await send()) overlay._close(true); };
          std.replaceWith(sendBtn);
        },
      });
    }
    load();
    return done;
  }

  App.diskSendDialog = diskSendDialog;
})();
