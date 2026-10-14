"""Рост по дням: новые в боте, триалы сайта, оплаты — со скользящими средними 3 и 7 дней.

    .venv/bin/python3 stats_growth.py                 # с 15.08.26 по вчера
    .venv/bin/python3 stats_growth.py --from 2026-09-28 --to 2026-10-05

Источники и правила (см. memory/feedback_stats_tables_define_columns.md):
  * Бот — новые аккаунты по users.start_date: /start в боте И вход на сайте через Telegram Login Widget (у вторых в
    логе нет «НОВЫЙ ПОЛЬЗОВАТЕЛЬ», есть «создан через Telegram Login Widget» — сверено 10.10.26, расхождений нет).
  * Триалы — trial_sessions.created_at, день по Москве.
  * Новые плательщики (MRR-прирост) — пользователь платит ВПЕРВЫЕ в жизни; повторные оплаты и продления
    сюда не входят. Кто пришёл до запуска UDM Pay (13.08.26) и платил переводом на карту (в БД оплаченных
    месяцев больше, чем видно в логах), — не «новый» ни в одну дату.
  * Все оплаты — по ДАТЕ ПЛАТЕЖА: строки «подписка активирована через UDM Pay|NOWPayments» в
    my_logger/info/ только для id из users (тестовые отсеиваются), плюс оплаты, которые бот
    потерял и зачислил позже, — на день реальной оплаты (LOST_PAYMENTS).
  * Продление (удержание; определение владельца 13.10.26) — из впервые заплативших помесячно, у кого платная подписка
    закончилась: (продлили + пытались продлить) ÷ все, у кого закончилась. Продлил — оплата ещё раз позже недели после
    первой. Пытался — нажимал оплату / создавал заказ / упёрся в недоступный UDM Pay позже недели после первой оплаты, но
    так и не заплатил (сбой оплаты 11.10.26: им дали бесплатный доступ, поэтому «оплачено до» у них в будущем — они
    всё равно считаются закончившимися). Считается на сегодня, не на --to.
  * Конверсия — из пришедших в бот, у кого 14-дневный триал уже кончился, доля с paid_periods > 0.
"""
import argparse
import collections
import glob
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
FIRST_DAY = date(2026, 8, 15)
TRIAL_DAYS = 14
# Оплаты, которые из-за бага нескольких «СБП» зачислили вручную позже (memory: project_lost_payments_multi_order):
# (user_id, день реальной оплаты, день записи в логе или None, если записи «активирована» не было)
LOST_PAYMENTS = [
    (80344908, date(2026, 9, 2), None),
    (6289915099, date(2026, 9, 9), date(2026, 10, 1)),
]
ACTIVATED = re.compile(r"^\S+ (\d\d-\w{3}-\d\d) INFO: (\d+): подписка активирована через (UDM Pay|NOWPayments|Whop)")
# цена конкретной оплаты: activate_paid_subscription пишет её строкой «Данные» прямо перед «активирована» (с 30.09.26;
# раньше цены в строке нет — тогда месячная: годовых оплат до 08.10 не было, видно по paid_periods)
PAID_PRICE = re.compile(r"INFO: (\d+): Данные \{'price': (\d+), 'paid_periods'")
# попытка оплатить: кнопки оплаты в боте, созданный заказ/счёт, недоступный UDM Pay
ATTEMPT = re.compile(r"^\S+ (\d\d-\w{3}-\d\d) \w+: (\d+): (Нажал кнопку (pay|add_subscription|select_plan_\w+|"
                     r"pay_method_\w+|change_plan)\.|UDM Pay не ответил|создан (заказ|счёт))")


UDM_START = date(2026, 8, 13)


def collect(db_path="users.db"):
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    users = {u: (datetime.strptime(sd, "%d.%m.%y").date(), pp or 0, price)
             for u, sd, pp, price in c.execute("SELECT user_id, start_date, paid_periods, price FROM users")}
    # оплачено до (бонусы за приглашённых и промо тоже двигают дату, поэтому не «оплата + 30 дней»)
    paid_until = {u: datetime.strptime(d, "%d.%m.%y").date()
                  for u, d in c.execute("SELECT user_id, next_payment_date FROM users")}
    bot, tri, pay, first = (collections.Counter() for _ in range(4))
    newcomers = {}  # впервые заплатившие помесячно (не до UDM Pay) -> даты оплат
    for sd, _, _ in users.values():
        bot[sd] += 1
    for (ca,) in c.execute("SELECT created_at FROM trial_sessions"):
        tri[datetime.fromisoformat(ca.replace(" ", "T")).astimezone(MSK).date()] += 1
    moved = {(u, logged) for u, _, logged in LOST_PAYMENTS if logged}
    paid_days = collections.defaultdict(list)
    paid_prices = collections.defaultdict(list)  # цена каждой оплаты, в том же порядке, что paid_days
    last_price = {}
    attempts = collections.defaultdict(list)
    for f in sorted(glob.glob("my_logger/info/*/*_info.log")):
        for line in open(f, errors="replace"):
            am = ATTEMPT.match(line)
            if am:
                attempts[int(am.group(2))].append(datetime.strptime(am.group(1), "%d-%b-%y").date())
                continue
            pm = PAID_PRICE.search(line)
            if pm:
                last_price[int(pm.group(1))] = int(pm.group(2))
                continue
            m = ACTIVATED.match(line)
            if m and int(m.group(2)) in users:
                uid, d = int(m.group(2)), datetime.strptime(m.group(1), "%d-%b-%y").date()
                price = last_price.pop(uid, 99)
                if (uid, d) not in moved:
                    paid_days[uid].append(d)
                    paid_prices[uid].append(price)
    for uid, real_day, _ in LOST_PAYMENTS:
        paid_days[uid].append(real_day)
        paid_prices[uid].append(99)
    for uid, days in paid_days.items():
        for d in days:
            pay[d] += 1
        start, paid_months, _ = users[uid]
        # по цене КАЖДОЙ оплаты, а не по текущей: кто перешёл с месяца на год, иначе выглядел бы так,
        # будто все его оплаты годовые (contract_lawyer, 08.10.26)
        logged_months = sum(12 if p == 999 else 1 for p in paid_prices[uid])
        first_price = min(zip(days, paid_prices[uid]))[1]
        paid_before_udm = start < UDM_START and paid_months > logged_months
        if not paid_before_udm:
            first[min(days)] += 1
            if first_price != 999:
                newcomers[uid] = (paid_until[uid], sorted(days))
    cohort = [(sd, pp) for sd, pp, _ in users.values()]
    return bot, tri, pay, first, cohort, paid_days, newcomers, attempts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", type=date.fromisoformat, default=FIRST_DAY)
    ap.add_argument("--to", dest="end", type=date.fromisoformat,
                    default=datetime.now(MSK).date() - timedelta(days=1))
    a = ap.parse_args()
    bot, tri, pay, first, users, paid_days, newcomers, attempts = collect()
    # окно средних считаем от FIRST_DAY, чтобы --from не обнулял начало средних
    days = [FIRST_DAY + timedelta(i) for i in range((a.end - FIRST_DAY).days + 1)]

    def ma(cnt, i, n):
        return f"{sum(cnt[days[j]] for j in range(i - n + 1, i + 1)) / n:.1f}" if i >= n - 1 else "—"

    print("| Дата | Бот | ср.3 | ср.7 | Триалы | ср.3 | ср.7 | Новые плательщики | ср.3 | ср.7 | Все оплаты |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, d in enumerate(days):
        if d >= a.start:
            print(f"| {d:%d.%m} | {bot[d]} | {ma(bot, i, 3)} | {ma(bot, i, 7)} | {tri[d]} | {ma(tri, i, 3)} | "
                  f"{ma(tri, i, 7)} | {first[d]} | {ma(first, i, 3)} | {ma(first, i, 7)} | {pay[d]} |")
    shown = [d for d in days if d >= a.start]
    n_first, n_all = sum(first[d] for d in shown), sum(pay[d] for d in shown)
    print(f"\nИтого {shown[0]:%d.%m}–{shown[-1]:%d.%m}: все оплаты {n_all} = новые плательщики {n_first} + "
          f"повторные/продления {n_all - n_first}")
    repeat = {u: ds for u, ds in paid_days.items() if len([d for d in ds if a.start <= d <= a.end]) > 1}
    print(f"Платили больше одного раза за период: {len(repeat)} чел. — "
          + ", ".join(f"{u} ({'/'.join(f'{d:%d.%m}' for d in sorted(ds))})" for u, ds in repeat.items()))
    cutoff = a.end - timedelta(days=TRIAL_DAYS)
    cohort = [pp for d, pp in users if FIRST_DAY <= d <= cutoff]
    paid = sum(1 for pp in cohort if pp > 0)
    # Удержание (первое продление) на СЕГОДНЯ — см. докстринг. Повтор оплаты в пределах недели — двойное нажатие.
    # Годовые не считаются — им продлевать через год.
    today = datetime.now(MSK).date()
    renewed = [u for u, (_, ds) in newcomers.items() if any(d > ds[0] + timedelta(days=7) for d in ds)]
    # попытка засчитывается, если была в конце оплаченного срока (за 10 дней до «оплачено до» и позже): клик по
    # оплате посреди месяца — не продление. 10, а не 3 дня — потому что пострадавшим от сбоя UDM Pay срок сдвинули
    # на +7 дней (632675764 — до 19.10, mairamay — до 20.10).
    tried = [u for u, (until, ds) in newcomers.items()
             if u not in renewed and any(d > ds[0] + timedelta(days=7) and d >= until - timedelta(days=10)
                                         for d in attempts.get(u, []))]
    gave_up = [u for u, (until, _) in newcomers.items() if u not in renewed and u not in tried and until < today]
    ended = len(renewed) + len(tried) + len(gave_up)
    if ended:
        print(f"Продление (на {today:%d.%m}): платная подписка закончилась у {ended} — продлили {len(renewed)}, "
              f"пытались продлить {len(tried)}, не пытались {len(gave_up)} — удержание "
              f"{100 * (len(renewed) + len(tried)) / ended:.0f}% ((продлили + пытались) ÷ закончилась); "
              f"у {len(newcomers) - ended} срок ещё не подошёл")
    print(f"\nКонверсия: пришли в бот {FIRST_DAY:%d.%m}–{cutoff:%d.%m} (триал кончился) — "
          f"оплатили {paid} из {len(cohort)} ({100 * paid / len(cohort):.0f}%)")


if __name__ == "__main__":
    main()
