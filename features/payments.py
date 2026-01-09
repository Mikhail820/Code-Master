"""
Модуль платежей: Т-Банк и Telegram Stars
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlencode

import aiohttp
from aiogram import types, Bot
from aiogram.types import (
    LabeledPrice, PreCheckoutQuery, SuccessfulPayment,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from core.database import db
from core.lifecycle import lifecycle
from config import (
    T_BANK_TOKEN, T_BANK_SHOP_ID, PAYMENT_PROVIDER,
    TARIFFS, STARS_TO_RUB, BOT_TOKEN, ADMIN_IDS
)

logger = logging.getLogger(__name__)


class PaymentProcessor:
    """Обработчик платежей через Т-Банк и Telegram Stars"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        
        self._validate_config()
    
    def _validate_config(self):
        """Валидация платежной конфигурации"""
        if PAYMENT_PROVIDER == "tbank" and not T_BANK_TOKEN:
            logger.warning("T_BANK_TOKEN не установлен. Платежи через Т-Банк недоступны.")
        
        if PAYMENT_PROVIDER == "stars":
            logger.info("Платежи через Telegram Stars активированы")
    
    async def create_invoice(
        self,
        user_id: int,
        tariff_key: str,
        payment_method: str = "tbank"
    ) -> Optional[Dict[str, Any]]:
        """Создание платежной инвойса."""
        if tariff_key not in TARIFFS:
            logger.error(f"Неизвестный тариф: {tariff_key}")
            return None
        
        tariff = TARIFFS[tariff_key]
        
        if tariff_key == "demo":
            success = await lifecycle.add_days_to_user(
                user_id=user_id,
                days=tariff["days"],
                days_type="trial",
                reason="demo_tariff"
            )
            
            return {
                "type": "free",
                "success": success,
                "days": tariff["days"]
            }
        
        payment_id = await db.create_payment(
            user_id=user_id,
            amount=tariff["price"],
            currency="RUB",
            payment_method=payment_method,
            days_awarded=tariff["days"],
            metadata={
                "tariff": tariff_key,
                "tariff_name": tariff["name"],
                "user_id": user_id
            }
        )
        
        if payment_method == "tbank":
            return await self._create_tbank_invoice(user_id, tariff, payment_id)
        elif payment_method == "stars":
            return await self._create_stars_invoice(user_id, tariff, payment_id)
        else:
            logger.error(f"Неизвестный метод оплаты: {payment_method}")
            return None
    
    async def _create_tbank_invoice(
        self,
        user_id: int,
        tariff: Dict[str, Any],
        payment_id: int
    ) -> Optional[Dict[str, Any]]:
        """Создание инвойса для Т-Банка"""
        if not T_BANK_TOKEN:
            logger.error("T_BANK_TOKEN не настроен")
            return None
        
        try:
            invoice_data = {
                "shop_id": T_BANK_SHOP_ID,
                "amount": str(tariff["price"]),
                "currency": "RUB",
                "order_id": str(payment_id),
                "description": f"CodeMaster: {tariff['name']} ({tariff['days']} дней)",
                "success_url": f"https://t.me/{self.bot.username}?start=payment_success_{payment_id}",
                "fail_url": f"https://t.me/{self.bot.username}?start=payment_failed_{payment_id}",
                "custom_data": json.dumps({
                    "user_id": user_id,
                    "tariff": tariff,
                    "payment_id": payment_id
                })
            }
            
            signature = self._generate_tbank_signature(invoice_data)
            invoice_data["sign"] = signature
            
            invoice_url = f"https://pay.tbank.ru/api/v1/invoices?{urlencode(invoice_data)}"
            
            return {
                "type": "tbank",
                "payment_id": payment_id,
                "invoice_url": invoice_url,
                "amount": tariff["price"],
                "currency": "RUB",
                "days": tariff["days"],
                "description": f"CodeMaster: {tariff['name']}"
            }
            
        except Exception as e:
            logger.error(f"Ошибка создания инвойса Т-Банка: {e}")
            await db.update_payment_status(payment_id, "failed")
            return None
    
    async def _create_stars_invoice(
        self,
        user_id: int,
        tariff: Dict[str, Any],
        payment_id: int
    ) -> Dict[str, Any]:
        """Создание инвойса для Telegram Stars"""
        stars_amount = int(tariff["price"] / STARS_TO_RUB)
        
        return {
            "type": "stars",
            "payment_id": payment_id,
            "provider_token": T_BANK_TOKEN if T_BANK_TOKEN else "TEST_TOKEN",
            "currency": "XTR",
            "prices": [LabeledPrice(label=f"{tariff['name']} ({tariff['days']} дней)", amount=stars_amount * 100)],
            "payload": f"payment_{payment_id}",
            "description": f"CodeMaster: {tariff['name']} - {tariff['days']} дней",
            "need_email": False,
            "need_phone": False,
            "send_email_to_provider": False,
            "send_phone_to_provider": False,
            "is_flexible": False
        }
    
    def _generate_tbank_signature(self, data: Dict[str, str]) -> str:
        """Генерация подписи для Т-Банка"""
        sorted_keys = sorted(data.keys())
        
        sign_string = "&".join(f"{key}={data[key]}" for key in sorted_keys)
        sign_string += T_BANK_TOKEN
        
        signature = hmac.new(
            T_BANK_TOKEN.encode(),
            sign_string.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return signature
    
    async def process_tbank_callback(self, callback_data: Dict[str, Any]) -> Tuple[bool, str]:
        """Обработка callback от Т-Банка."""
        try:
            if not self._validate_tbank_signature(callback_data):
                return False, "Неверная подпись"
            
            payment_id = int(callback_data.get("order_id", 0))
            status = callback_data.get("status", "").lower()
            
            if status == "success":
                async with await db.connect() as conn:
                    conn.row_factory = aiosqlite.Row
                    async with conn.execute(
                        "SELECT * FROM payments WHERE payment_id = ?",
                        (payment_id,)
                    ) as cursor:
                        payment = await cursor.fetchone()
                
                if not payment:
                    return False, "Платеж не найден"
                
                await db.update_payment_status(
                    payment_id=payment_id,
                    status="success",
                    telegram_charge_id=callback_data.get("transaction_id")
                )
                
                user_id = payment["user_id"]
                days_awarded = payment["days_awarded"]
                
                success = await lifecycle.add_days_to_user(
                    user_id=user_id,
                    days=days_awarded,
                    days_type="paid",
                    payment_id=payment_id
                )
                
                if success:
                    await self._send_payment_success_notification(user_id, days_awarded)
                    
                    await self._process_referral_payment(user_id)
                    
                    logger.info(f"Платеж {payment_id} успешно обработан. Начислено {days_awarded} дней")
                    return True, "Платеж успешно обработан"
                else:
                    return False, "Ошибка начисления дней"
            
            elif status in ["failed", "canceled"]:
                await db.update_payment_status(payment_id, "failed")
                return False, "Платеж отменен"
            
            else:
                return False, f"Неизвестный статус: {status}"
                
        except Exception as e:
            logger.error(f"Ошибка обработки callback Т-Банка: {e}")
            return False, f"Ошибка обработки: {str(e)}"
    
    async def process_stars_payment(self, successful_payment: SuccessfulPayment) -> bool:
        """Обработка платежа через Telegram Stars"""
        try:
            payload = successful_payment.invoice_payload
            if not payload.startswith("payment_"):
                return False
            
            payment_id = int(payload.replace("payment_", ""))
            
            async with await db.connect() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    "SELECT * FROM payments WHERE payment_id = ?",
                    (payment_id,)
                ) as cursor:
                    payment = await cursor.fetchone()
            
            if not payment:
                logger.error(f"Платеж {payment_id} не найден в БД")
                return False
            
            await db.update_payment_status(
                payment_id=payment_id,
                status="success",
                telegram_charge_id=successful_payment.telegram_payment_charge_id
            )
            
            user_id = payment["user_id"]
            days_awarded = payment["days_awarded"]
            
            success = await lifecycle.add_days_to_user(
                user_id=user_id,
                days=days_awarded,
                days_type="paid",
                payment_id=payment_id
            )
            
            if success:
                await self._send_payment_success_notification(user_id, days_awarded)
                
                await self._process_referral_payment(user_id)
                
                logger.info(f"Stars платеж {payment_id} успешно обработан")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Ошибка обработки Stars платежа: {e}")
            return False
    
    def _validate_tbank_signature(self, data: Dict[str, Any]) -> bool:
        """Валидация подписи от Т-Банка"""
        try:
            received_sign = data.pop("sign", "")
            
            generated_sign = self._generate_tbank_signature(data)
            
            return hmac.compare_digest(received_sign, generated_sign)
            
        except Exception as e:
            logger.error(f"Ошибка валидации подписи Т-Банка: {e}")
            return False
    
    async def _process_referral_payment(self, user_id: int):
        """Обработка реферальных начислений при успешной оплате."""
        try:
            user = await db.get_user(user_id)
            if not user:
                return
            
            referrer_id = user.get("referrer_id")
            if not referrer_id:
                return
            
            await lifecycle.add_days_to_user(
                user_id=referrer_id,
                days=15,
                days_type="bonus",
                reason="referral_first_payment"
            )
            
            await lifecycle.add_days_to_user(
                user_id=user_id,
                days=10,
                days_type="bonus",
                reason="welcome_first_payment"
            )
            
            await db.mark_referral_payment_event(user_id, referrer_id)
            
            logger.info(f"Реферальные начисления за платеж: {user_id} -> {referrer_id}")
            
        except Exception as e:
            logger.error(f"Ошибка обработки реферальных начислений: {e}")
    
    async def _send_payment_success_notification(self, user_id: int, days: int):
        """Отправка уведомления об успешной оплате"""
        try:
            async with await db.connect() as conn:
                async with conn.execute(
                    "SELECT telegram_id FROM users WHERE user_id = ?",
                    (user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        telegram_id = row[0]
                    else:
                        return
            
            message = (
                "🎉 <b>Оплата успешно принята!</b>\n\n"
                f"На ваш баланс начислено <b>{days} дней</b> обслуживания.\n\n"
                "Ваши боты-визитки теперь активны.\n"
                "Спасибо за выбор CodeMaster! 💙"
            )
            
            await self.bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode="HTML"
            )
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления об оплате: {e}")
    
    async def send_admin_payment_notification(self, payment_data: Dict[str, Any]):
        """Отправка уведомления админу о новом платеже"""
        if not ADMIN_IDS:
            return
        
        message = (
            "💰 <b>Новый платеж</b>\n\n"
            f"ID: <code>{payment_data.get('payment_id')}</code>\n"
            f"Сумма: {payment_data.get('amount')} {payment_data.get('currency')}\n"
            f"Дней: {payment_data.get('days')}\n"
            f"Метод: {payment_data.get('type')}\n"
            f"Время: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await self.bot.send_message(
                    chat_id=admin_id,
                    text=message,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления админу {admin_id}: {e}")
    
    async def get_payment_history(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Получение истории платежей пользователя"""
        async with await db.connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """
                SELECT 
                    payment_id, amount, currency, payment_method,
                    payment_status, days_awarded, created_at, completed_at
                FROM payments 
                WHERE user_id = ? 
                ORDER BY created_at DESC 
                LIMIT ?
                """,
                (user_id, limit)
            ) as cursor:
                rows = await cursor.fetchall()
                
                result = []
                for row in rows:
                    payment = dict(row)
                    
                    if payment["created_at"]:
                        payment["created_at"] = payment["created_at"][:19].replace("T", " ")
                    
                    if payment["completed_at"]:
                        payment["completed_at"] = payment["completed_at"][:19].replace("T", " ")
                    
                    status_map = {
                        "pending": "⏳ Ожидание",
                        "success": "✅ Успешно",
                        "failed": "❌ Отменен"
                    }
                    payment["status_text"] = status_map.get(payment["payment_status"], payment["payment_status"])
                    
                    result.append(payment)
                
                return result
    
    async def get_available_tariffs(self) -> List[Dict[str, Any]]:
        """Получение доступных тарифов"""
        tariffs = []
        
        for key, tariff in TARIFFS.items():
            if key == "demo":
                continue
                
            tariffs.append({
                "key": key,
                "name": tariff["name"],
                "days": tariff["days"],
                "price": tariff["price"],
                "price_per_day": round(tariff["price"] / tariff["days"], 2),
                "best_value": key in ["yearly", "quarterly"]
            })
        
        return sorted(tariffs, key=lambda x: x["price"])
    
    def get_tariffs_keyboard(self) -> InlineKeyboardMarkup:
        """Клавиатура с тарифами"""
        buttons = []
        
        for key, tariff in TARIFFS.items():
            if key == "demo":
                continue
                
            price_text = f"{tariff['price']}₽" if tariff['price'] > 0 else "Бесплатно"
            button_text = f"{tariff['name']} - {price_text}"
            
            buttons.append([
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"tariff_{key}"
                )
            ])
        
        buttons.append([
            InlineKeyboardButton(text="❓ Помощь", callback_data="payment_help"),
            InlineKeyboardButton(text="📊 Баланс", callback_data="check_balance")
        ])
        
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    
    async def close(self):
        """Закрытие ресурсов"""
        await self.session.close()


payment_processor: Optional[PaymentProcessor] = None

def init_payment_processor(bot: Bot):
    """Инициализация платежного процессора"""
    global payment_processor
    payment_processor = PaymentProcessor(bot)
    return payment_processor