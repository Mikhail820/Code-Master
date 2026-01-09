"""
Полная реализация БД согласно ТЗ CodeMaster
Включает: очередь расходования, транзакции, рефералы, когорты
"""

import aiosqlite
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger(__name__)

DB_PATH = "codemaster.db"


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
    
    async def connect(self):
        return await aiosqlite.connect(self.db_path, isolation_level='IMMEDIATE')
    
    # ========== ИНИЦИАЛИЗАЦИЯ БД ==========
    
    async def init_db(self):
        """Создание всей схемы БД из ТЗ"""
        async with await self.connect() as db:
            await db.executescript("""
                -- 1. ОСНОВНАЯ ТАБЛИЦА ПОЛЬЗОВАТЕЛЕЙ
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    referrer_id INTEGER,
                    cohort_date DATE DEFAULT CURRENT_DATE,
                    source TEXT DEFAULT 'organic',
                    is_sub_active BOOLEAN DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_active_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (referrer_id) REFERENCES users(user_id)
                );
                
                -- 2. БАЛАНСЫ И СТАТУСЫ (ядро экономики)
                CREATE TABLE IF NOT EXISTS user_balances (
                    user_id INTEGER PRIMARY KEY,
                    trial_days INTEGER DEFAULT 10,
                    paid_until DATETIME,
                    bonus_days INTEGER DEFAULT 0,
                    total_active_days INTEGER GENERATED ALWAYS AS (
                        trial_days + 
                        COALESCE(
                            MAX(0, 
                                CASE WHEN paid_until IS NOT NULL 
                                THEN JULIANDAY(paid_until) - JULIANDAY('now') 
                                ELSE 0 END
                            ), 0
                        ) +
                        bonus_days
                    ) VIRTUAL,
                    current_status TEXT DEFAULT 'frozen',
                    status_changed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_premium BOOLEAN DEFAULT 0,
                    premium_since DATETIME,
                    last_billing_date DATETIME,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                
                -- 3. ТРАНЗАКЦИИ ДНЕЙ (полный аудит)
                CREATE TABLE IF NOT EXISTS days_transactions (
                    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    transaction_type TEXT NOT NULL,
                    days_change INTEGER NOT NULL,
                    balance_type TEXT NOT NULL,
                    new_balance INTEGER NOT NULL,
                    related_user_id INTEGER,
                    metadata TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id),
                    FOREIGN KEY (related_user_id) REFERENCES users(user_id)
                );
                
                -- 4. РЕФЕРАЛЬНЫЕ СОБЫТИЯ (3-контурная система)
                CREATE TABLE IF NOT EXISTS referral_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    referrer_id INTEGER NOT NULL,
                    referred_id INTEGER NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    reward_granted BOOLEAN DEFAULT 0,
                    reward_type TEXT,
                    days_awarded INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    pending_until DATETIME,
                    FOREIGN KEY (referrer_id) REFERENCES users(user_id),
                    FOREIGN KEY (referred_id) REFERENCES users(user_id)
                );
                
                -- 5. БОТЫ КЛИЕНТОВ (с шифрованием)
                CREATE TABLE IF NOT EXISTS bots (
                    bot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    token_encrypted TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL,
                    bot_username TEXT,
                    config_json TEXT DEFAULT '{}',
                    is_running BOOLEAN DEFAULT 0,
                    last_active DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (owner_id) REFERENCES users(user_id)
                );
                
                -- 6. ПЛАТЕЖИ (Т-Банк и Telegram Stars)
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    currency TEXT DEFAULT 'RUB',
                    payment_method TEXT NOT NULL,
                    payment_status TEXT DEFAULT 'pending',
                    telegram_payment_charge_id TEXT UNIQUE,
                    days_awarded INTEGER,
                    metadata TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                
                -- 7. КОГОРТНЫЙ АНАЛИЗ (агрегационная)
                CREATE TABLE IF NOT EXISTS cohort_metrics (
                    cohort_date DATE,
                    day_number INTEGER,
                    users_count INTEGER,
                    active_users INTEGER,
                    paid_users INTEGER,
                    total_revenue REAL,
                    avg_referrals FLOAT,
                    avg_lifetime_days FLOAT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (cohort_date, day_number)
                );
                
                -- 8. АУДИТ-ЛОГ (для отладки)
                CREATE TABLE IF NOT EXISTS audit_log (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    details TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                -- ИНДЕКСЫ для производительности
                CREATE INDEX IF NOT EXISTS idx_users_status ON users(is_sub_active);
                CREATE INDEX IF NOT EXISTS idx_balances_status ON user_balances(current_status);
                CREATE INDEX IF NOT EXISTS idx_balances_premium ON user_balances(is_premium);
                CREATE INDEX IF NOT EXISTS idx_transactions_user ON days_transactions(user_id);
                CREATE INDEX IF NOT EXISTS idx_transactions_type ON days_transactions(transaction_type);
                CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referral_events(referrer_id);
                CREATE INDEX IF NOT EXISTS idx_referrals_pending ON referral_events(pending_until) WHERE pending_until IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
                CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(payment_status);
                CREATE INDEX IF NOT EXISTS idx_bots_owner ON bots(owner_id);
                CREATE INDEX IF NOT EXISTS idx_bots_running ON bots(is_running);
                CREATE INDEX IF NOT EXISTS idx_cohort_date ON cohort_metrics(cohort_date);
            """)
            await db.commit()
            logger.info("База данных инициализирована")
    
    # ========== ПОЛЬЗОВАТЕЛИ ==========
    
    async def create_or_update_user(
        self,
        telegram_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        referrer_id: Optional[int] = None,
        source: str = "organic"
    ) -> int:
        """Создание/обновление пользователя, возвращает user_id"""
        async with await self.connect() as db:
            async with db.execute(
                "SELECT user_id FROM users WHERE telegram_id = ?",
                (telegram_id,)
            ) as cursor:
                row = await cursor.fetchone()
            
            if row:
                user_id = row[0]
                await db.execute(
                    """
                    UPDATE users SET
                        username = COALESCE(?, username),
                        first_name = COALESCE(?, first_name),
                        last_name = COALESCE(?, last_name),
                        last_active_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    """,
                    (username, first_name, last_name, user_id)
                )
            else:
                cursor = await db.execute(
                    """
                    INSERT INTO users 
                    (telegram_id, username, first_name, last_name, referrer_id, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (telegram_id, username, first_name, last_name, referrer_id, source)
                )
                user_id = cursor.lastrowid
                
                await db.execute(
                    "INSERT INTO user_balances (user_id) VALUES (?)",
                    (user_id,)
                )
                
                await self.log_audit(
                    user_id=user_id,
                    action="USER_REGISTERED",
                    details={
                        "telegram_id": telegram_id,
                        "referrer_id": referrer_id,
                        "source": source
                    }
                )
            
            await db.commit()
            return user_id
    
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Получение пользователя с балансами"""
        async with await self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT 
                    u.*,
                    ub.trial_days,
                    ub.paid_until,
                    ub.bonus_days,
                    ub.total_active_days,
                    ub.current_status,
                    ub.is_premium,
                    ub.premium_since,
                    ub.last_billing_date
                FROM users u
                LEFT JOIN user_balances ub ON u.user_id = ub.user_id
                WHERE u.user_id = ?
                """,
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
    
    async def update_subscription_status(self, user_id: int, is_active: bool):
        """Обновление статуса подписки на канал"""
        async with await self.connect() as db:
            await db.execute(
                "UPDATE users SET is_sub_active = ? WHERE user_id = ?",
                (int(is_active), user_id)
            )
            await db.commit()
            await self.log_audit(
                user_id=user_id,
                action="SUBSCRIPTION_CHANGED",
                details={"is_active": is_active}
            )
    
    # ========== УПРАВЛЕНИЕ ДНЯМИ ==========
    
    async def add_trial_days(self, user_id: int, days: int, reason: str = ""):
        """Добавление trial-дней"""
        async with await self.connect() as db:
            async with db.execute(
                "SELECT trial_days FROM user_balances WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                current = row[0] if row else 0
            
            new_balance = current + days
            await db.execute(
                "UPDATE user_balances SET trial_days = ? WHERE user_id = ?",
                (new_balance, user_id)
            )
            
            await self.log_days_transaction(
                user_id=user_id,
                transaction_type="TRIAL_ADD",
                days_change=days,
                balance_type="trial",
                new_balance=new_balance,
                metadata={"reason": reason}
            )
            
            await db.commit()
    
    async def add_paid_days(self, user_id: int, days: int, payment_id: Optional[int] = None):
        """Добавление оплаченных дней (расширяет paid_until)"""
        async with await self.connect() as db:
            async with db.execute(
                "SELECT paid_until FROM user_balances WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                current_until = datetime.fromisoformat(row[0]) if row and row[0] else datetime.utcnow()
            
            new_until = max(datetime.utcnow(), current_until) + timedelta(days=days)
            
            await db.execute(
                "UPDATE user_balances SET paid_until = ? WHERE user_id = ?",
                (new_until.isoformat(), user_id)
            )
            
            await self.log_days_transaction(
                user_id=user_id,
                transaction_type="PAID_ADD",
                days_change=days,
                balance_type="paid",
                new_balance=days,
                metadata={
                    "paid_until": new_until.isoformat(),
                    "payment_id": payment_id
                }
            )
            
            await db.commit()
    
    async def add_bonus_days(self, user_id: int, days: int, reason: str = ""):
        """Добавление бонусных дней"""
        async with await self.connect() as db:
            async with db.execute(
                "SELECT bonus_days FROM user_balances WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                current = row[0] if row else 0
            
            new_balance = current + days
            
            if new_balance >= 30:
                await db.execute(
                    """
                    UPDATE user_balances 
                    SET bonus_days = ?,
                        is_premium = 1,
                        premium_since = COALESCE(premium_since, CURRENT_TIMESTAMP)
                    WHERE user_id = ?
                    """,
                    (new_balance, user_id)
                )
            else:
                await db.execute(
                    "UPDATE user_balances SET bonus_days = ? WHERE user_id = ?",
                    (new_balance, user_id)
                )
            
            await self.log_days_transaction(
                user_id=user_id,
                transaction_type="BONUS_ADD",
                days_change=days,
                balance_type="bonus",
                new_balance=new_balance,
                metadata={"reason": reason}
            )
            
            await db.commit()
    
    async def consume_day(self, user_id: int) -> bool:
        """
        Списывает 1 день по очереди: Trial → Paid → Bonus
        Возвращает True если дни были, False если закончились
        """
        async with await self.connect() as db:
            async with db.execute(
                """
                SELECT trial_days, paid_until, bonus_days 
                FROM user_balances 
                WHERE user_id = ?
                """,
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return False
                
                trial_days, paid_until_str, bonus_days = row
                paid_until = datetime.fromisoformat(paid_until_str) if paid_until_str else None
            
            now = datetime.utcnow()
            
            if trial_days > 0:
                new_trial = trial_days - 1
                await db.execute(
                    "UPDATE user_balances SET trial_days = ? WHERE user_id = ?",
                    (new_trial, user_id)
                )
                
                await self.log_days_transaction(
                    user_id=user_id,
                    transaction_type="DAILY_CONSUMPTION",
                    days_change=-1,
                    balance_type="trial",
                    new_balance=new_trial,
                    metadata={"source": "trial"}
                )
                
            elif paid_until and paid_until > now:
                new_paid_until = paid_until - timedelta(days=1)
                await db.execute(
                    "UPDATE user_balances SET paid_until = ? WHERE user_id = ?",
                    (new_paid_until.isoformat(), user_id)
                )
                
                remaining_days = max(0, (new_paid_until - now).days)
                await self.log_days_transaction(
                    user_id=user_id,
                    transaction_type="DAILY_CONSUMPTION",
                    days_change=-1,
                    balance_type="paid",
                    new_balance=remaining_days,
                    metadata={"source": "paid"}
                )
                
            elif bonus_days > 0:
                new_bonus = bonus_days - 1
                await db.execute(
                    "UPDATE user_balances SET bonus_days = ? WHERE user_id = ?",
                    (new_bonus, user_id)
                )
                
                await self.log_days_transaction(
                    user_id=user_id,
                    transaction_type="DAILY_CONSUMPTION",
                    days_change=-1,
                    balance_type="bonus",
                    new_balance=new_bonus,
                    metadata={"source": "bonus"}
                )
                
            else:
                await db.execute(
                    """
                    UPDATE user_balances 
                    SET current_status = 'expired',
                        status_changed_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    """,
                    (user_id,)
                )
                await self.log_audit(
                    user_id=user_id,
                    action="DAYS_EXPIRED",
                    details={"timestamp": now.isoformat()}
                )
                return False
            
            await db.execute(
                """
                UPDATE user_balances 
                SET last_billing_date = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (user_id,)
            )
            
            await db.commit()
            return True
    
    # ========== БОТЫ ==========
    
    async def create_bot(
        self,
        user_id: int,
        token_encrypted: str,
        token_hash: str,
        bot_username: str,
        config: Dict[str, Any] = None
    ) -> int:
        """Создание записи о боте"""
        async with await self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO bots 
                (owner_id, token_encrypted, token_hash, bot_username, config_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    token_encrypted,
                    token_hash,
                    bot_username,
                    json.dumps(config or {})
                )
            )
            bot_id = cursor.lastrowid
            
            await self.log_audit(
                user_id=user_id,
                action="BOT_CREATED",
                details={"bot_id": bot_id, "bot_username": bot_username}
            )
            
            await db.commit()
            return bot_id
    
    async def get_user_bots(self, user_id: int) -> List[Dict[str, Any]]:
        """Получение всех ботов пользователя"""
        async with await self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM bots WHERE owner_id = ? ORDER BY created_at DESC",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
    
    async def update_bot_config(self, bot_id: int, config: Dict[str, Any]):
        """Обновление конфигурации бота"""
        async with await self.connect() as db:
            await db.execute(
                "UPDATE bots SET config_json = ? WHERE bot_id = ?",
                (json.dumps(config), bot_id)
            )
            await db.commit()
    
    async def set_bot_running(self, bot_id: int, is_running: bool):
        """Обновление статуса запуска бота"""
        async with await self.connect() as db:
            await db.execute(
                "UPDATE bots SET is_running = ?, last_active = CURRENT_TIMESTAMP WHERE bot_id = ?",
                (int(is_running), bot_id)
            )
            await db.commit()
    
    # ========== РЕФЕРАЛЬНАЯ СИСТЕМА ==========
    
    async def create_referral_event(
        self,
        referrer_id: int,
        referred_id: int,
        event_type: str,
        pending_days: int = 3
    ) -> bool:
        """
        Создание реферального события с отложенным начислением
        Возвращает True если событие создано, False если уже существует
        """
        async with await self.connect() as db:
            try:
                pending_until = datetime.utcnow() + timedelta(days=pending_days)
                await db.execute(
                    """
                    INSERT INTO referral_events 
                    (referrer_id, referred_id, event_type, pending_until)
                    VALUES (?, ?, ?, ?)
                    """,
                    (referrer_id, referred_id, event_type, pending_until.isoformat())
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False
    
    async def get_pending_referrals(self) -> List[Dict[str, Any]]:
        """Получение рефералов, готовых к начислению (прошло 3 дня)"""
        async with await self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM referral_events 
                WHERE pending_until IS NOT NULL 
                AND pending_until <= datetime('now')
                AND reward_granted = 0
                """
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
    
    async def mark_referral_rewarded(
        self,
        event_id: int,
        reward_type: str,
        days_awarded: int
    ):
        """Отметка реферала как награжденного"""
        async with await self.connect() as db:
            await db.execute(
                """
                UPDATE referral_events 
                SET reward_granted = 1,
                    reward_type = ?,
                    days_awarded = ?,
                    pending_until = NULL
                WHERE event_id = ?
                """,
                (reward_type, days_awarded, event_id)
            )
            await db.commit()
    
    async def get_user_referrals(self, user_id: int) -> List[Dict[str, Any]]:
        """Получение рефералов пользователя"""
        async with await db.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT re.*, u.username, u.first_name
                FROM referral_events re
                JOIN users u ON re.referred_id = u.user_id
                WHERE re.referrer_id = ?
                ORDER BY re.created_at DESC
                """,
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
    
    # ========== ПЛАТЕЖИ ==========
    
    async def create_payment(
        self,
        user_id: int,
        amount: float,
        currency: str,
        payment_method: str,
        days_awarded: int,
        metadata: Optional[Dict] = None
    ) -> int:
        """Создание записи о платеже"""
        async with await self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO payments 
                (user_id, amount, currency, payment_method, days_awarded, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    amount,
                    currency,
                    payment_method,
                    days_awarded,
                    json.dumps(metadata or {})
                )
            )
            payment_id = cursor.lastrowid
            
            await self.log_audit(
                user_id=user_id,
                action="PAYMENT_CREATED",
                details={
                    "payment_id": payment_id,
                    "amount": amount,
                    "method": payment_method
                }
            )
            
            await db.commit()
            return payment_id
    
    async def update_payment_status(
        self,
        payment_id: int,
        status: str,
        telegram_charge_id: Optional[str] = None
    ):
        """Обновление статуса платежа"""
        async with await self.connect() as db:
            update_fields = ["payment_status = ?", "completed_at = CURRENT_TIMESTAMP"]
            params = [status]
            
            if telegram_charge_id:
                update_fields.append("telegram_payment_charge_id = ?")
                params.append(telegram_charge_id)
            
            params.append(payment_id)
            
            await db.execute(
                f"""
                UPDATE payments 
                SET {', '.join(update_fields)}
                WHERE payment_id = ?
                """,
                params
            )
            
            await self.log_audit(
                user_id=None,
                action="PAYMENT_UPDATED",
                details={
                    "payment_id": payment_id,
                    "new_status": status,
                    "charge_id": telegram_charge_id
                }
            )
            
            await db.commit()
    
    # ========== АНАЛИТИКА ==========
    
    async def update_cohort_metrics(self):
        """Обновление метрик когорт (вызывать ежедневно)"""
        async with await self.connect() as db:
            await db.execute(
                "DELETE FROM cohort_metrics WHERE created_at >= date('now', 'start of day')"
            )
            
            await db.executescript("""
                INSERT INTO cohort_metrics 
                (cohort_date, day_number, users_count, active_users, paid_users, total_revenue, avg_referrals)
                
                WITH cohort_days AS (
                    SELECT 
                        cohort_date,
                        CAST(JULIANDAY('now') - JULIANDAY(cohort_date) AS INTEGER) AS day_number
                    FROM users
                    GROUP BY cohort_date
                    HAVING day_number >= 0
                ),
                user_activity AS (
                    SELECT 
                        u.cohort_date,
                        u.user_id,
                        CASE WHEN julianday('now') - julianday(u.last_active_at) <= 1 THEN 1 ELSE 0 END AS is_active_today,
                        CASE WHEN ub.paid_until >= date('now') THEN 1 ELSE 0 END AS has_active_paid
                    FROM users u
                    LEFT JOIN user_balances ub ON u.user_id = ub.user_id
                ),
                referral_counts AS (
                    SELECT 
                        u.cohort_date,
                        u.user_id,
                        COUNT(re.event_id) AS referral_count
                    FROM users u
                    LEFT JOIN referral_events re ON u.user_id = re.referrer_id
                    GROUP BY u.user_id
                ),
                payment_totals AS (
                    SELECT 
                        u.cohort_date,
                        SUM(p.amount) as total_revenue
                    FROM users u
                    LEFT JOIN payments p ON u.user_id = p.user_id AND p.payment_status = 'success'
                    GROUP BY u.cohort_date
                )
                
                SELECT 
                    cd.cohort_date,
                    cd.day_number,
                    COUNT(DISTINCT u.user_id) as users_count,
                    SUM(ua.is_active_today) as active_users,
                    SUM(ua.has_active_paid) as paid_users,
                    COALESCE(pt.total_revenue, 0) as total_revenue,
                    COALESCE(AVG(rc.referral_count), 0) as avg_referrals
                FROM cohort_days cd
                JOIN users u ON u.cohort_date = cd.cohort_date
                LEFT JOIN user_activity ua ON u.user_id = ua.user_id
                LEFT JOIN referral_counts rc ON u.user_id = rc.user_id
                LEFT JOIN payment_totals pt ON u.cohort_date = pt.cohort_date
                GROUP BY cd.cohort_date, cd.day_number
            """)
            
            await db.commit()
    
    async def get_daily_stats(self) -> Dict[str, Any]:
        """Получение ежедневной статистики"""
        async with await self.connect() as db:
            stats = {}
            
            async with db.execute("""
                SELECT 
                    COUNT(*) as total_users,
                    SUM(CASE WHEN is_sub_active = 1 THEN 1 ELSE 0 END) as active_subscribers,
                    SUM(CASE WHEN ub.current_status = 'active' THEN 1 ELSE 0 END) as active_bots,
                    COUNT(DISTINCT b.bot_id) as total_bots
                FROM users u
                LEFT JOIN user_balances ub ON u.user_id = ub.user_id
                LEFT JOIN bots b ON u.user_id = b.owner_id
            """) as cursor:
                row = await cursor.fetchone()
                if row:
                    stats.update(dict(row))
            
            async with db.execute("""
                SELECT 
                    COUNT(*) as total_payments,
                    SUM(amount) as total_revenue,
                    SUM(days_awarded) as total_days_sold
                FROM payments 
                WHERE payment_status = 'success'
            """) as cursor:
                row = await cursor.fetchone()
                if row:
                    stats.update(dict(row))
            
            async with db.execute("""
                SELECT 
                    COUNT(*) as total_referrals,
                    SUM(CASE WHEN reward_granted = 1 THEN 1 ELSE 0 END) as completed_referrals,
                    SUM(days_awarded) as total_days_awarded
                FROM referral_events
            """) as cursor:
                row = await cursor.fetchone()
                if row:
                    stats.update(dict(row))
            
            return stats
    
    # ========== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ==========
    
    async def log_days_transaction(
        self,
        user_id: int,
        transaction_type: str,
        days_change: int,
        balance_type: str,
        new_balance: int,
        related_user_id: Optional[int] = None,
        metadata: Optional[Dict] = None
    ):
        """Логирование транзакции с днями"""
        async with await self.connect() as db:
            await db.execute(
                """
                INSERT INTO days_transactions 
                (user_id, transaction_type, days_change, balance_type, new_balance, related_user_id, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    transaction_type,
                    days_change,
                    balance_type,
                    new_balance,
                    related_user_id,
                    json.dumps(metadata or {})
                )
            )
    
    async def log_audit(self, user_id: Optional[int], action: str, details: Optional[Dict] = None):
        """Логирование аудита"""
        async with await self.connect() as db:
            await db.execute(
                "INSERT INTO audit_log (user_id, action, details) VALUES (?, ?, ?)",
                (user_id, action, json.dumps(details) if details else None)
            )
    
    async def cleanup_expired_users(self, days_to_keep: int = 7):
        """Очистка пользователей в статусе expired дольше N дней"""
        async with await self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT u.user_id 
                FROM users u
                JOIN user_balances ub ON u.user_id = ub.user_id
                WHERE ub.current_status = 'expired'
                AND ub.status_changed_at <= datetime('now', ?)
                """,
                (f"-{days_to_keep} days",)
            ) as cursor:
                users_to_delete = [row[0] for row in await cursor.fetchall()]
            
            for user_id in users_to_delete:
                await self.delete_bots_by_owner(user_id)
                await db.execute(
                    "UPDATE user_balances SET current_status = 'deleted' WHERE user_id = ?",
                    (user_id,)
                )
                await self.log_audit(
                    user_id=user_id,
                    action="USER_DELETED_AUTO",
                    details={"reason": f"expired_for_{days_to_keep}_days"}
                )
            
            await db.commit()
            return len(users_to_delete)
    
    async def delete_bots_by_owner(self, owner_id: int):
        """Удаление всех ботов пользователя"""
        async with await self.connect() as db:
            await db.execute(
                "DELETE FROM bots WHERE owner_id = ?",
                (owner_id,)
            )


db = Database()