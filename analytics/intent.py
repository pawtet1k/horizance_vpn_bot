"""Все, кто хотел оплатить (любой канал), по окнам 5 дней. Только чтение логов и users.db."""
import re,glob,sqlite3,collections
from datetime import datetime,date,timedelta
P='/home/pashavpsadm/horizance_vpn_bot/'
OWNER=183640993
L=re.compile(r"^(\d\d:\d\d:\d\d) (\d\d-\w{3}-\d\d) \w+: (\d+): (.*)")
INTENT=re.compile(r"Нажал кнопку (pay|add_subscription|select_plan_\w+|pay_method_\w+|change_plan|devx\w*|extra\w*)\.|^(UDM Pay не ответил|ошибка создания заказа UDM Pay|создан заказ|создан счёт|создана оплата|доп\. устройства — доплата)")
FAIL=re.compile(r"^(UDM Pay не ответил при создании заказа|ошибка создания заказа UDM Pay: No query results)")
intent=collections.defaultdict(list); fail=collections.defaultdict(list); paid=collections.defaultdict(list)
for f in sorted(glob.glob(P+'my_logger/info/*/*_info.log')):
    for l in open(f,errors='replace'):
        m=L.match(l)
        if not m: continue
        t=datetime.strptime(m.group(2)+' '+m.group(1),'%d-%b-%y %H:%M:%S'); u=int(m.group(3)); x=m.group(4)
        if INTENT.search(x): intent[u].append(t)
        if FAIL.match(x): fail[u].append(t)
        if x.startswith('подписка активирована через') or x.startswith('доп. устройства оплачены'): paid[u].append(t)
db=sqlite3.connect(f'file:{P}users.db?mode=ro',uri=True)
users={u:(n,pp) for u,n,pp in db.execute('select user_id,username,paid_periods from users')}
for u,ca,pa in db.execute('select user_id,created_at,paid_at from payment_orders'):
    u=int(u); intent[u].append(datetime.fromisoformat(ca)+timedelta(hours=3))
    if pa: paid[u].append(datetime.fromisoformat(pa)+timedelta(hours=3))
OUT=datetime(2026,10,11,8,0)
W=[(date(2026,9,25),date(2026,9,29)),(date(2026,9,30),date(2026,10,4)),(date(2026,10,5),date(2026,10,9)),(date(2026,10,10),date(2026,10,14))]
import sys
detail=len(sys.argv)>1
for a,b in W:
    s=datetime(a.year,a.month,a.day); e=datetime(b.year,b.month,b.day)+timedelta(days=1)
    rows=[]
    for u,ts in intent.items():
        if u==OWNER or u not in users: continue
        tw=[t for t in ts if s<=t<e]
        if not tw: continue
        t0=min(tw)
        first_time = not any(p<t0 for p in paid[u])
        did = any(p>=t0 - timedelta(minutes=5) and p<e+timedelta(days=0) for p in paid[u])
        stopped = (not did) and any(s<=f<e and f>=OUT for f in fail[u])
        rows.append((u,users[u][0],t0,first_time,'оплатил' if did else ('сбой' if stopped else 'передумал')))
    fn=[r for r in rows if r[3]]; rn=[r for r in rows if not r[3]]
    c=lambda rs,k: sum(r[4]==k for r in rs)
    print(f"{a:%d.%m}–{b:%d.%m}: хотели {len(rows)} (впервые {len(fn)}, продление {len(rn)}) | оплатили {c(rows,'оплатил')} (впервые {c(fn,'оплатил')}) | остановил сбой {c(rows,'сбой')} (впервые {c(fn,'сбой')}) | передумали сами {c(rows,'передумал')} (впервые {c(fn,'передумал')})")
    if detail and a==W[-1][0]:
        for r in sorted(rows,key=lambda r:r[2]): print('   ',r[0],'@'+str(r[1]),r[2].strftime('%d.%m %H:%M'),'впервые' if r[3] else 'продление',r[4])
