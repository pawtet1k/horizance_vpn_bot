# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot (Horizance VPN) built on Telethon that sells and manages a VLESS+Reality VPN
subscription service backed by Xray-core. The bot handles onboarding, payments (UDM Pay/СБП,
live in production since 2026-08-13 — see below; YooKassa integration is dead code, never
actually used), and provisions/revokes VPN client credentials across one or more remote Xray
servers over SSH.

This is not a git repository — there is no version control here, so be extra careful with
destructive file operations and confirm before overwriting anything.

**Owner's notes and feedback for agents** live in
`/home/pashavpsadm/.claude/projects/-home-pashavpsadm-horizance-vpn-bot/memory/` (index: `MEMORY.md`).
Read the index before user-facing work, especially the `feedback_*` files: how to write to customers,
copy tone, verify-before-claiming, mobile layout checks.

## Running

```bash
# dependencies (a .venv already exists; activate it or use its python directly)
.venv/bin/pip install -r requirements.txt

# run the bot (long-running process, connects to Telegram via Telethon)
.venv/bin/python3 main.py
```

The bot requires a `credentials.py` file in the repo root (not checked into any VCS) that defines:
`API_ID`, `API_HASH`, `BOT_TOKEN`, `SERVERS` (list of dicts describing each Xray node — host, port,
username, password/ssh_key_path, local_host flag), `SERVER_PUBLIC_KEY`, `SNI`, `XRAY_SHORTID`,
`TRIAL_LINK`, `yookassa_id`, `yookassa_key`, `SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD`/`SMTP_FROM`/`SMTP_TO`.
Never print or log the contents of this file.

### Tests

```bash
.venv/bin/python3 test.py
# or
.venv/bin/python3 -m unittest test.py
```

`test.py` mocks `db.UserManager.DB_NAME` to `test_users.db` and `settings.XRAY_CONFIG_PATH` to
`test_config.json` before running, and drives handlers directly (no real Telethon dispatcher/connection).
All 88 tests currently pass (last verified 2026-10-13). test.py sets `HORIZANCE_LOG_DIR` to a temp dir before importing the project, so tests no longer write into the production `my_logger/` (older prod log lines with ids 333001, 444001, 777xxx, 888xxx or node "DE 10.0.0.1" came from tests before 2026-09-26). Two behavior notes worth knowing before "fixing" a future failure here:
`start_vpn_setup` always re-prompts for device OS via `/config` (or the `generate_settings_link` button)
even if `device_os` was already saved from a previous run — there's no "skip prompt, just reprovision"
path, by design; and the promo free-trial-extension keyword checked in `main.handle_message` is the
literal (lowercased) string `"живой"`, not `"Беру"`.

### Standalone / ops scripts (not part of the bot's runtime process)

- `deploy_xray.py` — SSHs into a single target server (`get_target_server(i)` indexes into
  `credentials.SERVERS`), installs/verifies Xray, uploads the local Xray config, patches it
  (`xver`, `dest`), restarts the service, and calls `restore_users.restore_vpn`.
- `restore_users.py` — re-pushes every non-inactive user's `client_uuid` (from both the `users` and
  `trial_sessions` tables) into a server's running Xray instance via `VPNManager._register_clients`.
  Run this after an Xray reinstall/restart on a given server.
- `resources_monitoring.py` — one-shot disk/memory/network usage check against hardcoded limits
  (`MAX_BLOCK_STORAGE_GB`, `MAX_OUTBOUND_TRAFFIC_TB`), meant to run on a VPN node itself.
- `db_interventions.py` — a one-off legacy script that seeded `users.db` with hardcoded historical
  user rows. Kept for reference only; not part of normal operation.
- `site_stats.sh` + `site_stats_summary.py` + `goaccess.conf` — **traffic stats for both websites
  from the nginx logs, via GoAccess.** **`./site_stats.sh --days 30` is the answer whenever anyone
  asks how many visitors a site had.** Without `--days` the script sees only the current day —
  logrotate truncates the log every night — so a bare `./site_stats.sh` silently under-reports by
  roughly 30x. It prints a text digest (visitors, pages, referrers, search phrases, 404s);
  `--html` also writes a report into `my_logger/stats/`; `--old` reads the pre-2026-08-26 archive
  (different log format). Needs `sudo` (nginx logs are `root:adm`). Reference point: 26.08–06.09.2026
  gave 2 206 unique visitors on horizancevpn.com against 11 visits in Yandex.Metrika for the same
  window — that gap is mostly the cookie gate, but **also a bug fixed 2026-10-05**: the Trusted Types CSP (`website/app.py`, since 2026-07-27) silently blocked the Metrika/Clarity/GA4 loaders in Chrome even after consent. base.html now defines a `trustedTypes` default policy with a script-URL allowlist — any new third-party script loaded from JS must be added there or it won't load in Chrome.
  **Use this, not Yandex.Metrika/GA4/Clarity, whenever you need real traffic numbers:** those three
  only load after the visitor accepts the cookie banner, so they miss most of the audience (77% of
  `trial_get_link` goals never register — see `docs/TODO.md`). The nginx logs see everyone, stay on
  this machine and need no consent. Note `/var/log/nginx/access.log` now starts each line with
  `$host` (`log_format vhost_combined`, added 2026-08-26) so the two sites can be told apart;
  rotated archives predating that use plain `combined`, which is what `--old` switches to.
  **Retention (since 2026-09-26):** nginx logs are kept ~100 days (`/etc/logrotate.d/nginx`, `rotate 100`,
  was 14). **Permanent per-day totals** (requests, visitors, pageviews, trials issued, /sub hits) per site
  live in `my_logger/stats/daily_visits.csv`, written nightly by `site_stats_daily.py`
  (`/etc/cron.d/horizance-site-stats`, 00:35, as root) — it merges, never deletes, so history survives
  log rotation. Backfilled from 2026-08-26 (older logs were already rotated away).

## Architecture

**Entry point:** `main.py` builds the Telethon `TelegramClient`, registers all `/command` and
`CallbackQuery` handlers, and delegates the actual logic to `bot.py`. Every handler is wrapped in
`safe_handler`, which catches exceptions, logs them with `exc_info`, and sends the user a generic
"something went wrong" reply — handlers themselves should not need their own try/except for
top-level failures. `initialize_bot_classes()` constructs the three manager singletons
(`UserManager`, `VPNManager`, `SubscriptionManager`) and a `pymorphy3` analyzer used for
Russian word declension in bot copy; this must run before `main()`.

Two background loops are started in `main()`: `utils.start_timer` (every 12h — runs
`SubscriptionManager.process_payments` and `delete_expired_trial_clients`) and `utils.ping`
(hourly Telethon keepalive ping).

**Manager layering** (`bot.py` handlers call into these; managers don't call each other except
`SubscriptionManager` holding references to `UserManager`/`VPNManager`):

- `db.UserManager` — thin `sqlite3` wrapper around `users.db`. Two tables: `users` (one row per
  Telegram user, columns mirror `models.User`'s constructor kwargs) and `trial_sessions` (columns
  mirror `models.TrialUser`, keyed by `client_uuid`, used for the separate free-trial flow).
  All queries go through `execute_query`; rows are returned as `sqlite3.Row` and hydrated into
  `User`/`TrialUser` objects. `add_user`/`add_trial_session` build the INSERT column list directly
  from `vars(User(**kwargs))`, so adding a field to `User`'s `default_values` also requires adding
  the matching column to `create_table`'s schema.
- `vpn.VPNManager` — talks to Xray, not to the database. It reads the **local** Xray config
  (`settings.XRAY_CONFIG_PATH`) once at startup to determine the inbound tag and Xray gRPC API port
  (`inbounds[0]` = the VLESS inbound, `inbounds[1]` = the API inbound). Client add/remove
  (`_register_clients` / `_unregister_client`) run the `xray api adu`/`rmu` CLI commands over SSH
  (`asyncssh`) against **every** server in `credentials.SERVERS` in parallel via
  `run_on_all_servers`/`asyncio.gather`, so a single subscription change fans out and keeps all VPN
  nodes' client lists in sync. Listing clients (`get_vpn_clients`) only queries the local node.
- `subscription.SubscriptionManager` — subscription/payment lifecycle. **Live payment provider is
  UDM Pay** (`udmpay.py`, СБП/SBP QR payments; `settings.UDMPAY_ENABLED = True` in production since
  2026-08-13). The YooKassa code path (`Payment.create`/`Payment.find_one`, `create_payment`'s
  manual-bank-transfer fallback) is legacy/dead — only reachable when `UDMPAY_ENABLED = False`.
  UDM Pay flow: `add_subscription`/`_create_udmpay_payment` create an order via
  `sbp-subscription/create-with-payment`, send the user a `paymentLink`, and return immediately —
  confirmation is **webhook-driven, not polled** (`website/routers/payments.py` receives
  `PaymentSuccessWebhookPayload`/`SubscriptionSuccessWebhookPayload` etc., and **re-verifies every
  webhook against the UDM Pay API** before trusting it, since UDM Pay's webhooks aren't signed).
  `pending_order_uid` (a `users`/`web_users` column) routes an incoming webhook — which carries only
  `orderId`, no user identifier — back to the right user, and doubles as an anti-double-charge lock.
  Since 2026-09-30 every created order is also journaled in the `payment_orders` table (`db.record_payment_order`),
  and webhooks resolve the owner via `db.get_user_by_order` — `pending_order_uid` holds only the LAST order, and people
  who tapped "СБП" several times then paid an earlier link lost their payment (3 cases). `paid_at` prevents double credit.
  `activate_paid_subscription` extends the subscription, increments `paid_periods` by
  `settings.get_periods_increment(price)` (1 for monthly, **12 for yearly** — this tracks paid
  *months*, not billing events), and emails `settings.NOTIFY_EMAIL` with payment details, because
  **UDM Pay sends no payment notifications of its own** and the money settles with UDM Pay, not
  directly in the merchant's bank. Users pick a plan (month/year) via `bot.py`'s
  `select_plan_month`/`select_plan_year`/`change_plan` callbacks or the website's `/pay` →
  `/pay/{month,year}`; `plan_choice_buttons(exclude_price=...)` hides whichever tariff is already
  active. **Recurring auto-renewal via UDM Pay (`charge_subscription`'s `_charge_udmpay_subscription`
  branch) is unverified in production** — the SBP subscription *binding* step (needed before a
  renewal charge can succeed) has not been observed completing successfully; see
  `~/.claude/projects/.../memory/project_udmpay_integration.md` for the full history before touching
  this. `process_payments` (called from the 12h timer) walks all users, warns at 1-day-left, and
  calls `charge_subscription` at 0-days-left, which either re-charges a saved payment token, or —
  for trial/canceled/active users with no token — sends a "your VPN is disabled" message and
  deactivates the user (`UserStatus.INACTIVE` + `VPNManager.remove_client_from_xray`).
  **A second payment provider, NOWPayments (crypto/USDT, `nowpayments.py`), is enabled in
  production** (`settings.NOWPAYMENTS_ENABLED = True` as of 2026-10-05; payout set up — see
  `memory/project_nowpayments_integration.md`). When enabled, after picking a plan the bot shows an extra "СБП or
  Криптовалюта" choice (`bot.callback_choose_payment_method`); with the flag off this step is
  skipped entirely and behavior is identical to UDM-Pay-only. Crypto has no real auto-renewal —
  `add_crypto_subscription`/`_create_crypto_payment` create a fresh one-off invoice each period,
  same shape as a manual/UDM Pay purchase, not a silent recurring charge (see
  `~/.claude/projects/.../memory/project_nowpayments_integration.md` for why — short version:
  crypto has no non-custodial equivalent of a saved card/SBP auto-debit). `activate_paid_subscription`/
  `fail_pending_payment`/`_notify_admin_of_payment` in `subscription.py` take a `provider` param
  and are shared by both payment providers — don't assume they're UDM-Pay-only when editing.
- `models.User` / `models.TrialUser` — plain attribute-bag constructors (not dataclasses); both
  fill in defaults for any kwarg not supplied, which is what lets `UserManager` construct them
  directly from a raw DB row (`User(**dict(row))`). `UserStatus` is a `str` Enum with
  `TRIAL`/`ACTIVE`/`INACTIVE`/`CANCELED`, plus `label_ru()` for user-facing status text.

**Bot flow highlights** (`bot.py`): `/start` creates the user on first contact (capturing an optional
referrer id parsed from the deep-link payload, e.g. `t.me/bot?start=<referrer_id>`) and shows a
status card; VPN setup (`start_vpn_setup` → `resolve_device_os` → `finish_vpn_setup`) asks for the
client OS via inline buttons, then calls `VPNManager.setup_xray` to (re)register the user's UUID in
Xray and sends back a `vpn://` config link built from `credentials.SUBSCRIPTION_DOMAIN`
(`horizancevpn.com` — independent of `credentials.SNI`, which is only the Reality camouflage
domain and must never be used for building page/API URLs) + the client UUID (not `VPNManager`'s own
commented-out `generate_settings_link`, which is dead code). `/pay` and the
`add_subscription`/`cancel_subscription` callbacks route through `SubscriptionManager`. `/help` sets a
per-user "waiting for question" flag so the *next* plain-text message from that user gets forwarded
via `utils.email_support` (SMTP) instead of being treated as a normal message.

**Logging:** everything imports the module-level `logger` from `my_logger.py` (`from my_logger import
logger`) — this sets up date/month-partitioned file handlers under `my_logger/{debug,info}/<mm-yy>/`,
plus flat `my_logger/<mm-yy>_warning.log` and `my_logger/error.log`, all in `Europe/Moscow` time, and
auto-rotates handlers when the calendar date changes. `MyLogger.py` (capital-letter file, class-based
singleton) is a separate, unused legacy implementation — do not import from it; it's dead code kept
around from an earlier refactor.

Most user-facing strings are Russian; keep new user-facing copy consistent with that (and with the
existing tone/emoji conventions) unless told otherwise.

## Английский интерфейс и журнал переписки (с 11.10.2026)

- **Язык**: иностранцу (язык Telegram/браузера не ru/uk/be/kk, без кириллицы в имени, IP не RU) бот, сайт и заглушки Happ
  отвечают по-английски. Бот — `i18n.py` (`is_english`, `tr(user|id, ru, en)`), сайт — middleware `site_language` в
  `website/app.py` → `request.state.en`, в шаблонах `{{ t(ru, en) }}` / `is_en()`. Поисковым роботам всегда русская версия.
  Новый пользовательский текст пиши сразу в двух языках.
- **Перед любым сообщением пользователю — прочитать `docs/USER_COMMUNICATION_RULES.md`.**
- **Переписка с пользователями** — только через `utils.send_telegram_api_message`: он пишет каждое сообщение в дневной лог
  строкой «Ответ техподдержки для <id>: …». Ручные сообщения до 11.10 — в `my_logger/info/support_restored.log`.
- **Брошенные оплаты**: `node_monitor._abandoned_orders` сообщает владельцу о заказах без оплаты старше 30 мин.
- **Инбаунд `vless_xhttp_pu` (NL:2096, packet-up)** добавлен через `xray api adi` для теста одного пользователя и
  **не переживает рестарт xray**.

## Мониторинг UDM Pay и дубли «брошенных оплат» (с 11.10.2026, Sonnet в отдельной сессии)

- 11.10 07:46–… UDM Pay (84.201.157.37:443) не принимал TCP-соединения ни с нашей ноды, ни с check-host (RU/NL/DE) — их сбой, не наш код.
  При этом заказ не создаётся → в `payment_orders` строки нет → «брошенная оплата» не срабатывает (клиентка 307069466).
- `node_monitor.run_udmpay_watch` (запускается в `main.py`, если `UDMPAY_ENABLED`): пинг раз в 60 с, письмо «недоступен» после 2 промахов подряд и
  «снова онлайн» с длительностью; каждая проверка — в `my_logger/stats/udmpay_uptime.csv` (utc, ok, error, sec).
- Дубли писем про kodredim: `_last_alert` жил только в памяти, каждый рестарт бота присылал письмо снова (окно заказа — сутки).
  Теперь ключи пишутся в `abandoned_alerted.json` рядом с `node_traffic_records.json`.

### 12.10.2026: сбой UDM Pay продолжается (дроны по ЦОД Яндекса, UDM Pay в Yandex Cloud AS200350)
- Текст ошибки в `subscription.py` (`httpx.TransportError`) теперь про атаки на ЦОД Яндекса. Клиентам 466446017, 307069466, 616093378, 33670846 и 632675764 отправлено сообщение (в логе «Ответ техподдержки…»).
- 632675764 бот отключил 12.10 09:19 — возвращён: `status=active`, `next_payment_date=19.10.26`, `setup_xray` выполнен. Срок поставлен с запасом, чтобы `process_payments` не писал «истекает завтра» и не отключал, пока оплата не работает; продлить или вернуть к реальному сроку после восстановления UDM Pay.
- **Рассылка «оплата заработала» запрограммирована** (`node_monitor.notify_recovered`, вызывается из `run_udmpay_watch` после 2 успешных пингов подряд).
  Получатели: `payment_outage.json` → `users` (кому UDM Pay не создал заказ; пишет `subscription.py`; вне объявленного сбоя список стирается) + неоплаченные `payment_orders` с даты `since`
  (крипта), минус оплатившие после и владелец. Сообщение с кнопками тарифов, уходит через `send_telegram_api_message` (лог «Ответ техподдержки…»).
  Состояние сбоя (`down_since`) лежит в том же файле и переживает рестарт. Проверяется доступность хоста, не создание заказа.
- Правила статусов при возврате (владелец, 12.10): `active` — только тем, кто хоть раз успешно оплатил (`paid_periods>0`); остальным `trial`.
  616093378 (никогда не платила) возвращена как `trial` до 19.10.26.
- Не-в-списке рассылки: 956118484 (@kodredim — крипто-счёт 10.10, до сбоя; бот заблокирован) — исключён: `since` = 2026-10-11.

### 13.10.2026 (Opus): правка кода сбоя UDM Pay
- Текст клиенту при недоступном UDM Pay — `SubscriptionManager._udmpay_unreachable_text`: причина из `settings.PAYMENT_OUTAGE_REASON`
  показывается **только пока сбой объявлен** (`payment_outage.is_down()`); иначе прежнее «попробуйте через минуту».
  **После восстановления UDM Pay очистить `PAYMENT_OUTAGE_REASON = ("", "")`** — следующий сбой может быть не про дронов.
- Пинг: «лежит» = нет HTTP-ответа (код ответа корня API в рабочем режиме не известен, 5xx не считаем сбоем); код пишется в csv.
- Возврат за сбой: 140785730 (mairamay) — `active` до 20.10.26. Временные даты 632675764 (19.10) и 140785730 (20.10) **сдвинуть**,
  если оплата не заработает до 18–19.10: иначе бот пришлёт «истекает завтра» и отключит. `stats_growth.py` считает их «срок не подошёл».
- Трафик в `users.traffic_in/out` — в **МБ**, не в байтах.

### 13.10.2026: устройства подписки — /devices (бот и сайт) и опция «доп. устройства»
- Бот: `/devices` (в меню команд) — список устройств, «Убрать»/«Вернуть», опция «➕ Ещё 3 устройства». Сайт: `/devices`
  (карточка «Мои устройства» в ЛК), POST `/devices/remove|restore`, POST `/devices/extra/{on|off|with_subscription}`,
  GET `/devices/extra?method=` (доплата).
- **Модель оплаты — add-on с общей датой продления (co-terming, как в SaaS):** `users.extra_devices=1` — опция подключена и
  продлевается вместе с подпиской одним платежом (`SubscriptionManager._order_amounts`: месяц 99+99, год 999+999; в журнале
  `payment_orders.price` = сумма, `base_price` = тариф, срок и `paid_periods` — по `base_price`). Подключил посреди оплаченного
  срока — `buy_extra_devices`: доплата за остаток пропорционально дням (`extra_devices_quote`, минимум 10 ₽ / $1.8 — у
  NOWPayments минимум счёта), заказ `product='extra_devices'` → `_activate_extra_devices` (`extra_devices_until` = дата подписки).
  На триале опция просто включается и входит в первую оплату. Отключение — устройства до конца оплаченного срока.
- `device_guard`: лимит = `DEVICE_LIMIT` (+3, пока `extra_devices_until` не прошёл); служебные hwid (`DEVICE_LIMIT_EXEMPT_HWIDS`)
  в лимит не входят и в /devices не показываются. Убранное устройство (`devices.removed_at`) получает заглушку `device_removed`.
  Опция закончилась, а устройств больше лимита — работают первые по времени подключения, остальным заглушка `device_limit`.
- Брошенные оплаты: отметка «владельцу уже написали» — `payment_orders.abandoned_alerted_at` (файл abandoned_alerted.json удалён).
- INCY: профиль маршрутизации Happ отдаётся INCY как `incy://routing/onadd/…`, пока за флагом `settings.INCY_ROUTING_ENABLED=False`;
  проверка на устройстве — `/sub/<uuid>?incy_routing=1`.
- Удержание в `stats_growth.py` (с 13.10, определение владельца): (продлили + пытались продлить) ÷ все, у кого закончилась платная подписка.

### 14.10.2026: UDM Pay ожил без нашего магазина
- 14.10 04:53 МСК хост UDM Pay вернулся (новый IP 80.90.183.132, уже не Yandex Cloud), но на любой запрос нашего логина
  отвечает `HTTP 404 «No query results for model [App\Models\Accounts\Account]»` — магазин не найден. Нужна поддержка UDM Pay.
  Старый пинг хоста принял это за восстановление и в 04:54 разослал «оплата снова работает» 7 людям — зря.
- Теперь `udmpay.health_check` (запрос /order/status нашим логином) — основа `run_udmpay_watch`; `account_missing` = сбой.
  Ошибка 404 Account при создании заказа → тот же текст, что при сбое (с предложением криптовалюты), и попытка пишется в
  `payment_outage.json`. Эти 7 человек возвращены в список рассылки — получат её повторно при настоящем восстановлении.
- Тесты подменяют `settings.PAYMENT_OUTAGE_PATH` во всех BotIntegrationTestCase (раньше тест писал в боевой файл).
- `payment_outage.json` → `attempts` {user_id: время последней неудачной СБП}; письмо о брошенной оплате пишет «перед этим СБП
  не сработала», только если `failed_between` нашёл попытку этого человека за 2 ч до заказа (раньше — любому во время сбоя).
- Поддержка UDM Pay: восстановят 14–15.10 — эта дата в `PAYMENT_OUTAGE_REASON`; не заработает к 16.10 — поправить текст.
- Метки источника: все ссылки сайта на бота — `/go/bot?src=<страница>` → `t.me/…?start=site_<страница>` (`settings.site_bot_link`);
  бот пишет «Пришёл с сайта: <страница>» и ставит пригласившим владельца, как раньше (stats_growth «ссылка с сайта»). Ссылки в статьях
  блога (`blog_articles.json`) — `src=blog_<slug>`. Нечисловой /start больше не роняет создание пользователя.
