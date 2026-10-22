// Выпадающие списки в стиле кабинета. Настоящий <select> остаётся в форме (он и отправляется),
// скрипт рисует поверх него кнопку и список. На телефонах и без скрипта работает системный.
(function () {
  if (window.matchMedia && window.matchMedia("(pointer: coarse)").matches) return;

  var SEARCH_FROM = 40;  // длинные списки (города) получают строку поиска
  var openBox = null;

  function norm(t) { return t.toLowerCase().replace(/ё/g, "е"); }

  function close(box, focusBtn) {
    if (!box) return;
    box.classList.remove("open");
    box.querySelector(".dd-btn").setAttribute("aria-expanded", "false");
    if (focusBtn) box.querySelector(".dd-btn").focus();
    if (openBox === box) openBox = null;
  }

  function build(select) {
    if (select.multiple || select.dataset.dd) return;
    select.dataset.dd = "1";

    var box = document.createElement("div");
    box.className = "dd";
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "dd-btn";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    var pop = document.createElement("div");
    pop.className = "dd-pop";
    var search = null, empty = null;
    if (select.options.length > SEARCH_FROM) {
      search = document.createElement("input");
      search.type = "text";
      search.className = "dd-search";
      search.placeholder = "Начните вводить название";
      search.setAttribute("autocomplete", "off");
      search.setAttribute("aria-label", "Поиск по списку");
      pop.appendChild(search);
    }
    var list = document.createElement("ul");
    list.className = "dd-list";
    list.setAttribute("role", "listbox");
    list.tabIndex = -1;
    pop.appendChild(list);
    if (search) {
      empty = document.createElement("div");
      empty.className = "dd-empty";
      empty.textContent = "Ничего не найдено";
      empty.hidden = true;
      pop.appendChild(empty);
    }

    var items = [];
    Array.prototype.forEach.call(select.options, function (o, i) {
      var li = document.createElement("li");
      li.className = "dd-opt";
      li.setAttribute("role", "option");
      li.textContent = o.textContent;
      li.dataset.i = i;
      li.dataset.t = norm(o.textContent);
      li.addEventListener("mousedown", function (e) { e.preventDefault(); });
      li.addEventListener("click", function () { choose(i); close(box, true); });
      list.appendChild(li);
      items.push(li);
    });

    var active = select.selectedIndex;

    function paint() {
      btn.textContent = select.options[select.selectedIndex].textContent;
      items.forEach(function (li, i) {
        li.setAttribute("aria-selected", i === select.selectedIndex ? "true" : "false");
        li.classList.toggle("on", i === active);
      });
    }
    function choose(i) {
      if (select.selectedIndex !== i) {
        select.selectedIndex = i;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      active = i;
      paint();
    }
    function visible() { return items.filter(function (li) { return !li.hidden; }); }
    function show_(i) {
      active = i;
      paint();
      items[active].scrollIntoView({ block: "nearest" });
    }
    function step(dir) {
      var j = active + dir;
      while (j >= 0 && j < items.length && items[j].hidden) j += dir;
      if (j >= 0 && j < items.length) show_(j);
    }
    function edge(first) {
      var v = visible();
      if (v.length) show_(parseInt(v[first ? 0 : v.length - 1].dataset.i, 10));
    }
    function filter() {
      var q = norm(search.value.trim());
      items.forEach(function (li) { li.hidden = q !== "" && li.dataset.t.indexOf(q) === -1; });
      var v = visible();
      empty.hidden = v.length > 0;
      if (v.length && items[active].hidden) show_(parseInt(v[0].dataset.i, 10));
      else paint();
    }
    function open() {
      if (openBox && openBox !== box) close(openBox);
      if (search) { search.value = ""; items.forEach(function (li) { li.hidden = false; }); empty.hidden = true; }
      active = select.selectedIndex;
      paint();
      box.classList.add("open");
      btn.setAttribute("aria-expanded", "true");
      openBox = box;
      items[active].scrollIntoView({ block: "nearest" });
      if (search) search.focus();
    }

    function onKey(e) {
      var isOpen = box.classList.contains("open");
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!isOpen) open(); else step(e.key === "ArrowDown" ? 1 : -1);
      } else if (e.key === "Home" && isOpen && e.target === btn) { e.preventDefault(); edge(true); }
      else if (e.key === "End" && isOpen && e.target === btn) { e.preventDefault(); edge(false); }
      else if ((e.key === "Enter" || (e.key === " " && e.target === btn)) && isOpen) {
        e.preventDefault(); choose(active); close(box, true);
      } else if (e.key === "Escape" && isOpen) { e.preventDefault(); close(box, true); }
      else if (e.key === "Tab" && isOpen) { close(box); }
      else if (e.target === btn && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        if (search) { e.preventDefault(); if (!isOpen) open(); search.value = e.key; filter(); return; }
        var ch = norm(e.key);
        for (var k = 1; k <= items.length; k++) {
          var j = (active + k) % items.length;
          if (items[j].dataset.t.indexOf(ch) === 0) {
            if (!isOpen) open();
            show_(j);
            break;
          }
        }
      }
    }
    btn.addEventListener("click", function () {
      if (box.classList.contains("open")) close(box); else open();
    });
    btn.addEventListener("keydown", onKey);
    if (search) {
      search.addEventListener("input", filter);
      search.addEventListener("keydown", onKey);
    }

    select.parentNode.insertBefore(box, select);
    box.appendChild(btn);
    box.appendChild(pop);
    box.appendChild(select);
    select.classList.add("dd-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    var label = select.id && document.querySelector('label[for="' + select.id + '"]');
    if (label) {
      if (!label.id) label.id = select.id + "-label";
      list.setAttribute("aria-labelledby", label.id);
      btn.setAttribute("aria-labelledby", label.id + " " + (btn.id = select.id + "-btn"));
    }
    paint();
  }

  document.addEventListener("click", function (e) {
    if (openBox && !openBox.contains(e.target)) close(openBox);
  });

  Array.prototype.forEach.call(document.querySelectorAll("select"), build);
})();

// Предпросмотр выбранного файла-картинки до сохранения (data:-адрес: CSP не пускает blob:).
(function () {
  Array.prototype.forEach.call(document.querySelectorAll("input[type=file][data-preview]"), function (input) {
    var box = document.getElementById(input.dataset.preview);
    if (!box) return;
    var before = box.innerHTML;
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (!file) { box.innerHTML = before; return; }
      if (!/^image\//.test(file.type)) return;
      var max = parseInt(input.dataset.max || "0", 10);
      if (max && file.size > max) {
        input.value = "";
        box.innerHTML = "";
        var warn = document.createElement("span");
        warn.className = "error";
        warn.setAttribute("role", "alert");
        warn.textContent = "Файл " + Math.round(file.size / 1000) + " КБ: логотип должен быть не больше " + Math.round(max / 1000) + " КБ. Выберите файл поменьше.";
        box.appendChild(warn);
        return;
      }
      var reader = new FileReader();
      reader.onload = function () {
        var img = document.createElement("img");
        img.alt = "Новый логотип";
        img.src = reader.result;
        box.innerHTML = "";
        box.appendChild(img);
        var note = document.createElement("span");
        note.className = "muted small";
        note.textContent = "Предпросмотр. Нажмите «Сохранить», чтобы применить.";
        box.appendChild(note);
      };
      reader.readAsDataURL(file);
    });
  });
})();

// После ошибки формы страница открывается сверху: прокручиваем к сообщению.
(function () {
  var err = document.querySelector("form .error[role=alert]");
  if (err && err.scrollIntoView) err.scrollIntoView({ block: "center" });
})();

// «← Назад» ведёт в родительский раздел страницы. Если мы пришли оттуда же, возвращаемся по
// истории, чтобы не плодить записи; иначе просто идём по ссылке. Так «назад» не ходит по кругу
// между двумя соседними страницами (например, проверка и отчёт).
(function () {
  Array.prototype.forEach.call(document.querySelectorAll("a.back"), function (a) {
    a.addEventListener("click", function (e) {
      var from = null;
      try { from = document.referrer ? new URL(document.referrer) : null; } catch (err) { from = null; }
      if (history.length > 1 && from && from.origin === location.origin && from.pathname === a.pathname) {
        e.preventDefault();
        history.back();
      }
    });
  });
})();

// Подтверждение опасных действий: форме с data-confirm перед отправкой задаём вопрос.
// (Встроенный onsubmit запрещён политикой безопасности сайта.)
document.addEventListener("submit", function (e) {
  var q = e.target && e.target.getAttribute && e.target.getAttribute("data-confirm");
  if (q && !window.confirm(q)) e.preventDefault();
}, true);
