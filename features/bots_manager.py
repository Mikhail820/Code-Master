"""
Модуль создания и управления ботами-визитками
Интеграция с @BotFather и запуск дочерних ботов
"""

import asyncio
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.client.session.aiohttp import AiohttpSession

from core.database import db
from core.security import token_encryptor, TokenEncryptor
from core.lifecycle import lifecycle
from config import MINI_APP_URL, BOT_TOKEN, DEBUG

logger = logging.getLogger(__name__)
router = Router()

_running_bots: Dict[int, Dict[str, Any]] = {}


class BotCreationError(Exception):
    """Ошибка создания бота"""
    pass


class BotsManager:
    """Менеджер ботов-визиток"""
    
    def __init__(self, master_bot: Bot):
        self.master_bot = master_bot
        self._bot_tasks = {}
        
        self.default_config = {
            "welcome_message": "👋 Добро пожаловать! Я ваш визитный бот.\n\n"
                              "Нажмите на кнопки ниже, чтобы связаться со мной.",
            "buttons": [
                {"text": "📞 Телефон", "type": "phone", "value": ""},
                {"text": "📧 Email", "type": "email", "value": ""},
                {"text": "🌐 Сайт", "type": "url", "value": ""},
                {"text": "💬 Telegram", "type": "tg", "value": ""}
            ],
            "theme": "light",
            "auto_replies": True
        }
    
    async def create_new_bot(self, user_id: int, bot_token: str) -> Dict[str, Any]:
        """Создание нового бота-визитки."""
        can_create, reason = await lifecycle.can_create_bot(user_id)
        if not can_create:
            raise BotCreationError(reason)
        
        token_info = await self._validate_bot_token(bot_token)
        if not token_info:
            raise BotCreationError("❌ Неверный токен бота. Проверьте правильность.")
        
        bot_username = token_info.get("username")
        
        if await self._bot_exists(bot_username):
            raise BotCreationError("❌ Этот бот уже зарегистрирован в системе.")
        
        token_encrypted = token_encryptor.encrypt_token(bot_token)
        token_hash = TokenEncryptor.hash_token(bot_token)
        
        bot_id = await db.create_bot(
            user_id=user_id,
            token_encrypted=token_encrypted,
            token_hash=token_hash,
            bot_username=bot_username,
            config=self.default_config
        )
        
        try:
            await self._start_bot_instance(bot_id, bot_token, bot_username)
            
            await db.log_audit(
                user_id=user_id,
                action="BOT_CREATED_SUCCESS",
                details={
                    "bot_id": bot_id,
                    "bot_username": bot_username,
                    "config": self.default_config
                }
            )
            
            logger.info(f"Создан новый бот {bot_username} для пользователя {user_id}")
            
            return {
                "bot_id": bot_id,
                "username": bot_username,
                "config": self.default_config,
                "status": "running",
                "created_at": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            await db.delete_bots_by_owner(user_id)
            raise BotCreationError(f"❌ Ошибка запуска бота: {str(e)}")
    
    async def _validate_bot_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Валидация токена бота через Telegram API."""
        try:
            session = AiohttpSession()
            test_bot = Bot(token=token, session=session)
            
            bot_info = await test_bot.get_me()
            
            await session.close()
            
            return {
                "id": bot_info.id,
                "username": bot_info.username,
                "first_name": bot_info.first_name,
                "is_bot": bot_info.is_bot
            }
            
        except Exception as e:
            logger.error(f"Ошибка валидации токена: {e}")
            return None
    
    async def _bot_exists(self, bot_username: str) -> bool:
        """Проверяет, зарегистрирован ли бот в системе"""
        async with await db.connect() as conn:
            async with conn.execute(
                "SELECT 1 FROM bots WHERE bot_username = ? LIMIT 1",
                (bot_username,)
            ) as cursor:
                return await cursor.fetchone() is not None
    
    async def _start_bot_instance(self, bot_id: int, bot_token: str, bot_username: str):
        """Запуск экземпляра бота в отдельной задаче."""
        bot = Bot(token=bot_token)
        dp = Dispatcher()
        
        dp.message.register(self._handle_visiting_card_message)
        dp.callback_query.register(self._handle_visiting_card_callback)
        
        async def run_bot():
            try:
                logger.info(f"Запуск бота {bot_username} (ID: {bot_id})")
                await db.set_bot_running(bot_id, True)
                
                _running_bots[bot_id] = {
                    "bot": bot,
                    "dispatcher": dp,
                    "username": bot_username,
                    "started_at": datetime.utcnow()
                }
                
                await dp.start_polling(bot)
                
            except asyncio.CancelledError:
                logger.info(f"Бот {bot_username} остановлен")
            except Exception as e:
                logger.error(f"Ошибка в боте {bot_username}: {e}")
            finally:
                await db.set_bot_running(bot_id, False)
                _running_bots.pop(bot_id, None)
                try:
                    await bot.session.close()
                except:
                    pass
        
        task = asyncio.create_task(run_bot())
        self._bot_tasks[bot_id] = task
    
    async def stop_bot(self, bot_id: int):
        """Остановка бота"""
        if bot_id in self._bot_tasks:
            task = self._bot_tasks[bot_id]
            task.cancel()
            
            try:
                await task
            except asyncio.CancelledError:
                pass
            
            del self._bot_tasks[bot_id]
            
            if bot_id in _running_bots:
                del _running_bots[bot_id]
            
            await db.set_bot_running(bot_id, False)
            logger.info(f"Бот {bot_id} остановлен")
    
    async def restart_bot(self, bot_id: int):
        """Перезапуск бота"""
        await self.stop_bot(bot_id)
        
        async with await db.connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT token_encrypted, bot_username, owner_id FROM bots WHERE bot_id = ?",
                (bot_id,)
            ) as cursor:
                bot_data = await cursor.fetchone()
        
        if bot_data:
            token = token_encryptor.decrypt_token(bot_data["token_encrypted"])
            
            await self._start_bot_instance(
                bot_id, 
                token, 
                bot_data["bot_username"]
            )
    
    async def _handle_visiting_card_message(self, message: Message):
        """Обработчик сообщений для бота-визитки."""
        bot_id = await self._get_bot_id_by_token(message.bot.token)
        if not bot_id:
            return
        
        can_respond = await lifecycle.can_bot_respond(bot_id)
        if not can_respond:
            await message.answer(
                "⏸️ Этот бот временно неактивен. "
                "Владелец должен пополнить баланс или подписаться на канал."
            )
            return
        
        config = await self._get_bot_config(bot_id)
        
        if message.text in ["/start", "start", "начать"]:
            keyboard = self._create_visiting_card_keyboard(config["buttons"])
            
            await message.answer(
                config["welcome_message"],
                reply_markup=keyboard
            )
        
        elif message.text in ["/help", "помощь", "help"]:
            await message.answer(
                "Это бот-визитка. Он предоставляет контактную информацию своего владельца.\n\n"
                "Используйте кнопки ниже для связи."
            )
        
        elif config.get("auto_replies", True):
            await message.answer(
                "🤖 Я автоматический бот-визитка.\n"
                "Используйте кнопки ниже для получения контактной информации."
            )
    
    async def _handle_visiting_card_callback(self, callback_query: types.CallbackQuery):
        """Обработчик callback-запросов (кнопок)"""
        bot_id = await self._get_bot_id_by_token(callback_query.bot.token)
        if not bot_id:
            return
        
        can_respond = await lifecycle.can_bot_respond(bot_id)
        if not can_respond:
            await callback_query.answer("Бот временно неактивен", show_alert=True)
            return
        
        data = callback_query.data
        
        if data.startswith("contact_"):
            contact_type = data.split("_")[1]
            
            config = await self._get_bot_config(bot_id)
            button = next(
                (btn for btn in config["buttons"] if btn.get("type") == contact_type),
                None
            )
            
            if button and button.get("value"):
                value = button["value"]
                
                if contact_type == "phone":
                    await callback_query.message.answer(f"📞 Телефон: {value}")
                elif contact_type == "email":
                    await callback_query.message.answer(f"📧 Email: {value}")
                elif contact_type == "url":
                    await callback_query.message.answer(f"🌐 Сайт: {value}")
                elif contact_type == "tg":
                    await callback_query.message.answer(f"💬 Telegram: @{value}")
            
            await callback_query.answer()
    
    def _create_visiting_card_keyboard(self, buttons: List[Dict]) -> InlineKeyboardMarkup:
        """Создание клавиатуры для бота-визитки"""
        keyboard = []
        
        for button in buttons:
            btn_type = button.get("type", "url")
            btn_text = button.get("text", "Кнопка")
            
            if btn_type == "phone":
                keyboard.append([
                    InlineKeyboardButton(
                        text=btn_text,
                        callback_data=f"contact_phone"
                    )
                ])
            elif btn_type == "email":
                keyboard.append([
                    InlineKeyboardButton(
                        text=btn_text,
                        callback_data=f"contact_email"
                    )
                ])
            elif btn_type == "url" and button.get("value"):
                keyboard.append([
                    InlineKeyboardButton(
                        text=btn_text,
                        url=button["value"]
                    )
                ])
            elif btn_type == "tg" and button.get("value"):
                keyboard.append([
                    InlineKeyboardButton(
                        text=btn_text,
                        url=f"https://t.me/{button['value']}"
                    )
                ])
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    async def _get_bot_id_by_token(self, token: str) -> Optional[int]:
        """Получение ID бота по токену"""
        token_hash = TokenEncryptor.hash_token(token)
        
        async with await db.connect() as conn:
            async with conn.execute(
                "SELECT bot_id FROM bots WHERE token_hash = ?",
                (token_hash,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None
    
    async def _get_bot_config(self, bot_id: int) -> Dict[str, Any]:
        """Получение конфигурации бота"""
        async with await db.connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT config_json FROM bots WHERE bot_id = ?",
                (bot_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row and row["config_json"]:
                    return json.loads(row["config_json"])
        
        return self.default_config
    
    async def get_user_bots_info(self, user_id: int) -> List[Dict[str, Any]]:
        """Получение информации о ботах пользователя"""
        bots = await db.get_user_bots(user_id)
        result = []
        
        for bot in bots:
            token_preview = "..." + bot["token_encrypted"][-10:] if not DEBUG else "[DEBUG]"
            
            result.append({
                "bot_id": bot["bot_id"],
                "username": bot["bot_username"],
                "is_running": bool(bot["is_running"]),
                "last_active": bot["last_active"],
                "created_at": bot["created_at"],
                "token_preview": token_preview,
                "config": json.loads(bot["config_json"]) if bot["config_json"] else {}
            })
        
        return result
    
    async def update_bot_config(self, bot_id: int, config: Dict[str, Any]) -> bool:
        """Обновление конфигурации бота"""
        try:
            if not self._validate_bot_config(config):
                return False
            
            await db.update_bot_config(bot_id, config)
            
            await self.restart_bot(bot_id)
            
            await db.log_audit(
                user_id=None,
                action="BOT_CONFIG_UPDATED",
                details={"bot_id": bot_id, "config": config}
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка обновления конфигурации бота {bot_id}: {e}")
            return False
    
    def _validate_bot_config(self, config: Dict) -> bool:
        """Валидация конфигурации бота"""
        required_fields = ["welcome_message", "buttons"]
        
        for field in required_fields:
            if field not in config:
                return False
        
        if not isinstance(config["buttons"], list):
            return False
        
        for button in config["buttons"]:
            if not isinstance(button, dict):
                return False
            
            if "text" not in button or "type" not in button:
                return False
            
            btn_type = button["type"]
            if btn_type not in ["phone", "email", "url", "tg"]:
                return False
            
            if btn_type in ["url", "tg"] and not button.get("value"):
                return False
        
        return True


@router.message(Command("createbot"))
async def cmd_create_bot(message: Message):
    """Команда создания нового бота"""
    user_id = message.from_user.id
    
    can_create, reason = await lifecycle.can_create_bot(user_id)
    if not can_create:
        await message.answer(reason)
        return
    
    await message.answer(
        "🤖 <b>Создание бота-визитки</b>\n\n"
        "1. Перейдите к <a href='https://t.me/BotFather'>@BotFather</a>\n"
        "2. Создайте нового бота командой /newbot\n"
        "3. Получите токен бота (выглядит как: <code>123456789:ABCdefGHIjklMNOpqrSTUvwxYZ</code>)\n"
        "4. Пришлите токен сюда\n\n"
        "<i>Токен нужен только для подключения. Мы его зашифруем.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )


@router.message(Command("mybots"))
async def cmd_my_bots(message: Message, bots_manager: BotsManager):
    """Команда просмотра своих ботов"""
    user_id = message.from_user.id
    
    bots_info = await bots_manager.get_user_bots_info(user_id)
    
    if not bots_info:
        await message.answer(
            "🤖 У вас пока нет ботов.\n"
            "Создайте первого бота командой /createbot"
        )
        return
    
    response = ["<b>Ваши боты-визитки:</b>\n"]
    
    for i, bot in enumerate(bots_info, 1):
        status = "🟢 Запущен" if bot["is_running"] else "🔴 Остановлен"
        response.append(
            f"{i}. @{bot['username']} - {status}\n"
            f"   Создан: {bot['created_at'][:10]}\n"
            f"   ID: <code>{bot['bot_id']}</code>\n"
        )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⚙️ Управление ботами",
            web_app=WebAppInfo(url=f"{MINI_APP_URL}/bots?user_id={user_id}")
        )
    ]])
    
    await message.answer(
        "\n".join(response),
        parse_mode="HTML",
        reply_markup=keyboard
    )


@router.message(Command("botconfig"))
async def cmd_bot_config(message: Message):
    """Команда настройки бота"""
    user_id = message.from_user.id
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⚙️ Настроить бота",
            web_app=WebAppInfo(url=f"{MINI_APP_URL}/config?user_id={user_id}")
        )
    ]])
    
    await message.answer(
        "⚙️ <b>Настройка бота-визитки</b>\n\n"
        "В Mini App вы можете:\n"
        "• Изменить приветственное сообщение\n"
        "• Настроить контактные кнопки\n"
        "• Выбрать тему оформления\n"
        "• Включить/выключить автоответы\n\n"
        "<i>Настройки применяются мгновенно ко всем вашим ботам.</i>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


bots_manager: Optional[BotsManager] = None

def init_bots_manager(master_bot: Bot):
    """Инициализация менеджера ботов"""
    global bots_manager
    bots_manager = BotsManager(master_bot)
    return bots_manager