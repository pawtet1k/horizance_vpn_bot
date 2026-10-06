import sqlite3
from models import User, TrialUser, WebUser, trial_expired
from datetime import datetime, timezone
import credentials
import settings
from my_logger import logger


class UserManager:
    DB_NAME = 'users.db'

    def __init__(self, db_name=None):
        self.db_name = db_name if db_name is not None else self.DB_NAME
        self.create_table()
        logger.debug(f'Экземпляр UserManager для {self.db_name} создан.')

    def execute_query(
        self,
        query,
        params=(),
        fetchone=False,
        fetchall=False
    ):
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            if fetchone:
                return cursor.fetchone()
            if fetchall:
                return cursor.fetchall()

    # Общий блок колонок VPN-подписки — одинаковый для users и web_users,
    # зеркалит models._subscription_defaults().
    _SUBSCRIPTION_COLUMNS_SQL = '''
        referrer_id INTEGER,
        referrals INTEGER,
        paid_periods INTEGER,
        client_ip TEXT,
        device_os TEXT,
        client_uuid TEXT,
        start_date TEXT,
        next_payment_date TEXT,
        status TEXT,
        payment_token TEXT,
        pending_order_uid TEXT,
        pending_subscription_uid TEXT,
        vpn_protocol TEXT,
        server_public_key TEXT,
        server_ip TEXT,
        price INTEGER,
        fingerprint TEXT,
        traffic_in INTEGER,
        traffic_out INTEGER,
        traffic_period_start INTEGER,
        traffic_period_date TEXT,
        notifications TEXT
    '''

    def create_table(self):
        query = f'''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            phone TEXT,
            language TEXT,
            {self._SUBSCRIPTION_COLUMNS_SQL}
        )'''
        #self.execute_query("ALTER TABLE users RENAME TO dump")
        self.execute_query(query)
        try:
            self.execute_query("ALTER TABLE users ADD COLUMN notifications TEXT")
        except sqlite3.OperationalError:
            pass  # столбец уже существует у ранее созданной таблицы
        try:
            self.execute_query("ALTER TABLE users ADD COLUMN pending_order_uid TEXT")
        except sqlite3.OperationalError:
            pass  # столбец уже существует у ранее созданной таблицы
        try:
            self.execute_query(
                "ALTER TABLE users ADD COLUMN pending_subscription_uid TEXT"
            )
        except sqlite3.OperationalError:
            pass  # столбец уже существует у ранее созданной таблицы
        for table in ("users", "web_users"):
            try:
                self.execute_query(f"ALTER TABLE {table} ADD COLUMN traffic_period_start INTEGER")
            except sqlite3.OperationalError:
                pass  # столбец уже есть (или web_users ещё не создана — тогда он в CREATE)
            try:
                self.execute_query(f"ALTER TABLE {table} ADD COLUMN traffic_period_date TEXT")
            except sqlite3.OperationalError:
                pass
        self.create_trial_table()
        self.create_web_users_table()
        self.create_payment_orders_table()
        logger.debug('Таблица users загружена.')
        #self.execute_query(
          #  "ALTER TABLE users ADD COLUMN language INTEGER DEFAULT '0'"
        #)
        #self.execute_query("DROP TABLE dump")
        #logger.info('Операция с таблицей прошла успешно.')

    def add_user(self, **kwargs):
        user = User(**kwargs)
        user_data = vars(user)
        columns = ', '.join(user_data.keys())
        placeholders = ', '.join('?' * len(user_data))
        query = f'INSERT INTO users ({columns}) VALUES ({placeholders})'
        self.execute_query(query, tuple(user_data.values()))
        user_id = kwargs.get('user_id')
        logger.info(f'{user_id}: Пользователь добавлен в базу данных.')
        return self.get_user(user_id)

    def get_user(self, user_id):
        query = 'SELECT * FROM users WHERE user_id = ?'
        row = self.execute_query(query, (user_id,), fetchone=True)
        logger.debug(f'{user_id}: Сработал запрос информации из базы данных.')
        return User(**dict(row)) if row else None

    def update_user(self, user_id, **kwargs):
        columns = ', '.join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values())
        values.append(user_id)
        query = f'UPDATE users SET {columns} WHERE user_id = ?'
        self.execute_query(query, values)
        logger.info(f'{user_id}: Данные {kwargs} были успешно обновлены.')

    def is_user_new(self, user_id):
        query = 'SELECT 1 FROM users WHERE user_id = ?'
        row = self.execute_query(query, (user_id,), fetchone=True)
        logger.info(
            f'{user_id}: Проверка, новый ли пользователь: {not bool(row)}'
        )
        return not bool(row)

    # Все созданные заказы оплаты. pending_order_uid хранит только ПОСЛЕДНИЙ
    # заказ: человек жмёт «СБП» несколько раз, платит по первой ссылке, а
    # вебхук об этом заказе уже не находил владельца — деньги пришли, подписка
    # нет (02.09, 09.09, 30.09.26). Здесь владелец находится по любому заказу,
    # а paid_at не даёт зачесть один заказ дважды при повторном вебхуке.
    def create_payment_orders_table(self):
        self.execute_query("""CREATE TABLE IF NOT EXISTS payment_orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER,
            provider TEXT,
            price INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            paid_at TEXT
        )""")
        try:
            self.execute_query("ALTER TABLE payment_orders ADD COLUMN payment_link TEXT")
        except sqlite3.OperationalError:
            pass  # столбец уже существует

    def record_payment_order(self, order_id, user_id, provider, price, payment_link=None):
        if order_id:
            self.execute_query(
                "INSERT OR IGNORE INTO payment_orders "
                "(order_id, user_id, provider, price, payment_link) VALUES (?, ?, ?, ?, ?)",
                (str(order_id), user_id, provider, price, payment_link),
            )

    def get_recent_unpaid_order(self, user_id, provider, price, minutes):
        """Последний неоплаченный заказ с сохранённой ссылкой не старше minutes."""
        row = self.execute_query(
            "SELECT * FROM payment_orders WHERE user_id = ? AND provider = ? AND price = ? "
            "AND paid_at IS NULL AND payment_link IS NOT NULL "
            "AND created_at >= datetime('now', ?) ORDER BY created_at DESC LIMIT 1",
            (user_id, provider, price, f"-{int(minutes)} minutes"), fetchone=True,
        )
        return dict(row) if row else None

    def get_payment_order(self, order_id):
        row = self.execute_query(
            "SELECT * FROM payment_orders WHERE order_id = ?", (str(order_id),), fetchone=True
        )
        return dict(row) if row else None

    def mark_payment_order_paid(self, order_id) -> bool:
        """True — заказ зачтён сейчас, False — уже был зачтён раньше."""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.execute(
                "UPDATE payment_orders SET paid_at = datetime('now') "
                "WHERE order_id = ? AND paid_at IS NULL",
                (str(order_id),),
            )
            return cursor.rowcount == 1

    def get_user_by_order(self, order_id):
        """Владелец заказа: сначала по последнему заказу, затем по журналу."""
        user = self.get_user_by_pending_order_uid(order_id)
        if user is None:
            order = self.get_payment_order(order_id)
            if order:
                user = self.get_user(order["user_id"])
                logger.info(f"Заказ {order_id} найден в журнале заказов: user {order['user_id']}")
        return user

    def get_user_by_pending_order_uid(self, order_uid):
        query = 'SELECT * FROM users WHERE pending_order_uid = ?'
        row = self.execute_query(query, (order_uid,), fetchone=True)
        logger.info(
            f'Поиск по pending_order_uid={order_uid}: {"успешно" if row else "не найден"}'
        )
        return User(**dict(row)) if row else None

    def get_router_users(self):
        """Все, у кого настроен роутер и доступ не закрыт (триал, оплачено, отменено до конца периода)."""
        rows = self.execute_query(
            "SELECT * FROM users WHERE device_os = ? AND status != 'inactive'", (settings.ROUTER_DEVICE,),
            fetchall=True
        )
        return [User(**dict(r)) for r in rows or []]

    def add_traffic(self, user_id, delta_in_mb: int, delta_out_mb: int) -> int:
        """Прибавляет снятый с нод трафик и возвращает итог in+out в МБ."""
        self.execute_query(
            "UPDATE users SET traffic_in = COALESCE(traffic_in, 0) + ?, "
            "traffic_out = COALESCE(traffic_out, 0) + ? WHERE user_id = ?",
            (delta_in_mb, delta_out_mb, user_id))
        row = self.execute_query(
            "SELECT COALESCE(traffic_in, 0) + COALESCE(traffic_out, 0) AS total FROM users WHERE user_id = ?",
            (user_id,), fetchone=True)
        return row["total"] if row else 0

    def count_active_users(self) -> int:
        query = "SELECT COUNT(*) AS n FROM users WHERE status = 'active'"
        row = self.execute_query(query, (), fetchone=True)
        return row['n'] if row else 0

    def get_user_by_pending_subscription_uid(self, subscription_uid):
        """Владелец подписки СБП, ожидающей подтверждения привязки.

        Отдельно от get_user_by_pending_order_uid: вебхук об оплате и вебхук
        о привязке приходят на один и тот же заказ с разницей в секунды, и
        первый обнуляет pending_order_uid. Кто приходил вторым — терял
        пользователя. subscriptionUid известен с момента создания заказа и
        не зависит от того, кто выиграл эту гонку.
        """
        query = 'SELECT * FROM users WHERE pending_subscription_uid = ?'
        row = self.execute_query(query, (subscription_uid,), fetchone=True)
        logger.info(
            f'Поиск по pending_subscription_uid={subscription_uid}: '
            f'{"успешно" if row else "не найден"}'
        )
        return User(**dict(row)) if row else None

    def get_user_by_uuid(self, client_uuid):
        query = 'SELECT * FROM users WHERE client_uuid = ?'
        row = self.execute_query(query, (client_uuid,), fetchone=True)
        logger.info(f'Поиск по uuid={client_uuid}: {"успешно" if row else "не найден"}')
        return User(**dict(row)) if row else None

    def load_users(self):
        query = 'SELECT * FROM users'
        users = self.execute_query(query, fetchall=True)
        logger.debug(f'Была запрошена выгрузка всей базы данных')
        return [dict(user) for user in users]

    def get_users_with_uuid(self, table) -> list[dict]:
        query = f'SELECT * FROM {table} WHERE client_uuid IS NOT NULL'
        rows = self.execute_query(query, fetchall=True)
        logger.debug(
            f'Запрошены {table} юзеры с UUID, найдено: {len(rows)}'
        )
        return [dict(row) for row in rows] if rows else []

    def get_not_inactive_users_with_uuid(self, table) -> list[dict]:
        query = f'SELECT * FROM {table} WHERE client_uuid IS NOT NULL AND status != "inactive"'
        rows = self.execute_query(query, fetchall=True)
        logger.debug(
            f'Запрошены {table} юзеры, не inactive с UUID, найдено: {len(rows)}'
        )
        return [dict(row) for row in rows] if rows else []

# Trial table methods:

    def create_trial_table(self):
        query = '''CREATE TABLE IF NOT EXISTS trial_sessions (
            user_id TEXT NOT NULL,
            fingerprint TEXT,
            device_os TEXT,
            client_uuid TEXT PRIMARY KEY,
            client_ip TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            traffic_in INTEGER,
            traffic_out INTEGER
        )'''
        self.execute_query(query)
        logger.debug('Таблица trial_sessions загружена.')

    def add_trial_session(self, **kwargs) -> None:
        trial_user = TrialUser(**kwargs)
        user_data = vars(trial_user)
        columns = ', '.join(user_data.keys())
        placeholders = ', '.join('?' * len(user_data))
        query = f'''INSERT OR IGNORE INTO trial_sessions
            ({columns}) VALUES ({placeholders})'''
        self.execute_query(query, tuple(user_data.values()))
        user_id = kwargs.get('user_id')
        client_uuid = kwargs.get('client_uuid')
        logger.info(
            f'Trial-сессия для {user_id} добавлена: uuid={client_uuid[:8]}'
        )

    def get_expired_trial_sessions(self) -> list[dict]:
        # без WHERE по expires_at: строкой его сравнивать нельзя (см.
        # models.trial_expired), а строк всего сотни
        rows = self.execute_query('SELECT * FROM trial_sessions', fetchall=True)
        return [
            dict(row) for row in rows or [] if trial_expired(row['expires_at'])
        ]

    def delete_trial_session(self, client_uuid: str):
        query = 'DELETE FROM trial_sessions WHERE client_uuid = ?'
        self.execute_query(query, (client_uuid,))
        logger.info(f'Trial-сессия удалена: uuid={client_uuid[:8]}')

# Web-user table (аккаунты личного кабинета через Яндекс ID):

    def create_web_users_table(self):
        query = f'''CREATE TABLE IF NOT EXISTS web_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            login TEXT,
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            {self._SUBSCRIPTION_COLUMNS_SQL}
        )'''
        self.execute_query(query)
        try:
            self.execute_query("ALTER TABLE web_users ADD COLUMN pending_order_uid TEXT")
        except sqlite3.OperationalError:
            pass  # столбец уже существует у ранее созданной таблицы
        try:
            self.execute_query(
                "ALTER TABLE web_users ADD COLUMN pending_subscription_uid TEXT"
            )
        except sqlite3.OperationalError:
            pass  # столбец уже существует у ранее созданной таблицы
        logger.debug('Таблица web_users загружена.')
