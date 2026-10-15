"""Полная воронка от базы новых (окна по 5 дней, первые 24 ч от прихода) + где бросают оплату. Только чтение."""
import re,glob,sqlite3,collections
from datetime import datetime,date,timedelta,timezone
P='/home/pashavpsadm/horizance_vpn_bot/'; OWNER=183640993; MSK=timezone(timedelta(hours=3))
L=re.compile(r"^(\d\d:\d\d:\d\d) (\d\d-\w{3}-\d\d) \w+: (\d+): (.*)")
ev=collections.defaultdict(list)   # u -> [(t, step)]
arr={}
def step(x):
    if re.match(r"Нажал кнопку (pay|add_subscription|change_plan)\.",x): return 'click'
    if re.match(r"Нажал кнопку select_plan_\w+\.",x) or x.startswith("Данные {'price'"): return 'plan'
    if re.match(r"Нажал кнопку pay_method_\w+\.",x): return 'method'
    if x.startswith(("создан заказ","создан счёт","создана оплата","повторно отдана ссылка")): return 'link'
    if x.startswith(("UDM Pay не ответил при создании заказа","ошибка создания заказа UDM Pay")): return 'fail'
    if x.startswith("подписка активирована через"): return 'paid'
for f in sorted(glob.glob(P+'my_logger/info/*/*_info.log')):
    for l in open(f,errors='replace'):
        m=L.match(l)
        if not m: continue
        t=datetime.strptime(m.group(2)+' '+m.group(1),'%d-%b-%y %H:%M:%S'); u=int(m.group(3)); x=m.group(4)
        if x.startswith("НОВЫЙ ПОЛЬЗОВАТЕЛЬ") or "создан через Telegram Login Widget" in x: arr.setdefault(u,t)
        s=step(x)
        if s: ev[u].append((t,s))
db=sqlite3.connect(f'file:{P}users.db?mode=ro',uri=True)
users={u for (u,) in db.execute('select user_id from users')}
for u,ca,pa in db.execute('select user_id,created_at,paid_at from payment_orders'):
    u=int(u); ev[u].append((datetime.fromisoformat(ca)+timedelta(hours=3),'link'))
    if pa: ev[u].append((datetime.fromisoformat(pa)+timedelta(hours=3),'paid'))
dev=collections.defaultdict(list)
for uid,fs in db.execute("select user_id,first_seen from devices where user_id not like 't_%'"):
    try: u=int(uid)
    except: continue
    d=datetime.fromisoformat(fs.replace(' ','T')); d=d.astimezone(MSK).replace(tzinfo=None) if d.tzinfo else d
    dev[u].append(d)
OUT=datetime(2026,10,11,8,0); NOW=datetime.now(MSK).replace(tzinfo=None)
ORDER=['click','plan','method','reached','paid']
W=[(date(2026,9,25),date(2026,9,29)),(date(2026,9,30),date(2026,10,4)),(date(2026,10,5),date(2026,10,9)),(date(2026,10,10),date(2026,10,14))]
def reached(u,s,e):  # дошёл до момента оплаты: получил ссылку/счёт или упёрся в сбой
    return {k for t,k in ev[u] if s<=t<=e}
print("ВОРОНКА НОВЫХ (первые 24 ч от прихода; 'оплатили' = оплатили + остановил сбой)")
for a,b in W:
    S=datetime(a.year,a.month,a.day); E=datetime(b.year,b.month,b.day)+timedelta(days=1)
    base=[u for u,t in arr.items() if S<=t<E and u in users and u!=OWNER]
    c=collections.Counter(); partial=0
    for u in base:
        t0=arr[u]; t1=t0+timedelta(hours=24); partial+= t1>NOW
        ks=reached(u,t0,t1)
        if any(t0-timedelta(hours=1)<=d<=t1 for d in dev[u]): c['vpn']+=1
        if ks & {'click','plan','method','link','fail','paid'}: c['click']+=1
        if ks & {'plan','method','link','fail','paid'}: c['plan']+=1
        if ks & {'method','link','fail','paid'}: c['method']+=1
        if ks & {'link','fail','paid'}: c['reached']+=1
        if 'paid' in ks: c['paid']+=1
        elif 'fail' in ks and any(k=='fail' and t>=OUT and t0<=t<=t1 for t,k in ev[u]): c['paid_cf']+=1
    n=len(base); pc=lambda k:f"{c[k]} ({100*c[k]/n:.0f}%)"
    print(f"{a:%d.%m}–{b:%d.%m}: пришли {n} | настроили VPN {pc('vpn')} | нажали оплатить {pc('click')} | выбрали тариф {pc('plan')} | выбрали способ {pc('method')} | получили ссылку или сбой {pc('reached')} | оплатили {c['paid']} + сбой {c['paid_cf']} = {c['paid']+c['paid_cf']} ({100*(c['paid']+c['paid_cf'])/n:.0f}%)" + (f" | неполные сутки у {partial}" if partial else ""))
print("\nГДЕ БРОСАЮТ (все, кто хотел оплатить в окне, любой канал; без оплаты и без сбоя)")
for a,b in W:
    S=datetime(a.year,a.month,a.day); E=datetime(b.year,b.month,b.day)+timedelta(days=1)
    stop=collections.Counter(); who=collections.defaultdict(list); total=0
    for u in ev:
        if u==OWNER or u not in users: continue
        ks=reached(u,S,E)
        if not ks or ks=={'paid'} and False: continue
        if not ks & {'click','plan','method','link','fail','paid'}: continue
        total+=1
        if 'paid' in reached(u,S,E+timedelta(days=3)): continue
        if 'fail' in ks and any(k=='fail' and t>=OUT and S<=t<E for t,k in ev[u]): continue
        last='получил ссылку, не оплатил' if 'link' in ks else 'выбрал способ, ссылки нет' if 'method' in ks else 'выбрал тариф, способ не выбрал' if 'plan' in ks else 'увидел тарифы, не выбрал'
        stop[last]+=1; who[last].append(u)
    print(f"{a:%d.%m}–{b:%d.%m}: хотели {total}, бросили {sum(stop.values())}: "+'; '.join(f"{k} — {v}" for k,v in stop.most_common()))

print("\nКТО БРОСИЛ НА ТАРИФАХ (время первого клика, был ли новым в эти сутки)")
for a,b in W:
    S=datetime(a.year,a.month,a.day); E=datetime(b.year,b.month,b.day)+timedelta(days=1)
    for u in ev:
        if u==OWNER or u not in users: continue
        ks=reached(u,S,E)
        if 'click' not in ks or ks & {'plan','method','link','fail','paid'}: continue
        if 'paid' in reached(u,S,E+timedelta(days=3)): continue
        t=min(t for t,k in ev[u] if k=='click' and S<=t<E)
        new = u in arr and t-arr[u] < timedelta(hours=24)
        print(f"  {a:%d.%m}  {u}  {t:%d.%m %H:%M}  {'новый' if new else 'пришёл '+(arr[u].strftime('%d.%m') if u in arr else 'давно')}")
