import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

# Загрузка переменных окружения из .env
load_dotenv()

# Поддержка UTF-8 в консоли Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
    Message,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
DATA_PATH = os.path.join(os.path.dirname(__file__), "bot_data.json")


# ==============================================================================
# УПРАВЛЕНИЕ ДАННЫМИ
# ==============================================================================

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Файл {CONFIG_PATH} не найден!")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_data() -> dict:
    if not os.path.exists(DATA_PATH):
        return {}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_data(data: dict):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_today_str(tz) -> str:
    return datetime.now(tz).strftime("%Y-%m-%d")


def get_today_data(tz) -> tuple[str, dict]:
    date_str = get_today_str(tz)
    data = load_data()
    if date_str not in data:
        data[date_str] = {
            "duty_users": [],
            "rounds": {}
        }
        save_data(data)
    return date_str, data[date_str]


def is_admin(user_id: int) -> bool:
    config = load_config()
    admin_ids = config.get("admin_ids", [])
    if not admin_ids:
        admin_ids.append(user_id)
        config["admin_ids"] = admin_ids
        save_config(config)
        logger.info(f"Пользователь {user_id} назначен администратором.")
        return True
    return user_id in admin_ids


# FSM-состояния
class AnnouncementState(StatesGroup):
    waiting_for_content = State()
    confirm = State()


class PollState(StatesGroup):
    waiting_for_question = State()
    waiting_for_options = State()
    confirm = State()


class AddReminderState(StatesGroup):
    waiting_for_time = State()
    waiting_for_label = State()


class AddMentionState(StatesGroup):
    waiting_for_username = State()


# Глобальные объекты
dp = Dispatcher(storage=MemoryStorage())
scheduler_instance: AsyncIOScheduler = None


# ==============================================================================
# ДИЗАЙН КЛАВИАТУР
# ==============================================================================

def get_admin_bottom_keyboard() -> ReplyKeyboardMarkup:
    """Удобное нижнее меню с большими кнопками в личке"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🎛 Панель управления"),
                KeyboardButton(text="📋 Отчёт за сегодня")
            ],
            [
                KeyboardButton(text="👮‍♂️ Кто на смене"),
                KeyboardButton(text="🔔 Тестовый обход")
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def get_admin_inline_menu() -> InlineKeyboardMarkup:
    """Интерактивное меню админ-панели с четкой группировкой"""
    b = InlineKeyboardBuilder()
    # Секция: Публикации в группу
    b.row(
        InlineKeyboardButton(text="📢 Сделать объявление", callback_data="admin_announcement"),
        InlineKeyboardButton(text="📊 Создать опрос", callback_data="admin_poll")
    )
    # Секция: Контроль и графики
    b.row(
        InlineKeyboardButton(text="⏰ График обходов", callback_data="admin_schedule"),
        InlineKeyboardButton(text="👥 Сотрудники", callback_data="admin_mentions")
    )
    # Секция: Смены и отчёты
    b.row(
        InlineKeyboardButton(text="👮‍♂️ Дежурные на сегодня", callback_data="admin_duty_menu"),
        InlineKeyboardButton(text="📋 Отчёт дня", callback_data="admin_view_report")
    )
    # Секция: Действие и статус
    b.row(
        InlineKeyboardButton(text="🚨 Экстренный вызов", callback_data="admin_call_now"),
        InlineKeyboardButton(text="ℹ️ Состояние системы", callback_data="admin_status")
    )
    return b.as_markup()


def get_schedule_keyboard() -> InlineKeyboardMarkup:
    config = load_config()
    reminders = config.get("reminders", [])
    b = InlineKeyboardBuilder()

    for idx, r in enumerate(reminders):
        icon = "✅ ВКЛ" if r.get("enabled", True) else "❌ ВЫКЛ"
        b.button(text=f"{r.get('time')}  {icon}", callback_data=f"toggle_rem_{idx}")

    b.adjust(2)
    b.row(
        InlineKeyboardButton(text="➕ Добавить время", callback_data="add_reminder"),
        InlineKeyboardButton(text="🗑 Удалить время", callback_data="del_reminder_menu")
    )
    b.row(
        InlineKeyboardButton(text="◀️ Назад в меню", callback_data="admin_main")
    )
    return b.as_markup()


def get_mentions_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="➕ Добавить сотрудника", callback_data="add_mention"),
        InlineKeyboardButton(text="🗑 Удалить сотрудника", callback_data="del_mention_menu")
    )
    b.row(
        InlineKeyboardButton(text="◀️ Назад в меню", callback_data="admin_main")
    )
    return b.as_markup()


def get_duty_keyboard() -> InlineKeyboardMarkup:
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    _, day_data = get_today_data(tz)
    duty_users = set(day_data.get("duty_users", []))
    all_staff = config.get("mentions", [])

    b = InlineKeyboardBuilder()
    for idx, staff in enumerate(all_staff):
        status = "✅ НА СМЕНЕ" if staff in duty_users else "❌ ВЫХОДНОЙ"
        b.button(text=f"{staff}: {status}", callback_data=f"toggle_duty_{idx}")

    b.adjust(1)
    b.row(
        InlineKeyboardButton(text="🔄 Сбросить выбор (все дежурят)", callback_data="reset_duty"),
        InlineKeyboardButton(text="◀️ Назад в меню", callback_data="admin_main")
    )
    return b.as_markup()


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отменить", callback_data="admin_cancel")
    return b.as_markup()


# ==============================================================================
# НАПОМИНАНИЯ И КОНТРОЛЬ ОБХОДОВ
# ==============================================================================

async def check_unconfirmed_round(bot: Bot, date_str: str, time_str: str, label: str, mentions_str: str):
    """Срабатывает через 15 минут при отсутствии подтверждения"""
    data = load_data()
    day_data = data.get(date_str, {})
    round_info = day_data.get("rounds", {}).get(time_str)

    if not round_info or round_info.get("status") != "pending":
        return

    round_info["status"] = "missed"
    data[date_str]["rounds"][time_str] = round_info
    save_data(data)

    config = load_config()
    chat_id = config.get("chat_id")
    admin_ids = config.get("admin_ids", [])
    timeout = config.get("escalation_minutes", 15)

    alert_card = (
        f"⚠️ <b>{time_str} — Обход не подтверждён!</b> ({label})\n"
        f"👥 Дежурные: {mentions_str}\n"
        f"<i>Прошло {timeout} минут, никто не подтвердил выход на обход.</i>"
    )

    if chat_id:
        try:
            await bot.send_message(chat_id=chat_id, text=alert_card, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Ошибка отправки тревоги: {e}")

    admin_card = (
        f"🚨 <b>[Тревога руководству]</b>\n"
        f"Обход в <b>{time_str}</b> ({label}) не подтверждён вовремя!\n"
        f"Дежурные: {mentions_str}"
    )
    for a_id in admin_ids:
        try:
            await bot.send_message(chat_id=a_id, text=admin_card, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Ошибка уведомления админа {a_id}: {e}")


async def send_reminder(bot: Bot, reminder_info: dict):
    config = load_config()
    chat_id = config.get("chat_id")
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    date_str, day_data = get_today_data(tz)

    if not chat_id:
        logger.warning("chat_id не настроен. Напишите /setchat в рабочей группе.")
        return

    duty_users = day_data.get("duty_users", [])
    if duty_users:
        mentions_str = " ".join(duty_users)
    else:
        all_mentions = config.get("mentions", [])
        mentions_str = " ".join(all_mentions) if all_mentions else "Все сотрудники"

    time_str = reminder_info.get("time", now.strftime("%H:%M"))
    label = reminder_info.get("label", "обход")

    advance = int(config.get("advance_minutes", 0))
    if reminder_info.get("scheduled") and advance > 0:
        reminder_card = (
            f"🔔 <b>Через {advance} мин обход в {time_str}!</b> ({label})\n"
            f"👥 Дежурные: {mentions_str}"
        )
    else:
        reminder_card = (
            f"🔔 <b>{time_str} — Время обхода!</b> ({label})\n"
            f"👥 Дежурные: {mentions_str}"
        )

    b = InlineKeyboardBuilder()
    b.button(
        text="✅ Я на обходе",
        callback_data=f"confirm:{date_str}:{time_str}"
    )

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=reminder_card,
            reply_markup=b.as_markup(),
            parse_mode=ParseMode.HTML
        )
        logger.info(f"Напоминание [{time_str}] отправлено в чат {chat_id}")

        data = load_data()
        if date_str not in data:
            data[date_str] = {"duty_users": duty_users, "rounds": {}}
        data[date_str]["rounds"][time_str] = {
            "label": label,
            "status": "pending",
            "message_id": sent.message_id,
            "sent_at": now.strftime("%H:%M:%S"),
            "mentions": mentions_str
        }
        save_data(data)

        # Контроль забывчивости
        timeout = config.get("escalation_minutes", 15)
        escalation_time = now + timedelta(minutes=timeout)

        global scheduler_instance
        if scheduler_instance:
            scheduler_instance.add_job(
                check_unconfirmed_round,
                trigger="date",
                run_date=escalation_time,
                args=[bot, date_str, time_str, label, mentions_str],
                id=f"escalation_{date_str}_{time_str}",
                replace_existing=True
            )

    except Exception as e:
        logger.error(f"Ошибка отправки напоминания: {e}")


@dp.callback_query(F.data.startswith("confirm:"))
async def cb_confirm_round(callback: CallbackQuery):
    # Формат: confirm:<YYYY-MM-DD>:<время>; во времени тоже есть двоеточие (09:20),
    # поэтому делим только по первым двум
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("⚠️ Некорректные данные кнопки.", show_alert=True)
        return
    date_str, time_str = parts[1], parts[2]

    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    confirm_time_str = now.strftime("%H:%M")

    data = load_data()
    day_data = data.get(date_str, {})
    round_info = day_data.get("rounds", {}).get(time_str)

    if not round_info:
        await callback.answer("⚠️ Данные обхода устарели.", show_alert=True)
        return

    if round_info.get("status") == "confirmed":
        by = round_info.get("confirmed_by", "сотрудник")
        at = round_info.get("confirmed_at", "")
        await callback.answer(f"Обход уже был принят: {by} в {at}", show_alert=True)
        return

    user = callback.from_user
    username_str = f"@{user.username}" if user.username else user.full_name

    round_info["status"] = "confirmed"
    round_info["confirmed_by"] = username_str
    round_info["confirmed_user_id"] = user.id
    round_info["confirmed_at"] = confirm_time_str
    data[date_str]["rounds"][time_str] = round_info
    save_data(data)

    # Отменяем тревогу
    global scheduler_instance
    if scheduler_instance:
        try:
            scheduler_instance.remove_job(f"escalation_{date_str}_{time_str}")
        except Exception:
            pass

    confirmed_card = f"✅ <b>{time_str} — Обход выполнил {username_str} в {confirm_time_str}</b>"

    try:
        # Убираем кнопку, чтобы чат оставался чистым и аккуратным
        await callback.message.edit_text(
            text=confirmed_card,
            reply_markup=None,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.warning(f"Ошибка обновления карточки: {e}")

    await callback.answer(f"✅ Обход в {time_str} принят! Отличная работа.", show_alert=False)


@dp.callback_query(F.data == "already_done")
async def cb_already_done(callback: CallbackQuery):
    await callback.answer("Этот обход уже успешно выполнен и занесен в отчет дня ✅")


# ==============================================================================
# ОТЧЁТНОСТЬ
# ==============================================================================

def generate_report_text(date_str: str) -> str:
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)

    data = load_data()
    day_data = data.get(date_str, {})
    rounds = day_data.get("rounds", {})
    duty_users = day_data.get("duty_users", [])

    duty_str = " ".join(duty_users) if duty_users else "Все сотрудники (общий график)"

    total = len(rounds)
    confirmed = sum(1 for r in rounds.values() if r.get("status") == "confirmed")
    missed = sum(1 for r in rounds.values() if r.get("status") == "missed")
    pending = total - confirmed - missed

    percent = int((confirmed / total * 100)) if total > 0 else 0

    lines = []
    for t in sorted(rounds.keys()):
        r = rounds[t]
        lbl = r.get("label", "обход")
        st = r.get("status")
        if st == "confirmed":
            by = r.get("confirmed_by", "—")
            at = r.get("confirmed_at", "")
            lines.append(f"🟢 <code>{t}</code> | {lbl.ljust(15)} | {by} ({at})")
        elif st == "missed":
            lines.append(f"🔴 <code>{t}</code> | {lbl.ljust(15)} | <b>ПРОПУЩЕН</b>")
        else:
            lines.append(f"🟡 <code>{t}</code> | {lbl.ljust(15)} | <i>Ожидает</i>")

    details = "\n".join(lines) if lines else "<i>Обходы за сегодня ещё не проводились.</i>"

    return (
        "📊 <b>ИТОГОВЫЙ ОТЧЁТ ЗА СМЕНУ</b>\n"
        f"📅 <b>Дата:</b> <code>{date_str}</code>\n"
        f"🕒 <b>Время выгрузки:</b> <code>{now.strftime('%H:%M:%S')}</code> (Ташкент)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👮‍♂️ <b>Дежурные:</b> {duty_str}\n\n"
        "📈 <b>Результаты:</b>\n"
        f"• Всего обходов: <b>{total}</b>\n"
        f"• Выполнено:  <b>{confirmed}</b> ({percent}%) 🟢\n"
        f"• Пропущено:  <b>{missed}</b> 🔴\n"
        f"• В процессе:  <b>{pending}</b> 🟡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>Хронология:</b>\n"
        f"{details}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def send_daily_report(bot: Bot):
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    date_str = get_today_str(tz)
    report_text = generate_report_text(date_str)

    chat_id = config.get("chat_id")
    admin_ids = config.get("admin_ids", [])

    if chat_id:
        try:
            await bot.send_message(chat_id=chat_id, text=report_text, parse_mode=ParseMode.HTML)
            logger.info(f"Отчет отправлен в чат {chat_id}")
        except Exception as e:
            logger.error(f"Ошибка отправки отчета в группу: {e}")

    for a_id in admin_ids:
        try:
            await bot.send_message(chat_id=a_id, text=report_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Ошибка отправки отчета админу {a_id}: {e}")


# ==============================================================================
# ОБРАБОТКА КОМАНД И НИЖНИХ КНОПОК
# ==============================================================================

@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    if message.chat.type == "private":
        welcome = (
            "✨ <b>Добро пожаловать в систему контроля обходов!</b>\n\n"
            "Я автоматизирую контроль графика, фиксирую подтверждения дежурных "
            "и формирую ежедневную отчетность для руководства.\n\n"
            "🎛 <b>Быстрый доступ:</b> используйте кнопки внизу экрана или команду /admin."
        )
        await message.reply(welcome, reply_markup=get_admin_bottom_keyboard(), parse_mode=ParseMode.HTML)
    else:
        group_welcome = (
            "👋 <b>Бот контроля обходов активен в группе!</b>\n\n"
            "📌 <b>Основные команды:</b>\n"
            "• <code>/duty</code> — посмотреть дежурных или заступить на смену\n"
            "• <code>/report</code> — отчёт по обходам за сегодня\n"
            "• <code>/status</code> — статус системы и точное время"
        )
        await message.reply(group_welcome, parse_mode=ParseMode.HTML)


# Обработка нажатий на постоянные кнопки внизу экрана
@dp.message(F.text == "🎛 Панель управления")
@dp.message(Command("admin"))
async def cmd_admin_open(message: Message, state: FSMContext):
    if message.chat.type != "private":
        await message.reply("⚠️ Панель управления доступна только в личных сообщениях с ботом.")
        return

    if not is_admin(message.from_user.id):
        await message.reply("⛔️ Доступ ограничен только для администраторов.")
        return

    await state.clear()
    panel_text = (
        "🎛 <b>Панель управления системой обходов</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите нужный раздел с помощью кнопок ниже:"
    )
    await message.answer(panel_text, reply_markup=get_admin_inline_menu(), parse_mode=ParseMode.HTML)


@dp.message(F.text == "📋 Отчёт за сегодня")
@dp.message(Command("report"))
async def cmd_report_open(message: Message):
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    date_str = get_today_str(tz)
    await message.reply(generate_report_text(date_str), parse_mode=ParseMode.HTML)


@dp.message(F.text == "👮‍♂️ Кто на смене")
@dp.message(Command("duty"))
async def cmd_duty_open(message: Message):
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    _, day_data = get_today_data(tz)
    duty_users = day_data.get("duty_users", [])

    if duty_users:
        duty_text = f"🟢 <b>На смене сейчас:</b> {' '.join(duty_users)}"
    else:
        duty_text = "⚪️ <i>Дежурные на сегодня ещё не выбраны (уведомления приходят всем сотрудникам).</i>"

    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="👮‍♂️ Заступить на смену", callback_data="claim_duty_self"),
        InlineKeyboardButton(text="🚪 Сдать смену", callback_data="unclaim_duty_self")
    )

    card = (
        "📋 <b>График дежурства на сегодня</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{duty_text}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Нажмите кнопку ниже, чтобы заступить или сдать смену:</i>"
    )
    await message.reply(card, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)


@dp.message(F.text == "🔔 Тестовый обход")
@dp.message(Command("test"))
async def cmd_test_open(message: Message, bot: Bot):
    test_reminder = {"time": "ТЕСТ", "label": "пробный обход для проверки"}
    await send_reminder(bot, test_reminder)


@dp.message(Command("setchat"))
async def cmd_setchat(message: Message):
    config = load_config()
    config["chat_id"] = message.chat.id
    save_config(config)

    chat_name = message.chat.title if message.chat.title else "текущая группа"
    await message.reply(
        "🎉 <b>Группа успешно привязана к боту!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <b>Чат:</b> {chat_name}\n"
        f"🆔 <b>ID:</b> <code>{message.chat.id}</code>\n\n"
        "Все уведомления об обходах, тревоги и отчеты теперь будут направляться сюда.",
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    _, day_data = get_today_data(tz)

    chat_id = config.get("chat_id")
    chat_info = f"<code>{chat_id}</code>" if chat_id else "⚠️ <i>Не привязана (напишите /setchat в группе)</i>"
    mentions = " ".join(config.get("mentions", [])) if config.get("mentions") else "—"
    duty_users = " ".join(day_data.get("duty_users", [])) or "Все по общему списку"

    reminders = config.get("reminders", [])
    active_count = sum(1 for r in reminders if r.get("enabled", True))

    card = (
        "ℹ️ <b>ТЕКУЩИЙ СТАТУС СИСТЕМЫ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 <b>Время в Ташкенте:</b> <code>{now.strftime('%d.%m.%Y %H:%M:%S')}</code>\n"
        f"📍 <b>Привязанный чат:</b> {chat_info}\n"
        f"👮‍♂️ <b>Дежурные сегодня:</b> {duty_users}\n"
        f"👥 <b>Штат сотрудников:</b> {mentions}\n"
        f"⏰ <b>Активных обходов:</b> {active_count} из {len(reminders)}\n"
        f"⏱ <b>Время на реакцию:</b> {config.get('escalation_minutes', 15)} мин\n"
        f"🔔 <b>Напоминание заранее:</b> за {config.get('advance_minutes', 0)} мин\n"
        f"📅 <b>Рабочие дни:</b> Понедельник — Суббота\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await message.reply(card, parse_mode=ParseMode.HTML)


# ==============================================================================
# ОБРАБОТЧИКИ НАВИГАЦИИ АДМИНКИ (CALLBACKS)
# ==============================================================================

@dp.callback_query(F.data == "admin_main")
async def cb_admin_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    panel_text = (
        "🎛 <b>Панель управления системой обходов</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите нужный раздел с помощью кнопок ниже:"
    )
    await callback.message.edit_text(panel_text, reply_markup=get_admin_inline_menu(), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.callback_query(F.data == "admin_cancel")
async def cb_admin_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "❌ <b>Действие отменено.</b>\n\n🎛 <b>Панель управления:</b>",
        reply_markup=get_admin_inline_menu(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer("Отменено")


@dp.callback_query(F.data == "admin_view_report")
async def cb_admin_view_report(callback: CallbackQuery):
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    date_str = get_today_str(tz)
    report_text = generate_report_text(date_str)

    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="◀️ Назад в меню", callback_data="admin_main"))

    await callback.message.edit_text(report_text, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.callback_query(F.data == "admin_status")
async def cb_admin_status(callback: CallbackQuery):
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    chat_id = config.get("chat_id", "Не привязан")
    admins = config.get("admin_ids", [])
    reminders = config.get("reminders", [])
    active = sum(1 for r in reminders if r.get("enabled", True))
    mentions = " ".join(config.get("mentions", [])) or "не указаны"

    card = (
        "ℹ️ <b>СОСТОЯНИЕ СИСТЕМЫ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 <b>Время в Ташкенте:</b> <code>{now.strftime('%d.%m.%Y %H:%M:%S')}</code>\n"
        f"📍 <b>ID группы:</b> <code>{chat_id}</code>\n"
        f"👥 <b>Сотрудников:</b> {mentions}\n"
        f"👑 <b>Администраторов:</b> {len(admins)}\n"
        f"⏰ <b>Обходов:</b> {active} активных (из {len(reminders)})\n"
        f"⏱ <b>Таймаут тревоги:</b> {config.get('escalation_minutes', 15)} мин\n"
        f"🔔 <b>Напоминание заранее:</b> за {config.get('advance_minutes', 0)} мин\n"
        f"📋 <b>Ежедневный отчёт:</b> в {config.get('daily_report_time', '18:45')}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    b = InlineKeyboardBuilder()
    b.button(text="◀️ Назад в меню", callback_data="admin_main")
    await callback.message.edit_text(card, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
    await callback.answer()


# ==============================================================================
# ДЕЖУРСТВА И СМЕНЫ
# ==============================================================================

@dp.callback_query(F.data == "admin_duty_menu")
async def cb_admin_duty_menu(callback: CallbackQuery):
    card = (
        "👮‍♂️ <b>Управление дежурными на смене</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Нажмите на сотрудника, чтобы переключить статус:\n\n"
        "• <b>✅ НА СМЕНЕ</b> — бот будет вызывать его на обход\n"
        "• <b>❌ ВЫХОДНОЙ</b> — бот не будет его беспокоить\n\n"
        "<i>Если дежурный не выбран, уведомления получают все.</i>"
    )
    await callback.message.edit_text(card, reply_markup=get_duty_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_duty_"))
async def cb_toggle_duty(callback: CallbackQuery):
    idx = int(callback.data.split("_")[2])
    config = load_config()
    all_staff = config.get("mentions", [])

    if 0 <= idx < len(all_staff):
        target = all_staff[idx]
        tz_name = config.get("timezone", "Asia/Tashkent")
        tz = pytz.timezone(tz_name)
        date_str, day_data = get_today_data(tz)
        duty_users = day_data.get("duty_users", [])

        data = load_data()
        if target in duty_users:
            duty_users.remove(target)
            msg = f"{target}: ❌ ВЫХОДНОЙ"
        else:
            duty_users.append(target)
            msg = f"{target}: ✅ НА СМЕНЕ"

        data[date_str]["duty_users"] = duty_users
        save_data(data)

        await callback.message.edit_reply_markup(reply_markup=get_duty_keyboard())
        await callback.answer(msg)
    else:
        await callback.answer("Ошибка индекса")


@dp.callback_query(F.data == "reset_duty")
async def cb_reset_duty(callback: CallbackQuery):
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    date_str, _ = get_today_data(tz)

    data = load_data()
    data[date_str]["duty_users"] = []
    save_data(data)

    await callback.message.edit_reply_markup(reply_markup=get_duty_keyboard())
    await callback.answer("Дежурные сброшены: уведомления получают все")


@dp.callback_query(F.data.in_(["claim_duty_self", "unclaim_duty_self"]))
async def cb_claim_duty_self(callback: CallbackQuery):
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    date_str, day_data = get_today_data(tz)
    duty_users = day_data.get("duty_users", [])

    user = callback.from_user
    username_str = f"@{user.username}" if user.username else user.full_name

    data = load_data()
    if callback.data == "claim_duty_self":
        if username_str not in duty_users:
            duty_users.append(username_str)
            data[date_str]["duty_users"] = duty_users
            save_data(data)
            await callback.answer(f"✅ Вы заступили на смену: {username_str}", show_alert=True)
        else:
            await callback.answer("Вы уже находитесь на смене!", show_alert=False)
    else:
        if username_str in duty_users:
            duty_users.remove(username_str)
            data[date_str]["duty_users"] = duty_users
            save_data(data)
            await callback.answer(f"🚪 Вы сдали смену: {username_str}", show_alert=True)
        else:
            await callback.answer("Вас не было в списке дежурных.", show_alert=False)

    cur_text = " ".join(duty_users) if duty_users else "не назначены"
    card = (
        "📋 <b>График дежурства на сегодня</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Сейчас на смене:</b> {cur_text}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Нажмите кнопку ниже, чтобы заступить или сдать смену:</i>"
    )
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="👮‍♂️ Заступить на смену", callback_data="claim_duty_self"),
        InlineKeyboardButton(text="🚪 Сдать смену", callback_data="unclaim_duty_self")
    )
    try:
        await callback.message.edit_text(card, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ==============================================================================
# УПРАВЛЕНИЕ РАСПИСАНИЕМ
# ==============================================================================

@dp.callback_query(F.data == "admin_schedule")
async def cb_admin_schedule(callback: CallbackQuery):
    card = (
        "⏰ <b>Управление графиком обходов</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Нажмите на нужное время, чтобы включить или отключить:\n\n"
        "• <b>✅ ВКЛ</b> — обход активен (напоминание придёт)\n"
        "• <b>❌ ВЫКЛ</b> — обход отключен (напоминание пропустится)"
    )
    await callback.message.edit_text(card, reply_markup=get_schedule_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_rem_"))
async def cb_toggle_reminder(callback: CallbackQuery, bot: Bot):
    global scheduler_instance
    idx = int(callback.data.split("_")[2])
    config = load_config()
    reminders = config.get("reminders", [])

    if 0 <= idx < len(reminders):
        current = reminders[idx].get("enabled", True)
        reminders[idx]["enabled"] = not current
        config["reminders"] = reminders
        save_config(config)

        if scheduler_instance:
            setup_jobs(scheduler_instance, bot)

        await callback.message.edit_reply_markup(reply_markup=get_schedule_keyboard())
        status_text = "ВКЛЮЧЕНО ✅" if not current else "ОТКЛЮЧЕНО ❌"
        await callback.answer(f"{reminders[idx]['time']}: {status_text}")
    else:
        await callback.answer("Ошибка")


@dp.callback_query(F.data == "add_reminder")
async def cb_add_reminder(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddReminderState.waiting_for_time)
    await callback.message.edit_text(
        "⏰ <b>Добавление нового обхода</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Введите время в формате <b>ЧЧ:ММ</b> (например: <code>13:15</code>):",
        reply_markup=get_cancel_keyboard(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@dp.message(AddReminderState.waiting_for_time)
async def process_reminder_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    if ":" not in time_str or len(time_str.split(":")) != 2:
        await message.reply("⚠️ Неверный формат. Введите время в формате <b>ЧЧ:ММ</b> (например: <code>13:00</code>):")
        return

    try:
        h, m = map(int, time_str.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        formatted_time = f"{h:02d}:{m:02d}"
    except ValueError:
        await message.reply("⚠️ Часы от 0 до 23, минуты от 0 до 59. Попробуйте еще раз:")
        return

    await state.update_data(time=formatted_time)
    await state.set_state(AddReminderState.waiting_for_label)
    await message.answer(
        f"Время: <b>{formatted_time}</b>\n\n"
        "Введите краткое название (например: <code>дневной обход</code>):",
        reply_markup=get_cancel_keyboard(),
        parse_mode=ParseMode.HTML
    )


@dp.message(AddReminderState.waiting_for_label)
async def process_reminder_label(message: Message, state: FSMContext, bot: Bot):
    global scheduler_instance
    label = message.text.strip()
    data = await state.get_data()
    formatted_time = data["time"]

    config = load_config()
    reminders = config.get("reminders", [])
    reminders.append({"time": formatted_time, "label": label, "enabled": True})
    reminders.sort(key=lambda x: x["time"])
    config["reminders"] = reminders
    save_config(config)

    if scheduler_instance:
        setup_jobs(scheduler_instance, bot)

    await state.clear()
    await message.answer(
        f"🎉 Обход <b>{formatted_time}</b> ({label}) успешно добавлен в расписание!",
        reply_markup=get_admin_inline_menu(),
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data == "del_reminder_menu")
async def cb_del_reminder_menu(callback: CallbackQuery):
    config = load_config()
    reminders = config.get("reminders", [])
    b = InlineKeyboardBuilder()

    for idx, r in enumerate(reminders):
        b.button(text=f"🗑 {r.get('time')} - {r.get('label', '')[:10]}", callback_data=f"del_rem_{idx}")

    b.adjust(2)
    b.row(InlineKeyboardButton(text="◀️ Назад к графику", callback_data="admin_schedule"))

    await callback.message.edit_text(
        "🗑 <b>Выберите время для удаления:</b>",
        reply_markup=b.as_markup(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("del_rem_"))
async def cb_del_reminder_confirm(callback: CallbackQuery, bot: Bot):
    global scheduler_instance
    idx = int(callback.data.split("_")[2])
    config = load_config()
    reminders = config.get("reminders", [])

    if 0 <= idx < len(reminders):
        deleted = reminders.pop(idx)
        config["reminders"] = reminders
        save_config(config)

        if scheduler_instance:
            setup_jobs(scheduler_instance, bot)

        await callback.answer(f"Удалено: {deleted['time']}")
        await cb_del_reminder_menu(callback)
    else:
        await callback.answer("Ошибка")


# ==============================================================================
# УПРАВЛЕНИЕ СОТРУДНИКАМИ
# ==============================================================================

@dp.callback_query(F.data == "admin_mentions")
async def cb_admin_mentions(callback: CallbackQuery):
    config = load_config()
    mentions = config.get("mentions", [])
    mentions_text = "\n".join([f"• <b>{m}</b>" for m in mentions]) if mentions else "<i>Список пока пуст</i>"

    card = (
        "👥 <b>Штат ответственных сотрудников</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{mentions_text}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Их никнеймы используются для тегов и назначения смен.</i>"
    )
    await callback.message.edit_text(card, reply_markup=get_mentions_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.callback_query(F.data == "add_mention")
async def cb_add_mention(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddMentionState.waiting_for_username)
    await callback.message.edit_text(
        "✍️ Отправьте юзернейм сотрудника в Telegram (например, <code>@username</code> или несколько через пробел):",
        reply_markup=get_cancel_keyboard(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@dp.message(AddMentionState.waiting_for_username)
async def process_add_mention(message: Message, state: FSMContext):
    parts = message.text.split()
    new_mentions = [p if p.startswith("@") else f"@{p}" for p in parts]

    config = load_config()
    current = config.get("mentions", [])
    for m in new_mentions:
        if m not in current:
            current.append(m)

    config["mentions"] = current
    save_config(config)

    await state.clear()
    await message.answer(
        f"✅ Добавлены сотрудники:\n{' '.join(new_mentions)}",
        reply_markup=get_admin_inline_menu(),
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data == "del_mention_menu")
async def cb_del_mention_menu(callback: CallbackQuery):
    config = load_config()
    mentions = config.get("mentions", [])
    b = InlineKeyboardBuilder()

    for idx, m in enumerate(mentions):
        b.button(text=f"🗑 {m}", callback_data=f"del_mention_{idx}")

    b.adjust(2)
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_mentions"))

    await callback.message.edit_text(
        "🗑 <b>Нажмите на сотрудника для удаления из штата:</b>",
        reply_markup=b.as_markup(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("del_mention_"))
async def cb_del_mention(callback: CallbackQuery):
    idx = int(callback.data.split("_")[2])
    config = load_config()
    mentions = config.get("mentions", [])

    if 0 <= idx < len(mentions):
        deleted = mentions.pop(idx)
        config["mentions"] = mentions
        save_config(config)
        await callback.answer(f"Удален: {deleted}")
        await cb_del_mention_menu(callback)
    else:
        await callback.answer("Ошибка")


# ==============================================================================
# ОБЪЯВЛЕНИЯ
# ==============================================================================

@dp.callback_query(F.data == "admin_announcement")
async def cb_admin_announcement(callback: CallbackQuery, state: FSMContext):
    config = load_config()
    if not config.get("chat_id"):
        await callback.answer("⚠️ Сначала привяжите группу командой /setchat!", show_alert=True)
        return

    await state.set_state(AnnouncementState.waiting_for_content)
    card = (
        "📢 <b>Публикация объявления в группу</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Отправьте текст объявления (можно с фото).\n"
        "Бот покажет аккуратный предпросмотр перед отправкой."
    )
    await callback.message.edit_text(card, reply_markup=get_cancel_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AnnouncementState.waiting_for_content)
async def process_announcement_content(message: Message, state: FSMContext):
    if message.photo:
        photo_id = message.photo[-1].file_id
        caption = message.caption or ""
        await state.update_data(type="photo", photo_id=photo_id, text=caption)
    else:
        text = message.text or message.html_text or ""
        await state.update_data(type="text", text=text)

    await state.set_state(AnnouncementState.confirm)

    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Отправить в группу", callback_data="send_announcement_regular"),
        InlineKeyboardButton(text="📌 Отправить и закрепить", callback_data="send_announcement_pin")
    )
    b.row(
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")
    )

    data = await state.get_data()
    preview_header = "👁 <b>ПРЕДПРОСМОТР ОБЪЯВЛЕНИЯ:</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    if data.get("type") == "photo":
        await message.answer_photo(
            photo=data["photo_id"],
            caption=preview_header + (data.get("text") or ""),
            reply_markup=b.as_markup(),
            parse_mode=ParseMode.HTML
        )
    else:
        await message.answer(
            preview_header + (data.get("text") or ""),
            reply_markup=b.as_markup(),
            parse_mode=ParseMode.HTML
        )


@dp.callback_query(F.data.in_(["send_announcement_regular", "send_announcement_pin"]))
async def cb_send_announcement(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    pin = callback.data == "send_announcement_pin"
    config = load_config()
    chat_id = config.get("chat_id")

    try:
        if data.get("type") == "photo":
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=data["photo_id"],
                caption=data.get("text", ""),
                parse_mode=ParseMode.HTML
            )
        else:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=data.get("text", ""),
                parse_mode=ParseMode.HTML
            )

        if pin:
            try:
                await bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id)
            except Exception as e:
                logger.warning(f"Не удалось закрепить: {e}")

        await state.clear()
        pin_badge = " (и закреплено 📌)" if pin else ""
        await callback.message.answer(
            f"🎉 <b>Объявление успешно опубликовано{pin_badge}!</b>",
            reply_markup=get_admin_inline_menu(),
            parse_mode=ParseMode.HTML
        )
        await callback.answer("Отправлено!")
    except Exception as e:
        logger.error(f"Ошибка публикации объявления: {e}")
        await callback.answer(f"Ошибка: {e}", show_alert=True)


# ==============================================================================
# ОПРОСЫ
# ==============================================================================

@dp.callback_query(F.data == "admin_poll")
async def cb_admin_poll(callback: CallbackQuery, state: FSMContext):
    config = load_config()
    if not config.get("chat_id"):
        await callback.answer("⚠️ Сначала привяжите группу командой /setchat!", show_alert=True)
        return

    await state.set_state(PollState.waiting_for_question)
    card = (
        "📊 <b>Создание опроса для сотрудников</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Шаг 1: Введите <b>вопрос для опроса</b> (до 300 симв.):"
    )
    await callback.message.edit_text(card, reply_markup=get_cancel_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(PollState.waiting_for_question)
async def process_poll_question(message: Message, state: FSMContext):
    question = message.text.strip()
    if len(question) > 300:
        await message.reply(f"⚠️ Вопрос слишком длинный ({len(question)} симв.). В Telegram лимит 300 символов. Сократите:")
        return

    await state.update_data(question=question)
    await state.set_state(PollState.waiting_for_options)
    card = (
        f"❓ Вопрос: <b>{question}</b>\n\n"
        "Шаг 2: Введите <b>варианты ответов</b> (каждый вариант с новой строки, от 2 до 10 вариантов):\n\n"
        "<i>Пример:</i>\n"
        "Да, готов\n"
        "Нужна замена\n"
        "Воздержусь"
    )
    await message.answer(card, reply_markup=get_cancel_keyboard(), parse_mode=ParseMode.HTML)


@dp.message(PollState.waiting_for_options)
async def process_poll_options(message: Message, state: FSMContext):
    lines = [line.strip() for line in message.text.split("\n") if line.strip()]
    if len(lines) < 2:
        await message.reply("⚠️ Укажите как минимум 2 варианта ответа (каждый с новой строки):")
        return
    if len(lines) > 10:
        await message.reply("⚠️ Telegram поддерживает максимум 10 вариантов. Сократите список:")
        return

    long_options = [opt for opt in lines if len(opt) > 100]
    if long_options:
        await message.reply(f"⚠️ Вариант не должен превышать 100 символов:\n<i>{long_options[0][:40]}...</i>")
        return

    await state.update_data(options=lines)
    await state.set_state(PollState.confirm)

    data = await state.get_data()
    options_preview = "\n".join([f"• {opt}" for opt in lines])

    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Опубликовать опрос", callback_data="send_poll_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")
    )

    card = (
        "👁 <b>ПРЕДПРОСМОТР ОПРОСА:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"❓ <b>Вопрос:</b> {data['question']}\n\n"
        f"📝 <b>Варианты ответов:</b>\n{options_preview}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Опубликовать опрос в группе?"
    )
    await message.answer(card, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "send_poll_confirm")
async def cb_send_poll_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    config = load_config()
    chat_id = config.get("chat_id")

    try:
        await bot.send_poll(
            chat_id=chat_id,
            question=data["question"],
            options=data["options"],
            is_anonymous=False
        )
        await state.clear()
        await callback.message.edit_text(
            "🎉 <b>Опрос успешно опубликован в рабочей группе!</b>",
            reply_markup=get_admin_inline_menu(),
            parse_mode=ParseMode.HTML
        )
        await callback.answer("Опрос опубликован!")
    except Exception as e:
        logger.error(f"Ошибка опроса: {e}")
        await callback.answer(f"Ошибка: {e}", show_alert=True)


# ==============================================================================
# ЭКСТРЕННЫЙ ВЫЗОВ
# ==============================================================================

@dp.callback_query(F.data == "admin_call_now")
async def cb_admin_call_now(callback: CallbackQuery):
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🚨 Да, отправить оповещение", callback_data="confirm_call_now"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin_main")
    )
    card = (
        "🚨 <b>Экстренный вызов на обход</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Отправить внеплановое оповещение в группу прямо сейчас с тегами дежурных?"
    )
    await callback.message.edit_text(card, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.callback_query(F.data == "confirm_call_now")
async def cb_confirm_call_now(callback: CallbackQuery, bot: Bot):
    reminder_info = {"time": "СЕЙЧАС", "label": "внеплановый срочный обход"}
    await send_reminder(bot, reminder_info)
    await callback.message.edit_text(
        "✅ <b>Внеплановое оповещение успешно отправлено в группу!</b>",
        reply_markup=get_admin_inline_menu(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer("Отправлено!")


# ==============================================================================
# НАСТРОЙКА РАСПИСАНИЯ И МЕНЮ КОМАНД
# ==============================================================================

def setup_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    scheduler.remove_all_jobs()
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    days = config.get("days", "mon-sat")
    reminders = config.get("reminders", [])

    advance = max(0, int(config.get("advance_minutes", 0)))

    count = 0
    for r in reminders:
        if not r.get("enabled", True):
            continue
        time_str = r.get("time")
        if not time_str or ":" not in time_str:
            continue
        try:
            hour, minute = map(int, time_str.split(":"))
            # Напоминание приходит заранее: за advance минут до времени обхода
            # (не раньше 00:00 того же дня, чтобы не сбить день недели)
            fire_total = max(0, hour * 60 + minute - advance)
            fire_hour, fire_minute = divmod(fire_total, 60)
            trigger = CronTrigger(day_of_week=days, hour=fire_hour, minute=fire_minute, timezone=tz)
            scheduler.add_job(
                send_reminder,
                trigger=trigger,
                args=[bot, {**r, "scheduled": True}],
                name=f"Reminder_{time_str}",
                id=f"rem_job_{time_str}",
                replace_existing=True
            )
            count += 1
        except Exception as e:
            logger.error(f"Ошибка планирования {r}: {e}")

    report_time = config.get("daily_report_time", "18:45")
    try:
        rh, rm = map(int, report_time.split(":"))
        scheduler.add_job(
            send_daily_report,
            trigger=CronTrigger(day_of_week=days, hour=rh, minute=rm, timezone=tz),
            args=[bot],
            name="DailyReport",
            id="daily_report_job",
            replace_existing=True
        )
    except Exception as e:
        logger.error(f"Ошибка планирования отчета: {e}")

    logger.info(f"Запланировано активных обходов: {count} из {len(reminders)}")


async def register_bot_commands(bot: Bot):
    """Регистрирует подсказки команд в меню Telegram"""
    private_commands = [
        BotCommand(command="admin", description="🎛 Панель управления ботом"),
        BotCommand(command="report", description="📋 Отчёт по обходам за сегодня"),
        BotCommand(command="duty", description="👮‍♂️ Дежурные на смене"),
        BotCommand(command="test", description="🔔 Проверить вызов на обход"),
        BotCommand(command="status", description="ℹ️ Статус системы и время"),
        BotCommand(command="help", description="❓ Справка по работе бота")
    ]
    group_commands = [
        BotCommand(command="duty", description="👮‍♂️ Кто на смене / Заступить"),
        BotCommand(command="report", description="📋 Отчёт по обходам за сегодня"),
        BotCommand(command="setchat", description="📌 Привязать эту группу"),
        BotCommand(command="status", description="ℹ️ Время в Ташкенте и график"),
        BotCommand(command="test", description="🔔 Проверить оповещение")
    ]
    try:
        await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
    except Exception as e:
        logger.warning(f"Не удалось зарегистрировать подсказки команд: {e}")


async def main():
    config = load_config()
    token = (os.getenv("BOT_TOKEN") or config.get("bot_token", "")).strip()

    if not token or token == "ВСТАВЬТЕ_ТОКЕН_СЮДА" or token == "YOUR_BOT_TOKEN_HERE":
        logger.error("ОШИБКА: Токен бота не указан! Укажите его в .env или config.json")
        return

    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # Регистрация меню команд
    await register_bot_commands(bot)

    global scheduler_instance
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler_instance = scheduler
    setup_jobs(scheduler, bot)
    scheduler.start()

    logger.info("Бот успешно запущен и ожидает сообщений...")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
