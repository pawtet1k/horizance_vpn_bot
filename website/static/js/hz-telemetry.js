/* Обезличенная телеметрия Horizance (см. telemetry.py): без cookie, без localStorage, без идентификаторов.
   Шлёт на свой сервер только «событие + путь страницы».
   Что считается:
     pv                      — просмотр страницы
     <data-t>                — нажатие на элемент с атрибутом data-t="имя" (латиница, цифры, _.:-)
     js_error, js_err:<файл>:<строка> — ошибка JavaScript на странице (без текста ошибки — он может содержать данные)
     js_reject               — необработанный отказ Promise
     fld:<форма>:<поле>     — поле формы заполнено (один раз за просмотр, без значения)
     submit:<форма>         — попытка отправить форму
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
  // где упало: файл:строка (без текста ошибки) — js_err:app.js:120, js_err:x:0 = чужой скрипт
  window.addEventListener('error', function (e) {
    track('js_error');
    var f = String((e && e.filename) || 'x').split('?')[0].split('/').pop().toLowerCase()
      .replace(/[^a-z0-9_.-]/g, '').slice(-30) || 'inline';
    track('js_err:' + f + ':' + ((e && e.lineno) || 0));
  });
  window.addEventListener('unhandledrejection', function () { track('js_reject'); });
  // form funnel: which fields people actually fill (never the values) —
  // fld:<form id>:<field name>, once per field per page view; plus submit attempts
  var seenFld = {};
  function fldName(el) {
    var f = el.form, fid = f ? (f.id || 'form') : 'nofrm';
    var n = (el.name || el.id || '').toLowerCase().replace(/[^a-z0-9_]/g, '');
    return n ? ('fld:' + fid.toLowerCase().replace(/[^a-z0-9_-]/g, '') + ':' + n).slice(0, 48) : '';
  }
  document.addEventListener('change', function (e) {
    var el = e.target;
    if (!el || !/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.type === 'hidden' || el.type === 'password') return;
    var k = fldName(el);
    if (k && !seenFld[k]) { seenFld[k] = 1; track(k); }
  }, true);
  document.addEventListener('submit', function (e) {
    var f = e.target;
    track(('submit:' + ((f && f.id) || 'form').toLowerCase().replace(/[^a-z0-9_-]/g, '')).slice(0, 48));
  }, true);
  window.addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(); });
})();
