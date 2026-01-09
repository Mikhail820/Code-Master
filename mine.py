"""
Главный файл CodeMaster - Master бот
Интеграция всех модулей системы
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import core.database as db
from core.lifecycle import lifecycle
from core.security import token_encryptor, TokenEncryptor
from config import BOT_TOKEN, CHANNEL_ID, DEBUG, WEB_APP_HOST, WEB_APP_PORT, CRYPTO_KEY
from features.bots_manager import router as bots_router, init_bots_manager
from features.payments import init_payment_processor
from features.referral import init_referral_system
from utils.scheduler import scheduler
from web.mini_app import init_mini_app
from web.admin_panel import init_admin_panel

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

dp.include_router(bots_router)


@asynccontextmanager
async def lifespan():
    """Управление жизненным циклом приложения"""
    logger.info("=== CodeMaster запускается ===")
    
    await db.init_db()
    logger.info("✅ База данных инициализирована")
    
    global token_encryptor
    token_encryptor = TokenEncryptor(CRYPTO_KEY)
    logger.info("✅ Шифрование инициализировано")
    
    init_bots_manager(bot)
    init_payment_processor(bot)
    init_referral_system(bot)
    
    await scheduler.start()
    
    from core.lifecycle import lifecycle
    scheduler.schedule_daily(lifecycle.daily_billing_task, hour=3, minute=0, name="daily_billing")
    scheduler.schedule_periodic(lifecycle.check_expired_notifications, interval_seconds=3600, name="expired_notifications")
    
    if WEB_APP_HOST and WEB_APP_PORT:
        mini_app = await init_mini_app()
        admin_app = await init_admin_panel()
        
        app = web.Application()
        app.add_subapp("/mini-app", mini_app)
        app.add_subapp("/admin", admin_app)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, WEB_APP_HOST, WEB_APP_PORT)
        await site.start()
        logger.info(f"✅ Веб-сервер запущен на {WEB_APP_HOST}:{WEB_APP_PORT}")
    
    logger.info("=== CodeMaster запущен успешно ===")
    
    yield
    
    logger.info("=== CodeMaster останавливается ===")
    
    await scheduler.stop()
    await bot.session.close()
    
    logger.info("=== CodeMaster остановлен ===")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Команда /start с реферальной поддержкой"""
    user_id = message.from_user.id
    args = message.text.split()[1:] if len(message.text.split()) > 1 else []
    
    referrer_id = None
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0].replace("ref_", ""))
        except ValueError:
            pass
    
    await db.create_or_update_user(
        telegram_id=user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        referrer_id=referrer_id,
        source="referral" if referrer_id else "organic"
    )
    
    is_subscribed = False
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        is_subscribed = member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Ошибка проверки подписки: {e}")
    
    status = await lifecycle.get_user_status(user_id, is_subscribed)
    
    if status == "active":
        from features.payments import payment_processor
        
        await message.answer(
            f"👑 <b>Добро пожаловать в CodeMaster!</b>\n\n"
            f"Ваш статус: 🟢 <b>ACTIVE</b>\n"
            f"Вы можете создавать и управлять ботами-визитками.\n\n"
            f"<b>Доступные команды:</b>\n"
            f"/createbot - Создать нового бота\n"
            f"/mybots - Мои боты\n"
            f"/buy - Купить дни\n"
            f"/balance - Мой баланс\n"
            f"/referral - Реферальная программа\n"
            f"/help - Помощь",
            parse_mode="HTML",
            reply_markup=payment_processor.get_tariffs_keyboard() if payment_processor else None
        )
    else:
        await message.answer(
            f"🔒 <b>Требуется активация</b>\n\n"
            f"Для использования CodeMaster необходимо:\n"
            f"1. Подписаться на канал: {CHANNEL_ID}\n"
            f"2. Иметь активные дни на балансе\n\n"
            f"<i>После подписки отправьте /start снова</i>",
            parse_mode="HTML"
        )


@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    """Проверка баланса"""
    user_id = message.from_user.id
    
    summary = await lifecycle.get_days_summary(user_id)
    
    response = (
        f"💰 <b>Ваш баланс</b>\n\n"
        f"• Пробные дни: {summary['trial_days']}\n"
        f"• Оплаченные дни: {summary['paid_days']}\n"
        f"• Бонусные дни: {summary['bonus_days']}\n"
        f"• Всего дней: {summary['total_days']}\n\n"
    )
    
    if summary['is_premium']:
        response += f"🎖️ <b>Premium статус активен</b>\n"
        if summary['premium_since']:
            response += f"С: {summary['premium_since'][:10]}\n"
    elif summary['bonus_days'] >= 20:
        response += f"🎯 До Premium осталось: {30 - summary['bonus_days']} бонусных дней\n"
    
    await message.answer(response, parse_mode="HTML")


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Справка по командам"""
    help_text = (
        "🆘 <b>CodeMaster - Помощь</b>\n\n"
        
        "<b>Основные команды:</b>\n"
        "/start - Запуск бота\n"
        "/createbot - Создать бота-визитку\n"
        "/mybots - Мои боты\n"
        "/botconfig - Настроить бота\n"
        "/buy - Купить дни\n"
        "/balance - Мой баланс\n"
        "/referral - Реферальная программа\n"
        "/history - История платежей\n\n"
        
        "<b>Как это работает:</b>\n"
        "1. Создайте бота через @BotFather\n"
        "2. Пришлите токен в /createbot\n"
        "3. Настройте кнопки в Mini App\n"
        "4. Приглашайте друзей и получайте бонусы!\n\n"
        
        "<b>Поддержка:</b>\n"
        "По всем вопросам: @codemaster_support"
    )
    
    await message.answer(help_text, parse_mode="HTML")


async def main():
    """Главная функция запуска"""
    try:
        dp.startup.register(lifspan().__aenter__)
        dp.shutdown.register(lifspan().__aexit__)
        
        await dp.start_polling(bot)
        
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка по запросу пользователя")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
        raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
