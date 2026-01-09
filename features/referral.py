"""
Трёхконтурная реферальная система CodeMaster
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

from aiogram import Bot, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    Message
)

from core.database import db
from core.lifecycle import lifecycle
from config import REFERRAL_REWARDS, MAX_REFERRALS_PER_DAY, ABUSE_CHECK_HOURS
from features.payments import payment_processor

logger = logging.getLogger(__name__)


@dataclass
class ReferralReward:
    """Модель реферального вознаграждения"""
    event_type: str
    days: int
    delay_days: Optional[int] = None
    description: str = ""


class ReferralSystem:
    """Трёхконтурная реферальная система"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.rewards = {
            "bot_created": ReferralReward(
                event_type="bot_created",
                days=REFERRAL_REWARDS["bot_created"]["days"],
                delay_days=REFERRAL_REWARDS["bot_created"].get("delay_days", 3),
                description="+7 дней за создание бота другом"
            ),
            "first_payment_referrer": ReferralReward(
                event_type="first_payment",
                days=REFERRAL_REWARDS["first_payment_referrer"]["days"],
                description="+15 дней за первую оплату друга"
            ),
            "first_payment_referred": ReferralReward(
                event_type="first_payment",
                days=REFERRAL_REWARDS["first_payment_referred"]["days"],
                description="+10 дней новичку при первой оплате"
            )
        }
    
    async def handle_new_user(self, new_user_id: int, referrer_id: Optional[int] = None):
        """Обработка нового пользователя с рефералом."""
        if not referrer_id:
            return
        
        try:
            if await self._is_abuse_detected(referrer_id):
                logger.warning(f"Обнаружен абьюз у реферера {referrer_id}. Пропускаем начисление.")
                return
            
            created = await db.create_referral_event(
                referrer_id=referrer_id,
                referred_id=new_user_id,
                event_type="bot_created",
                pending_days=self.rewards["bot_created"].delay_days
            )
            
            if created:
                await self._send_referral_registered_notification(referrer_id, new_user_id)
                
                logger.info(
                    f"Создано реферальное событие: {referrer_id} -> {new_user_id} "
                    f"(начисление через {self.rewards['bot_created'].delay_days} дней)"
                )
            else:
                logger.warning(f"Реферальное событие уже существует: {referrer_id} -> {new_user_id}")
                
        except Exception as e:
            logger.error(f"Ошибка обработки нового пользователя с рефералом: {e}")
    
    async def handle_user_payment(self, user_id: int):
        """Обработка первой оплаты пользователя."""
        try:
            is_first_payment = await self._is_first_payment(user_id)
            if not is_first_payment:
                return
            
            user = await db.get_user(user_id)
            if not user:
                return
            
            referrer_id = user.get("referrer_id")
            if not referrer_id:
                return
            
            reward = self.rewards["first_payment_referrer"]
            success = await lifecycle.add_days_to_user(
                user_id=referrer_id,
                days=reward.days,
                days_type="bonus",
                reason=f"referral_first_payment_{user_id}"
            )
            
            if success:
                await db.create_referral_event(
                    referrer_id=referrer_id,
                    referred_id=user_id,
                    event_type="first_payment",
                    pending_days=0
                )
                
                await self._mark_first_payment_rewarded(user_id, referrer_id)
                
                await self._send_referral_payment_notification(referrer_id, user_id, reward.days)
            
            welcome_reward = self.rewards["first_payment_referred"]
            await lifecycle.add_days_to_user(
                user_id=user_id,
                days=welcome_reward.days,
                days_type="bonus",
                reason="welcome_first_payment"
            )
            
            logger.info(f"Обработана первая оплата пользователя {user_id}. Реферер {referrer_id} получил {reward.days} дней")
            
        except Exception as e:
            logger.error(f"Ошибка обработки платежа пользователя {user_id}: {e}")
    
    async def process_pending_referrals(self):
        """Обработка отложенных реферальных начислений."""
        try:
            pending_referrals = await db.get_pending_referrals()
            
            for referral in pending_referrals:
                event_id = referral["event_id"]
                referrer_id = referral["referrer_id"]
                referred_id = referral["referred_id"]
                
                referred_status = await lifecycle.get_user_status(referred_id)
                
                if referred_status == "active":
                    reward = self.rewards["bot_created"]
                    success = await lifecycle.add_days_to_user(
                        user_id=referrer_id,
                        days=reward.days,
                        days_type="bonus",
                        reason=f"referral_bot_created_{referred_id}"
                    )
                    
                    if success:
                        await db.mark_referral_rewarded(
                            event_id=event_id,
                            reward_type="bonus",
                            days_awarded=reward.days
                        )
                        
                        await self._send_referral_bonus_notification(
                            referrer_id,
                            referred_id,
                            reward.days
                        )
                        
                        logger.info(
                            f"Начислено {reward.days} дней рефереру {referrer_id} "
                            f"за реферала {referred_id}"
                        )
                    else:
                        logger.error(f"Ошибка начисления бонуса за реферала {referred_id}")
                else:
                    logger.info(
                        f"Реферал {referred_id} не активен (статус: {referred_status}). "
                        f"Начисление отложено."
                    )
            
            logger.info(f"Обработано {len(pending_referrals)} отложенных рефералов")
            
        except Exception as e:
            logger.error(f"Ошибка обработки отложенных рефералов: {e}")
    
    async def _is_abuse_detected(self, user_id: int) -> bool:
        """Проверка на абьюз реферальной системы."""
        try:
            async with await db.connect() as conn:
                async with conn.execute(
                    """
                    SELECT COUNT(*) as count
                    FROM referral_events
                    WHERE referrer_id = ?
                    AND created_at >= datetime('now', ?)
                    """,
                    (user_id, f"-{ABUSE_CHECK_HOURS} hours")
                ) as cursor:
                    row = await cursor.fetchone()
                    recent_referrals = row[0] if row else 0
            
            if recent_referrals >= MAX_REFERRALS_PER_DAY:
                logger.warning(
                    f"Обнаружен возможный абьюз у пользователя {user_id}: "
                    f"{recent_referrals} рефералов за {ABUSE_CHECK_HOURS} часов"
                )
                
                await db.log_audit(
                    user_id=user_id,
                    action="REFERRAL_ABUSE_DETECTED",
                    details={
                        "recent_referrals": recent_referrals,
                        "period_hours": ABUSE_CHECK_HOURS,
                        "limit": MAX_REFERRALS_PER_DAY
                    }
                )
                
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Ошибка проверки абьюза для {user_id}: {e}")
            return False
    
    async def _is_first_payment(self, user_id: int) -> bool:
        """Проверяет, является ли оплата первой для пользователя"""
        async with await db.connect() as conn:
            async with conn.execute(
                """
                SELECT COUNT(*) as count
                FROM payments
                WHERE user_id = ?
                AND payment_status = 'success'
                """,
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                successful_payments = row[0] if row else 0
            
            return successful_payments == 1
    
    async def _mark_first_payment_rewarded(self, user_id: int, referrer_id: int):
        """Помечает первую оплату как награжденную в реферальной системе"""
        try:
            async with await db.connect() as conn:
                await conn.execute(
                    """
                    UPDATE referral_events
                    SET reward_granted = 1,
                        reward_type = 'bonus',
                        days_awarded = ?,
                        pending_until = NULL
                    WHERE referrer_id = ?
                    AND referred_id = ?
                    AND event_type = 'first_payment'
                    """,
                    (self.rewards["first_payment_referrer"].days, referrer_id, user_id)
                )
                await conn.commit()
                
        except Exception as e:
            logger.error(f"Ошибка отметки первой оплаты: {e}")
    
    async def _send_referral_registered_notification(self, referrer_id: int, referred_id: int):
        """Уведомление о регистрации реферала"""
        try:
            async with await db.connect() as conn:
                async with conn.execute(
                    "SELECT telegram_id FROM users WHERE user_id = ?",
                    (referrer_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        return
                    telegram_id = row[0]
            
            referred_user = await db.get_user(referred_id)
            referred_name = (
                referred_user.get("first_name") or 
                referred_user.get("username") or 
                "новый пользователь"
            )
            
            message = (
                "👥 <b>Новый реферал!</b>\n\n"
                f"Пользователь <b>{referred_name}</b> зарегистрировался по вашей ссылке.\n\n"
                f"🎯 <i>Если он останется активным 3 дня, вы получите "
                f"{self.rewards['bot_created'].days} бонусных дней!</i>"
            )
            
            await self.bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode="HTML"
            )
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о регистрации реферала: {e}")
    
    async def _send_referral_payment_notification(self, referrer_id: int, referred_id: int, days: int):
        """Уведомление о первой оплате реферала"""
        try:
            async with await db.connect() as conn:
                async with conn.execute(
                    "SELECT telegram_id FROM users WHERE user_id = ?",
                    (referrer_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        return
                    telegram_id = row[0]
            
            message = (
                "💰 <b>Реферал совершил первую оплату!</b>\n\n"
                f"На ваш баланс начислено <b>+{days} бонусных дней</b>.\n\n"
                "🎖️ Продолжайте приглашать друзей, чтобы получить Premium статус!"
            )
            
            await self.bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode="HTML"
            )
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления об оплате реферала: {e}")
    
    async def _send_referral_bonus_notification(self, referrer_id: int, referred_id: int, days: int):
        """Уведомление о начислении бонуса за реферала"""
        try:
            async with await db.connect() as conn:
                async with conn.execute(
                    "SELECT telegram_id FROM users WHERE user_id = ?",
                    (referrer_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        return
                    telegram_id = row[0]
            
            referred_user = await db.get_user(referred_id)
            referred_name = (
                referred_user.get("first_name") or 
                referred_user.get("username") or 
                "ваш реферал"
            )
            
            message = (
                "🎁 <b>Бонус за реферала начислен!</b>\n\n"
                f"Пользователь <b>{referred_name}</b> остался активным 3 дня.\n"
                f"На ваш баланс начислено <b>+{days} бонусных дней</b>.\n\n"
                f"📊 Всего бонусных дней: {(await db.get_user(referrer_id))['bonus_days']}"
            )
            
            await self.bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode="HTML"
            )
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о бонусе: {e}")
    
    async def get_referral_stats(self, user_id: int) -> Dict[str, Any]:
        """Получение статистики рефералов"""
        try:
            referrals = await db.get_user_referrals(user_id)
            
            total_referrals = len(referrals)
            active_referrals = 0
            pending_referrals = 0
            rewarded_referrals = 0
            total_days_earned = 0
            
            for ref in referrals:
                if ref["reward_granted"]:
                    rewarded_referrals += 1
                    total_days_earned += ref["days_awarded"] or 0
                elif ref["pending_until"]:
                    pending_referrals += 1
                
                ref_status = await lifecycle.get_user_status(ref["referred_id"])
                if ref_status == "active":
                    active_referrals += 1
            
            user = await db.get_user(user_id)
            bonus_days = user["bonus_days"] if user else 0
            
            return {
                "total_referrals": total_referrals,
                "active_referrals": active_referrals,
                "pending_referrals": pending_referrals,
                "rewarded_referrals": rewarded_referrals,
                "total_days_earned": total_days_earned,
                "current_bonus_days": bonus_days,
                "days_to_premium": max(0, 30 - bonus_days),
                "referral_link": f"https://t.me/{(await self.bot.get_me()).username}?start=ref_{user_id}",
                "referrals": referrals[:10]
            }
            
        except Exception as e:
            logger.error(f"Ошибка получения статистики рефералов: {e}")
            return {}
    
    def get_referral_keyboard(self, user_id: int) -> InlineKeyboardMarkup:
        """Клавиатура для реферальной системы"""
        keyboard = [
            [
                InlineKeyboardButton(
                    text="👥 Мои рефералы",
                    callback_data=f"referral_stats_{user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data=f"referral_info_{user_id}"
                ),
                InlineKeyboardButton(
                    text="🔗 Получить ссылку",
                    callback_data=f"referral_link_{user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❓ Как это работает",
                    callback_data="referral_help"
                )
            ]
        ]
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def cmd_referral(message: Message, referral_system: ReferralSystem):
    """Команда /referral - реферальная программа"""
    user_id = message.from_user.id
    
    stats = await referral_system.get_referral_stats(user_id)
    
    if not stats:
        await message.answer(
            "❌ Не удалось загрузить статистику рефералов. Попробуйте позже."
        )
        return
    
    response = (
        "👑 <b>Реферальная программа CodeMaster</b>\n\n"
        
        "<b>Как это работает:</b>\n"
        "1. Приглашайте друзей по своей ссылке\n"
        "2. Если друг создаст бота и останется активным 3 дня → <b>+7 дней вам</b>\n"
        "3. Если друг совершит первую оплату → <b>+15 дней вам</b> и <b>+10 дней ему</b>\n\n"
        
        "<b>Ваша статистика:</b>\n"
        f"👥 Всего рефералов: <b>{stats['total_referrals']}</b>\n"
        f"✅ Награждено: <b>{stats['rewarded_referrals']}</b>\n"
        f"⏳ В ожидании: <b>{stats['pending_referrals']}</b>\n"
        f"💰 Всего заработано дней: <b>{stats['total_days_earned']}</b>\n"
        f"🎯 До Premium осталось дней: <b>{stats['days_to_premium']}</b>\n\n"
        
        "<i>Приглашайте друзей и получайте Premium статус быстрее!</i>"
    )
    
    await message.answer(
        response,
        parse_mode="HTML",
        reply_markup=referral_system.get_referral_keyboard(user_id)
    )


async def cmd_referral_link(message: Message, referral_system: ReferralSystem):
    """Команда для получения реферальной ссылки"""
    user_id = message.from_user.id
    
    stats = await referral_system.get_referral_stats(user_id)
    referral_link = stats.get("referral_link", "")
    
    response = (
        "🔗 <b>Ваша реферальная ссылка:</b>\n\n"
        f"<code>{referral_link}</code>\n\n"
        
        "<b>Как делиться:</b>\n"
        "1. Скопируйте ссылку выше\n"
        "2. Отправьте другу в Telegram\n"
        "3. Или поделитесь в соцсетях\n\n"
        
        "<i>Каждый приглашенный друг приближает вас к Premium статусу!</i>"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📋 Скопировать ссылку",
                callback_data=f"copy_link_{user_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="📢 Поделиться",
                switch_inline_query=f"Присоединяйся к CodeMaster! {referral_link}"
            )
        ]
    ])
    
    await message.answer(
        response,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True
    )


referral_system: Optional[ReferralSystem] = None

def init_referral_system(bot: Bot):
    """Инициализация реферальной системы"""
    global referral_system
    referral_system = ReferralSystem(bot)
    return referral_system