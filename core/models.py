"""
Pydantic модели для типизации данных CodeMaster
"""

from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from datetime import datetime


class UserBase(BaseModel):
    """Базовая модель пользователя"""
    user_id: int
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    referrer_id: Optional[int] = None
    is_sub_active: bool = False
    created_at: datetime


class UserBalance(BaseModel):
    """Модель баланса пользователя"""
    trial_days: int = 0
    paid_until: Optional[datetime] = None
    bonus_days: int = 0
    current_status: str = "frozen"
    is_premium: bool = False
    premium_since: Optional[datetime] = None


class BotConfig(BaseModel):
    """Модель конфигурации бота-визитки"""
    welcome_message: str = "👋 Добро пожаловать!"
    buttons: List[Dict[str, str]] = Field(default_factory=list)
    theme: str = "light"
    auto_replies: bool = True
    
    @validator('buttons')
    def validate_buttons(cls, v):
        for button in v:
            if 'text' not in button or 'type' not in button:
                raise ValueError('Каждая кнопка должна иметь text и type')
            
            btn_type = button['type']
            if btn_type not in ['phone', 'email', 'url', 'tg']:
                raise ValueError(f'Неизвестный тип кнопки: {btn_type}')
            
            if btn_type in ['url', 'tg'] and not button.get('value'):
                raise ValueError(f'Для типа {btn_type} требуется value')
        
        return v


class PaymentCreate(BaseModel):
    """Модель создания платежа"""
    tariff_key: str
    payment_method: str = "tbank"
    
    @validator('tariff_key')
    def validate_tariff(cls, v):
        from config import TARIFFS
        if v not in TARIFFS:
            raise ValueError(f'Неизвестный тариф: {v}')
        return v
    
    @validator('payment_method')
    def validate_method(cls, v):
        if v not in ['tbank', 'stars']:
            raise ValueError('Метод оплаты должен быть tbank или stars')
        return v


class ReferralEvent(BaseModel):
    """Модель реферального события"""
    referrer_id: int
    referred_id: int
    event_type: str
    reward_granted: bool = False