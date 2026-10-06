/* Обезличенная телеметрия Horizance (см. telemetry.py): без cookie, без localStorage, без идентификаторов.
   Шлёт на свой сервер только «событие + путь страницы».
   Что считается:
     pv                      — просмотр страницы
     <data-t>                — нажатие на элемент с атрибутом data-t="имя" (латиница, цифры, _.:-)
     js_error                — ошибка JavaScript на странице (без текста ошибки — он может содержать данные)
     window.hzT("имя")       — любое своё событие из кода страницы (например busy_timeout:payment) */
(function () {
  var queue = [], timer = null;
  function flush() {
    timer = null;
    if (!queue.length) return;
    var body = JSON.stringify({ e: queue.splice(0, 30) });
    try {
      if (!(navigator.sendBeacon && navigator.sendBeacon('/api/t', body))) {
        fetch('/api/t', { method: 'POST', body: body, keepalive: true }).catch(function () {});
      }
    } catch (e) {}
  }
  function track(name) {
    if (!/^[a-z0-9_.:-]{1,48}$/.test(name)) return;
    queue.push([name, location.pathname]);
    if (!timer) timer = setTimeout(flush, 2000);
  }
  window.hzT = track;
  track('pv');
  document.addEventListener('click', function (e) {
    var el = e.target.closest && e.target.closest('[data-t]');
    if (el) track(el.getAttribute('data-t'));
  }, true);
  window.addEventListener('error', function () { track('js_error'); });
  window.addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(); });
})();
