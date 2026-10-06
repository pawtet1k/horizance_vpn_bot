# Agent change log

Running log of changes made to this repo by AI agents, kept for context handoff between
sessions/agents. Append new entries at the top. Not a replacement for CLAUDE.md (architecture)
or memory (cross-session facts about the user/project) — this is specifically "what changed and why."

---

## 2026-09-06 (третий заход) — проверка моих же выводов, накопление трафика (Claude Opus 5)

Владелец потребовал перепроверить три моих утверждения. Два из трёх оказались
неверными. Фиксирую, чтобы следующий агент не наследовал ошибку.

### ОПРОВЕРГНУТО: «бесплатный VPN у отключённых»
Я писал, что из-за фильтра по локальной ноде люди с истёкшей подпиской
продолжали пользоваться VPN. **Проверено по access.log всех шести нод:
0 случаев.** Ни один просроченный триал (из 241) и ни один отключённый
пользователь (из 76) не подключался после окончания. Механизм в коде был
реальным, ущерб — нулевым. Правка `get_all_vpn_clients` остаётся как
защита, но продавать её как «закрыли утечку денег» нельзя.
Расхождение числа клиентов между нодами (56 против 69) — факт замера, но
объяснение ему я так и не доказал.

### ПОДТВЕРЖДЕНО частично: удаление клиентов
Владелец был прав: `remove_client_from_xray` работает по всем нодам
(`run_on_all_servers(_unregister_client)`). Моя претензия касалась только
того, КОГО выбирают для удаления в `get_expired_trial_clients`, а не того,
как удаление исполняется. Формулировка в прошлой записи была неточной.

### ОПРОВЕРГНУТО: «seo-ops скрипты сломаны»
`scripts/gsc_api.py` полностью рабочий. Я запускал его системным `python3`,
а у проекта есть свой `~/seo-ops/.venv` со всеми библиотеками.
**Запускать: `~/seo-ops/.venv/bin/python scripts/gsc_api.py ...`**

### Трафик: накопление вместо перезаписи (идея владельца)
`statsquery` поддерживает флаг `-reset`. Теперь при каждом обращении
приложения за подпиской показания снимаются с обнулением и **прибавляются**
к тому, что уже в базе (`save_traffic` через `COALESCE(...) + ?`), а в
заголовок `subscription-userinfo` отдаётся накопленный итог, а не прирост.
Так трафик перестаёт теряться при рестарте ноды.
Проверено вживую: у `183640993` снялось 46 917 395 / 61 286 243 байт со
всех нод, в базе стало 46/61 МБ, счётчики обнулились.

Две ошибки, пойманные тестом при этом:
- `_user_traffic(user_id, server, reset)` — `run_on_all_servers` дописывает
  сервер ПОСЛЕДНИМ, поэтому `reset` занял место сервера. Сигнатура
  исправлена на `(user_id, reset, server)`, в коде оставлен комментарий.
- `--pattern=user>>>ID` через SSH ломался: `_run_ssh` склеивает список в
  строку для шелла, а `>>>` шелл понимает как перенаправление (exit 2).
  Обёрнуто в `shlex.quote`. Локальный вызов через `subprocess` этой
  проблемы не имел — вот почему исходный автор считал трафик локально.

### IPv6 — документация проверена
`freedom.domainStrategy: UseIPv4` (уже стоит) действует **только на
доменные имена**, литеральный IPv6-адрес им не переписать. Чтобы IPv6 реально
заработал, нужен `sniffing.routeOnly: false` — тогда `destOverride` подменит
IPv6-адрес на распознанное имя, и оно резолвится в A-запись. Это меняет
обработку назначения для ВСЕГО трафика (документация отдельно предупреждает
о поломке Tor), при том что IPv6 — 23 соединения из ~6 млн. **НЕ применялось**,
ждёт решения владельца.

### Прочее
- В сообщение об оплате добавлена подсказка: если банк предложит разрешить
  автоплатёж — согласиться. Это единственное, чего не хватало для включения
  автопродления: из 17 плативших через UDM Pay у 11 подписка так и осталась
  в статусе `new`, потому что привязку никто не подтвердил.
- `site_stats.sh`: отсеиваются обращения по IP-адресу вместо домена
  (Reality-заглушка и сканеры). За 26.08–06.09 было 8067 «гостей», стало 5203.
  Зонды с настоящим SNI (`www.horizance.ru`) отделить нельзя — они в выборке.
- `docs/paid_periods_audit_2026-09-06.txt` — таблица расхождений `paid_periods`
  со сроком жизни: 24 строки, из них 6 отключённых, которые точно платили
  (колонка пустая, потому что платили переводом на карту до середины августа).

### Сверка по подписчикам
Все 17 плативших через UDM Pay **живы и активны**, потерянных нет.
23 активных = 17 (UDM Pay) + 6 старых, плативших на карту.

---

## 2026-09-06 (продолжение) — разбор ошибок предыдущего захода, DRY, продления (Claude Opus 5)

Владелец справедливо указал, что в первом заходе я писал новые скрипты, не
прочитав существующие. Этот заход начался с чтения `deploy_xray.py`, `vpn.py`,
`subscription.py`, `db.py` целиком.

### Убрано из моего же кода
- **Удалены `deploy_ipv6_fix.py` и `patch_ipv6_block.py`.** Они дублировали
  `deploy_xray.py`, который уже умеет заливать локальный конфиг на ноды,
  перезапускать xray и звать `restore_vpn`. Правило `::/0 → block` теперь живёт
  в локальном `/usr/local/etc/xray/config.json` и разъедется на ноды штатным
  `deploy_xray.py` — отдельный инструмент не нужен.
- `geo_assets.py` больше не имеет своей копии сборки ssh-параметров: она
  вынесена в `vpn.ssh_connect_kwargs()` и переиспользуется.

### Найденный баг: расхождение числа клиентов между нодами (56 против 69)
`subscription.get_expired_trial_clients` фильтровал просроченные триалы через
`vpn_manager.get_vpn_clients()`, а тот спрашивает **только 127.0.0.1**.
Просроченный триал, снятый с локальной ноды, в список не попадал — и на
остальных пяти нодах продолжал работать. Отсюда и разброс, и бесплатный VPN
у людей с истёкшим триалом.
- `vpn.get_all_vpn_clients()` — объединение по всем серверам, используется теперь тут.
- `vpn.run_on_all_servers` возвращает результаты (раньше None, поэтому за
  данными приходилось лезть к локальной ноде).
- Проверено после правки: в xray по всем нодам 58, лишних 0.

### `get_user_traffic` — починен
Был собственный вызов `xray api statsquery` к `127.0.0.1:62789` в
`website/routers/vpn.py`, то есть считалась одна нода из шести. Расчёт переехал
в `VPNManager.get_user_traffic` (суммирует по всем серверам), веб-роутер его
вызывает. Бэкап: `website/routers/vpn.py.bak_before_fleet_traffic`.

### Продления
- Списание по привязке теперь за **2 дня** до конца (`RENEWAL_ATTEMPT_DAYS_LEFT`),
  а не в последний день — остаётся запас на повтор.
- `charge_subscription(..., deactivate_on_failure=False)` для ранней попытки:
  неудача за 2 дня НЕ отключает пользователя (иначе отключали бы досрочно).
- Напоминание «истекает завтра» уходит только тем, у кого нет `pending_order_uid`
  (то есть платёж не в процессе); у кого продление прошло — дата уехала и до
  этой ветки они не доходят. В текст добавлена инструкция «отправьте /pay».
- Ветка напоминания не знает про провайдера — при переезде на другой эквайринг
  меняется только `charge_subscription`.

### Письма
- Письмо об оплате: добавлены дата регистрации с расчётом «на какой день после
  регистрации оплатил» и общее число активных подписок (`db.count_active_users`).
- Новое письмо `_notify_admin_of_churn` при переходе active → inactive: срок
  жизни, оплаченные месяцы, трафик, остаток активных и вся строка из базы.

### DRY
- `/pay` и кнопка «Подключить оплату» были двумя копиями одного тела — теперь
  один обработчик `main.add_subscription` с двумя подписками на события.
  Тесты обновлены (`test.py.bak_before_dry`).

### IPv6 — прежний вывод был преувеличен
Замер по логам: RU-нода 2 695 898 соединений с 21.03 → **15 на IPv6**;
DE 3 208 777 с 13.07 → **8**; NL 71 382 за сутки → 0; MX 27 710 → 0.
То есть happy eyeballs на клиентах работает, и IPv6 в туннель почти не
попадает. У `1682224052` VPN работал (25 000+ соединений через US 03–05.09);
не открывался конкретно **Telegram**, который его устройство резолвило в
IPv6-only. Правило `::/0 → block` оставлено: оно превращает зависание в
мгновенный отказ, после которого клиентский фолбэк срабатывает. Настоящее
исправление (переписывать IPv6-назначение на имя через `sniffing.routeOnly=false`
+ `domainStrategy: UseIPv4`) НЕ применялось — меняет маршрутизацию всего
трафика, нужен отдельный заход с тестами.

### Прочее
- `announce` в подписке снова нейтральный: баннер видят только те, у кого Happ
  уже стоит, — новости про App Store там бессмысленны по определению.
- `/happ/iphone` добавлена в футер `base.html` и в фолбэк-блок на `/trial`.
- `settings.IOS_ALTERNATIVE_CLIENTS` сведён к одному приложению — Karing.
- `/auth`: кнопка Telegram h-16 с тенью, четыре нерабочих способа — h-11,
  компактнее и тусклее. Иконки сохранены.

---

## 2026-09-06 — iOS без Happ, IPv6, гонка вебхуков оплаты, реклама, поддержка (Claude Opus 5)

**Контекст:** владелец пришёл с большим списком: Happ снова убрали из российского App Store,
поддержка через сайт вела на логин, реклама режется плохо, непонятно с рекуррентной оплатой,
SEO, охотники за Oracle. Ниже — только то, что реально изменено, с бэкапами для отката.
Версионирования пока нет, поэтому у каждого изменённого файла рядом лежит `*.bak_*`.

### Отменено из предыдущего захода этой же сессии
- **Убран пользовательский crontab**, который я же и добавил утром (обновление geo-баз).
  Владелец справедливо возразил: внешний крон не уезжает в репозиторий и настраивается
  руками на каждой машине. Удалён и крон, и обёртка `update_geosite_and_restore.sh`.
  Root-crontab (`docker restart mtproto-proxy`, бэкап) **не трогался**.
- Вместо крона обновление geo-баз встроено в собственный таймер бота — см. ниже.

### iOS / Happ
**Факты (проверено через `itunes.apple.com/lookup?id=…&country=…`, 06.09.2026):**
`id6783623643` мёртв везде (404 в логах с 14.07); `id6788279553` («Plus», стоял у нас) —
404 **с 07.08.2026**, снят из всех витрин, включая глобальную; `id6504287215`
(Flyfrog LLC) жив в US/DE/KZ/TR, v5.7.0 от 03.09.2026, в российской витрине его нет.
В логах `my_logger/*_warning.log` предупреждение о битой ссылке шло дважды в сутки
непрерывно с 07.08 — месяц.

- `settings.py`: iOS-ссылка → `id6504287215`; добавлен `IOS_ALTERNATIVE_CLIENTS`.
  Сознательно **одно** приложение — Karing (`id6472431552`): бесплатное, есть и в iOS-,
  и в macOS-витрине РФ, плюс собирается под Android/Windows/tvOS (github.com/KaringX/karing),
  из бесплатных остатков российской витрины самое обкатанное (308 оценок против 107 у
  OneXray, 62 у Wisp, 2 у sing-box MT). Бэкап: `settings.py.bak_before_ios_fallback`.
- `website/routers/vpn.py`: добавлен `is_happ_client(request)`; `/sub/{uuid}` больше не
  подмешивает `happ://routing/...` и `#providerid:` клиентам с чужим User-Agent — иначе
  они считают подписку битой. Бэкап: `routers/vpn.py.bak_before_universal_sub`.
- `bot.py`: `set_and_send_client_link` получил параметр `device_os`; на iOS в сообщение
  добавляется адрес подписки и ссылка на Karing. Бэкап: `bot.py.bak_before_ios_fallback`.
- `website/templates/_ios_appstore_fallback.html` переписан: приложение + поле с адресом
  подписки и кнопкой копирования. `routers/trial.py` передаёт `ios_clients`.
- **Важно:** macOS/Windows/Linux ставятся с GitHub разработчика, Android — из Google Play.
  App Store их не касается, паниковать не надо.

### Зависание на IPv6 — новая, ранее неизвестная причина «ничего не работает»
Пользователь `1682224052` (МегаФон, Екатеринбург) жаловался, что не работает ни один
регион. Проверено: uuid совпадает на всех 6 нодах, сессии в `access.log` авторизуются.
Но 06.09 все его 8 соединений шли на `tcp:[2001:67c:4e8:f002::a]:443` — это **сеть
Telegram, адрес IPv6**. Ни на одной ноде IPv6 нет (`ip -6 addr` пусто, маршрута нет),
sniffing стоит с `routeOnly: true` — значит адрес от клиента идёт в connect как есть и
висит до таймаута.

- Добавлен `patch_ipv6_block.py` (идемпотентный) и `deploy_ipv6_fix.py` (раскатка по
  `credentials.SERVERS` с рестартом и `restore_users`). В конфиг каждой ноды добавлено
  правило `"ip": ["::/0"] -> block` и `freedom.settings.domainStrategy = "UseIPv4"`.
  Бэкап на нодах: `/usr/local/etc/xray/config.json.bak_before_ipv6_block`.
- Раскатано на все 6 нод, после рестарта клиенты возвращены, везде по 58.

### Оплата: найдена и исправлена гонка вебхуков
`udmpay_subscription_webhook` искал пользователя по `pending_order_uid`, а
`activate_paid_subscription` это поле обнуляет. За 22.08–02.09 оплата выигрывала гонку
**5 раз из 6** — привязка терялась с записью «не найден пользователь для orderUid».
Единственный раз, когда вебхук успел (02.09, `6a979f967fb16`), банк ответил `rejected`.

- `models.py` / `db.py`: колонка `pending_subscription_uid` (+ миграция для `users` и
  `web_users`), метод `get_user_by_pending_subscription_uid`.
- `subscription.py`: `_create_udmpay_payment` сохраняет `subscriptionUid` из ответа
  `create-with-payment`; `bind_subscription` и `fail_subscription_bind` его снимают.
- `website/routers/payments.py`: поиск по `subscriptionUid`, запасной путь по `orderUid`
  для заказов, созданных до миграции.
- Бэкапы: `*.bak_before_subuid`.
- Текст обращения в UDM Pay — `docs/udmpay_support_request.md` (актуализирован).

### Реклама
- `website/routers/vpn.py`: `ROUTING_OVERRIDES` + `_apply_routing_overrides` — чужой
  профиль маршрутизации (roscomvpn) продолжаем тянуть ради свежих geosite/geoip и списков
  «что напрямую», но DNS подменяем своими: AdGuard DNS удалённым, Comss.one доменным.
  Замеры с российской ноды: 77.88.8.8 (что стояло) не блокирует ни один рекламный домен,
  оба новых отдают 0.0.0.0 на doubleclick.net / an.yandex.ru / mc.yandex.ru.
  Побочный эффект: своя же Яндекс.Метрика у наших пользователей считать не будет.
- `geo_assets.py` (новый) + вызов `refresh_geo_assets()` в `utils.start_timer`: раз в
  12 часов проверяет mtime `geosite.dat` на каждой ноде, старше недели — обновляет с
  Loyalsoldier, рестартит xray и **обязательно** зовёт `restore_users.restore_vpn`.
  Бэкап: `utils.py.bak_before_geo`.
- **Проверено и опровергнуто:** ни `xray.service`, ни его drop-in клиентов после рестарта
  не восстанавливают. Это надо помнить при любом рестарте xray.
- Базы обновлены на всех 6 нодах (были от 13.08 и старше).

### Поддержка на сайте
- `website/routers/actions.py`: `/help` работает **без входа** (поле «куда ответить»,
  лимит 3 обращения в час с IP). Исправлена повторная отправка: было возвращение
  отрендеренной страницы на POST, из-за чего восстановление вкладки слало форму заново
  (06.09 обращение записалось дважды, 09:23 и 10:43) — теперь POST/Redirect/GET на
  `/help?sent=1` плюс дедупликация по хешу на 15 минут.
  Бэкап: `routers/actions.py.bak_before_anon_help`.
- Письма с сайта уходят на `settings.SUPPORT_EMAIL` = support@horizance.ru
  (`utils.email_support` получил параметр `to`). Бот шлёт по-прежнему на `credentials.SMTP_TO`.
- `website/routers/faq.py`: FAQ расширен с 3 до 15 вопросов, собран по реальным обращениям
  из логов за июль–сентябрь. Разметка вынесена в `templates/_faq_accordion.html` и
  переиспользуется на `/faq` и `/help`.

### Контент и SEO
- Новая страница `/happ/iphone` (`templates/happ_iphone.html`, роут в `routers/happ.py`).
  Подана: IndexNow (200), Bing submit_url (ok), sitemap переподан в GSC. Добавлена в
  `sitemap.xml`, `llms.txt`, `~/seo-ops/seo-map-fax.yaml` (кластер C1b) и слинкована с `/happ`.
- `website/routers/vpn.py`: `announce` в заголовках подписки — вместо «Лето на дворе!»
  теперь ссылка на `/happ/iphone` (баннер видят все, кто не заходит ни в бот, ни на сайт).
- `site_stats.sh`: добавлен `--days N`. Без него виден только текущий день — logrotate
  режет лог каждую ночь, и все прежние замеры «за сегодня» были не тем, что нужно.
  Архив смешанный (формат сменился 26.08), старые строки отсеиваются по числу полей.
  Бэкап: `site_stats.sh.bak_before_days`.

### Подготовка к GitHub (сам репозиторий НЕ создавался)
- Добавлен `.gitignore`: `credentials.py`, `*.session`, `*.db`, логи, `.venv`, `*.bak_*`,
  выгрузки. **Проверить его глазами перед первым `git init`** — цена ошибки высокая.
- Токен профиля `pawtet1k` лежит в `/home/pashavpsadm/oracle_automation/gh-pawtet1k/hosts.yml`,
  скоупы `gist, read:org, repo`. Использовать так:
  `GH_CONFIG_DIR=/home/pashavpsadm/oracle_automation/gh-pawtet1k gh repo create ...`
  (глобальный `gh` авторизован под другим аккаунтом — `watotowaafrica`).

### Oracle
- `oracle_automation/supervise_migrate.sh` — надзиратель: молот по Мексике останавливался
  бы 08.09 (общий таймаут 604800 с), теперь перезапускается с 30-суточным окном, пока в
  `state.json` не появится `new_instance_id`. Голландский охотник в `while True`, не трогался.

### Найдено, но НЕ исправлено
- **`get_user_traffic` считает трафик только с локальной ноды** (`127.0.0.1:62789`).
  Кто ходит через US/DE/RU/KZ/MX, у того в базе `traffic_in=traffic_out=0` навсегда.
  Из-за этого рассылка `_vpn_not_enabled` («приложение установлено, но VPN выключен»)
  уходит людям, которые VPN активно пользуются.
- `xray.service` стоит с `Restart=on-failure`, и после аварийного рестарта клиентов
  никто не вернёт — они молча отвалятся до следующего обращения к боту.

---

## 2026-07-24 — SEO/security/Happ-iOS fixes (Claude, this session)

**Context:** user asked for a favicon check, SMTP From-name check, SEO meta audit, OWASP Top 10
review, PWA recommendation, a Happ/iOS App Store fallback for the `/trial` page, and a
Playwright/Chrome DevTools dependency check. Findings were gathered via 4 background research
forks (SEO audit, OWASP audit, Notion search, SEO-MCP-readiness check) and then implemented directly.

### Happ / iOS App Store link
- **Root cause found:** the hardcoded iOS link in `settings.py` (`VPN_CLIENT_DOWNLOAD_URLS["iOS"]`)
  pointed at App Store ID `6783623643`, which returns **404 in every region** (not just Russia) —
  it's simply a dead ID, unrelated to Roskomnadzor. Fixed to the current live ID `6504287215`
  ("Happ - Proxy Utility" / Flyfrog LLC): `https://apps.apple.com/us/app/happ-proxy-utility/id6504287215`.
- **Important nuance surfaced to user:** the `/us/` vs `/ru/` segment in an App Store URL is only
  a display locale — it does **not** control availability. Availability is tied to the signed-in
  Apple ID's account region. The live ID above returns 404 specifically under `/ru/` (consistent
  with RKN-driven removal from the Russian storefront) but works under `/us/`. So the URL fix alone
  does not unblock Russian users — they must change their Apple ID's App Store region.
- Added `website/templates/_ios_appstore_fallback.html` — a shared `<details>` fallback with
  step-by-step instructions for switching the Apple ID / App Store region — included on both
  `trial.html` (under the OS download grid) and `open_happ.html` (in the "install Happ" fallback
  state shown when the `happ://` deep link fails to open).
  - Both TestFlight links checked (the user-supplied `testflight.apple.com/join/mqS9UCR9` and
    another found via search, `.../join/XMls6Ckd`) currently show **"this beta is full"** —
    non-functional, so neither was used as the fallback link.

### SEO meta audit → fixes
- `index.html`: trimmed meta description from 193 → 133 chars (was truncating in SERPs).
- `faq.html`: was a **fully standalone HTML file** (doesn't extend `base.html`) with no meta
  description, canonical, or robots tag at all, despite being indexable and listed in `sitemap.xml`.
  Added all three directly in its own `<head>`.
- `help.html`: added `canonical` (was silently inheriting `/`'s canonical — self-canonical
  mismatch) and set `robots` to `noindex, follow` since the route requires login (redirects
  anonymous/crawler traffic to `/auth`, so indexing the real content isn't reachable anyway).
- `config_select_os.html` / `open_happ.html`: added explicit `noindex, nofollow` + canonical
  (defense-in-depth; both are already blanket-`Disallow`'d in `robots.txt`, so this only matters
  if something ever links to them directly or a non-compliant crawler ignores robots.txt).

### OWASP Top 10 → fixes
- **Rate limiting** (previously nonexistent anywhere): added `request_helpers.is_rate_limited()`,
  a small in-memory sliding-window limiter (no new dependency). Applied to:
  - `POST /trial/connect` (max 3/hour per IP) — this endpoint provisions a real Xray VPN client
    via SSH on every server per call; was previously loopable with zero limits.
  - `POST /api/auth/{provider}/click` (max 5/10min per IP) — was an unauthenticated email-flood
    vector (every call sends a real SMTP email).
  - Caveat: this is per-process/in-memory, not shared across workers — fine for the current
    single-uvicorn-process deployment, would need Redis/similar if that changes.
- **IP spoofing**: `request_helpers.get_client_ip()` previously trusted `X-Forwarded-For`/
  `X-Real-IP` unconditionally. Now only trusts those headers if the direct TCP peer is
  `127.0.0.1`/`::1` (i.e. actually came through the local nginx reverse proxy per
  `/etc/nginx/nginx.conf`'s `proxy_pass http://127.0.0.1:8001`); otherwise falls back to the raw
  peer IP, which can't be spoofed at the TCP layer. This also makes the rate limiting above
  meaningful (previously any attacker could forge a new `X-Forwarded-For` per request to dodge
  IP-based throttling).
- **Link injection in Telegram→web rendering**: `telegram_markdown.to_html()` escaped HTML
  *then* ran its `[label](url)` regex, so a Telegram display name containing
  `[x](javascript:...)` could survive into `{{ ... | safe }}` template rendering. Added a scheme
  allowlist (`_SAFE_URL_RE`, only `http(s)://`) — non-http(s) schemes are left as literal escaped
  text instead of becoming a clickable/executable link.
- **Security headers**: added a middleware in `website/app.py` setting
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: strict-origin-when-cross-origin`, `Strict-Transport-Security`. (CSP was
  intentionally skipped — the site loads Tailwind CDN + Font Awesome CDN inline scripts/styles;
  a correct CSP needs a real inventory of every external script/style origin first, or it'll
  break the page. Flagged as follow-up, not done in this pass.)

### Other
- `utils.send_email`: `From` header was the bare address (`noreply@horizance.ru`) with no display
  name — some mail clients show a raw address instead of a friendly sender name. Now uses
  `email.utils.formataddr()` with `credentials.SMTP_FROM_NAME` (added to `credentials.py`,
  defaults to "Horizance VPN" via `getattr` if absent).
- Ran `.venv/bin/python3 -m unittest test.py` after all edits — all 21 tests still pass.
  `website/app.py` still imports cleanly.
- Installed Playwright's Chromium browser binary (`npx playwright install chromium`) — it was
  missing from `~/.cache/ms-playwright` (only `ffmpeg` was present, no actual browser), which
  would have made `mcp__playwright__*` tool calls fail outright. All OS-level deps the user listed
  (libnss3, libatk1.0-0, etc.) were already satisfied under Ubuntu 24.04's `t64`-suffixed package
  names (e.g. `libatk1.0-0t64`) — `dpkg -s <exact-name>` reports them missing but the functional
  equivalent is installed; `npx playwright install-deps chromium` confirmed "already newest version"
  for everything.

### Found but NOT fixed (needs the user's decision)
- **`/srv/horizance_ru/website`** — a separate deployment of (an older copy of?) this same app,
  serving `horizance.ru`, is run via systemd (`horizance_ru.service`) with
  `uvicorn app:app --host 0.0.0.0 --port 8000`, i.e. bound to **all interfaces**, not `127.0.0.1`.
  Unlike the main `horizancevpn_com.service` (bound to `127.0.0.1:8001`, only reachable via nginx),
  this one is directly reachable from the internet on port 8000, bypassing nginx's IP-resolution
  entirely — meaning the IP-spoofing fix above doesn't help there, and no rate limiting applies
  either since nginx is skipped. Recommend rebinding to `127.0.0.1:8000` to match the main service
  (nginx already proxies `horizance.ru` → `localhost:8000` per its config) — **not done**, since
  it's a live production systemd service outside this repo's working directory and restarting it
  needs explicit sign-off.
