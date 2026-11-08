// Вкладка «Проекты» (наполняется на этапах 3–6).
"use strict";
(function () {
  const { $ } = App;
  $("projectsRoot").innerHTML = '<section class="panel"><p class="muted">Сбор проектов появится на следующем этапе.</p></section>';
  $("btnEndLesson").onclick = () => App.log("«Конец урока» появится на этапе 6", true);
})();
