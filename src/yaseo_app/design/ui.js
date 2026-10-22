// Выпадающие списки в стиле кабинета. Настоящий <select> остаётся в форме (он и отправляется),
// скрипт рисует поверх него кнопку и список. На телефонах и без скрипта работает системный.
(function () {
  if (window.matchMedia && window.matchMedia("(pointer: coarse)").matches) return;

  var openBox = null;

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
    var list = document.createElement("ul");
    list.className = "dd-list";
    list.setAttribute("role", "listbox");
    list.tabIndex = -1;

    var items = [];
    Array.prototype.forEach.call(select.options, function (o, i) {
      var li = document.createElement("li");
      li.className = "dd-opt";
      li.setAttribute("role", "option");
      li.textContent = o.textContent;
      li.dataset.i = i;
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
    function move(i) {
      active = Math.max(0, Math.min(items.length - 1, i));
      paint();
      items[active].scrollIntoView({ block: "nearest" });
    }
    function show() {
      if (openBox && openBox !== box) close(openBox);
      active = select.selectedIndex;
      paint();
      box.classList.add("open");
      btn.setAttribute("aria-expanded", "true");
      openBox = box;
      items[active].scrollIntoView({ block: "nearest" });
    }

    btn.addEventListener("click", function () {
      if (box.classList.contains("open")) close(box); else show();
    });
    btn.addEventListener("keydown", function (e) {
      var isOpen = box.classList.contains("open");
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!isOpen) show(); else move(active + (e.key === "ArrowDown" ? 1 : -1));
      } else if (e.key === "Home" && isOpen) { e.preventDefault(); move(0); }
      else if (e.key === "End" && isOpen) { e.preventDefault(); move(items.length - 1); }
      else if ((e.key === "Enter" || e.key === " ") && isOpen) {
        e.preventDefault(); choose(active); close(box, true);
      } else if (e.key === "Escape" && isOpen) { e.preventDefault(); close(box, true); }
      else if (e.key === "Tab" && isOpen) { close(box); }
      else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        var ch = e.key.toLowerCase();
        for (var k = 1; k <= items.length; k++) {
          var j = (active + k) % items.length;
          if (items[j].textContent.toLowerCase().indexOf(ch) === 0) {
            if (!isOpen) show();
            move(j);
            break;
          }
        }
      }
    });

    select.parentNode.insertBefore(box, select);
    box.appendChild(btn);
    box.appendChild(list);
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
