"""
Конфигурация CodeMaster согласно ТЗ
"""

import os
from dotenv import load_dotenv
from typing import List

load_dotenv()

# ========== ТЕЛЕГРАМ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# ========== БЕЗОПАСНОСТЬ ==========
CRYPTO_KEY = os.getenv("CRYPTO_KEY", "").encode()
if not CRYPTO_KEY:
    print("ВНИМАНИЕ: CRYPTO_KEY не установлен. Сгенерируйте через: openssl rand -base64 32")

# ========== ПЛАТЕЖИ ==========
T_BANK_TOKEN = os.getenv("T_BANK_TOKEN")
T_BANK_SHOP_ID = os.getenv("T_BANK_SHOP_ID")
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "tbank")

TARIFFS = {
    "demo": {"days": 10, "price": 0, "name": "Демо"},
    "monthly": {"days": 30, "price": 199, "name": "Месячный"},
    "quarterly": {"days": 90, "price": 490, "name": "Квартальный"},
    "yearly": {"days": 365, "price": 1490, "name": "Годовой"},
}

STARS_TO_RUB = 7.0

# ========== РЕФЕРАЛЬНАЯ СИСТЕМА ==========
REFERRAL_REWARDS = {
    "bot_created": {"days": 7, "delay_days": 3},
    "first_payment_referrer": {"days": 15},
    "first_payment_referred": {"days": 10},
}

MAX_REFERRALS_PER_DAY = 10
ABUSE_CHECK_HOURS = 24

# ========== НАСТРОЙКИ ПРИЛОЖЕНИЯ ==========
DEBUG = os.getenv("DEBUG", "False").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///codemaster.db")

MINI_APP_URL = os.getenv("MINI_APP_URL", "https://your-domain.com/mini-app")
WEB_APP_HOST = os.getenv("WEB_APP_HOST", "0.0.0.0")
WEB_APP_PORT = int(os.getenv("WEB_APP_PORT", "8080"))


def validate_config():
    """Проверка обязательных переменных"""
    errors = []
    
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN не установлен")
    
    if not CHANNEL_ID:
        errors.append("CHANNEL_ID не установлен")
    
    if not CRYPTO_KEY:
        errors.append("CRYPTO_KEY не установен. Сгенерируйте: openssl rand -base64 32")
    
    if PAYMENT_PROVIDER == "tbank" and not T_BANK_TOKEN:
        errors.append("T_BANK_TOKEN не установлен для платежей через Т-Банк")
    
    if errors:
        raise ValueError(f"Ошибки конфигурации:\n" + "\n".join(f"  - {e}" for e in errors))
    
    print("✓ Конфигурация загружена успешно")
    if DEBUG:
        print(f"  Режим отладки: ВКЛ")
        print(f"  Канал: {CHANNEL_ID}")
        print(f"  Админы: {ADMIN_IDS}")


try:
    validate_config()
except ValueError as e:
    print(e)
    if not DEBUG:
        exit(1)
