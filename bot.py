import asyncio
import csv
import math
import os
import random
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# =========================
# НАСТРОЙКИ
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Админ только один - твой Telegram ID.
ADMIN_ID = 7787565361

DB_FILE = "elections.db"

PARTIES = [
    "Новый 57",
    "Единый 57",
    "Бебрики",
]

# Это стартовые значения. Население можно менять прямо из админки командой:
# /setpop НОМЕР_РЕГИОНА НОВОЕ_НАСЕЛЕНИЕ
REGIONS = [
    ("Малаховщина", 6000),
    ("Кошкинская", 4500),
    ("Околофутбольная", 7200),
    ("Багричевская", 5500),
    ("Фазанская", 6400),
    ("Победская", 9000),
    ("Мельниковская", 8200),
    ("57 Дом", 3800),
]

# Обычные выборы идут 3 дня.
PROD_DURATION_SECONDS = 3 * 24 * 60 * 60

# Тест полностью проигрывает выборы за 1 минуту.
TEST_DURATION_SECONDS = 60

# Насколько часто бот пробует досчитать новые виртуальные голоса.
SIMULATION_TICK_SECONDS = 1.0

# Как часто редактировать живое табло.
TEST_SCOREBOARD_UPDATE_SECONDS = 2.0
PROD_SCOREBOARD_UPDATE_SECONDS = 15.0

# Небольшое "сглаживание", чтобы при 1-2 реальных голосах результат
# не становился абсолютно 100/0/0.
VOTE_SMOOTHING = 0.35

# Случайный шум в каждой новой пачке.
BATCH_NOISE_MIN = 0.92
BATCH_NOISE_MAX = 1.08

# В тесте ограничиваем слишком огромные скачки на одном тике.
TEST_MAX_REGION_BATCH_PER_TICK = 140

# В обычном режиме ограничение почти никогда не потребуется,
# но защищает от гигантского скачка после долгого простоя процесса.
PROD_MAX_REGION_BATCH_PER_TICK = 600

# =========================
# БАЗА
# =========================

db = sqlite3.connect(DB_FILE)
db.row_factory = sqlite3.Row


def db_execute(sql, params=()):
    cur = db.execute(sql, params)
    db.commit()
    return cur


def db_query_one(sql, params=()):
    return db.execute(sql, params).fetchone()


def db_query_all(sql, params=()):
    return db.execute(sql, params).fetchall()


def init_db():
    db_execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS region_config (
            region TEXT PRIMARY KEY,
            population INTEGER NOT NULL,
            turnout_pct REAL NOT NULL
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS real_votes (
            user_id INTEGER PRIMARY KEY,
            region TEXT NOT NULL,
            party TEXT NOT NULL,
            voted_at REAL NOT NULL
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS virtual_votes (
            region TEXT NOT NULL,
            party TEXT NOT NULL,
            votes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (region, party)
        )
    """)

    for region, population in REGIONS:
        exists = db_query_one(
            "SELECT region FROM region_config WHERE region = ?",
            (region,),
        )
        if not exists:
            turnout = round(random.uniform(52.0, 72.0), 1)
            db_execute(
                "INSERT INTO region_config(region, population, turnout_pct) VALUES (?, ?, ?)",
                (region, population, turnout),
            )

        for party in PARTIES:
            db_execute(
                """
                INSERT OR IGNORE INTO virtual_votes(region, party, votes)
                VALUES (?, ?, 0)
                """,
                (region, party),
            )

    defaults = {
        "state": "idle",
        "mode": "",
        "started_at": "0",
        "ends_at": "0",
        "paused_at": "0",
        "scoreboard_chat_id": "0",
        "scoreboard_message_id": "0",
    }
    for key, value in defaults.items():
        db_execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, value),
        )


def get_setting(key, default=""):
    row = db_query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    db_execute(
        """
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def election_state():
    return get_setting("state", "idle")


def election_mode():
    return get_setting("mode", "")


def is_running():
    return election_state() == "running"


def reset_votes():
    db_execute("DELETE FROM real_votes")
    db_execute("UPDATE virtual_votes SET votes = 0")


def randomize_turnout():
    rows = db_query_all("SELECT region FROM region_config ORDER BY rowid")
    for row in rows:
        turnout = round(random.uniform(52.0, 72.0), 1)
        db_execute(
            "UPDATE region_config SET turnout_pct = ? WHERE region = ?",
            (turnout, row["region"]),
        )


def get_regions():
    return db_query_all(
        "SELECT region, population, turnout_pct FROM region_config ORDER BY rowid"
    )


def region_name_by_index(index):
    rows = get_regions()
    if 0 <= index < len(rows):
        return rows[index]["region"]
    return None


def party_name_by_index(index):
    if 0 <= index < len(PARTIES):
        return PARTIES[index]
    return None


def real_vote_counts(region=None):
    if region:
        rows = db_query_all(
            """
            SELECT party, COUNT(*) AS c
            FROM real_votes
            WHERE region = ?
            GROUP BY party
            """,
            (region,),
        )
    else:
        rows = db_query_all(
            """
            SELECT party, COUNT(*) AS c
            FROM real_votes
            GROUP BY party
            """
        )

    result = {party: 0 for party in PARTIES}
    for row in rows:
        if row["party"] in result:
            result[row["party"]] = int(row["c"])
    return result


def virtual_vote_counts(region=None):
    if region:
        rows = db_query_all(
            "SELECT party, votes FROM virtual_votes WHERE region = ?",
            (region,),
        )
    else:
        rows = db_query_all(
            "SELECT party, SUM(votes) AS votes FROM virtual_votes GROUP BY party"
        )

    result = {party: 0 for party in PARTIES}
    for row in rows:
        if row["party"] in result:
            result[row["party"]] = int(row["votes"] or 0)
    return result


def total_virtual_for_region(region):
    row = db_query_one(
        "SELECT SUM(votes) AS s FROM virtual_votes WHERE region = ?",
        (region,),
    )
    return int(row["s"] or 0)


def total_real_voters():
    row = db_query_one("SELECT COUNT(*) AS c FROM real_votes")
    return int(row["c"] or 0)


def total_population():
    row = db_query_one("SELECT SUM(population) AS s FROM region_config")
    return int(row["s"] or 0)


def total_virtual_votes():
    row = db_query_one("SELECT SUM(votes) AS s FROM virtual_votes")
    return int(row["s"] or 0)


# =========================
# СИМУЛЯЦИЯ
# =========================

def smoothstep(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def cumulative_progress_share(progress):
    """
    Три "волны" активности:
    День 1 - около 28% всех бюллетеней.
    День 2 - еще около 36%.
    День 3 - оставшиеся около 36%.

    В тесте эти три дня просто сжимаются в 60 секунд.
    """
    progress = max(0.0, min(1.0, progress))

    if progress < 1 / 3:
        local = progress * 3
        return 0.28 * smoothstep(local)

    if progress < 2 / 3:
        local = (progress - 1 / 3) * 3
        return 0.28 + 0.36 * smoothstep(local)

    local = (progress - 2 / 3) * 3
    return 0.64 + 0.36 * smoothstep(local)


def current_progress():
    started = float(get_setting("started_at", "0") or 0)
    ends = float(get_setting("ends_at", "0") or 0)

    if started <= 0 or ends <= started:
        return 0.0

    now = time.time()
    return max(0.0, min(1.0, (now - started) / (ends - started)))


def party_weights_for_region(region):
    counts = real_vote_counts(region)
    total_real = sum(counts.values())

    # Пока в регионе нет ни одного реального человека,
    # партии стартуют с одинаковой вероятностью.
    if total_real == 0:
        return [1.0, 1.0, 1.0]

    weights = []
    for party in PARTIES:
        base = counts[party] + VOTE_SMOOTHING
        noise = random.uniform(BATCH_NOISE_MIN, BATCH_NOISE_MAX)
        weights.append(max(0.001, base * noise))
    return weights


def allocate_batch(region, amount):
    if amount <= 0:
        return {party: 0 for party in PARTIES}

    weights = party_weights_for_region(region)
    picks = random.choices(range(len(PARTIES)), weights=weights, k=amount)
    counter = Counter(picks)

    added = {}
    for idx, party in enumerate(PARTIES):
        value = int(counter.get(idx, 0))
        added[party] = value
        if value:
            db_execute(
                """
                UPDATE virtual_votes
                SET votes = votes + ?
                WHERE region = ? AND party = ?
                """,
                (value, region, party),
            )
    return added


def target_votes_for_region(population, turnout_pct):
    return int(round(population * (turnout_pct / 100.0)))


def simulate_one_tick(force_finish=False):
    if not is_running() and not force_finish:
        return False

    progress = 1.0 if force_finish else current_progress()
    cumulative_share = cumulative_progress_share(progress)
    mode = election_mode()

    max_batch = (
        TEST_MAX_REGION_BATCH_PER_TICK
        if mode == "test"
        else PROD_MAX_REGION_BATCH_PER_TICK
    )

    changed = False

    for row in get_regions():
        region = row["region"]
        population = int(row["population"])
        turnout_pct = float(row["turnout_pct"])

        target_total = target_votes_for_region(population, turnout_pct)
        expected_now = int(round(target_total * cumulative_share))
        current_total = total_virtual_for_region(region)
        delta = max(0, expected_now - current_total)

        if not force_finish:
            delta = min(delta, max_batch)

        if delta > 0:
            allocate_batch(region, delta)
            changed = True

    return changed


def finalize_election():
    # Досчитываем остаток до заранее заданной явки.
    for row in get_regions():
        region = row["region"]
        target_total = target_votes_for_region(
            int(row["population"]),
            float(row["turnout_pct"]),
        )
        current_total = total_virtual_for_region(region)
        delta = max(0, target_total - current_total)
        if delta:
            allocate_batch(region, delta)

    set_setting("state", "finished")


def start_new_election(mode):
    reset_votes()

    now = time.time()
    if mode == "test":
        duration = TEST_DURATION_SECONDS
    else:
        duration = PROD_DURATION_SECONDS

    set_setting("mode", mode)
    set_setting("started_at", now)
    set_setting("ends_at", now + duration)
    set_setting("paused_at", "0")
    set_setting("state", "running")


def pause_election():
    if is_running():
        set_setting("paused_at", time.time())
        set_setting("state", "paused")


def resume_election():
    if election_state() != "paused":
        return

    paused_at = float(get_setting("paused_at", "0") or 0)
    if paused_at > 0:
        paused_for = max(0.0, time.time() - paused_at)
        ends = float(get_setting("ends_at", "0") or 0)
        set_setting("ends_at", ends + paused_for)

    set_setting("paused_at", "0")
    set_setting("state", "running")


def stop_election_without_fill():
    if election_state() in {"running", "paused"}:
        set_setting("state", "finished")


# =========================
# ТЕКСТ / КНОПКИ
# =========================

def fmt_int(value):
    return f"{int(value):,}".replace(",", " ")


def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)

    if days:
        return f"{days}д {hours}ч {minutes}м"
    if hours:
        return f"{hours}ч {minutes}м {sec}с"
    if minutes:
        return f"{minutes}м {sec}с"
    return f"{sec}с"


def state_title():
    state = election_state()
    mode = election_mode()

    if state == "running":
        return "ТЕСТ 1 МИНУТА" if mode == "test" else "ВЫБОРЫ ИДУТ"
    if state == "paused":
        return "ПАУЗА"
    if state == "finished":
        return "ВЫБОРЫ ЗАВЕРШЕНЫ"
    return "ВЫБОРЫ НЕ ЗАПУЩЕНЫ"


def build_scoreboard(admin=False):
    state = election_state()
    now = time.time()
    ends = float(get_setting("ends_at", "0") or 0)

    total_pop = total_population()
    counted = total_virtual_votes()
    turnout_now = (counted / total_pop * 100.0) if total_pop else 0.0

    global_votes = virtual_vote_counts()
    global_total = sum(global_votes.values())

    lines = [
        f"{state_title()}",
        "",
        f"Население - {fmt_int(total_pop)}",
        f"Проголосовало - {fmt_int(counted)}",
        f"Явка сейчас - {turnout_now:.2f}%",
        f"Реальных игроков проголосовало - {total_real_voters()}",
    ]

    if state == "running":
        lines.append(f"До конца - {format_duration(ends - now)}")

    lines += ["", "ОБЩИЙ РЕЗУЛЬТАТ"]

    for party in PARTIES:
        votes = global_votes[party]
        pct = (votes / global_total * 100.0) if global_total else 0.0
        lines.append(f"{party} - {fmt_int(votes)} ({pct:.2f}%)")

    lines += ["", "ПО РЕГИОНАМ"]

    for row in get_regions():
        region = row["region"]
        population = int(row["population"])
        region_votes = virtual_vote_counts(region)
        region_total = sum(region_votes.values())
        region_turnout = (region_total / population * 100.0) if population else 0.0

        shares = []
        for party in PARTIES:
            value = region_votes[party]
            pct = (value / region_total * 100.0) if region_total else 0.0
            short = {
                "Новый 57": "Н57",
                "Единый 57": "Е57",
                "Бебрики": "Б",
            }[party]
            shares.append(f"{short} {pct:.1f}%")

        lines.append(
            f"{region} - {fmt_int(region_total)}/{fmt_int(population)} "
            f"({region_turnout:.1f}%) | " + " | ".join(shares)
        )

        if admin:
            target = float(row["turnout_pct"])
            real = real_vote_counts(region)
            lines.append(
                f"  скрытая итоговая явка - {target:.1f}% | "
                f"реальные: Н57 {real['Новый 57']}, "
                f"Е57 {real['Единый 57']}, Б {real['Бебрики']}"
            )

    if election_mode() == "test" and state in {"running", "paused"}:
        lines += [
            "",
            "Тестовый режим - 3 дня выборов сжаты в 60 секунд.",
        ]

    return "\n".join(lines)


def public_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Голосовать", callback_data="pub:vote"),
                InlineKeyboardButton(text="Результаты", callback_data="pub:results"),
            ],
            [
                InlineKeyboardButton(text="Мой голос", callback_data="pub:myvote"),
                InlineKeyboardButton(text="Как работает", callback_data="pub:info"),
            ],
        ]
    )


def regions_keyboard():
    rows = []
    regions = get_regions()
    for i, row in enumerate(regions):
        rows.append(
            [InlineKeyboardButton(
                text=row["region"],
                callback_data=f"vote:region:{i}",
            )]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="pub:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parties_keyboard(region_index):
    rows = []
    for i, party in enumerate(PARTIES):
        rows.append(
            [InlineKeyboardButton(
                text=party,
                callback_data=f"vote:party:{region_index}:{i}",
            )]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="pub:vote")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(region_index, party_index):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить",
                    callback_data=f"vote:confirm:{region_index}:{party_index}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Изменить",
                    callback_data=f"vote:region:{region_index}",
                )
            ],
        ]
    )


def admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Тест 1 мин", callback_data="adm:test"),
                InlineKeyboardButton(text="Запуск 3 дня", callback_data="adm:prod"),
            ],
            [
                InlineKeyboardButton(text="Пауза", callback_data="adm:pause"),
                InlineKeyboardButton(text="Продолжить", callback_data="adm:resume"),
            ],
            [
                InlineKeyboardButton(text="Обновить табло", callback_data="adm:update"),
                InlineKeyboardButton(text="Новое табло", callback_data="adm:publish"),
            ],
            [
                InlineKeyboardButton(text="Админ-статус", callback_data="adm:status"),
                InlineKeyboardButton(text="Рандом явки", callback_data="adm:turnout"),
            ],
            [
                InlineKeyboardButton(text="Экспорт CSV", callback_data="adm:export"),
                InlineKeyboardButton(text="Команды", callback_data="adm:help"),
            ],
            [
                InlineKeyboardButton(text="Завершить с досчетом", callback_data="adm:finish"),
                InlineKeyboardButton(text="Сбросить все", callback_data="adm:reset"),
            ],
        ]
    )


def yes_no_keyboard(yes_callback):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data=yes_callback),
                InlineKeyboardButton(text="Нет", callback_data="adm:cancel"),
            ]
        ]
    )


def is_admin(user_id):
    return user_id == ADMIN_ID


def admin_help_text():
    regions = get_regions()
    region_lines = [
        f"{i + 1}. {row['region']} - население {fmt_int(row['population'])}"
        for i, row in enumerate(regions)
    ]

    return (
        "АДМИН-КОМАНДЫ\n\n"
        "/admin - открыть панель\n"
        "/setpop НОМЕР НАСЕЛЕНИЕ - изменить население региона\n"
        "/setturnout НОМЕР ПРОЦЕНТ - задать итоговую явку региона\n"
        "/regions - список регионов и их номеров\n"
        "/adminstatus - подробная статистика, включая скрытую итоговую явку\n\n"
        "Примеры:\n"
        "/setpop 3 15000\n"
        "/setturnout 3 64.5\n\n"
        "Регионы:\n"
        + "\n".join(region_lines)
        + "\n\n"
        "Важно: админка не умеет тайно добавлять голоса конкретной партии. "
        "Можно менять только параметры симуляции - население, явку, запуск, паузу и сброс."
    )


# =========================
# TELEGRAM
# =========================

dp = Dispatcher()
bot = None
last_scoreboard_update = 0.0


async def safe_edit(message, text, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def publish_new_scoreboard(chat_id):
    msg = await bot.send_message(chat_id, build_scoreboard())
    set_setting("scoreboard_chat_id", msg.chat.id)
    set_setting("scoreboard_message_id", msg.message_id)
    return msg


async def update_scoreboard(force=False):
    global last_scoreboard_update

    chat_id = int(get_setting("scoreboard_chat_id", "0") or 0)
    message_id = int(get_setting("scoreboard_message_id", "0") or 0)

    if not chat_id or not message_id:
        return False

    mode = election_mode()
    min_interval = (
        TEST_SCOREBOARD_UPDATE_SECONDS
        if mode == "test"
        else PROD_SCOREBOARD_UPDATE_SECONDS
    )

    now = time.time()
    if not force and now - last_scoreboard_update < min_interval:
        return False

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=build_scoreboard(),
        )
        last_scoreboard_update = now
        return True
    except TelegramBadRequest as e:
        # Если текст не изменился - это нормально.
        if "message is not modified" in str(e).lower():
            last_scoreboard_update = now
            return False

        # Если сообщение удалили, просто отвязываем табло.
        text = str(e).lower()
        if "message to edit not found" in text or "message can't be edited" in text:
            set_setting("scoreboard_chat_id", "0")
            set_setting("scoreboard_message_id", "0")
            return False
        raise
    except TelegramForbiddenError:
        set_setting("scoreboard_chat_id", "0")
        set_setting("scoreboard_message_id", "0")
        return False


async def simulation_loop():
    while True:
        try:
            if is_running():
                ends_at = float(get_setting("ends_at", "0") or 0)

                if ends_at > 0 and time.time() >= ends_at:
                    finalize_election()
                    await update_scoreboard(force=True)

                    chat_id = int(get_setting("scoreboard_chat_id", "0") or 0)
                    if chat_id:
                        await bot.send_message(
                            chat_id,
                            "Голосование завершено. Итоговые результаты зафиксированы.",
                        )
                else:
                    changed = simulate_one_tick()
                    if changed:
                        await update_scoreboard(force=False)

            await asyncio.sleep(SIMULATION_TICK_SECONDS)

        except Exception as e:
            # Не роняем весь бот из-за одного неудачного обновления.
            print("simulation_loop error:", repr(e))
            await asyncio.sleep(2)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "Выборы Республики Победа\n\n"
        "Один Telegram-аккаунт - один реальный голос.\n"
        "Реальные голоса задают текущую поддержку партий, а бот постепенно "
        "генерирует поток бюллетеней с учетом населения и явки каждого региона.\n\n"
        "Выбирай действие:"
    )
    if is_admin(message.from_user.id):
        text += "\n\nАдмин-панель - /admin"

    await message.answer(text, reply_markup=public_menu())


@dp.message(Command("results"))
async def cmd_results(message: Message):
    await message.answer(build_scoreboard())


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Команда недоступна.")
        return

    await message.answer(
        "Админ-панель\n\n"
        "Все кнопки ниже работают только для твоего Telegram ID.",
        reply_markup=admin_menu(),
    )


@dp.message(Command("adminstatus"))
async def cmd_adminstatus(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Команда недоступна.")
        return
    await message.answer(build_scoreboard(admin=True))


@dp.message(Command("regions"))
async def cmd_regions(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Команда недоступна.")
        return
    await message.answer(admin_help_text())


@dp.message(Command("setpop"))
async def cmd_setpop(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Команда недоступна.")
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Формат: /setpop НОМЕР НАСЕЛЕНИЕ\nНапример: /setpop 3 15000")
        return

    try:
        idx = int(parts[1]) - 1
        population = int(parts[2])
    except ValueError:
        await message.answer("Номер региона и население должны быть числами.")
        return

    regions = get_regions()
    if not 0 <= idx < len(regions):
        await message.answer("Нет такого номера региона. Посмотри /regions")
        return

    if population < 10 or population > 100_000_000:
        await message.answer("Население должно быть от 10 до 100000000.")
        return

    region = regions[idx]["region"]
    db_execute(
        "UPDATE region_config SET population = ? WHERE region = ?",
        (population, region),
    )
    await message.answer(f"{region} - население теперь {fmt_int(population)}.")
    await update_scoreboard(force=True)


@dp.message(Command("setturnout"))
async def cmd_setturnout(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Команда недоступна.")
        return

    parts = message.text.replace(",", ".").split()
    if len(parts) != 3:
        await message.answer("Формат: /setturnout НОМЕР ПРОЦЕНТ\nНапример: /setturnout 3 64.5")
        return

    try:
        idx = int(parts[1]) - 1
        turnout = float(parts[2])
    except ValueError:
        await message.answer("Номер региона и процент должны быть числами.")
        return

    regions = get_regions()
    if not 0 <= idx < len(regions):
        await message.answer("Нет такого номера региона. Посмотри /regions")
        return

    if turnout < 1 or turnout > 95:
        await message.answer("Явка должна быть от 1% до 95%.")
        return

    region = regions[idx]["region"]
    db_execute(
        "UPDATE region_config SET turnout_pct = ? WHERE region = ?",
        (turnout, region),
    )
    await message.answer(f"{region} - итоговая явка выставлена на {turnout:.1f}%.")
    await update_scoreboard(force=True)


@dp.callback_query(F.data == "pub:home")
async def pub_home(callback: CallbackQuery):
    await safe_edit(
        callback.message,
        "Выборы Республики Победа\n\nВыбирай действие:",
        reply_markup=public_menu(),
    )
    await callback.answer()


@dp.callback_query(F.data == "pub:results")
async def pub_results(callback: CallbackQuery):
    await safe_edit(
        callback.message,
        build_scoreboard(),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Обновить", callback_data="pub:results")],
                [InlineKeyboardButton(text="Назад", callback_data="pub:home")],
            ]
        ),
    )
    await callback.answer()


@dp.callback_query(F.data == "pub:info")
async def pub_info(callback: CallbackQuery):
    text = (
        "Как работает симуляция\n\n"
        "1. Каждый реальный человек выбирает свой регион и одну партию.\n"
        "2. Один Telegram ID может проголосовать только один раз за текущие выборы.\n"
        "3. У каждого региона есть свое население и итоговая явка ниже 100%.\n"
        "4. Бюллетени появляются постепенно, а не сразу.\n"
        "5. Новые пачки распределяются с учетом реальных голосов в конкретном регионе.\n"
        "6. Если реальные голоса меняют расклад, уже посчитанные бюллетени не переписываются. "
        "Меняются только следующие пачки.\n"
        "7. Есть небольшой случайный шум, поэтому каждая пачка не повторяет проценты идеально.\n\n"
        "Обычный режим идет 3 дня. Тестовый режим у админа сжимает эти 3 дня в 60 секунд."
    )
    await safe_edit(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Назад", callback_data="pub:home")]
            ]
        ),
    )
    await callback.answer()


@dp.callback_query(F.data == "pub:myvote")
async def pub_myvote(callback: CallbackQuery):
    row = db_query_one(
        "SELECT region, party FROM real_votes WHERE user_id = ?",
        (callback.from_user.id,),
    )

    if row:
        text = f"Твой голос принят.\nРегион - {row['region']}\nПартия - {row['party']}"
    else:
        text = "Ты еще не голосовал в текущих выборах."

    await safe_edit(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Назад", callback_data="pub:home")]
            ]
        ),
    )
    await callback.answer()


@dp.callback_query(F.data == "pub:vote")
async def pub_vote(callback: CallbackQuery):
    if not is_running():
        await callback.answer("Сейчас голосование не идет.", show_alert=True)
        return

    existing = db_query_one(
        "SELECT region, party FROM real_votes WHERE user_id = ?",
        (callback.from_user.id,),
    )
    if existing:
        await callback.answer(
            f"Ты уже голосовал: {existing['party']} - {existing['region']}",
            show_alert=True,
        )
        return

    await safe_edit(
        callback.message,
        "Выбери свой регион:",
        reply_markup=regions_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("vote:region:"))
async def vote_region(callback: CallbackQuery):
    if not is_running():
        await callback.answer("Голосование уже закрыто.", show_alert=True)
        return

    try:
        region_index = int(callback.data.split(":")[2])
    except Exception:
        await callback.answer("Ошибка региона.", show_alert=True)
        return

    region = region_name_by_index(region_index)
    if not region:
        await callback.answer("Регион не найден.", show_alert=True)
        return

    await safe_edit(
        callback.message,
        f"Регион - {region}\n\nТеперь выбери партию:",
        reply_markup=parties_keyboard(region_index),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("vote:party:"))
async def vote_party(callback: CallbackQuery):
    if not is_running():
        await callback.answer("Голосование уже закрыто.", show_alert=True)
        return

    try:
        _, _, region_index, party_index = callback.data.split(":")
        region_index = int(region_index)
        party_index = int(party_index)
    except Exception:
        await callback.answer("Ошибка выбора.", show_alert=True)
        return

    region = region_name_by_index(region_index)
    party = party_name_by_index(party_index)
    if not region or not party:
        await callback.answer("Ошибка выбора.", show_alert=True)
        return

    await safe_edit(
        callback.message,
        f"Подтвердить голос?\n\nРегион - {region}\nПартия - {party}",
        reply_markup=confirm_keyboard(region_index, party_index),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("vote:confirm:"))
async def vote_confirm(callback: CallbackQuery):
    if not is_running():
        await callback.answer("Голосование уже закрыто.", show_alert=True)
        return

    try:
        _, _, region_index, party_index = callback.data.split(":")
        region_index = int(region_index)
        party_index = int(party_index)
    except Exception:
        await callback.answer("Ошибка выбора.", show_alert=True)
        return

    region = region_name_by_index(region_index)
    party = party_name_by_index(party_index)
    if not region or not party:
        await callback.answer("Ошибка выбора.", show_alert=True)
        return

    existing = db_query_one(
        "SELECT region, party FROM real_votes WHERE user_id = ?",
        (callback.from_user.id,),
    )
    if existing:
        await callback.answer("Твой голос уже был принят.", show_alert=True)
        return

    try:
        db_execute(
            """
            INSERT INTO real_votes(user_id, region, party, voted_at)
            VALUES (?, ?, ?, ?)
            """,
            (callback.from_user.id, region, party, time.time()),
        )
    except sqlite3.IntegrityError:
        await callback.answer("Твой голос уже был принят.", show_alert=True)
        return

    await safe_edit(
        callback.message,
        f"Голос принят.\n\nРегион - {region}\nПартия - {party}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Результаты", callback_data="pub:results")],
                [InlineKeyboardButton(text="В меню", callback_data="pub:home")],
            ]
        ),
    )
    await callback.answer("Голос принят.")
    await update_scoreboard(force=True)


async def admin_guard(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return False
    return True


@dp.callback_query(F.data == "adm:test")
async def adm_test(callback: CallbackQuery):
    if not await admin_guard(callback):
        return
    await callback.message.answer(
        "Запустить тест на 1 минуту?\n\n"
        "Текущие реальные и виртуальные голоса будут очищены.",
        reply_markup=yes_no_keyboard("adm:test_yes"),
    )
    await callback.answer()


@dp.callback_query(F.data == "adm:test_yes")
async def adm_test_yes(callback: CallbackQuery):
    if not await admin_guard(callback):
        return

    start_new_election("test")
    await callback.message.answer(
        "Тест запущен на 60 секунд.\n"
        "Три дня выборов сейчас будут проиграны в ускоренном режиме."
    )
    await publish_new_scoreboard(callback.message.chat.id)
    await callback.answer("Тест запущен.")


@dp.callback_query(F.data == "adm:prod")
async def adm_prod(callback: CallbackQuery):
    if not await admin_guard(callback):
        return
    await callback.message.answer(
        "Запустить настоящую трехдневную симуляцию?\n\n"
        "Текущие реальные и виртуальные голоса будут очищены.",
        reply_markup=yes_no_keyboard("adm:prod_yes"),
    )
    await callback.answer()


@dp.callback_query(F.data == "adm:prod_yes")
async def adm_prod_yes(callback: CallbackQuery):
    if not await admin_guard(callback):
        return

    start_new_election("prod")
    await callback.message.answer("Выборы запущены на 3 дня.")
    await publish_new_scoreboard(callback.message.chat.id)
    await callback.answer("Выборы запущены.")


@dp.callback_query(F.data == "adm:pause")
async def adm_pause(callback: CallbackQuery):
    if not await admin_guard(callback):
        return

    if not is_running():
        await callback.answer("Сейчас нечего ставить на паузу.", show_alert=True)
        return

    pause_election()
    await update_scoreboard(force=True)
    await callback.answer("Пауза включена.")


@dp.callback_query(F.data == "adm:resume")
async def adm_resume(callback: CallbackQuery):
    if not await admin_guard(callback):
        return

    if election_state() != "paused":
        await callback.answer("Выборы не на паузе.", show_alert=True)
        return

    resume_election()
    await update_scoreboard(force=True)
    await callback.answer("Выборы продолжены.")


@dp.callback_query(F.data == "adm:update")
async def adm_update(callback: CallbackQuery):
    if not await admin_guard(callback):
        return
    ok = await update_scoreboard(force=True)
    await callback.answer("Табло обновлено." if ok else "Табло уже актуально.")


@dp.callback_query(F.data == "adm:publish")
async def adm_publish(callback: CallbackQuery):
    if not await admin_guard(callback):
        return
    await publish_new_scoreboard(callback.message.chat.id)
    await callback.answer("Новое живое табло создано.")


@dp.callback_query(F.data == "adm:status")
async def adm_status(callback: CallbackQuery):
    if not await admin_guard(callback):
        return
    await callback.message.answer(build_scoreboard(admin=True))
    await callback.answer()


@dp.callback_query(F.data == "adm:turnout")
async def adm_turnout(callback: CallbackQuery):
    if not await admin_guard(callback):
        return

    randomize_turnout()
    await callback.message.answer(
        "Итоговая явка для всех регионов заново сгенерирована в диапазоне 52-72%.\n"
        "Посмотреть скрытые значения - /adminstatus"
    )
    await update_scoreboard(force=True)
    await callback.answer("Явка обновлена.")


@dp.callback_query(F.data == "adm:help")
async def adm_help(callback: CallbackQuery):
    if not await admin_guard(callback):
        return
    await callback.message.answer(admin_help_text())
    await callback.answer()


@dp.callback_query(F.data == "adm:finish")
async def adm_finish(callback: CallbackQuery):
    if not await admin_guard(callback):
        return

    if election_state() not in {"running", "paused"}:
        await callback.answer("Активных выборов нет.", show_alert=True)
        return

    await callback.message.answer(
        "Завершить прямо сейчас и мгновенно досчитать остаток до итоговой явки?",
        reply_markup=yes_no_keyboard("adm:finish_yes"),
    )
    await callback.answer()


@dp.callback_query(F.data == "adm:finish_yes")
async def adm_finish_yes(callback: CallbackQuery):
    if not await admin_guard(callback):
        return

    finalize_election()
    await update_scoreboard(force=True)
    await callback.message.answer("Выборы завершены, остаток досчитан.")
    await callback.answer()


@dp.callback_query(F.data == "adm:reset")
async def adm_reset(callback: CallbackQuery):
    if not await admin_guard(callback):
        return

    await callback.message.answer(
        "Сбросить все реальные и виртуальные голоса и остановить выборы?",
        reply_markup=yes_no_keyboard("adm:reset_yes"),
    )
    await callback.answer()


@dp.callback_query(F.data == "adm:reset_yes")
async def adm_reset_yes(callback: CallbackQuery):
    if not await admin_guard(callback):
        return

    reset_votes()
    set_setting("state", "idle")
    set_setting("mode", "")
    set_setting("started_at", "0")
    set_setting("ends_at", "0")
    set_setting("paused_at", "0")
    await update_scoreboard(force=True)
    await callback.message.answer("Все голоса сброшены. Выборы остановлены.")
    await callback.answer()


@dp.callback_query(F.data == "adm:cancel")
async def adm_cancel(callback: CallbackQuery):
    if not await admin_guard(callback):
        return
    await callback.answer("Отменено.")


def export_csv_file():
    path = Path("election_export.csv")

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")

        writer.writerow(["Сводка"])
        writer.writerow(["Состояние", election_state()])
        writer.writerow(["Режим", election_mode()])
        writer.writerow(["Реальных игроков", total_real_voters()])
        writer.writerow(["Виртуальных бюллетеней", total_virtual_votes()])
        writer.writerow([])

        writer.writerow(["Регионы"])
        writer.writerow([
            "Регион",
            "Население",
            "Итоговая явка %",
            "Новый 57",
            "Единый 57",
            "Бебрики",
            "Реальных голосов",
        ])

        for row in get_regions():
            region = row["region"]
            virtual = virtual_vote_counts(region)
            real = real_vote_counts(region)
            writer.writerow([
                region,
                row["population"],
                row["turnout_pct"],
                virtual["Новый 57"],
                virtual["Единый 57"],
                virtual["Бебрики"],
                sum(real.values()),
            ])

        writer.writerow([])
        writer.writerow(["Реальные голоса по регионам"])
        writer.writerow(["Регион", "Новый 57", "Единый 57", "Бебрики"])

        for row in get_regions():
            region = row["region"]
            real = real_vote_counts(region)
            writer.writerow([
                region,
                real["Новый 57"],
                real["Единый 57"],
                real["Бебрики"],
            ])

    return path


@dp.callback_query(F.data == "adm:export")
async def adm_export(callback: CallbackQuery):
    if not await admin_guard(callback):
        return

    path = export_csv_file()
    await callback.message.answer_document(
        FSInputFile(path),
        caption="Экспорт текущих выборов.",
    )
    await callback.answer("CSV готов.")


async def main():
    global bot

    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден BOT_TOKEN. "
            "Задай переменную окружения BOT_TOKEN с токеном от BotFather."
        )

    init_db()

    bot = Bot(BOT_TOKEN)

    # Если процесс перезапустился во время активных выборов,
    # симуляция продолжится из сохраненного состояния SQLite.
    asyncio.create_task(simulation_loop())

    print("Bot started.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
