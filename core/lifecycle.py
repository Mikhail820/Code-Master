"""
Движок жизненного цикла ботов согласно ТЗ CodeMaster
"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import logging

from core.database import db
from config import TARIFFS

logger = logging.getLogger(__name__)


class LifecycleEngine:
    """Единственный источник правды о состоянии пользователя и его ботов."""
    
    STATUS_ACTIVE = "active"
    STATUS_FROZEN = "frozen"
    STATUS_EXPIRED = "expired"
    STATUS_DELETED = "deleted"
    
    def __init__(self):
        self._status_cache = {}
        self._last_check = {}
    
    async def get_user_status(self, user_id: int, is_subscribed: bool = None) -> str:
        """Определяет и возвращает текущий статус пользователя."""
        cache_key = f"{user_id}_{is_subscribed}"
        now = datetime.utcnow()
        
        if cache_key in self._status_cache:
            cached_status, cached_time = self._status_cache[cache_key]
            if (now - cached_time).total_seconds() < 300:
                return cached_status
        
        user = await db.get_user(user_id)
        if not user:
            logger.warning(f"Пользователь {user_id} не найден в БД")
            return self.STATUS_DELETED
        
        if is_subscribed is None:
            is_subscribed = bool(user.get("is_sub_active", False))
        else:
            if is_subscribed != bool(user.get("is_sub_active")):
                await db.update_subscription_status(user_id, is_subscribed)
        
        if not is_subscribed:
            status = self.STATUS_FROZEN
        else:
            total_days = user.get("total_active_days", 0)
            
            if total_days > 0:
                status = self.STATUS_ACTIVE
                
                bonus_days = user.get("bonus_days", 0)
                is_premium = bonus_days >= 30
                
                current_premium = bool(user.get("is_premium", False))
                if is_premium != current_premium:
                    await self._update_user_premium_status(user_id, is_premium)
            
            else:
                status = self.STATUS_EXPIRED
                
                if user.get("current_status") != self.STATUS_EXPIRED:
                    await self._set_user_expired(user_id)
                    await self._send_expired_notification(user_id)
    
        current_status = user.get("current_status")
        if status != current_status:
            await self._update_user_status(user_id, status)
            
            await db.log_audit(
                user_id=user_id,
                action="STATUS_CHANGED",
                details={
                    "from": current_status,
                    "to": status,
                    "reason": "automatic_check"
                }
            )
            
            logger.info(f"Статус пользователя {user_id} изменен: {current_status} → {status}")
            
            await self._handle_status_change(user_id, current_status, status)
        
        self._status_cache[cache_key] = (status, now)
        
        return status
    
    async def daily_billing_task(self):
        """Задача для ежедневного списания дней."""
        logger.info("Запуск ежедневного биллинга...")
        
        try:
            async with await db.connect() as conn:
                conn.row_factory = lambda c, r: r[0]
                async with conn.execute(
                    """
                    SELECT u.user_id 
                    FROM users u
                    JOIN user_balances ub ON u.user_id = ub.user_id
                    WHERE u.is_sub_active = 1 
                    AND ub.current_status = 'active'
                    AND ub.total_active_days > 0
                    """
                ) as cursor:
                    user_ids = await cursor.fetchall()
            
            processed = 0
            expired = 0
            
            for user_id in user_ids:
                try:
                    had_days = await db.consume_day(user_id)
                    
                    if had_days:
                        processed += 1
                        await self.get_user_status(user_id)
                    else:
                        expired += 1
                        logger.info(f"У пользователя {user_id} закончились дни")
                        
                except Exception as e:
                    logger.error(f"Ошибка при списании дней у {user_id}: {e}")
                    await db.log_audit(
                        user_id=user_id,
                        action="BILLING_ERROR",
                        details={"error": str(e)}
                    )
            
            deleted_count = await db.cleanup_expired_users(days_to_keep=7)
            
            await db.update_cohort_metrics()
            
            logger.info(
                f"Биллинг завершен. "
                f"Обработано: {processed}, "
                f"Истекло: {expired}, "
                f"Удалено: {deleted_count}"
            )
            
            await self._send_billing_report(processed, expired, deleted_count)
            
        except Exception as e:
            logger.error(f"Критическая ошибка в daily_billing_task: {e}")
    
    async def add_days_to_user(
        self,
        user_id: int,
        days: int,
        days_type: str,
        reason: str = "",
        payment_id: Optional[int] = None
    ) -> bool:
        """Добавление дней пользователю с логированием."""
        try:
            if days_type == 'trial':
                await db.add_trial_days(user_id, days, reason)
            elif days_type == 'paid':
                await db.add_paid_days(user_id, days, payment_id)
            elif days_type == 'bonus':
                await db.add_bonus_days(user_id, days, reason)
            else:
                raise ValueError(f"Неизвестный тип дней: {days_type}")
            
            await self.get_user_status(user_id)
            
            logger.info(f"Добавлено {days} дней ({days_type}) пользователю {user_id}. Причина: {reason}")
            
            await self._send_days_added_notification(user_id, days, days_type, reason)
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка добавления дней пользователю {user_id}: {e}")
            await db.log_audit(
                user_id=user_id,
                action="ADD_DAYS_ERROR",
                details={
                    "days": days,
                    "type": days_type,
                    "reason": reason,
                    "error": str(e)
                }
            )
            return False
    
    async def get_days_summary(self, user_id: int) -> Dict[str, Any]:
        """Получение сводки по дням пользователя"""
        user = await db.get_user(user_id)
        if not user:
            return {}
        
        now = datetime.utcnow()
        paid_until = user.get("paid_until")
        
        paid_days = 0
        if paid_until:
            paid_until_dt = datetime.fromisoformat(paid_until) if isinstance(paid_until, str) else paid_until
            if paid_until_dt > now:
                paid_days = (paid_until_dt - now).days
        
        return {
            "trial_days": user.get("trial_days", 0),
            "paid_days": paid_days,
            "bonus_days": user.get("bonus_days", 0),
            "total_days": user.get("total_active_days", 0),
            "is_premium": bool(user.get("is_premium", False)),
            "premium_since": user.get("premium_since"),
            "status": user.get("current_status", "unknown"),
            "next_billing": self._get_next_billing_date(user)
        }
    
    async def can_create_bot(self, user_id: int) -> tuple[bool, str]:
        """Проверяет, может ли пользователь создать нового бота."""
        status = await self.get_user_status(user_id)
        
        if status == self.STATUS_FROZEN:
            return False, "❌ Для создания бота необходимо подписаться на канал"
        
        if status == self.STATUS_EXPIRED:
            return False, "❌ Дни обслуживания закончились. Пополните баланс."
        
        if status == self.STATUS_DELETED:
            return False, "❌ Ваш аккаунт удален. Обратитесь в поддержку."
        
        bots = await db.get_user_bots(user_id)
        if len(bots) >= 5:
            return False, "❌ Лимит ботов исчерпан (макс. 5). Удалите ненужных ботов."
        
        summary = await self.get_days_summary(user_id)
        if summary["total_days"] <= 0:
            return False, "❌ Нет активных дней для создания бота"
        
        return True, "✅ Вы можете создать нового бота"
    
    async def can_bot_respond(self, bot_id: int) -> bool:
        """Проверяет, может ли бот отвечать на сообщения."""
        try:
            async with await db.connect() as conn:
                async with conn.execute(
                    "SELECT owner_id FROM bots WHERE bot_id = ?",
                    (bot_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        return False
                    
                    owner_id = row[0]
            
            status = await self.get_user_status(owner_id)
            return status == self.STATUS_ACTIVE
            
        except Exception as e:
            logger.error(f"Ошибка проверки can_bot_respond для бота {bot_id}: {e}")
            return False
    
    async def check_expired_notifications(self):
        """Проверка пользователей в статусе EXPIRED."""
        try:
            async with await db.connect() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    """
                    SELECT u.user_id, u.telegram_id, ub.status_changed_at
                    FROM users u
                    JOIN user_balances ub ON u.user_id = ub.user_id
                    WHERE ub.current_status = 'expired'
                    AND ub.status_changed_at IS NOT NULL
                    """
                ) as cursor:
                    expired_users = [dict(row) for row in await cursor.fetchall()]
            
            now = datetime.utcnow()
            
            for user in expired_users:
                expired_since = datetime.fromisoformat(user["status_changed_at"])
                days_expired = (now - expired_since).days
                
                if days_expired in [1, 2, 3]:
                    await self._send_last_chance_notification(
                        user["user_id"],
                        user["telegram_id"],
                        days_expired
                    )
                    
        except Exception as e:
            logger.error(f"Ошибка в check_expired_notifications: {e}")
    
    def _get_next_billing_date(self, user: Dict[str, Any]) -> Optional[datetime]:
        """Рассчитывает дату следующего списания дней"""
        last_billing = user.get("last_billing_date")
        if not last_billing:
            return datetime.utcnow() + timedelta(days=1)
        
        last_billing_dt = datetime.fromisoformat(last_billing) if isinstance(last_billing, str) else last_billing
        return last_billing_dt + timedelta(days=1)
    
    async def _handle_status_change(self, user_id: int, old_status: str, new_status: str):
        """Обработчик изменения статуса"""
        if (old_status == self.STATUS_FROZEN and new_status == self.STATUS_ACTIVE) or \
           (old_status == self.STATUS_ACTIVE and new_status == self.STATUS_FROZEN):
            
            bots = await db.get_user_bots(user_id)
            is_running = new_status == self.STATUS_ACTIVE
            
            for bot in bots:
                await db.set_bot_running(bot["bot_id"], is_running)
            
            action = "BOTS_RESUMED" if is_running else "BOTS_PAUSED"
            await db.log_audit(
                user_id=user_id,
                action=action,
                details={"count": len(bots)}
            )
    
    async def _update_user_premium_status(self, user_id: int, is_premium: bool):
        """Обновление Premium статуса пользователя"""
        async with await db.connect() as db_conn:
            if is_premium:
                await db_conn.execute(
                    """
                    UPDATE user_balances 
                    SET is_premium = 1,
                        premium_since = COALESCE(premium_since, CURRENT_TIMESTAMP)
                    WHERE user_id = ?
                    """,
                    (user_id,)
                )
            else:
                await db_conn.execute(
                    "UPDATE user_balances SET is_premium = 0 WHERE user_id = ?",
                    (user_id,)
                )
            await db_conn.commit()
    
    async def _update_user_status(self, user_id: int, status: str):
        """Обновление статуса пользователя в БД"""
        async with await db.connect() as db_conn:
            await db_conn.execute(
                """
                UPDATE user_balances 
                SET current_status = ?,
                    status_changed_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (status, user_id)
            )
            await db_conn.commit()
    
    async def _set_user_expired(self, user_id: int):
        """Установка статуса expired для пользователя"""
        async with await db.connect() as db_conn:
            await db_conn.execute(
                """
                UPDATE user_balances 
                SET current_status = 'expired',
                    status_changed_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (user_id,)
            )
            await db_conn.commit()
    
    async def _send_expired_notification(self, user_id: int):
        """Отправка уведомления об истечении дней"""
        logger.info(f"Уведомление об истечении дней отправлено пользователю {user_id}")
    
    async def _send_last_chance_notification(self, user_id: int, telegram_id: int, day: int):
        """Отправка уведомления 'последнего шанса'"""
        messages = {
            1: "⏳ У вас закончились дни обслуживания. У вас есть 3 дня чтобы пополнить баланс.",
            2: "⏳ Остался 1 день до блокировки ботов. Пополните баланс сейчас!",
            3: "🚨 Сегодня последний день! После блокировки восстановить ботов будет сложнее."
        }
        
        if day in messages:
            logger.info(f"Уведомление 'последнего шанса' (день {day}) для {user_id}")
    
    async def _send_days_added_notification(self, user_id: int, days: int, days_type: str, reason: str):
        """Уведомление о добавлении дней"""
        logger.info(f"Уведомление о добавлении {days} дней ({days_type}) пользователю {user_id}")
    
    async def _send_billing_report(self, processed: int, expired: int, deleted: int):
        """Отправка отчета админу о биллинге"""
        if not processed and not expired and not deleted:
            return
        
        report = (
            "📊 Отчет ежедневного биллинга:\n"
            f"✅ Обработано пользователей: {processed}\n"
            f"⏳ Истекли дни у: {expired}\n"
            f"🗑️  Удалено аккаунтов: {deleted}\n"
            f"🕐 Время: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        
        logger.info(report)
    
    async def get_user_for_api(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Подготовка данных пользователя для API/Mini App"""
        user = await db.get_user(user_id)
        if not user:
            return None
        
        summary = await self.get_days_summary(user_id)
        bots = await db.get_user_bots(user_id)
        
        return {
            "user_id": user_id,
            "telegram_id": user.get("telegram_id"),
            "username": user.get("username"),
            "status": user.get("current_status"),
            "is_subscribed": bool(user.get("is_sub_active")),
            "is_premium": summary["is_premium"],
            "premium_since": summary["premium_since"],
            "days": summary,
            "bots_count": len(bots),
            "created_at": user.get("created_at"),
            "last_active": user.get("last_active_at")
        }


lifecycle = LifecycleEngine()