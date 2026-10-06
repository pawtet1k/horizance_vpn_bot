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
All 85 tests currently pass (last verified 2026-10-06). test.py sets `HORIZANCE_LOG_DIR` to a temp dir before importing the project, so tests no longer write into the production `my_logger/` (older prod log lines with ids 333001, 444001, 777xxx, 888xxx or node "DE 10.0.0.1" came from tests before 2026-09-26). Two behavior notes worth knowing before "fixing" a future failure here:
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
