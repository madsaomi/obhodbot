import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from html import escape as h
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

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_data_dir() -> str:
    """Папка для config.json и bot_data.json.

    DATA_DIR из окружения > /data (постоянное хранилище Amvera/Docker-том) > папка бота.
    """
    env_dir = os.getenv("DATA_DIR")
    if env_dir:
        return env_dir
    if sys.platform != "win32" and os.path.isdir("/data"):
        return "/data"
    return BASE_DIR


DATA_DIR = resolve_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DATA_PATH = os.path.join(DATA_DIR, "bot_data.json")
DELETES_PATH = os.path.join(DATA_DIR, "pending_deletes.json")
LOCK_PATH = os.path.join(DATA_DIR, "bot.lock")
LOG_PATH = os.path.join(DATA_DIR, "bot.log")

# Лог дублируется в файл: при фоновом запуске (run_silent.vbs) консоли нет
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    _file_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(_file_handler)
except OSError as _e:
    logger.warning(f"Не удалось открыть файл лога {LOG_PATH}: {_e}")


_lock_file = None


def acquire_single_instance_lock() -> bool:
    """Не даёт запустить вторую копию бота с той же папкой данных (иначе всё дублируется)."""
    global _lock_file
    _lock_file = open(LOCK_PATH, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            _lock_file.seek(0)
            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _lock_file.close()
        _lock_file = None
        return False
    return True


async def tg_retry(call, *args, attempts: int = 3, **kwargs):
    """Вызов Telegram API с повторами при сбое сети/сервера или флуд-лимите."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as e:
            last_exc, delay = e, e.retry_after + 1
        except (TelegramNetworkError, TelegramServerError) as e:
            last_exc, delay = e, 3 * attempt
        if attempt < attempts:
            logger.warning(f"Сбой связи с Telegram ({last_exc}), повтор {attempt + 1}/{attempts} через {delay} с")
            await asyncio.sleep(delay)
    raise last_exc


def ensure_config_exists():
    """При первом запуске в новой папке данных копирует туда стартовый конфиг."""
    if os.path.exists(CONFIG_PATH):
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    for name in ("config.json", "config.example.json"):
        src = os.path.join(BASE_DIR, name)
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(CONFIG_PATH):
            shutil.copyfile(src, CONFIG_PATH)
            logger.info(f"Создан {CONFIG_PATH} из {name}")
            return


# ==============================================================================
# УПРАВЛЕНИЕ ДАННЫМИ
# ==============================================================================

def atomic_write_json(path: str, obj):
    """Пишет во временный файл и подменяет им основной: при сбое старый файл остаётся целым."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def backup_corrupt_file(path: str) -> str:
    backup = f"{path}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        shutil.copyfile(path, backup)
    except OSError:
        pass
    return backup


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Файл {CONFIG_PATH} не найден!")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.critical(f"config.json повреждён ({e}). Исправьте файл или восстановите из config.example.json.")
        raise


def save_config(config: dict):
    atomic_write_json(CONFIG_PATH, config)


def load_data() -> dict:
    if not os.path.exists(DATA_PATH):
        return {}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # Не теряем историю молча: сохраняем копию повреждённого файла
        backup = backup_corrupt_file(DATA_PATH)
        logger.error(f"bot_data.json повреждён ({e}). Копия сохранена в {backup}, начинаю с пустых данных.")
        return {}


def save_data(data: dict):
    atomic_write_json(DATA_PATH, data)


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
        prune_old_days(data, tz)
        save_data(data)
    return date_str, data[date_str]


def prune_old_days(data: dict, tz):
    """Удаляет из bot_data.json дни старше keep_days (по умолчанию 90)."""
    keep_days = int(load_config().get("keep_days", 90))
    if keep_days <= 0:
        return
    cutoff = (datetime.now(tz) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for key in [k for k in data if k < cutoff]:
        del data[key]


DAY_NAMES = {"mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт", "fri": "Пт", "sat": "Сб", "sun": "Вс"}


def format_days(days: str) -> str:
    """'mon-sat' -> 'Пн–Сб', 'mon,wed,fri' -> 'Пн, Ср, Пт'."""
    parts = []
    for part in str(days).lower().split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            parts.append(f"{DAY_NAMES.get(a, a)}–{DAY_NAMES.get(b, b)}")
        elif part:
            parts.append(DAY_NAMES.get(part, part))
    return ", ".join(parts) or str(days)


def is_admin(user_id: int) -> bool:
    """Админы задаются только вручную в admin_ids (config.json)."""
    return user_id in load_config().get("admin_ids", [])


# Кнопки, доступные всем сотрудникам; все остальные callback'и — только админам
PUBLIC_CALLBACKS = {"already_done", "claim_duty_self", "unclaim_duty_self"}
PUBLIC_CALLBACK_PREFIXES = ("confirm:",)


async def admin_callback_guard(handler, event: CallbackQuery, data: dict):
    cb_data = event.data or ""
    if cb_data in PUBLIC_CALLBACKS or cb_data.startswith(PUBLIC_CALLBACK_PREFIXES):
        return await handler(event, data)
    if not is_admin(event.from_user.id):
        await event.answer("⛔️ Доступно только администраторам.", show_alert=True)
        return None
    return await handler(event, data)


# ==============================================================================
# ТИХИЙ РЕЖИМ ДЛЯ ГРУПП
# ==============================================================================

_background_tasks: set = set()


def group_autodelete_delay() -> int:
    return int(load_config().get("group_autodelete_seconds", 120))


def _load_pending_deletes() -> list[dict]:
    try:
        with open(DELETES_PATH, "r", encoding="utf-8") as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []


def _forget_pending_deletes(chat_id: int, message_ids: list[int]):
    drop = {(chat_id, mid) for mid in message_ids}
    items = [it for it in _load_pending_deletes() if (it.get("chat_id"), it.get("message_id")) not in drop]
    try:
        atomic_write_json(DELETES_PATH, items)
    except OSError as e:
        logger.warning(f"Не удалось обновить список удалений: {e}")


async def _delete_later(bot: Bot, chat_id: int, message_ids: list[int], delay: float):
    await asyncio.sleep(max(0, delay))
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception as e:
            # Чужие сообщения бот удаляет, только если он админ группы с правом удаления;
            # сообщения старше 48 часов Telegram удалить не даёт
            logger.debug(f"Не удалось удалить сообщение {mid}: {e}")
    _forget_pending_deletes(chat_id, message_ids)


def _start_delete_task(bot: Bot, chat_id: int, message_ids: list[int], delay: float):
    task = asyncio.create_task(_delete_later(bot, chat_id, message_ids, delay))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def schedule_delete(bot: Bot, chat_id: int, message_ids: list[int], delay: int):
    """Удаляет сообщения через delay секунд. План сохраняется на диск и переживает перезапуск."""
    if delay <= 0 or not message_ids:
        return
    due = time.time() + delay
    items = _load_pending_deletes()
    items += [{"chat_id": chat_id, "message_id": mid, "at": due} for mid in message_ids]
    try:
        atomic_write_json(DELETES_PATH, items)
    except OSError as e:
        logger.warning(f"Не удалось сохранить список удалений: {e}")
    _start_delete_task(bot, chat_id, message_ids, delay)


def restore_pending_deletes(bot: Bot):
    """После перезапуска доудаляет сообщения, которые бот не успел убрать."""
    items = _load_pending_deletes()
    now = time.time()
    for it in items:
        try:
            _start_delete_task(bot, int(it["chat_id"]), [int(it["message_id"])], float(it.get("at", now)) - now)
        except (KeyError, TypeError, ValueError):
            continue
    if items:
        logger.info(f"Восстановлено отложенных удалений сообщений: {len(items)}")


def schedule_group_cleanup(message: Message, *sent: Message):
    """В группах удаляет команду пользователя и ответ бота через group_autodelete_seconds."""
    if message.chat.type == "private":
        return
    ids = [message.message_id] + [m.message_id for m in sent if m is not None]
    schedule_delete(message.bot, message.chat.id, ids, group_autodelete_delay())


async def deny_if_not_allowed(message: Message) -> bool:
    """Отчёт, статус и дежурства видны только админам и в привязанной рабочей группе.

    Возвращает True, если доступ запрещён (ответ уже отправлен).
    """
    if message.from_user and is_admin(message.from_user.id):
        return False
    chat_id = load_config().get("chat_id")
    if message.chat.type != "private" and chat_id and message.chat.id == chat_id:
        return False
    await group_reply(message, "⛔️ Эта команда доступна только в рабочей группе или администраторам.")
    return True


async def group_reply(message: Message, text: str, **kwargs) -> Message:
    """reply(), который в группе потом сам убирает и команду, и ответ."""
    sent = await message.reply(text, **kwargs)
    schedule_group_cleanup(message, sent)
    return sent


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
dp.callback_query.outer_middleware(admin_callback_guard)
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

    # В кнопках — время обхода, а не номер в списке: старая клавиатура
    # не сработает на другой обход после добавления/удаления
    for r in reminders:
        icon = "✅ ВКЛ" if r.get("enabled", True) else "❌ ВЫКЛ"
        b.button(text=f"{r.get('time')}  {icon}", callback_data=f"toggle_rem_{r.get('time')}")

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
    for staff in all_staff:
        status = "✅ НА СМЕНЕ" if staff in duty_users else "❌ ВЫХОДНОЙ"
        b.button(text=f"{staff}: {status}", callback_data=f"toggle_duty_{staff_key(staff)}")

    b.adjust(1)
    b.row(
        InlineKeyboardButton(text="🔄 Сбросить выбор (все дежурят)", callback_data="reset_duty"),
        InlineKeyboardButton(text="◀️ Назад в меню", callback_data="admin_main")
    )
    return b.as_markup()


def staff_key(name: str) -> str:
    """Короткий стабильный ключ сотрудника для callback_data (лимит Telegram — 64 байта)."""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


def find_staff(mentions: list[str], key: str):
    return next((m for m in mentions if staff_key(m) == key), None)


def find_reminder(reminders: list[dict], time_str: str):
    return next((r for r in reminders if r.get("time") == time_str), None)


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
        f"⚠️ <b>{h(time_str)} — Обход не подтверждён!</b> ({h(label)})\n"
        f"👥 Дежурные: {h(mentions_str)}\n"
        f"<i>Прошло {timeout} минут, никто не подтвердил выход на обход.</i>"
    )

    is_test = round_kind(time_str, round_info) == "test"
    # Карточка живёт в том чате, куда её отправили (chat_id мог смениться)
    card_chat_id = round_info.get("chat_id", chat_id)

    if card_chat_id:
        # Не плодим сообщения: превращаем само напоминание в тревогу,
        # кнопка подтверждения остаётся (можно подтвердить с опозданием)
        b = InlineKeyboardBuilder()
        b.button(text="✅ Я на обходе", callback_data=f"confirm:{date_str}:{time_str}")
        edited = False
        message_id = round_info.get("message_id")
        if message_id:
            try:
                await tg_retry(
                    bot.edit_message_text,
                    chat_id=card_chat_id, message_id=message_id, text=alert_card,
                    reply_markup=b.as_markup(), parse_mode=ParseMode.HTML
                )
                edited = True
            except Exception as e:
                logger.warning(f"Не удалось обновить карточку обхода: {e}")
        if is_test:
            # Тестовую карточку не оставляем в группе навсегда
            if message_id:
                schedule_delete(bot, card_chat_id, [message_id], group_autodelete_delay())
        elif not edited:
            try:
                await tg_retry(bot.send_message, chat_id=card_chat_id, text=alert_card,
                               reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Ошибка отправки тревоги: {e}")

    admin_card = (
        f"🚨 <b>[Тревога руководству]</b>{' (тест)' if is_test else ''}\n"
        f"Обход в <b>{h(time_str)}</b> ({h(label)}) не подтверждён вовремя!\n"
        f"Дежурные: {h(mentions_str)}"
    )
    for a_id in admin_ids:
        try:
            await tg_retry(bot.send_message, chat_id=a_id, text=admin_card, parse_mode=ParseMode.HTML)
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

    kind = reminder_info.get("kind", "scheduled")
    if kind == "emergency":
        # У каждого срочного вызова свой ключ, чтобы второй вызов не затирал первый
        time_str = f"срочно {now.strftime('%H:%M')}"
    else:
        time_str = reminder_info.get("time", now.strftime("%H:%M"))
    label = reminder_info.get("label", "обход")

    advance = int(config.get("advance_minutes", 0))
    if kind == "scheduled" and reminder_info.get("scheduled") and advance > 0:
        reminder_card = (
            f"🔔 <b>Через {advance} мин обход в {h(time_str)}!</b> ({h(label)})\n"
            f"👥 Дежурные: {h(mentions_str)}"
        )
    else:
        reminder_card = (
            f"🔔 <b>{h(time_str)} — Время обхода!</b> ({h(label)})\n"
            f"👥 Дежурные: {h(mentions_str)}"
        )

    b = InlineKeyboardBuilder()
    b.button(
        text="✅ Я на обходе",
        callback_data=f"confirm:{date_str}:{time_str}"
    )

    try:
        sent = await tg_retry(
            bot.send_message,
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
            "kind": kind,
            "status": "pending",
            "chat_id": chat_id,
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

    # Подтверждение после тревоги — это опоздание, его видно в карточке и отчёте
    is_late = round_info.get("status") == "missed"
    round_info["status"] = "confirmed"
    round_info["late"] = is_late
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

    late_note = " (с опозданием)" if is_late else ""
    confirmed_card = f"✅ <b>{h(time_str)} — Обход выполнил {h(username_str)} в {confirm_time_str}{late_note}</b>"

    try:
        # Убираем кнопку, чтобы чат оставался чистым и аккуратным
        await callback.message.edit_text(
            text=confirmed_card,
            reply_markup=None,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.warning(f"Ошибка обновления карточки: {e}")

    # Тестовую карточку после проверки убираем из группы
    if round_kind(time_str, round_info) == "test" and callback.message and callback.message.chat.type != "private":
        schedule_delete(callback.bot, callback.message.chat.id, [callback.message.message_id], group_autodelete_delay())

    await callback.answer(f"✅ Обход в {time_str} принят{late_note}!", show_alert=False)


@dp.callback_query(F.data == "already_done")
async def cb_already_done(callback: CallbackQuery):
    await callback.answer("Этот обход уже успешно выполнен и занесен в отчет дня ✅")


# ==============================================================================
# ОТЧЁТНОСТЬ
# ==============================================================================

def round_kind(key: str, round_info: dict) -> str:
    """scheduled / test / emergency; старые записи без поля kind определяем по ключу."""
    kind = round_info.get("kind")
    if kind:
        return kind
    if key == "ТЕСТ":
        return "test"
    if key == "СЕЙЧАС" or key.startswith("срочно"):
        return "emergency"
    return "scheduled"


def is_working_day(config: dict, dt: datetime) -> bool:
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        trigger = CronTrigger(day_of_week=config.get("days", "mon-sat"), hour=0, minute=0, timezone=dt.tzinfo)
        nxt = trigger.get_next_fire_time(None, start)
    except Exception:
        return True
    return nxt is not None and nxt.date() == start.date()


def get_unsent_rounds(config: dict, date_str: str, recorded: dict) -> list[tuple[str, str]]:
    """Обходы сегодняшнего графика, время напоминания которых прошло, а записи нет.

    Так в отчёте видно, что бот был выключен или группа не привязана.
    """
    tz = pytz.timezone(config.get("timezone", "Asia/Tashkent"))
    now = datetime.now(tz)
    if date_str != now.strftime("%Y-%m-%d") or not is_working_day(config, now):
        return []
    advance = max(0, int(config.get("advance_minutes", 0)))
    result = []
    for r in config.get("reminders", []):
        t = r.get("time", "")
        if not r.get("enabled", True) or t in recorded or ":" not in t:
            continue
        try:
            hh, mm = map(int, t.split(":"))
        except ValueError:
            continue
        fire_total = max(0, hh * 60 + mm - advance)
        fire_at = now.replace(hour=fire_total // 60, minute=fire_total % 60, second=0, microsecond=0)
        if now >= fire_at + timedelta(minutes=2):
            result.append((t, r.get("label", "обход")))
    return result


def get_report_times(config: dict) -> list[str]:
    """Время автоотчётов: daily_report_times (список) или старое daily_report_time."""
    times = config.get("daily_report_times")
    if not times:
        times = [config.get("daily_report_time", "18:45")]
    return sorted(t for t in times if isinstance(t, str) and ":" in t)


def generate_report_text(date_str: str, title: str = "ОТЧЁТ ЗА СМЕНУ") -> str:
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)

    data = load_data()
    day_data = data.get(date_str, {})
    rounds = day_data.get("rounds", {})
    duty_users = day_data.get("duty_users", [])

    duty_str = h(" ".join(duty_users)) if duty_users else "Все сотрудники (общий график)"

    # В статистику идут только обходы по графику; тесты не считаются вовсе,
    # срочные вызовы показываются отдельным списком
    scheduled = {t: r for t, r in rounds.items() if round_kind(t, r) == "scheduled"}
    emergency = {t: r for t, r in rounds.items() if round_kind(t, r) == "emergency"}
    unsent = get_unsent_rounds(config, date_str, scheduled)

    on_time = sum(1 for r in scheduled.values() if r.get("status") == "confirmed" and not r.get("late"))
    late = sum(1 for r in scheduled.values() if r.get("status") == "confirmed" and r.get("late"))
    missed = sum(1 for r in scheduled.values() if r.get("status") == "missed")
    pending = sum(1 for r in scheduled.values() if r.get("status") not in ("confirmed", "missed"))
    total = len(scheduled) + len(unsent)

    percent = int((on_time / total * 100)) if total > 0 else 0

    def fmt_line(t: str, r: dict) -> str:
        lbl = h(r.get("label", "обход").ljust(15))
        st = r.get("status")
        if st == "confirmed":
            by = h(r.get("confirmed_by", "—"))
            at = r.get("confirmed_at", "")
            if r.get("late"):
                return f"🟠 <code>{h(t)}</code> | {lbl} | {by} ({at}, с опозданием)"
            return f"🟢 <code>{h(t)}</code> | {lbl} | {by} ({at})"
        if st == "missed":
            return f"🔴 <code>{h(t)}</code> | {lbl} | <b>ПРОПУЩЕН</b>"
        return f"🟡 <code>{h(t)}</code> | {lbl} | <i>Ожидает</i>"

    entries = [(t, fmt_line(t, r)) for t, r in scheduled.items()]
    entries += [(t, f"⚫ <code>{h(t)}</code> | {h(lbl.ljust(15))} | <b>НЕ ОТПРАВЛЕНО</b> (бот был недоступен)")
                for t, lbl in unsent]
    details = "\n".join(line for _, line in sorted(entries)) if entries else "<i>Обходы за сегодня ещё не проводились.</i>"

    extra = ""
    if emergency:
        extra = (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🚨 <b>Внеплановые вызовы:</b>\n"
            + "\n".join(fmt_line(t, r) for t, r in sorted(emergency.items()))
            + "\n"
        )

    unsent_line = f"• Не отправлено:  <b>{len(unsent)}</b> ⚫\n" if unsent else ""
    late_line = f"• С опозданием:  <b>{late}</b> 🟠\n" if late else ""

    return (
        f"📊 <b>{title}</b>\n"
        f"📅 <b>Дата:</b> <code>{date_str}</code>\n"
        f"🕒 <b>Время выгрузки:</b> <code>{now.strftime('%H:%M:%S')}</code> ({h(tz_name)})\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👮‍♂️ <b>Дежурные:</b> {duty_str}\n\n"
        "📈 <b>Результаты:</b>\n"
        f"• Всего обходов: <b>{total}</b>\n"
        f"• Выполнено вовремя:  <b>{on_time}</b> ({percent}%) 🟢\n"
        f"{late_line}"
        f"• Пропущено:  <b>{missed}</b> 🔴\n"
        f"{unsent_line}"
        f"• В процессе:  <b>{pending}</b> 🟡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>Хронология:</b>\n"
        f"{details}\n"
        f"{extra}"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def send_daily_report(bot: Bot, is_final: bool = True):
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    date_str = get_today_str(tz)
    title = "ИТОГОВЫЙ ОТЧЁТ ЗА СМЕНУ" if is_final else "ПРОМЕЖУТОЧНЫЙ ОТЧЁТ (ОБЕД)"
    report_text = generate_report_text(date_str, title)

    chat_id = config.get("chat_id")
    admin_ids = config.get("admin_ids", [])

    # Промежуточный отчёт — только руководству в личку; в группу идёт лишь итоговый.
    # Отчёты несрочные, поэтому отправляются без звука.
    if chat_id and is_final:
        try:
            await tg_retry(bot.send_message, chat_id=chat_id, text=report_text,
                                   parse_mode=ParseMode.HTML, disable_notification=True)
            logger.info(f"Отчет отправлен в чат {chat_id}")
        except Exception as e:
            logger.error(f"Ошибка отправки отчета в группу: {e}")

    for a_id in admin_ids:
        try:
            await tg_retry(bot.send_message, chat_id=a_id, text=report_text,
                                   parse_mode=ParseMode.HTML, disable_notification=True)
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
        await group_reply(message, welcome, reply_markup=get_admin_bottom_keyboard(), parse_mode=ParseMode.HTML)
    else:
        group_welcome = (
            "👋 <b>Бот контроля обходов активен в группе!</b>\n\n"
            "📌 <b>Основные команды:</b>\n"
            "• <code>/duty</code> — посмотреть дежурных или заступить на смену\n"
            "• <code>/report</code> — отчёт по обходам за сегодня\n"
            "• <code>/status</code> — статус системы и точное время"
        )
        await group_reply(message, group_welcome, parse_mode=ParseMode.HTML)


# Обработка нажатий на постоянные кнопки внизу экрана
@dp.message(F.text == "🎛 Панель управления")
@dp.message(Command("admin"))
async def cmd_admin_open(message: Message, state: FSMContext):
    if message.chat.type != "private":
        await group_reply(message, "⚠️ Панель управления доступна только в личных сообщениях с ботом.")
        return

    if not is_admin(message.from_user.id):
        await group_reply(message, "⛔️ Доступ ограничен только для администраторов.")
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
    if await deny_if_not_allowed(message):
        return
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    date_str = get_today_str(tz)
    await group_reply(message, generate_report_text(date_str), parse_mode=ParseMode.HTML)


@dp.message(F.text == "👮‍♂️ Кто на смене")
@dp.message(Command("duty"))
async def cmd_duty_open(message: Message):
    if await deny_if_not_allowed(message):
        return
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    _, day_data = get_today_data(tz)
    duty_users = day_data.get("duty_users", [])

    if duty_users:
        duty_text = f"🟢 <b>На смене сейчас:</b> {h(' '.join(duty_users))}"
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
    await group_reply(message, card, reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)


@dp.message(F.text == "🔔 Тестовый обход")
@dp.message(Command("test"))
async def cmd_test_open(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        await group_reply(message, "⛔️ Тестовый обход доступен только администраторам.")
        return
    test_reminder = {"time": "ТЕСТ", "label": "пробный обход для проверки", "kind": "test"}
    await send_reminder(bot, test_reminder)
    schedule_group_cleanup(message)  # саму команду /test в группе тоже убираем


@dp.message(Command("setchat"))
async def cmd_setchat(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await group_reply(message, "⛔️ Привязать группу может только администратор бота.")
        return
    if message.chat.type == "private":
        await group_reply(message, "⚠️ Команду /setchat нужно отправить в рабочей группе, а не в личке.")
        return
    config = load_config()
    config["chat_id"] = message.chat.id
    save_config(config)

    chat_name = h(message.chat.title) if message.chat.title else "текущая группа"
    await group_reply(
        message,
        "🎉 <b>Группа успешно привязана к боту!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <b>Чат:</b> {chat_name}\n"
        f"🆔 <b>ID:</b> <code>{message.chat.id}</code>\n\n"
        "Все уведомления об обходах, тревоги и отчеты теперь будут направляться сюда.",
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if await deny_if_not_allowed(message):
        return
    config = load_config()
    tz_name = config.get("timezone", "Asia/Tashkent")
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    _, day_data = get_today_data(tz)

    chat_id = config.get("chat_id")
    chat_info = f"<code>{chat_id}</code>" if chat_id else "⚠️ <i>Не привязана (напишите /setchat в группе)</i>"
    mentions = h(" ".join(config.get("mentions", []))) if config.get("mentions") else "—"
    duty_users = h(" ".join(day_data.get("duty_users", []))) or "Все по общему списку"

    reminders = config.get("reminders", [])
    active_count = sum(1 for r in reminders if r.get("enabled", True))

    card = (
        "ℹ️ <b>ТЕКУЩИЙ СТАТУС СИСТЕМЫ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 <b>Текущее время:</b> <code>{now.strftime('%d.%m.%Y %H:%M:%S')}</code> ({h(tz_name)})\n"
        f"📍 <b>Привязанный чат:</b> {chat_info}\n"
        f"👮‍♂️ <b>Дежурные сегодня:</b> {duty_users}\n"
        f"👥 <b>Штат сотрудников:</b> {mentions}\n"
        f"⏰ <b>Активных обходов:</b> {active_count} из {len(reminders)}\n"
        f"⏱ <b>Время на реакцию:</b> {config.get('escalation_minutes', 15)} мин\n"
        f"🔔 <b>Напоминание заранее:</b> за {config.get('advance_minutes', 0)} мин\n"
        f"📅 <b>Рабочие дни:</b> {h(format_days(config.get('days', 'mon-sat')))}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await group_reply(message, card, parse_mode=ParseMode.HTML)


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
    mentions = h(" ".join(config.get("mentions", []))) or "не указаны"

    card = (
        "ℹ️ <b>СОСТОЯНИЕ СИСТЕМЫ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 <b>Текущее время:</b> <code>{now.strftime('%d.%m.%Y %H:%M:%S')}</code> ({h(tz_name)})\n"
        f"📍 <b>ID группы:</b> <code>{chat_id}</code>\n"
        f"📅 <b>Рабочие дни:</b> {h(format_days(config.get('days', 'mon-sat')))}\n"
        f"👥 <b>Сотрудников:</b> {mentions}\n"
        f"👑 <b>Администраторов:</b> {len(admins)}\n"
        f"⏰ <b>Обходов:</b> {active} активных (из {len(reminders)})\n"
        f"⏱ <b>Таймаут тревоги:</b> {config.get('escalation_minutes', 15)} мин\n"
        f"🔔 <b>Напоминание заранее:</b> за {config.get('advance_minutes', 0)} мин\n"
        f"📋 <b>Отчёты:</b> в {', '.join(get_report_times(config))}\n"
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
    key = callback.data[len("toggle_duty_"):]
    config = load_config()
    all_staff = config.get("mentions", [])
    target = find_staff(all_staff, key)

    if target:
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
        await callback.message.edit_reply_markup(reply_markup=get_duty_keyboard())
        await callback.answer("Этого сотрудника уже нет в списке — список обновлён.", show_alert=True)


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
            # Текст всплывашки ограничен 200 символами, поэтому подсказка короткая
            no_username_hint = (
                "\n\n⚠️ Нет @username — бот не сможет вас отметить. Задайте его в настройках Telegram."
            ) if not user.username else ""
            await callback.answer(f"✅ Вы на смене: {username_str[:40]}{no_username_hint}", show_alert=True)
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

    cur_text = h(" ".join(duty_users)) if duty_users else "не назначены"
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
    time_key = callback.data[len("toggle_rem_"):]
    config = load_config()
    reminders = config.get("reminders", [])
    reminder = find_reminder(reminders, time_key)

    if reminder:
        current = reminder.get("enabled", True)
        reminder["enabled"] = not current
        config["reminders"] = reminders
        save_config(config)

        if scheduler_instance:
            setup_jobs(scheduler_instance, bot)

        await callback.message.edit_reply_markup(reply_markup=get_schedule_keyboard())
        status_text = "ВКЛЮЧЕНО ✅" if not current else "ОТКЛЮЧЕНО ❌"
        await callback.answer(f"{time_key}: {status_text}")
    else:
        await callback.message.edit_reply_markup(reply_markup=get_schedule_keyboard())
        await callback.answer("Этого обхода уже нет в графике — список обновлён.", show_alert=True)


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
    if not message.text:
        await message.reply("⚠️ Отправьте время текстом в формате <b>ЧЧ:ММ</b>:")
        return
    time_str = message.text.strip()
    if ":" not in time_str or len(time_str.split(":")) != 2:
        await message.reply("⚠️ Неверный формат. Введите время в формате <b>ЧЧ:ММ</b> (например: <code>13:00</code>):")
        return

    try:
        # hh/mm, а не h/m: h — это функция экранирования HTML
        hh, mm = map(int, time_str.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        formatted_time = f"{hh:02d}:{mm:02d}"
    except ValueError:
        await message.reply("⚠️ Часы от 0 до 23, минуты от 0 до 59. Попробуйте еще раз:")
        return

    existing = {r.get("time") for r in load_config().get("reminders", [])}
    if formatted_time in existing:
        await message.reply(f"⚠️ Обход в <b>{formatted_time}</b> уже есть в графике. Введите другое время:")
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
    if not message.text or not message.text.strip():
        await message.reply("⚠️ Отправьте название обхода текстом:")
        return
    label = message.text.strip()[:64]
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
        f"🎉 Обход <b>{formatted_time}</b> ({h(label)}) успешно добавлен в расписание!",
        reply_markup=get_admin_inline_menu(),
        parse_mode=ParseMode.HTML
    )


async def render_del_reminder_menu(callback: CallbackQuery):
    config = load_config()
    reminders = config.get("reminders", [])
    b = InlineKeyboardBuilder()

    for r in reminders:
        b.button(text=f"🗑 {r.get('time')} - {r.get('label', '')[:10]}", callback_data=f"del_rem_{r.get('time')}")

    b.adjust(2)
    b.row(InlineKeyboardButton(text="◀️ Назад к графику", callback_data="admin_schedule"))

    await callback.message.edit_text(
        "🗑 <b>Выберите время для удаления:</b>",
        reply_markup=b.as_markup(),
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data == "del_reminder_menu")
async def cb_del_reminder_menu(callback: CallbackQuery):
    await render_del_reminder_menu(callback)
    await callback.answer()


@dp.callback_query(F.data.startswith("del_rem_"))
async def cb_del_reminder_confirm(callback: CallbackQuery, bot: Bot):
    global scheduler_instance
    time_key = callback.data[len("del_rem_"):]
    config = load_config()
    reminders = config.get("reminders", [])
    reminder = find_reminder(reminders, time_key)

    if reminder:
        reminders.remove(reminder)
        config["reminders"] = reminders
        save_config(config)

        if scheduler_instance:
            setup_jobs(scheduler_instance, bot)

        await render_del_reminder_menu(callback)
        await callback.answer(f"Удалено: {time_key}")
    else:
        await render_del_reminder_menu(callback)
        await callback.answer("Этого обхода уже нет в графике — список обновлён.", show_alert=True)


# ==============================================================================
# УПРАВЛЕНИЕ СОТРУДНИКАМИ
# ==============================================================================

@dp.callback_query(F.data == "admin_mentions")
async def cb_admin_mentions(callback: CallbackQuery):
    config = load_config()
    mentions = config.get("mentions", [])
    mentions_text = "\n".join([f"• <b>{h(m)}</b>" for m in mentions]) if mentions else "<i>Список пока пуст</i>"

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
    if not message.text or not message.text.split():
        await message.reply("⚠️ Отправьте юзернейм текстом, например <code>@username</code>:")
        return
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
        f"✅ Добавлены сотрудники:\n{h(' '.join(new_mentions))}",
        reply_markup=get_admin_inline_menu(),
        parse_mode=ParseMode.HTML
    )


async def render_del_mention_menu(callback: CallbackQuery):
    config = load_config()
    mentions = config.get("mentions", [])
    b = InlineKeyboardBuilder()

    for m in mentions:
        b.button(text=f"🗑 {m}", callback_data=f"del_mention_{staff_key(m)}")

    b.adjust(2)
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_mentions"))

    await callback.message.edit_text(
        "🗑 <b>Нажмите на сотрудника для удаления из штата:</b>",
        reply_markup=b.as_markup(),
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data == "del_mention_menu")
async def cb_del_mention_menu(callback: CallbackQuery):
    await render_del_mention_menu(callback)
    await callback.answer()


@dp.callback_query(F.data.startswith("del_mention_"))
async def cb_del_mention(callback: CallbackQuery):
    key = callback.data[len("del_mention_"):]
    config = load_config()
    mentions = config.get("mentions", [])
    target = find_staff(mentions, key)

    if target:
        mentions.remove(target)
        config["mentions"] = mentions
        save_config(config)
        await render_del_mention_menu(callback)
        await callback.answer(f"Удален: {target}")
    else:
        await render_del_mention_menu(callback)
        await callback.answer("Этого сотрудника уже нет в списке — список обновлён.", show_alert=True)


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
    # html_text сохраняет форматирование (жирный, ссылки) и экранирует спецсимволы
    if message.photo:
        photo_id = message.photo[-1].file_id
        caption = message.html_text if message.caption else ""
        await state.update_data(type="photo", photo_id=photo_id, text=caption)
    elif message.text:
        await state.update_data(type="text", text=message.html_text)
    else:
        await message.reply("⚠️ Поддерживается только текст или фото с подписью. Отправьте ещё раз:")
        return

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
    # Шапка отдельным сообщением: если приклеить её к тексту, длинное объявление
    # (подпись до 1024, текст до 4096 символов) не влезет в лимит Telegram
    await message.answer("👁 <b>ПРЕДПРОСМОТР ОБЪЯВЛЕНИЯ:</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━", parse_mode=ParseMode.HTML)

    if data.get("type") == "photo":
        await message.answer_photo(
            photo=data["photo_id"],
            caption=data.get("text") or None,
            reply_markup=b.as_markup(),
            parse_mode=ParseMode.HTML
        )
    else:
        await message.answer(
            data.get("text") or "",
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
            sent = await tg_retry(
            bot.send_photo,
                chat_id=chat_id,
                photo=data["photo_id"],
                caption=data.get("text", ""),
                parse_mode=ParseMode.HTML
            )
        else:
            sent = await tg_retry(
            bot.send_message,
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
    if not message.text or not message.text.strip():
        await message.reply("⚠️ Отправьте вопрос текстом:")
        return
    question = message.text.strip()
    if len(question) > 300:
        await message.reply(f"⚠️ Вопрос слишком длинный ({len(question)} симв.). В Telegram лимит 300 символов. Сократите:")
        return

    await state.update_data(question=question)
    await state.set_state(PollState.waiting_for_options)
    card = (
        f"❓ Вопрос: <b>{h(question)}</b>\n\n"
        "Шаг 2: Введите <b>варианты ответов</b> (каждый вариант с новой строки, от 2 до 10 вариантов):\n\n"
        "<i>Пример:</i>\n"
        "Да, готов\n"
        "Нужна замена\n"
        "Воздержусь"
    )
    await message.answer(card, reply_markup=get_cancel_keyboard(), parse_mode=ParseMode.HTML)


@dp.message(PollState.waiting_for_options)
async def process_poll_options(message: Message, state: FSMContext):
    if not message.text:
        await message.reply("⚠️ Отправьте варианты ответов текстом, каждый с новой строки:")
        return
    lines =[line.strip() for line in message.text.split("\n") if line.strip()]
    if len(lines) < 2:
        await message.reply("⚠️ Укажите как минимум 2 варианта ответа (каждый с новой строки):")
        return
    if len(lines) > 10:
        await message.reply("⚠️ Telegram поддерживает максимум 10 вариантов. Сократите список:")
        return

    long_options = [opt for opt in lines if len(opt) > 100]
    if long_options:
        await message.reply(f"⚠️ Вариант не должен превышать 100 символов:\n<i>{h(long_options[0][:40])}...</i>")
        return

    await state.update_data(options=lines)
    await state.set_state(PollState.confirm)

    data = await state.get_data()
    options_preview = "\n".join([f"• {h(opt)}" for opt in lines])

    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Опубликовать опрос", callback_data="send_poll_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")
    )

    card = (
        "👁 <b>ПРЕДПРОСМОТР ОПРОСА:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"❓ <b>Вопрос:</b> {h(data['question'])}\n\n"
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
        await tg_retry(
            bot.send_poll,
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
    reminder_info = {"label": "внеплановый срочный обход", "kind": "emergency"}
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

def restore_pending_escalations(scheduler: AsyncIOScheduler, bot: Bot):
    """После перезапуска заново ставит 15-минутные проверки для неподтверждённых обходов.

    Сегодняшние: если срок ещё не вышел — проверка в срок, если вышел — сразу.
    Обходы прошлых дней просто помечаются пропущенными, без рассылки тревог.
    """
    config = load_config()
    tz = pytz.timezone(config.get("timezone", "Asia/Tashkent"))
    timeout = config.get("escalation_minutes", 15)
    now = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")

    data = load_data()
    changed = False
    restored = 0
    for date_str, day_data in data.items():
        for time_str, info in day_data.get("rounds", {}).items():
            if info.get("status") != "pending":
                continue
            if date_str < today:
                info["status"] = "missed"
                changed = True
                continue
            if date_str > today:
                continue
            try:
                sent_at = tz.localize(datetime.strptime(f"{date_str} {info.get('sent_at')}", "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError):
                sent_at = now
            run_date = max(sent_at + timedelta(minutes=timeout), now + timedelta(seconds=5))
            scheduler.add_job(
                check_unconfirmed_round,
                trigger="date",
                run_date=run_date,
                args=[bot, date_str, time_str, info.get("label", "обход"), info.get("mentions", "")],
                id=f"escalation_{date_str}_{time_str}",
                replace_existing=True
            )
            restored += 1
    if changed:
        save_data(data)
    if restored:
        logger.info(f"Восстановлено проверок неподтверждённых обходов: {restored}")


def setup_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    # Пересоздаём только расписание и отчёт; задачи escalation_* (идущие
    # 15-минутные проверки) не трогаем, иначе тревоги терялись бы
    for job in scheduler.get_jobs():
        if job.id.startswith(("rem_job_", "daily_report_job")):
            scheduler.remove_job(job.id)
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

    report_times = get_report_times(config)
    for i, report_time in enumerate(report_times):
        is_final = i == len(report_times) - 1
        try:
            rh, rm = map(int, report_time.split(":"))
            scheduler.add_job(
                send_daily_report,
                trigger=CronTrigger(day_of_week=days, hour=rh, minute=rm, timezone=tz),
                args=[bot, is_final],
                name=f"DailyReport_{report_time}",
                id=f"daily_report_job_{report_time}",
                replace_existing=True
            )
        except Exception as e:
            logger.error(f"Ошибка планирования отчета {report_time}: {e}")

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
        BotCommand(command="status", description="ℹ️ Статус системы, время и график"),
        BotCommand(command="test", description="🔔 Проверить оповещение")
    ]
    try:
        await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
    except Exception as e:
        logger.warning(f"Не удалось зарегистрировать подсказки команд: {e}")


async def main():
    ensure_config_exists()
    logger.info(f"Папка данных: {DATA_DIR}")
    if not acquire_single_instance_lock():
        logger.error(
            "Бот уже запущен (возможно, из автозагрузки). Вторая копия не стартует, "
            "иначе напоминания и отчёты приходили бы дважды."
        )
        return
    config = load_config()
    if not config.get("admin_ids"):
        logger.warning(
            "Список admin_ids пуст: админ-панель никому не доступна. "
            "Впишите свой Telegram ID в admin_ids в config.json и перезапустите бота."
        )
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
    # misfire_grace_time: если запуск задержался (сон ПК, нагрузка), задача всё равно
    # выполнится в течение 5 минут, а не пропадёт молча (по умолчанию допуск 1 с)
    scheduler = AsyncIOScheduler(timezone=tz, job_defaults={"misfire_grace_time": 300, "coalesce": True})
    scheduler_instance = scheduler
    setup_jobs(scheduler, bot)
    restore_pending_escalations(scheduler, bot)
    scheduler.start()
    restore_pending_deletes(bot)

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
