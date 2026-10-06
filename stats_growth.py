"""Рост по дням: новые в боте, триалы сайта, оплаты — со скользящими средними 3 и 7 дней.

    .venv/bin/python3 stats_growth.py                 # с 15.08.26 по вчера
    .venv/bin/python3 stats_growth.py --from 2026-09-28 --to 2026-10-05

Источники и правила (см. memory/feedback_stats_tables_define_columns.md):
  * Бот — users.start_date (сверено с логами «НОВЫЙ ПОЛЬЗОВАТЕЛЬ»: совпадает без тестовых id).
  * Триалы — trial_sessions.created_at, день по Москве.
  * Новые плательщики (MRR-прирост) — пользователь платит ВПЕРВЫЕ в жизни; повторные оплаты и продления
    сюда не входят. Кто пришёл до запуска UDM Pay (13.08.26) и платил переводом на карту (в БД оплаченных
    месяцев больше, чем видно в логах), — не «новый» ни в одну дату.
  * Все оплаты — по ДАТЕ ПЛАТЕЖА: строки «подписка активирована через UDM Pay|NOWPayments» в
    my_logger/info/ только для id из users (тестовые отсеиваются), плюс оплаты, которые бот
    потерял и зачислил позже, — на день реальной оплаты (LOST_PAYMENTS).
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
ACTIVATED = re.compile(r"^\S+ (\d\d-\w{3}-\d\d) INFO: (\d+): подписка активирована через (UDM Pay|NOWPayments)")


UDM_START = date(2026, 8, 13)


def collect(db_path="users.db"):
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    users = {u: (datetime.strptime(sd, "%d.%m.%y").date(), pp or 0, price)
             for u, sd, pp, price in c.execute("SELECT user_id, start_date, paid_periods, price FROM users")}
    bot, tri, pay, first = (collections.Counter() for _ in range(4))
    for sd, _, _ in users.values():
        bot[sd] += 1
    for (ca,) in c.execute("SELECT created_at FROM trial_sessions"):
        tri[datetime.fromisoformat(ca.replace(" ", "T")).astimezone(MSK).date()] += 1
    moved = {(u, logged) for u, _, logged in LOST_PAYMENTS if logged}
    paid_days = collections.defaultdict(list)
    for f in sorted(glob.glob("my_logger/info/*/*_info.log")):
        for line in open(f, errors="replace"):
            m = ACTIVATED.match(line)
            if m and int(m.group(2)) in users:
                uid, d = int(m.group(2)), datetime.strptime(m.group(1), "%d-%b-%y").date()
                if (uid, d) not in moved:
                    paid_days[uid].append(d)
    for uid, real_day, _ in LOST_PAYMENTS:
        paid_days[uid].append(real_day)
    for uid, days in paid_days.items():
        for d in days:
            pay[d] += 1
        start, paid_months, price = users[uid]
        logged_months = len(days) * (12 if price == 999 else 1)
        paid_before_udm = start < UDM_START and paid_months > logged_months
        if not paid_before_udm:
            first[min(days)] += 1
    cohort = [(sd, pp) for sd, pp, _ in users.values()]
    return bot, tri, pay, first, cohort, paid_days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", type=date.fromisoformat, default=FIRST_DAY)
    ap.add_argument("--to", dest="end", type=date.fromisoformat,
                    default=datetime.now(MSK).date() - timedelta(days=1))
    a = ap.parse_args()
    bot, tri, pay, first, users, paid_days = collect()
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
    print(f"\nКонверсия: пришли в бот {FIRST_DAY:%d.%m}–{cutoff:%d.%m} (триал кончился) — "
          f"оплатили {paid} из {len(cohort)} ({100 * paid / len(cohort):.0f}%)")


if __name__ == "__main__":
    main()
