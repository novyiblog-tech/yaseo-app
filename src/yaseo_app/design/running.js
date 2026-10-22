// Ход проверки на странице сайта: блок обновляется сам, остальная страница не трогается.
(function () {
  var box = document.getElementById("running");
  if (!box) return;
  var timer = setInterval(function () {
    fetch(location.pathname, { credentials: "same-origin" })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, "text/html").getElementById("running");
        if (fresh) { box.innerHTML = fresh.innerHTML; }
        else { clearInterval(timer); location.reload(); }
      })
      .catch(function () {});
  }, 3000);
})();
