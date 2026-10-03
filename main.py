"""
FastAPI backend для Telegram Mini App "Мой планировщик"
"""

import os
import json
import base64
import httpx
import asyncio
import hashlib
import hmac
import html
import time as unix_time
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://planner-frontend-sable.vercel.app")
ALLOW_INSECURE_DEMO = os.environ.get("ALLOW_INSECURE_DEMO", "false").lower() == "true"
APP_ENV = os.environ.get("APP_ENV", "production").lower()
ALLOWED_ORIGINS = [origin.strip() for origin in os.environ.get(
    "ALLOWED_ORIGINS",
    "https://planner-frontend-sable.vercel.app,https://jackoli-09.github.io,http://127.0.0.1:8134,http://localhost:8134"
).split(",") if origin.strip()]
TZ = ZoneInfo("Europe/Moscow")
IS_SERVERLESS = bool(os.environ.get("VERCEL"))
# Сессии подписываются отдельным ключом, а не самим BOT_TOKEN.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")


def session_signing_key() -> bytes:
    if SESSION_SECRET:
        return SESSION_SECRET.encode()
    return hmac.new(BOT_TOKEN.encode(), b"planner-session-v2", hashlib.sha256).digest()
CRON_SECRET = os.environ.get("CRON_SECRET", "")
# Владелец продукта: получает отзывы и видит статистику. Telegram user id.
OWNER_USER_ID = int(os.environ.get("OWNER_USER_ID", "0") or 0)
# Supabase pooler (порт 6543, transaction mode) не поддерживает prepared statements
USE_DB_POOLER = ":6543/" in DATABASE_URL or os.environ.get("DB_POOLER", "").lower() == "true"
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(90 * 24 * 60 * 60)))

pool: Optional[asyncpg.Pool] = None
telegram_menu_configured = False
telegram_menu_status = "not_checked"
import logging

DEFAULT_SUPPLEMENTS = [
    {"name": "Витамин D3", "emoji": "☀", "dose": "2000 МЕ", "times": ["Утро"]},
    {"name": "Омега-3", "emoji": "Ω", "dose": "1000 мг", "times": ["Утро", "Вечер"]},
    {"name": "Магний B6", "emoji": "Mg", "dose": "400 мг", "times": ["Вечер"]},
    {"name": "Цинк", "emoji": "Zn", "dose": "15 мг", "times": ["Утро"]},
    {"name": "Креатин", "emoji": "Cr", "dose": "5 г", "times": ["Утро"]},
    {"name": "Протеин", "emoji": "P", "dose": "30 г", "times": ["День"]},
]

DEFAULT_WORKOUT_GROUPS = [
    {"name": "Грудь + Трицепс", "short": "Грудь+Три", "exercises": ["Жим штанги лёжа", "Жим штанги на наклонной скамье", "Жим гантелей на наклонной скамье", "Разводка гантелей лёжа", "Сведение в тренажёре (Бабочка)", "Французский жим", "Разгибание на трицепс (блок)"]},
    {"name": "Спина + Бицепс", "short": "Спина+Би", "exercises": ["Становая тяга", "Румынская тяга", "Тяга штанги в наклоне", "Тяга верхнего блока к груди", "Тяга нижнего блока к поясу", "Тяга нижнего блока одной рукой", "Тяга в хаммере", "Подъём штанги на бицепс", "Подъём EZ-штанги на бицепс", "Подъём гантелей на бицепс сидя", "Подъём гантелей на бицепс стоя", "Сгибание на скамье Скотта", "Молотки (Hammer Curl)"]},
    {"name": "Ноги + Ягодицы", "short": "Ноги", "exercises": ["Приседания со штангой", "Жим ногами", "Разгибание ног (тренажёр)", "Сгибание ног (тренажёр)", "Выпады с гантелями", "Подъём на носки"]},
    {"name": "Плечи + Трапеции", "short": "Плечи", "exercises": ["Жим гантелей сидя (плечи)", "Жим штанги стоя", "Подъём гантелей в стороны", "Подъём гантелей перед собой", "Разведение в тренажёре (задние дельты)", "Тяга к подбородку", "Шраги"]},
    {"name": "Руки (Би + Три)", "short": "Руки", "exercises": ["Подъём штанги на бицепс", "Молотки (Hammer Curl)", "Французский жим", "Разгибание на трицепс (блок)"]},
    {"name": "Пресс + Кор", "short": "Пресс", "exercises": ["Скручивания", "Подъём ног в висе", "Планка", "Боковая планка", "Велосипед", "Русский твист"]},
]


# ════════════════════════════════════════════════════════════════
# MODELS
# ════════════════════════════════════════════════════════════════
class WorkoutIn(BaseModel):
    client_id: Optional[str] = None
    date: date
    muscle: str
    exercise: str
    sets: int
    reps: int
    weight: float
    rpe: Optional[float] = Field(default=None, ge=1, le=10)
    note: Optional[str] = Field(default=None, max_length=200)

class WorkoutGroupItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    short: Optional[str] = Field(default=None, max_length=24)
    exercises: list[str] = Field(default_factory=list, max_length=40)

class WorkoutGroupsIn(BaseModel):
    groups: list[WorkoutGroupItem] = Field(min_length=1, max_length=24)

class WorkoutProgramDay(BaseModel):
    weekday: int = Field(ge=0, le=6)
    rest: bool = False
    group: Optional[str] = Field(default=None, max_length=80)

class WorkoutProgramIn(BaseModel):
    days: list[WorkoutProgramDay] = Field(min_length=7, max_length=7)

class TaskIn(BaseModel):
    id: str
    text: str
    prio: str
    dl: Optional[date] = None
    cat: Optional[str] = None
    done: bool = False
    start_time: Optional[time] = None
    duration_minutes: int = Field(default=30, ge=5, le=1440)
    repeat_rule: Literal["none", "daily", "weekdays", "weekly"] = "none"

class TaskTemplateItem(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    prio: Literal["h", "m", "l"] = "m"
    cat: Optional[str] = Field(default=None, max_length=80)
    start_time: Optional[time] = None
    duration_minutes: int = Field(default=30, ge=5, le=1440)
    repeat_rule: Literal["none", "daily", "weekdays", "weekly"] = "none"

class TaskTemplateIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    tasks: list[TaskTemplateItem] = Field(min_length=1, max_length=20)

class SupplementIn(BaseModel):
    client_id: Optional[str] = None
    name: str
    emoji: str = "💊"
    dose: str = ""
    times: list[str]

class SuppCheckIn(BaseModel):
    date: date
    supp_name: str
    time_slot: str
    checked: bool = True

class BodyWeightIn(BaseModel):
    date: date
    weight: float

class BodyCalIn(BaseModel):
    date: date
    calories: int

class BodyMeasureIn(BaseModel):
    date: date
    weight: float
    height: float
    chest: Optional[float] = None
    waist: Optional[float] = None
    hips: Optional[float] = None
    fat: Optional[float] = None
    muscle: Optional[float] = None

class FoodLogIn(BaseModel):
    client_id: Optional[str] = None
    date: date
    meal_type: str
    food_id: str
    food_name: str
    serving_desc: Optional[str] = None
    calories: Optional[float] = None
    protein: Optional[float] = None
    fat: Optional[float] = None
    carbs: Optional[float] = None
    amount: float = 1.0

class FoodFavoriteIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=80)
    food_id: str = Field(default="manual", max_length=160)
    food_name: str = Field(min_length=1, max_length=200)
    serving_desc: Optional[str] = Field(default=None, max_length=120)
    calories: float = Field(default=0, ge=0, le=2000)
    protein: float = Field(default=0, ge=0, le=500)
    fat: float = Field(default=0, ge=0, le=500)
    carbs: float = Field(default=0, ge=0, le=500)
    amount: float = Field(default=100, ge=1, le=5000)
    meal_type: Literal["завтрак", "обед", "ужин", "перекус"] = "перекус"

class BulkImportIn(BaseModel):
    workouts: list[WorkoutIn] = []

class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    goal: Literal["lose", "maintain", "gain"]
    sex: Literal["male", "female"]
    age: int = Field(ge=18, le=100)
    height: float = Field(ge=120, le=230)
    weight: float = Field(ge=35, le=350)
    target_weight: Optional[float] = Field(default=None, ge=35, le=350)
    activity: Literal["low", "light", "medium", "high"]
    calorie_goal: int = Field(ge=1200, le=6000)
    onboarding_completed: bool = True

class NotificationSettingsIn(BaseModel):
    notif_morning: time = time(8, 0)
    notif_morning_on: bool = True
    notif_workout: time = time(10, 0)
    notif_workout_on: bool = True
    notif_evening: time = time(21, 0)
    notif_evening_on: bool = True
    notif_tasks: time = time(20, 0)
    notif_tasks_on: bool = True
    notif_weekly: time = time(19, 0)
    notif_weekly_on: bool = True
    timezone: str = Field(default="Europe/Moscow", min_length=1, max_length=64)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, telegram_menu_configured
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required for persistent product storage")
    if ALLOW_INSECURE_DEMO and APP_ENV not in {"development", "test"}:
        raise RuntimeError("ALLOW_INSECURE_DEMO is forbidden outside development and test")
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=0 if IS_SERVERLESS else 1,
        max_size=3 if IS_SERVERLESS else 10,
        command_timeout=30,
        statement_cache_size=0 if USE_DB_POOLER else 100,
    )
    await init_db()
    task = None
    if not IS_SERVERLESS:
        # Постоянный сервер: меню бота при старте и фоновая рассылка.
        # В serverless (Vercel) это делает /api/cron/notifications по расписанию.
        telegram_menu_configured = await configure_telegram_menu_button()
        task = asyncio.create_task(notification_scheduler())
    yield
    if task:
        task.cancel()
    await pool.close()


app = FastAPI(title="Planner API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def configure_telegram_menu_button() -> bool:
    global telegram_menu_status
    if not BOT_TOKEN:
        telegram_menu_status = "bot_token_missing"
        logging.error("BOT_TOKEN is missing; Telegram Mini App menu cannot be configured")
        return False
    payload = {
        "menu_button": {
            "type": "web_app",
            "text": "Открыть планировщик",
            "web_app": {"url": FRONTEND_URL},
        }
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton",
                json=payload,
            )
        data = response.json()
        if response.is_success and data.get("ok") is True:
            telegram_menu_status = "ok"
            return True
        description = str(data.get("description") or "unknown").lower()
        if response.status_code == 401:
            telegram_menu_status = "telegram_unauthorized"
        elif "url" in description or "web app" in description:
            telegram_menu_status = "telegram_rejected_url"
        else:
            telegram_menu_status = f"telegram_http_{response.status_code}"
        logging.error("Telegram menu configuration failed: HTTP %s: %s", response.status_code, description[:120])
    except Exception as error:
        telegram_menu_status = "network_error_" + type(error).__name__.lower()
        logging.exception("Telegram menu configuration failed")
    return False


def verify_telegram_init_data(init_data: str, max_age_seconds: int = 86400) -> int:
    if not BOT_TOKEN:
        raise HTTPException(503, "Telegram authentication is not configured")
    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = values.pop("hash")
        auth_date = int(values.get("auth_date", "0"))
        if not auth_date or abs(int(unix_time.time()) - auth_date) > max_age_seconds:
            raise HTTPException(401, "Telegram authorization has expired")
        data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            raise HTTPException(401, "Invalid Telegram authorization")
        user = json.loads(values.get("user", "{}"))
        user_id = int(user.get("id", 0))
        if user_id <= 0:
            raise HTTPException(401, "Telegram user is missing")
        return user_id
    except HTTPException:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(401, "Invalid Telegram authorization")


def create_session_token(user_id: int, ttl_seconds: int = SESSION_TTL_SECONDS) -> tuple[str, int]:
    if not BOT_TOKEN:
        raise HTTPException(503, "Telegram authentication is not configured")
    expires_at = int(unix_time.time()) + ttl_seconds
    payload = f"v2:{user_id}:{expires_at}"
    signature = hmac.new(session_signing_key(), payload.encode(), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{payload}:{encoded_signature}", expires_at


def verify_session_token(token: str) -> int:
    if not BOT_TOKEN:
        raise HTTPException(503, "Telegram authentication is not configured")
    try:
        version, raw_user_id, raw_expires_at, received_signature = token.split(":", 3)
        user_id = int(raw_user_id)
        expires_at = int(raw_expires_at)
        if version != "v2" or user_id <= 0 or expires_at <= int(unix_time.time()):
            raise HTTPException(401, "Session has expired")
        payload = f"{version}:{user_id}:{expires_at}"
        signature = hmac.new(session_signing_key(), payload.encode(), hashlib.sha256).digest()
        expected_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        if not hmac.compare_digest(expected_signature, received_signature):
            raise HTTPException(401, "Invalid session")
        return user_id
    except HTTPException:
        raise
    except (TypeError, ValueError):
        raise HTTPException(401, "Invalid session")


async def authenticated_user(
    telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
    demo_user_id: Optional[int] = Header(None, alias="X-User-Id"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> int:
    if authorization and authorization.startswith("Bearer "):
        return verify_session_token(authorization[7:].strip())
    if telegram_init_data:
        return verify_telegram_init_data(telegram_init_data)
    if ALLOW_INSECURE_DEMO and demo_user_id and demo_user_id > 0:
        return demo_user_id
    raise HTTPException(401, "Open the planner inside Telegram")


# ════════════════════════════════════════════════════════════════
# DB SCHEMA
# ════════════════════════════════════════════════════════════════
async def init_db():
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                source TEXT DEFAULT 'telegram',
                created_at TIMESTAMPTZ DEFAULT now(),
                last_seen_at TIMESTAMPTZ DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS workouts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                client_id TEXT,
                date DATE NOT NULL,
                muscle TEXT NOT NULL,
                exercise TEXT NOT NULL,
                sets INT NOT NULL,
                reps INT NOT NULL,
                weight NUMERIC NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_workouts_user ON workouts(user_id);
            CREATE INDEX IF NOT EXISTS idx_workouts_user_date ON workouts(user_id, date DESC);

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                prio TEXT NOT NULL,
                dl DATE,
                cat TEXT,
                done BOOLEAN DEFAULT FALSE,
                start_time TIME,
                duration_minutes INT DEFAULT 30,
                repeat_rule TEXT DEFAULT 'none',
                created_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (user_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_user_done ON tasks(user_id, done);

            CREATE TABLE IF NOT EXISTS task_templates (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                client_id TEXT NOT NULL,
                name TEXT NOT NULL,
                tasks JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (user_id, client_id)
            );
            CREATE INDEX IF NOT EXISTS idx_task_templates_user ON task_templates(user_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS supplements (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                client_id TEXT,
                name TEXT NOT NULL,
                emoji TEXT,
                dose TEXT,
                times JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_supps_user ON supplements(user_id);

            CREATE TABLE IF NOT EXISTS supplement_checks (
                user_id BIGINT NOT NULL,
                date DATE NOT NULL,
                supp_name TEXT NOT NULL,
                time_slot TEXT NOT NULL,
                checked BOOLEAN DEFAULT TRUE,
                PRIMARY KEY (user_id, date, supp_name, time_slot)
            );
            CREATE INDEX IF NOT EXISTS idx_supp_checks_user_date ON supplement_checks(user_id, date DESC);

            CREATE TABLE IF NOT EXISTS body_weight (
                user_id BIGINT NOT NULL,
                date DATE NOT NULL,
                weight NUMERIC NOT NULL,
                PRIMARY KEY (user_id, date)
            );

            CREATE TABLE IF NOT EXISTS body_calories (
                user_id BIGINT NOT NULL,
                date DATE NOT NULL,
                calories INT NOT NULL,
                PRIMARY KEY (user_id, date)
            );

            CREATE TABLE IF NOT EXISTS body_measures (
                user_id BIGINT NOT NULL,
                date DATE NOT NULL,
                weight NUMERIC NOT NULL,
                height NUMERIC NOT NULL,
                chest NUMERIC,
                waist NUMERIC,
                hips NUMERIC,
                fat NUMERIC,
                muscle NUMERIC,
                updated_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (user_id, date)
            );

            CREATE TABLE IF NOT EXISTS food_log (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                client_id TEXT,
                date DATE NOT NULL,
                meal_type TEXT NOT NULL,
                food_id TEXT NOT NULL,
                food_name TEXT NOT NULL,
                serving_desc TEXT,
                calories NUMERIC,
                protein NUMERIC,
                fat NUMERIC,
                carbs NUMERIC,
                amount NUMERIC DEFAULT 1,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_food_log_user ON food_log(user_id);
            CREATE INDEX IF NOT EXISTS idx_food_log_user_date ON food_log(user_id, date DESC);

            CREATE TABLE IF NOT EXISTS food_favorites (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                client_id TEXT NOT NULL,
                food_id TEXT NOT NULL,
                food_name TEXT NOT NULL,
                serving_desc TEXT,
                calories NUMERIC DEFAULT 0,
                protein NUMERIC DEFAULT 0,
                fat NUMERIC DEFAULT 0,
                carbs NUMERIC DEFAULT 0,
                amount NUMERIC DEFAULT 100,
                meal_type TEXT DEFAULT 'перекус',
                created_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (user_id, client_id)
            );
            CREATE INDEX IF NOT EXISTS idx_food_favorites_user ON food_favorites(user_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                notif_morning TEXT DEFAULT '08:00',
                notif_morning_on BOOLEAN DEFAULT TRUE,
                notif_workout TEXT DEFAULT '10:00',
                notif_workout_on BOOLEAN DEFAULT TRUE,
                notif_evening TEXT DEFAULT '21:00',
                notif_evening_on BOOLEAN DEFAULT TRUE,
                notif_tasks TEXT DEFAULT '20:00',
                notif_tasks_on BOOLEAN DEFAULT TRUE,
                notif_weekly TEXT DEFAULT '19:00',
                notif_weekly_on BOOLEAN DEFAULT TRUE,
                timezone TEXT DEFAULT 'Europe/Moscow',
                name TEXT,
                goal TEXT,
                sex TEXT,
                age INT,
                height NUMERIC,
                weight NUMERIC,
                target_weight NUMERIC,
                activity TEXT,
                calorie_goal INT,
                onboarding_completed BOOLEAN DEFAULT FALSE,
                workout_groups JSONB,
                active_workout_program JSONB,
                seeded_defaults BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMPTZ DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS user_activity_days (
                user_id BIGINT NOT NULL,
                day DATE NOT NULL,
                PRIMARY KEY (user_id, day)
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS notification_deliveries (
                user_id BIGINT NOT NULL,
                kind TEXT NOT NULL,
                scheduled_key TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (user_id, kind, scheduled_key)
            );
        """)
        await conn.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'tasks_pkey'
                      AND conrelid = 'tasks'::regclass
                      AND pg_get_constraintdef(oid) = 'PRIMARY KEY (id)'
                ) THEN
                    ALTER TABLE tasks DROP CONSTRAINT tasks_pkey;
                    ALTER TABLE tasks ADD PRIMARY KEY (user_id, id);
                END IF;
            END $$;
        """)
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS seeded_defaults BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS timezone TEXT DEFAULT 'Europe/Moscow'")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS name TEXT")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS goal TEXT")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS sex TEXT")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS age INT")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS height NUMERIC")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS weight NUMERIC")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS target_weight NUMERIC")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS activity TEXT")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS calorie_goal INT")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS workout_groups JSONB")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS active_workout_program JSONB")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS notif_tasks TEXT DEFAULT '20:00'")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS notif_tasks_on BOOLEAN DEFAULT TRUE")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS notif_weekly TEXT DEFAULT '19:00'")
        await conn.execute("ALTER TABLE food_log ADD COLUMN IF NOT EXISTS client_id TEXT")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_food_log_user_client ON food_log(user_id, client_id) WHERE client_id IS NOT NULL")
        await conn.execute("ALTER TABLE workouts ADD COLUMN IF NOT EXISTS client_id TEXT")
        await conn.execute("ALTER TABLE workouts ADD COLUMN IF NOT EXISTS rpe NUMERIC")
        await conn.execute("ALTER TABLE workouts ADD COLUMN IF NOT EXISTS note TEXT")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_workouts_user_client ON workouts(user_id, client_id) WHERE client_id IS NOT NULL")
        await conn.execute("ALTER TABLE supplements ADD COLUMN IF NOT EXISTS client_id TEXT")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_supplements_user_client ON supplements(user_id, client_id) WHERE client_id IS NOT NULL")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS start_time TIME")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS duration_minutes INT DEFAULT 30")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS repeat_rule TEXT DEFAULT 'none'")


async def ensure_user_settings(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id) VALUES ($1)
            ON CONFLICT (user_id) DO UPDATE SET last_seen_at=now()
        """, user_id)
        # Активный день для метрик удержания: только факт входа, без содержимого записей.
        await conn.execute(
            "INSERT INTO user_activity_days (user_id, day) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            user_id, datetime.now(TZ).date()
        )
        settings = await conn.fetchrow("""
            INSERT INTO user_settings (user_id) VALUES ($1)
            ON CONFLICT (user_id) DO UPDATE SET updated_at=user_settings.updated_at
            RETURNING seeded_defaults, workout_groups
        """, user_id)
        if settings and not settings["workout_groups"]:
            await conn.execute(
                "UPDATE user_settings SET workout_groups=$2::jsonb WHERE user_id=$1",
                user_id, json.dumps(DEFAULT_WORKOUT_GROUPS, ensure_ascii=False)
            )
        if settings and not settings["seeded_defaults"]:
            async with conn.transaction():
                for supp in DEFAULT_SUPPLEMENTS:
                    await conn.execute("""
                        INSERT INTO supplements (user_id, name, emoji, dose, times)
                        SELECT $1,$2,$3,$4,$5::jsonb
                        WHERE NOT EXISTS (
                            SELECT 1 FROM supplements WHERE user_id=$1 AND lower(name)=lower($2)
                        )
                    """, user_id, supp["name"], supp["emoji"], supp["dose"], json.dumps(supp["times"]))
                await conn.execute(
                    "UPDATE user_settings SET seeded_defaults=TRUE, updated_at=now() WHERE user_id=$1",
                    user_id
                )


def parse_times(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return json.loads(value)


async def fetch_user_state(user_id: int) -> dict:
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        workouts = await conn.fetch(
            "SELECT id, client_id, date, muscle, exercise, sets, reps, weight, rpe, note FROM workouts WHERE user_id=$1 ORDER BY date DESC, id DESC",
            user_id
        )
        tasks = await conn.fetch(
            "SELECT id, text, prio, dl, cat, done, start_time, duration_minutes, repeat_rule FROM tasks WHERE user_id=$1 ORDER BY created_at DESC",
            user_id
        )
        task_templates = await conn.fetch(
            "SELECT id, client_id, name, tasks FROM task_templates WHERE user_id=$1 ORDER BY created_at DESC",
            user_id
        )
        supps = await conn.fetch(
            "SELECT id, client_id, name, emoji, dose, times FROM supplements WHERE user_id=$1 ORDER BY id",
            user_id
        )
        checks = await conn.fetch(
            "SELECT date, supp_name, time_slot, checked FROM supplement_checks WHERE user_id=$1",
            user_id
        )
        weight = await conn.fetch(
            "SELECT date, weight FROM body_weight WHERE user_id=$1 ORDER BY date",
            user_id
        )
        calories = await conn.fetch(
            "SELECT date, calories FROM body_calories WHERE user_id=$1 ORDER BY date",
            user_id
        )
        food_calories = await conn.fetch(
            """SELECT date, ROUND(SUM(COALESCE(calories, 0)))::int AS calories
               FROM food_log WHERE user_id=$1 GROUP BY date ORDER BY date""",
            user_id
        )
        measures = await conn.fetch(
            "SELECT date, weight, height, chest, waist, hips, fat, muscle FROM body_measures WHERE user_id=$1 ORDER BY date",
            user_id
        )
        food_recent = await conn.fetch(
            "SELECT * FROM food_log WHERE user_id=$1 ORDER BY date DESC, created_at DESC LIMIT 100",
            user_id
        )
        food_favorites = await conn.fetch(
            "SELECT id, client_id, food_id, food_name, serving_desc, calories, protein, fat, carbs, amount, meal_type FROM food_favorites WHERE user_id=$1 ORDER BY created_at DESC",
            user_id
        )
        settings = await conn.fetchrow("SELECT * FROM user_settings WHERE user_id=$1", user_id)

    daily_calories = {
        row["date"]: {"date": row["date"], "calories": row["calories"], "source": "manual"}
        for row in calories
    }
    for row in food_calories:
        daily_calories[row["date"]] = {
            "date": row["date"], "calories": row["calories"], "source": "food"
        }

    return {
        "user_id": user_id,
        "settings": dict(settings) if settings else {},
        "workouts": [dict(r) for r in workouts],
        "tasks": [dict(r) for r in tasks],
        "task_templates": [dict(r) | {"tasks": parse_times(r["tasks"])} for r in task_templates],
        "supplements": [dict(r) | {"times": parse_times(r["times"])} for r in supps],
        "supplement_checks": [dict(r) for r in checks],
        "body_weight": [dict(r) for r in weight],
        "body_calories": [daily_calories[key] for key in sorted(daily_calories)],
        "body_measures": [dict(r) for r in measures],
        "food_log_recent": [dict(r) for r in food_recent],
        "food_favorites": [dict(r) for r in food_favorites],
    }


# ════════════════════════════════════════════════════════════════
# УВЕДОМЛЕНИЯ — Telegram Bot
# ════════════════════════════════════════════════════════════════
async def send_telegram(user_id: int, text: str):
    """Отправляем сообщение пользователю через Telegram Bot API."""
    if not BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": user_id, "text": text, "parse_mode": "HTML"}
            )
    except Exception as e:
        logging.warning(f"Telegram send error: {e}")


async def claim_notification(user_id: int, kind: str, scheduled_key: str) -> bool:
    async with pool.acquire() as conn:
        claimed = await conn.fetchval("""
            INSERT INTO notification_deliveries (user_id, kind, scheduled_key)
            VALUES ($1,$2,$3)
            ON CONFLICT DO NOTHING
            RETURNING 1
        """, user_id, kind, scheduled_key)
    return bool(claimed)


def scheduled_due(value, now: datetime, window_minutes: int) -> bool:
    """Время HH:MM наступило в последние window_minutes минут (по часовому поясу now)."""
    try:
        hour, minute = map(int, str(value)[:5].split(":"))
    except (TypeError, ValueError):
        return False
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    elapsed = (now - scheduled).total_seconds()
    return 0 <= elapsed < window_minutes * 60


async def run_notifications_tick(now_utc: Optional[datetime] = None, window_minutes: int = 1) -> int:
    """Один проход рассылки. Повторы защищены claim_notification (ключ = дата + время)."""
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT * FROM user_settings")
    for u in users:
        try:
            await notify_user(u, now_utc, window_minutes)
        except Exception as e:
            logging.error("Notification error for user %s: %s", u["user_id"], e)
    try:
        await send_owner_digest(now_utc)
    except Exception as e:
        logging.error("Owner digest error: %s", e)
    return len(users)


async def notify_user(u, now_utc: Optional[datetime], window_minutes: int):
    uid = u["user_id"]
    try:
        user_tz = ZoneInfo(u["timezone"] or "Europe/Moscow")
    except Exception:
        user_tz = TZ
    now = (now_utc or datetime.now(ZoneInfo("UTC"))).astimezone(user_tz)
    current_weekday = now.weekday()

    def due(value) -> bool:
        return scheduled_due(value, now, window_minutes)

    def key(value) -> str:
        return f"{now.date().isoformat()}T{value}"
    
    # Утренние добавки
    if (u["notif_morning_on"] and due(u["notif_morning"])
            and await claim_notification(uid, "supplements_morning", key(u["notif_morning"]))):
        async with pool.acquire() as conn:
            supps = await conn.fetch(
                "SELECT name FROM supplements WHERE user_id=$1 AND times::text ILIKE '%утро%'", uid
            )
        if supps:
            names = ", ".join(html.escape(s["name"]) for s in supps[:3])
            await send_telegram(uid, "☀️ <b>Доброе утро!</b>\n\nНе забудь принять добавки: " + names)
    
    # Напоминание о тренировке
    if (u["notif_workout_on"] and due(u["notif_workout"])
            and await claim_notification(uid, "workout", key(u["notif_workout"]))):
        # Проверяем был ли уже подход сегодня
        async with pool.acquire() as conn:
            today_workouts = await conn.fetchval(
                "SELECT COUNT(*) FROM workouts WHERE user_id=$1 AND date=$2",
                uid, now.date()
            )
        if today_workouts == 0:
            text = workout_reminder_text(u["active_workout_program"], current_weekday)
            if text:
                await send_telegram(uid, text)
    
    # Вечерние добавки
    if (u["notif_evening_on"] and due(u["notif_evening"])
            and await claim_notification(uid, "supplements_evening", key(u["notif_evening"]))):
        async with pool.acquire() as conn:
            supps = await conn.fetch(
                "SELECT name FROM supplements WHERE user_id=$1 AND times::text ILIKE '%вечер%'", uid
            )
        if supps:
            names = ", ".join(html.escape(s["name"]) for s in supps[:3])
            await send_telegram(uid, "🌙 <b>Вечерние добавки</b>\n\nПора принять: " + names + "\n\nХорошего сна!")

    # Незавершенные задачи
    if (u["notif_tasks_on"] and due(u["notif_tasks"])
            and await claim_notification(uid, "tasks", key(u["notif_tasks"]))):
        async with pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE user_id=$1 AND done=FALSE AND (dl IS NULL OR dl<=$2)",
                uid, now.date()
            )
        if remaining:
            await send_telegram(
                uid,
                f"📋 <b>Осталось задач: {remaining}</b>\n\nЗакрой главное или перенеси дела на другой день."
            )
    
    # Еженедельный отчёт — воскресенье 19:00
    if (u["notif_weekly_on"] and current_weekday == 6 and due(u["notif_weekly"])
            and await claim_notification(uid, "weekly_report", key(u["notif_weekly"]))):
        await send_weekly_report(uid)


WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def workout_reminder_text(program_raw, python_weekday: int) -> Optional[str]:
    """Текст напоминания по недельной программе пользователя.
    В программе weekday — как в JS getDay() (0 = воскресенье). None = день отдыха."""
    day_name = WEEKDAYS_RU[python_weekday]
    js_weekday = (python_weekday + 1) % 7
    program = parse_times(program_raw) if program_raw else []
    plan = next((d for d in program if isinstance(d, dict) and d.get("weekday") == js_weekday), None)
    if plan is None:
        return f"💪 <b>Сегодня {day_name}</b>\n\nВремя тренировки — не пропусти!"
    if plan.get("rest") or not plan.get("group"):
        return None
    return f"💪 <b>Сегодня {day_name}: {html.escape(str(plan['group']))}</b>\n\nОткрой планировщик и запиши первый подход."


async def notification_scheduler():
    """Фоновый режим для постоянного сервера (Railway/VPS): проход раз в минуту."""
    logging.info("Notification scheduler started")
    while True:
        try:
            await asyncio.sleep(60)
            await run_notifications_tick(window_minutes=2)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"Scheduler error: {e}")


async def send_weekly_report(user_id: int):
    """Отправляем еженедельный отчёт."""
    try:
        from datetime import timedelta
        today = datetime.now(TZ).date()
        week_ago = today - timedelta(days=7)
        
        async with pool.acquire() as conn:
            workouts = await conn.fetchval(
                "SELECT COUNT(DISTINCT date) FROM workouts WHERE user_id=$1 AND date >= $2",
                user_id, week_ago
            )
            best = await conn.fetch("""
                SELECT exercise, MAX(ROUND(weight*(1+reps::numeric/30),1)) as orm
                FROM workouts WHERE user_id=$1 AND date >= $2
                GROUP BY exercise ORDER BY orm DESC LIMIT 3
            """, user_id, week_ago)
            food = await conn.fetch(
                "SELECT date, SUM(calories) as cal FROM food_log WHERE user_id=$1 AND date >= $2 GROUP BY date",
                user_id, week_ago
            )
        
        text = "Отчет за неделю\n\n"
        text += "Тренировок: " + str(workouts) + " из 7 дней\n"
        
        if best:
            text += "\nЛучшие результаты:\n"
            for b in best:
                text += "  - " + str(b["exercise"]) + ": " + str(b["orm"]) + " кг 1RM\n"
        
        if food:
            avg_cal = sum(float(f["cal"]) for f in food) / len(food)
            text += "\nСреднее калорий/день: " + str(round(avg_cal)) + " ккал\n"
        
        text += "\nОткрой планировщик чтобы увидеть полный отчёт!"
        await send_telegram(user_id, text)
    except Exception as e:
        logging.error("Weekly report error: " + str(e))


# ════════════════════════════════════════════════════════════════
# PRODUCT BOOTSTRAP / HEALTH
# ════════════════════════════════════════════════════════════════
@app.get("/api/health")
async def health():
    if pool is None:
        raise HTTPException(503, "Database pool is not ready")
    try:
        async with pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
        return {
            "status": "ok", "database": "ok", "value": value,
            "telegram_menu": "ok" if telegram_menu_configured else "error",
            "telegram_menu_status": telegram_menu_status,
        }
    except Exception as e:
        logging.exception("Healthcheck failed")
        raise HTTPException(503, f"Database unavailable: {type(e).__name__}")


@app.post("/api/cron/notifications")
async def cron_notifications(authorization: Optional[str] = Header(None, alias="Authorization")):
    """Внешний планировщик (GitHub Actions) дёргает раз в ~10 минут."""
    global telegram_menu_configured
    if not CRON_SECRET or not hmac.compare_digest(authorization or "", f"Bearer {CRON_SECRET}"):
        raise HTTPException(401, "Invalid cron secret")
    if not telegram_menu_configured:
        telegram_menu_configured = await configure_telegram_menu_button()
    users = await run_notifications_tick(window_minutes=30)
    return {"status": "ok", "users": users}


@app.post("/api/auth/session")
async def create_auth_session(
    telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    if not telegram_init_data:
        raise HTTPException(401, "Open the planner inside Telegram")
    user_id = verify_telegram_init_data(telegram_init_data, max_age_seconds=7 * 24 * 60 * 60)
    token, expires_at = create_session_token(user_id)
    return {"token": token, "user_id": user_id, "expires_at": expires_at}


@app.get("/api/bootstrap")
async def bootstrap(user_id: int = Depends(authenticated_user)):
    state = await fetch_user_state(user_id)
    state["is_owner"] = bool(OWNER_USER_ID) and user_id == OWNER_USER_ID
    return state


@app.post("/api/settings/profile")
async def save_profile(profile: "ProfileIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE user_settings SET
                name=$2, goal=$3, sex=$4, age=$5, height=$6, weight=$7,
                target_weight=$8, activity=$9, calorie_goal=$10,
                onboarding_completed=$11, updated_at=now()
            WHERE user_id=$1
            RETURNING name, goal, sex, age, height, weight, target_weight,
                      activity, calorie_goal, onboarding_completed, updated_at
        """, user_id, profile.name, profile.goal, profile.sex, profile.age,
             profile.height, profile.weight, profile.target_weight, profile.activity,
             profile.calorie_goal, profile.onboarding_completed)
    return dict(row)


@app.post("/api/settings/notifications")
async def save_notification_settings(
    settings: "NotificationSettingsIn",
    user_id: int = Depends(authenticated_user),
):
    try:
        ZoneInfo(settings.timezone)
    except Exception:
        raise HTTPException(422, "Invalid timezone")
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE user_settings SET
                notif_morning=$2, notif_morning_on=$3,
                notif_workout=$4, notif_workout_on=$5,
                notif_evening=$6, notif_evening_on=$7,
                notif_tasks=$8, notif_tasks_on=$9,
                notif_weekly=$10, notif_weekly_on=$11,
                timezone=$12, updated_at=now()
            WHERE user_id=$1
            RETURNING notif_morning, notif_morning_on, notif_workout, notif_workout_on,
                      notif_evening, notif_evening_on, notif_tasks, notif_tasks_on,
                      notif_weekly, notif_weekly_on, timezone, updated_at
        """, user_id, settings.notif_morning.strftime("%H:%M"), settings.notif_morning_on,
             settings.notif_workout.strftime("%H:%M"), settings.notif_workout_on,
             settings.notif_evening.strftime("%H:%M"), settings.notif_evening_on,
             settings.notif_tasks.strftime("%H:%M"), settings.notif_tasks_on,
             settings.notif_weekly.strftime("%H:%M"), settings.notif_weekly_on,
             settings.timezone)
    return dict(row)


@app.post("/api/settings/workout-groups")
async def save_workout_groups(
    payload: "WorkoutGroupsIn",
    user_id: int = Depends(authenticated_user),
):
    await ensure_user_settings(user_id)
    groups = []
    for group in payload.groups:
        exercises = []
        seen = set()
        for raw in group.exercises:
            name = str(raw or "").strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                exercises.append(name[:120])
        groups.append({
            "name": group.name.strip(),
            "short": (group.short or group.name).strip()[:24],
            "exercises": exercises,
        })
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE user_settings SET workout_groups=$2::jsonb, updated_at=now()
            WHERE user_id=$1
            RETURNING workout_groups, updated_at
        """, user_id, json.dumps(groups, ensure_ascii=False))
    return {"workout_groups": parse_times(row["workout_groups"]), "updated_at": row["updated_at"]}


@app.post("/api/settings/workout-program")
async def save_workout_program(
    payload: "WorkoutProgramIn",
    user_id: int = Depends(authenticated_user),
):
    await ensure_user_settings(user_id)
    seen = set()
    program = []
    for day in sorted(payload.days, key=lambda item: item.weekday):
        if day.weekday in seen:
            continue
        seen.add(day.weekday)
        group = str(day.group or "").strip()
        rest = bool(day.rest or not group)
        program.append({
            "weekday": day.weekday,
            "rest": rest,
            "group": None if rest else group[:80],
        })
    if len(program) != 7 or seen != set(range(7)):
        raise HTTPException(422, "Program must include weekdays 0-6")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE user_settings SET active_workout_program=$2::jsonb, updated_at=now()
            WHERE user_id=$1
            RETURNING active_workout_program, updated_at
        """, user_id, json.dumps(program, ensure_ascii=False))
    return {"active_workout_program": parse_times(row["active_workout_program"]), "updated_at": row["updated_at"]}


# ════════════════════════════════════════════════════════════════
# WORKOUTS
# ════════════════════════════════════════════════════════════════
@app.get("/api/workouts")
async def get_workouts(user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, client_id, date, muscle, exercise, sets, reps, weight, rpe, note FROM workouts WHERE user_id=$1 ORDER BY date DESC, id DESC",
            user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/workouts")
async def add_workout(w: "WorkoutIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO workouts (user_id, client_id, date, muscle, exercise, sets, reps, weight, rpe, note)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (user_id, client_id) WHERE client_id IS NOT NULL
            DO UPDATE SET date=$3, muscle=$4, exercise=$5, sets=$6, reps=$7, weight=$8, rpe=$9, note=$10
            RETURNING id
        """, user_id, w.client_id, w.date, w.muscle, w.exercise, w.sets, w.reps, w.weight,
             w.rpe, (w.note or "").strip() or None)
    return {"status": "ok", "id": row["id"]}


@app.delete("/api/workouts/{workout_id}")
async def delete_workout(workout_id: int, user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM workouts WHERE id=$1 AND user_id=$2",
            workout_id, user_id
        )
    if result == "DELETE 0":
        raise HTTPException(404, "Workout not found")
    return {"status": "ok"}


@app.post("/api/workouts/bulk")
async def bulk_import_workouts(payload: "BulkImportIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for w in payload.workouts:
                await conn.execute(
                    "INSERT INTO workouts (user_id, date, muscle, exercise, sets, reps, weight) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                    user_id, w.date, w.muscle, w.exercise, w.sets, w.reps, w.weight
                )
    return {"status": "ok", "imported": len(payload.workouts)}


# ════════════════════════════════════════════════════════════════
# TASKS
# ════════════════════════════════════════════════════════════════
@app.get("/api/tasks")
async def get_tasks(user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, text, prio, dl, cat, done, start_time, duration_minutes, repeat_rule FROM tasks WHERE user_id=$1 ORDER BY created_at DESC",
            user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/tasks")
async def upsert_task(t: "TaskIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO tasks (id, user_id, text, prio, dl, cat, done, start_time, duration_minutes, repeat_rule)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (user_id, id) DO UPDATE
            SET text=$3, prio=$4, dl=$5, cat=$6, done=$7,
                start_time=$8, duration_minutes=$9, repeat_rule=$10
        """, t.id, user_id, t.text, t.prio, t.dl, t.cat, t.done,
             t.start_time, max(5, min(t.duration_minutes, 1440)), t.repeat_rule)
    return {"status": "ok"}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE id=$1 AND user_id=$2", task_id, user_id)
    return {"status": "ok"}


@app.get("/api/task-templates")
async def get_task_templates(user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, client_id, name, tasks FROM task_templates WHERE user_id=$1 ORDER BY created_at DESC",
            user_id
        )
    return [dict(row) | {"tasks": parse_times(row["tasks"])} for row in rows]


@app.post("/api/task-templates")
async def save_task_template(template: "TaskTemplateIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    tasks = [item.model_dump(mode="json") for item in template.tasks]
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO task_templates (user_id, client_id, name, tasks)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (user_id, client_id) DO UPDATE SET name=$3, tasks=$4
            RETURNING id
        """, user_id, template.client_id, template.name, json.dumps(tasks))
    return {"status": "ok", "id": row["id"]}


@app.delete("/api/task-templates/{template_id}")
async def delete_task_template(template_id: int, user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM task_templates WHERE id=$1 AND user_id=$2", template_id, user_id)
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════
# SUPPLEMENTS
# ════════════════════════════════════════════════════════════════
@app.get("/api/supplements")
async def get_supplements(user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, client_id, name, emoji, dose, times FROM supplements WHERE user_id=$1",
            user_id
        )
        return [dict(r) | {"times": parse_times(r["times"])} for r in rows]


@app.post("/api/supplements")
async def add_supplement(s: "SupplementIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO supplements (user_id, client_id, name, emoji, dose, times)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (user_id, client_id) WHERE client_id IS NOT NULL
            DO UPDATE SET name=$3, emoji=$4, dose=$5, times=$6
            RETURNING id
        """, user_id, s.client_id, s.name, s.emoji, s.dose, json.dumps(s.times))
    return {"status": "ok", "id": row["id"]}


@app.delete("/api/supplements/{supp_id}")
async def delete_supplement(supp_id: int, user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM supplements WHERE id=$1 AND user_id=$2", supp_id, user_id)
    return {"status": "ok"}


@app.get("/api/supplement_checks")
async def get_supp_checks(user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date, supp_name, time_slot, checked FROM supplement_checks WHERE user_id=$1",
            user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/supplement_checks")
async def toggle_supp_check(c: "SuppCheckIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        if c.checked:
            await conn.execute("""
                INSERT INTO supplement_checks (user_id, date, supp_name, time_slot, checked)
                VALUES ($1,$2,$3,$4,TRUE)
                ON CONFLICT (user_id, date, supp_name, time_slot) DO UPDATE SET checked=TRUE
            """, user_id, c.date, c.supp_name, c.time_slot)
        else:
            await conn.execute(
                "DELETE FROM supplement_checks WHERE user_id=$1 AND date=$2 AND supp_name=$3 AND time_slot=$4",
                user_id, c.date, c.supp_name, c.time_slot
            )
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════
# BODY (weight + calories + measurements)
# ════════════════════════════════════════════════════════════════
@app.get("/api/body/weight")
async def get_body_weight(user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date, weight FROM body_weight WHERE user_id=$1 ORDER BY date", user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/body/weight")
async def upsert_body_weight(b: "BodyWeightIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO body_weight (user_id, date, weight) VALUES ($1,$2,$3)
            ON CONFLICT (user_id, date) DO UPDATE SET weight=$3
        """, user_id, b.date, b.weight)
    return {"status": "ok"}


@app.delete("/api/body/weight/{entry_date}")
async def delete_body_weight(entry_date: date, user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM body_weight WHERE user_id=$1 AND date=$2",
            user_id, entry_date
        )
    return {"status": "ok"}


@app.get("/api/body/calories")
async def get_body_calories(user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date, calories FROM body_calories WHERE user_id=$1 ORDER BY date", user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/body/calories")
async def upsert_body_calories(b: "BodyCalIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO body_calories (user_id, date, calories) VALUES ($1,$2,$3)
            ON CONFLICT (user_id, date) DO UPDATE SET calories=$3
        """, user_id, b.date, b.calories)
    return {"status": "ok"}


@app.delete("/api/body/calories/{entry_date}")
async def delete_body_calories(entry_date: date, user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM body_calories WHERE user_id=$1 AND date=$2",
            user_id, entry_date
        )
    return {"status": "ok"}


@app.get("/api/body/measures")
async def get_body_measures(user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date, weight, height, chest, waist, hips, fat, muscle FROM body_measures WHERE user_id=$1 ORDER BY date",
            user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/body/measures")
async def upsert_body_measures(b: "BodyMeasureIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO body_measures (user_id, date, weight, height, chest, waist, hips, fat, muscle)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (user_id, date) DO UPDATE SET
                    weight=$3, height=$4, chest=$5, waist=$6, hips=$7,
                    fat=$8, muscle=$9, updated_at=now()
            """, user_id, b.date, b.weight, b.height, b.chest, b.waist, b.hips, b.fat, b.muscle)
            await conn.execute("""
                INSERT INTO body_weight (user_id, date, weight) VALUES ($1,$2,$3)
                ON CONFLICT (user_id, date) DO UPDATE SET weight=$3
            """, user_id, b.date, b.weight)
    return {"status": "ok"}


@app.delete("/api/body/measures/{entry_date}")
async def delete_body_measures(entry_date: date, user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM body_measures WHERE user_id=$1 AND date=$2",
            user_id, entry_date
        )
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════
# ПИТАНИЕ — Open Food Facts (бесплатно, без ключей)
# ════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════
# OPEN FOOD FACTS — бесплатно, без ключей, есть русские продукты
# ════════════════════════════════════════════════════════════════

def parse_off_product(p: dict) -> dict:
    """Парсим продукт из Open Food Facts в наш формат."""
    nutriments = p.get("nutriments", {})
    name = (p.get("product_name_ru") or p.get("product_name") or p.get("product_name_en") or "").strip()
    brand = p.get("brands", "").split(",")[0].strip()
    return {
        "food_id": p.get("_id", p.get("id", "")),
        "food_name": name or "Неизвестный продукт",
        "brand_name": brand,
        "serving_desc": "на 100г",
        "calories": round(float(nutriments.get("energy-kcal_100g") or nutriments.get("energy_100g", 0) or 0), 1),
        "protein":  round(float(nutriments.get("proteins_100g", 0) or 0), 1),
        "fat":      round(float(nutriments.get("fat_100g", 0) or 0), 1),
        "carbs":    round(float(nutriments.get("carbohydrates_100g", 0) or 0), 1),
    }


@app.get("/api/food/search")
async def search_food(q: str, user_id: int = Depends(authenticated_user)):
    """Поиск еды через Open Food Facts — пробуем несколько эндпоинтов."""
    urls = [
        ("https://world.openfoodfacts.org/cgi/search.pl", {
            "search_terms": q, "search_simple": 1, "action": "process",
            "json": 1, "page_size": 15, "sort_by": "unique_scans_n",
            "fields": "id,product_name,product_name_ru,product_name_en,brands,nutriments",
        }),
        ("https://world.openfoodfacts.net/cgi/search.pl", {
            "search_terms": q, "search_simple": 1, "action": "process",
            "json": 1, "page_size": 15,
            "fields": "id,product_name,product_name_ru,product_name_en,brands,nutriments",
        }),
    ]
    last_err = None
    for url, params in urls:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, params=params,
                    headers={"User-Agent": "PlannerApp/1.0"})
                logging.warning(f"OFF [{resp.status_code}] {url}")
                if resp.status_code >= 500:
                    last_err = f"OFF returned {resp.status_code}"
                    continue
                resp.raise_for_status()
                data = resp.json()
            products = data.get("products", [])
            results = [parse_off_product(p) for p in products
                       if p.get("product_name") or p.get("product_name_ru")]
            results = [r for r in results if r["food_name"] != "Неизвестный продукт"]
            return {"results": results[:10]}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logging.error(f"OFF error on {url}: {last_err}")
            continue
    raise HTTPException(503, f"Food search unavailable: {last_err}")


@app.get("/api/food/barcode")
async def search_by_barcode(barcode: str, user_id: int = Depends(authenticated_user)):
    """Поиск еды по штрихкоду через Open Food Facts."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json",
                headers={"User-Agent": "PlannerApp/1.0"}
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") != 1:
            return {"results": []}

        product = data.get("product", {})
        item = parse_off_product(product)
        return {"results": [item]}
    except Exception as e:
        logging.error(f"Barcode error: {type(e).__name__}: {e}")
        raise HTTPException(500, f"Barcode error: {type(e).__name__}: {str(e)}")


@app.get("/api/food/log")
async def get_food_log(log_date: Optional[str] = None, user_id: int = Depends(authenticated_user)):
    """Получить лог питания за день."""
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        if log_date:
            rows = await conn.fetch(
                "SELECT * FROM food_log WHERE user_id=$1 AND date=$2 ORDER BY created_at",
                user_id, date.fromisoformat(log_date)
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM food_log WHERE user_id=$1 ORDER BY date DESC, created_at DESC LIMIT 100",
                user_id
            )
        return [dict(r) for r in rows]


@app.post("/api/food/log")
async def add_food_log(entry: "FoodLogIn", user_id: int = Depends(authenticated_user)):
    """Добавить еду в дневник."""
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO food_log (user_id, client_id, date, meal_type, food_id, food_name, serving_desc,
                                  calories, protein, fat, carbs, amount)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (user_id, client_id) WHERE client_id IS NOT NULL
            DO UPDATE SET date=$3, meal_type=$4, food_id=$5, food_name=$6,
                          serving_desc=$7, calories=$8, protein=$9, fat=$10, carbs=$11, amount=$12
            RETURNING id
        """, user_id, entry.client_id, entry.date, entry.meal_type, entry.food_id, entry.food_name,
            entry.serving_desc, entry.calories, entry.protein, entry.fat, entry.carbs, entry.amount)
    return {"status": "ok", "id": row["id"]}


@app.delete("/api/food/log/{entry_id}")
async def delete_food_log(entry_id: int, user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM food_log WHERE id=$1 AND user_id=$2", entry_id, user_id)
    return {"status": "ok"}


@app.get("/api/food/favorites")
async def get_food_favorites(user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, client_id, food_id, food_name, serving_desc,
                   calories, protein, fat, carbs, amount, meal_type
            FROM food_favorites WHERE user_id=$1 ORDER BY created_at DESC
        """, user_id)
    return [dict(row) for row in rows]


@app.post("/api/food/favorites")
async def save_food_favorite(entry: "FoodFavoriteIn", user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO food_favorites (
                user_id, client_id, food_id, food_name, serving_desc,
                calories, protein, fat, carbs, amount, meal_type
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (user_id, client_id) DO UPDATE SET
                food_id=$3, food_name=$4, serving_desc=$5, calories=$6,
                protein=$7, fat=$8, carbs=$9, amount=$10, meal_type=$11
            RETURNING id
        """, user_id, entry.client_id, entry.food_id, entry.food_name, entry.serving_desc,
             entry.calories, entry.protein, entry.fat, entry.carbs, entry.amount, entry.meal_type)
    return {"status": "ok", "id": row["id"]}


@app.delete("/api/food/favorites/{favorite_id}")
async def delete_food_favorite(favorite_id: int, user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM food_favorites WHERE id=$1 AND user_id=$2", favorite_id, user_id)
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════
# ИИ АНАЛИЗ — прогресс и плато через Claude
# ════════════════════════════════════════════════════════════════
@app.get("/api/ai/analysis")
async def ai_analysis(user_id: int = Depends(authenticated_user)):
    """Анализ прогресса, плато и рекомендации через Groq (Llama 3.3 70B)."""
    await ensure_user_settings(user_id)
    if not GROQ_API_KEY:
        raise HTTPException(503, "AI not configured")

    async with pool.acquire() as conn:
        workouts = await conn.fetch(
            """SELECT date, exercise, sets, reps, weight,
               ROUND(weight * (1 + reps::numeric/30), 1) as orm
               FROM workouts WHERE user_id=$1 ORDER BY date""",
            user_id
        )
        body_weight = await conn.fetch(
            "SELECT date, weight FROM body_weight WHERE user_id=$1 ORDER BY date DESC LIMIT 30",
            user_id
        )

    # Группируем по упражнениям
    exercises_data = {}
    for r in workouts:
        ex = r["exercise"]
        if ex not in exercises_data:
            exercises_data[ex] = []
        exercises_data[ex].append({
            "date": str(r["date"]),
            "sets": r["sets"],
            "reps": r["reps"],
            "weight": float(r["weight"]),
            "1rm": float(r["orm"])
        })

    # Только упражнения с 3+ сессиями
    key_exercises = {k: v for k, v in exercises_data.items() if len(v) >= 3}

    prompt = f"""Ты персональный тренер и аналитик. Проанализируй данные тренировок пользователя.

ИСТОРИЯ ТРЕНИРОВОК (по упражнениям, отсортировано по дате):
{json.dumps(key_exercises, ensure_ascii=False, indent=2)}

ДИНАМИКА ВЕСА ТЕЛА (последние 30 записей):
{json.dumps([dict(r) for r in body_weight], ensure_ascii=False, default=str)}

Дай анализ на русском языке в формате JSON:
{{
  "summary": "краткое резюме прогресса за весь период (2-3 предложения)",
  "top_achievements": ["достижение 1", "достижение 2", "достижение 3"],
  "plateau": [
    {{
      "exercise": "название",
      "last_weight": 0,
      "sessions_stuck": 0,
      "recommendation": "конкретная рекомендация"
    }}
  ],
  "progress": [
    {{
      "exercise": "название",
      "start_1rm": 0,
      "current_1rm": 0,
      "growth_percent": 0
    }}
  ],
  "weekly_recommendation": "что делать на следующей неделе",
  "recovery_note": "заметка о восстановлении если есть паттерны"
}}

Плато — если за последние 3+ сессии 1RM не вырос более чем на 2.5%.
Отвечай ТОЛЬКО валидным JSON без markdown, без комментариев, без ```json."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "max_tokens": 1500,
                    "temperature": 0.3,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Ты спортивный аналитик. Отвечай только валидным JSON без markdown."
                        },
                        {"role": "user", "content": prompt}
                    ]
                }
            )
            resp.raise_for_status()
            result = resp.json()
            text = result["choices"][0]["message"]["content"].strip()
            # Убираем markdown если модель всё же добавила
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"AI returned invalid JSON: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"AI error: {str(e)}")


@app.get("/api/ai/day-plan")
async def ai_day_plan(user_id: int = Depends(authenticated_user)):
    """Собирает практичный план дня: задачи, питание и тренировочный фокус."""
    await ensure_user_settings(user_id)
    if not GROQ_API_KEY:
        raise HTTPException(503, "AI not configured")

    today = datetime.now(TZ).date()
    async with pool.acquire() as conn:
        settings = await conn.fetchrow("SELECT * FROM user_settings WHERE user_id=$1", user_id)
        tasks = await conn.fetch(
            """SELECT text, prio, dl, cat, done, start_time, duration_minutes, repeat_rule
               FROM tasks WHERE user_id=$1 AND done=FALSE AND (dl IS NULL OR dl <= $2)
               ORDER BY dl NULLS LAST, start_time NULLS LAST, id DESC LIMIT 12""",
            user_id, today
        )
        workouts = await conn.fetch(
            """SELECT date, muscle, exercise, sets, reps, weight
               FROM workouts WHERE user_id=$1 ORDER BY date DESC, id DESC LIMIT 40""",
            user_id
        )
        food = await conn.fetch(
            """SELECT date, meal_type, food_name, serving_desc, calories, protein, fat, carbs
               FROM food_log WHERE user_id=$1 ORDER BY date DESC, created_at DESC LIMIT 40""",
            user_id
        )

    profile = {}
    if settings:
        profile = {
            "goal": settings["goal"],
            "age": settings["age"],
            "height": float(settings["height"]) if settings["height"] is not None else None,
            "weight": float(settings["weight"]) if settings["weight"] is not None else None,
            "target_weight": float(settings["target_weight"]) if settings["target_weight"] is not None else None,
            "activity": settings["activity"],
            "calorie_goal": settings["calorie_goal"],
            "workout_groups": settings["workout_groups"] or DEFAULT_WORKOUT_GROUPS,
            "active_workout_program": settings["active_workout_program"],
        }

    prompt = f"""Ты продуктовый ИИ-планировщик внутри фитнес-планера.
Собери реалистичный план на сегодня на русском языке. Учитывай цель, невыполненные задачи, историю питания и тренировок.

ПРОФИЛЬ:
{json.dumps(profile, ensure_ascii=False, default=str)}

НЕЗАВЕРШЕННЫЕ ЗАДАЧИ:
{json.dumps([dict(r) for r in tasks], ensure_ascii=False, default=str)}

ПОСЛЕДНИЕ ТРЕНИРОВКИ:
{json.dumps([dict(r) for r in workouts], ensure_ascii=False, default=str)}

ПОСЛЕДНЕЕ ПИТАНИЕ:
{json.dumps([dict(r) for r in food], ensure_ascii=False, default=str)}

Верни ТОЛЬКО валидный JSON без markdown:
{{
  "title": "короткое название плана",
  "summary": "1-2 предложения, почему такой план",
  "tasks": [
    {{"text":"конкретная задача", "prio":"h|m|l", "cat":"Фокус|Питание|Тренировка|Восстановление|Работа|Личное", "duration_minutes":30}}
  ],
  "nutrition": [
    {{"meal":"завтрак|обед|ужин|перекус", "idea":"что съесть/подготовить", "reason":"зачем"}}
  ],
  "workout": {{
    "recommended": true,
    "group": "название группы или отдых",
    "focus": "цель тренировки или восстановления",
    "exercises": ["упражнение 1", "упражнение 2", "упражнение 3"]
  }},
  "recovery": ["короткая рекомендация 1", "короткая рекомендация 2"]
}}

Ограничения: tasks максимум 5, nutrition максимум 4, exercises максимум 5. Не назначай тяжелую тренировку, если по истории она была вчера или сегодня. Если данных мало, сделай мягкий базовый план."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "max_tokens": 1400,
                    "temperature": 0.35,
                    "messages": [
                        {"role": "system", "content": "Ты аккуратный ИИ-планировщик. Отвечай только валидным JSON без markdown."},
                        {"role": "user", "content": prompt}
                    ]
                }
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            plan = json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"AI returned invalid JSON: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"AI error: {str(e)}")

    plan["generated_at"] = datetime.now(TZ).isoformat()
    return plan


@app.get("/api/ai/exercise-advice")
async def ai_exercise_advice(
    exercise: str,
    muscle: Optional[str] = None,
    user_id: int = Depends(authenticated_user)
):
    """Советует, какие упражнения поставить в комплекс вокруг выбранного движения."""
    await ensure_user_settings(user_id)
    exercise = (exercise or "").strip()[:120]
    muscle = (muscle or "").strip()[:120]
    if not exercise:
        raise HTTPException(422, "exercise is required")
    if not GROQ_API_KEY:
        raise HTTPException(503, "AI not configured")

    async with pool.acquire() as conn:
        settings = await conn.fetchrow("SELECT workout_groups, goal FROM user_settings WHERE user_id=$1", user_id)
        selected_history = await conn.fetch(
            """SELECT date, muscle, exercise, sets, reps, weight,
                      ROUND(weight * (1 + reps::numeric/30), 1) as orm
               FROM workouts
               WHERE user_id=$1 AND lower(exercise)=lower($2)
               ORDER BY date DESC, id DESC LIMIT 12""",
            user_id, exercise
        )
        recent_workouts = await conn.fetch(
            """SELECT date, muscle, exercise, sets, reps, weight
               FROM workouts WHERE user_id=$1 ORDER BY date DESC, id DESC LIMIT 30""",
            user_id
        )

    workout_groups = DEFAULT_WORKOUT_GROUPS
    if settings and settings["workout_groups"]:
        workout_groups = settings["workout_groups"]

    prompt = f"""Ты тренер внутри фитнес-планера. Пользователь выбрал упражнение: {exercise}.
Текущая группа/сплит: {muscle or "не указана"}.
Цель пользователя: {settings["goal"] if settings else "maintain"}.

ГРУППЫ ПОЛЬЗОВАТЕЛЯ:
{json.dumps(workout_groups, ensure_ascii=False, default=str)}

ИСТОРИЯ ЭТОГО УПРАЖНЕНИЯ:
{json.dumps([dict(r) for r in selected_history], ensure_ascii=False, default=str)}

ПОСЛЕДНИЕ ТРЕНИРОВКИ:
{json.dumps([dict(r) for r in recent_workouts], ensure_ascii=False, default=str)}

Верни ТОЛЬКО валидный JSON без markdown:
{{
  "summary": "1-2 предложения: как лучше использовать упражнение в комплексе",
  "companion_exercises": ["упражнение 1", "упражнение 2", "упражнение 3", "упражнение 4"],
  "sets_reps": "короткая схема подходов и повторений",
  "progression": "как прогрессировать в следующих тренировках",
  "caution": "короткое предупреждение по перегрузке/технике"
}}

Ограничения: не советуй больше 5 упражнений. Не ставь в один комплекс слишком много тяжелых базовых движений. Учитывай, если выбранное упражнение уже тяжелое."""

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "max_tokens": 900,
                    "temperature": 0.35,
                    "messages": [
                        {"role": "system", "content": "Ты аккуратный силовой тренер. Отвечай только валидным JSON без markdown."},
                        {"role": "user", "content": prompt}
                    ]
                }
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            advice = json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"AI returned invalid JSON: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"AI error: {str(e)}")

    advice["exercise"] = exercise
    advice["generated_at"] = datetime.now(TZ).isoformat()
    return advice


# ════════════════════════════════════════════════════════════════
# EXPORT
# ════════════════════════════════════════════════════════════════
@app.get("/api/export")
async def export_all(user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    async with pool.acquire() as conn:
        workouts = await conn.fetch("SELECT date, muscle, exercise, sets, reps, weight, rpe, note FROM workouts WHERE user_id=$1", user_id)
        tasks = await conn.fetch("SELECT id, text, prio, dl, cat, done, start_time, duration_minutes, repeat_rule FROM tasks WHERE user_id=$1", user_id)
        task_templates = await conn.fetch("SELECT client_id, name, tasks FROM task_templates WHERE user_id=$1", user_id)
        supps = await conn.fetch("SELECT name, emoji, dose, times FROM supplements WHERE user_id=$1", user_id)
        checks = await conn.fetch("SELECT date, supp_name, time_slot, checked FROM supplement_checks WHERE user_id=$1", user_id)
        weight = await conn.fetch("SELECT date, weight FROM body_weight WHERE user_id=$1", user_id)
        cal = await conn.fetch("SELECT date, calories FROM body_calories WHERE user_id=$1", user_id)
        measures = await conn.fetch("SELECT date, weight, height, chest, waist, hips, fat, muscle FROM body_measures WHERE user_id=$1", user_id)
        food = await conn.fetch("SELECT client_id, date, meal_type, food_id, food_name, serving_desc, calories, protein, fat, carbs, amount FROM food_log WHERE user_id=$1", user_id)
        favorites = await conn.fetch("SELECT client_id, food_id, food_name, serving_desc, calories, protein, fat, carbs, amount, meal_type FROM food_favorites WHERE user_id=$1", user_id)
        settings = await conn.fetchrow("SELECT * FROM user_settings WHERE user_id=$1", user_id)

    profile_keys = ["name", "goal", "sex", "age", "height", "weight", "target_weight",
                    "activity", "calorie_goal", "onboarding_completed", "workout_groups",
                    "active_workout_program", "updated_at"]
    notification_keys = ["notif_morning", "notif_morning_on", "notif_workout", "notif_workout_on",
                         "notif_evening", "notif_evening_on", "notif_tasks", "notif_tasks_on",
                         "notif_weekly", "notif_weekly_on", "timezone", "updated_at"]

    return {
        "exported_at": str(date.today()),
        "user_id": user_id,
        "workouts": [dict(r) for r in workouts],
        "tasks": [dict(r) for r in tasks],
        "task_templates": [dict(r) | {"tasks": parse_times(r["tasks"])} for r in task_templates],
        "supplements": [dict(r) | {"times": parse_times(r["times"])} for r in supps],
        "supplement_checks": [dict(r) for r in checks],
        "body_weight": [dict(r) for r in weight],
        "body_calories": [dict(r) for r in cal],
        "body_measures": [dict(r) for r in measures],
        "food_log": [dict(r) for r in food],
        "food_favorites": [dict(r) for r in favorites],
        "profile": {key: settings[key] for key in profile_keys} if settings else {},
        "notifications": {key: settings[key] for key in notification_keys} if settings else {},
    }


class FeedbackIn(BaseModel):
    text: str = Field(min_length=2, max_length=2000)


@app.post("/api/feedback")
async def send_feedback(f: FeedbackIn, user_id: int = Depends(authenticated_user)):
    await ensure_user_settings(user_id)
    text = f.text.strip()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO feedback (user_id, text) VALUES ($1,$2)", user_id, text)
    if OWNER_USER_ID:
        await send_telegram(OWNER_USER_ID, "💬 <b>Отзыв о планировщике</b>\n\n" + html.escape(text[:1500]))
    return {"status": "ok"}


async def product_stats() -> dict:
    today = datetime.now(TZ).date()
    async with pool.acquire() as conn:
        firsts = await conn.fetch("SELECT user_id, MIN(day) AS first_day FROM user_activity_days GROUP BY user_id")
        days = await conn.fetch("SELECT user_id, day FROM user_activity_days WHERE day >= $1", today - timedelta(days=120))
        usage = {}
        for table in ("tasks", "workouts", "food_log", "body_weight", "supplement_checks"):
            usage[table] = await conn.fetchval(f"SELECT COUNT(DISTINCT user_id) FROM {table}")
        feedback_count = await conn.fetchval("SELECT COUNT(*) FROM feedback")
    first_day = {r["user_id"]: r["first_day"] for r in firsts}
    active = {}
    for r in days:
        active.setdefault(r["user_id"], set()).add(r["day"])

    def active_between(days_set, start, end):
        return any(start <= d <= end for d in days_set)

    def retention(offset_from: int, offset_to: int) -> dict:
        # Доля пользователей, вернувшихся в окне [first+from, first+to]; только созревшие когорты.
        eligible = [u for u, f in first_day.items() if f + timedelta(days=offset_to) <= today]
        returned = [u for u in eligible if active_between(active.get(u, set()),
                    first_day[u] + timedelta(days=offset_from), first_day[u] + timedelta(days=offset_to))]
        return {"eligible": len(eligible), "returned": len(returned),
                "rate": round(len(returned) / len(eligible), 3) if eligible else None}

    def active_last(n):
        return sum(1 for s in active.values() if any(d > today - timedelta(days=n) for d in s))

    return {
        "date": today.isoformat(),
        "users_total": len(first_day),
        "new_7d": sum(1 for f in first_day.values() if f > today - timedelta(days=7)),
        "active_1d": active_last(1), "active_7d": active_last(7), "active_30d": active_last(30),
        "retention_d1": retention(1, 1),
        "retention_d7": retention(7, 13),
        "retention_d30": retention(30, 59),
        "feature_users": usage,
        "feedback_total": feedback_count,
    }


@app.get("/api/admin/stats")
async def admin_stats(user_id: int = Depends(authenticated_user)):
    if not OWNER_USER_ID or user_id != OWNER_USER_ID:
        raise HTTPException(403, "Only the product owner can see stats")
    return await product_stats()


def format_stats(st: dict) -> str:
    def pct(r):
        return "—" if r["rate"] is None else f"{round(r['rate'] * 100)}% ({r['returned']}/{r['eligible']})"
    fu = st["feature_users"]
    return (
        "📊 <b>Планировщик: неделя</b>\n\n"
        f"Пользователей: {st['users_total']} (новых за 7 дн.: {st['new_7d']})\n"
        f"Активных: день {st['active_1d']} · неделя {st['active_7d']} · месяц {st['active_30d']}\n"
        f"Удержание D1: {pct(st['retention_d1'])}\nD7: {pct(st['retention_d7'])}\nD30: {pct(st['retention_d30'])}\n\n"
        f"Пользуются: тренировки {fu['workouts']}, питание {fu['food_log']}, задачи {fu['tasks']}, "
        f"вес {fu['body_weight']}, добавки {fu['supplement_checks']}\n"
        f"Отзывов всего: {st['feedback_total']}"
    )


async def send_owner_digest(now_utc: Optional[datetime] = None):
    """Понедельник 10:00 МСК: сводка метрик владельцу (один раз в неделю)."""
    if not OWNER_USER_ID:
        return
    now = (now_utc or datetime.now(ZoneInfo("UTC"))).astimezone(TZ)
    if now.weekday() != 0 or not scheduled_due("10:00", now, 30):
        return
    if await claim_notification(OWNER_USER_ID, "owner_digest", now.date().isoformat()):
        await send_telegram(OWNER_USER_ID, format_stats(await product_stats()))


USER_DATA_TABLES = (
    "workouts", "tasks", "task_templates", "supplements", "supplement_checks",
    "body_weight", "body_calories", "body_measures", "food_log", "food_favorites",
    "notification_deliveries", "user_activity_days", "feedback", "user_settings", "users",
)


@app.delete("/api/account")
async def delete_account(
    confirm: str = Header("", alias="X-Confirm-Delete"),
    user_id: int = Depends(authenticated_user),
):
    """Полное удаление всех данных пользователя (право на удаление ПДн)."""
    if confirm != "DELETE":
        raise HTTPException(400, "Send header X-Confirm-Delete: DELETE to confirm")
    deleted = {}
    async with pool.acquire() as conn:
        async with conn.transaction():
            for table in USER_DATA_TABLES:
                result = await conn.execute(f"DELETE FROM {table} WHERE user_id=$1", user_id)
                deleted[table] = int(result.split()[-1])
    logging.info("Account deleted: user_id=%s rows=%s", user_id, sum(deleted.values()))
    return {"status": "deleted", "rows": deleted}


@app.get("/")
async def root():
    return {"status": "ok", "service": "planner-api"}
