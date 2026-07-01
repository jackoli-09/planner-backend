"""
FastAPI backend для Telegram Mini App "Мой планировщик"
Хранит данные в PostgreSQL (Supabase), привязка к telegram_user_id.

Запуск локально:
    pip install -r requirements.txt
    uvicorn main:app --reload

Переменные окружения (.env):
    DATABASE_URL=postgresql://user:pass@host:port/dbname
"""

import os
import json
from datetime import date
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pool: Optional[asyncpg.Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await init_db()
    yield
    await pool.close()


app = FastAPI(title="Planner API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # для Mini App ограничь на свой vercel-домен в проде
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ════════════════════════════════════════════════════════════════
# DB SCHEMA
# ════════════════════════════════════════════════════════════════
async def init_db():
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS workouts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                date DATE NOT NULL,
                muscle TEXT NOT NULL,
                exercise TEXT NOT NULL,
                sets INT NOT NULL,
                reps INT NOT NULL,
                weight NUMERIC NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_workouts_user ON workouts(user_id);

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                prio TEXT NOT NULL,
                dl DATE,
                cat TEXT,
                done BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);

            CREATE TABLE IF NOT EXISTS supplements (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
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
        """)


# ════════════════════════════════════════════════════════════════
# AUTH HELPER — извлекаем user_id из заголовка (присылает фронт)
# ════════════════════════════════════════════════════════════════
def get_user_id(x_user_id: str = Header(...)) -> int:
    try:
        return int(x_user_id)
    except ValueError:
        raise HTTPException(400, "Invalid user id")


# ════════════════════════════════════════════════════════════════
# MODELS
# ════════════════════════════════════════════════════════════════
class WorkoutIn(BaseModel):
    date: date
    muscle: str
    exercise: str
    sets: int
    reps: int
    weight: float


class TaskIn(BaseModel):
    id: str
    text: str
    prio: str
    dl: Optional[date] = None
    cat: Optional[str] = None
    done: bool = False


class SupplementIn(BaseModel):
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


class BulkImportIn(BaseModel):
    workouts: list[WorkoutIn] = []


# ════════════════════════════════════════════════════════════════
# WORKOUTS
# ════════════════════════════════════════════════════════════════
@app.get("/api/workouts")
async def get_workouts(user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date, muscle, exercise, sets, reps, weight FROM workouts WHERE user_id=$1 ORDER BY date",
            user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/workouts")
async def add_workout(w: WorkoutIn, user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workouts (user_id, date, muscle, exercise, sets, reps, weight) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            user_id, w.date, w.muscle, w.exercise, w.sets, w.reps, w.weight
        )
    return {"status": "ok"}


@app.post("/api/workouts/bulk")
async def bulk_import_workouts(payload: BulkImportIn, user_id: int = Header(..., alias="X-User-Id")):
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
async def get_tasks(user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, text, prio, dl, cat, done FROM tasks WHERE user_id=$1 ORDER BY created_at DESC",
            user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/tasks")
async def upsert_task(t: TaskIn, user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO tasks (id, user_id, text, prio, dl, cat, done)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (id) DO UPDATE SET done=$7
        """, t.id, user_id, t.text, t.prio, t.dl, t.cat, t.done)
    return {"status": "ok"}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE id=$1 AND user_id=$2", task_id, user_id)
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════
# SUPPLEMENTS
# ════════════════════════════════════════════════════════════════
@app.get("/api/supplements")
async def get_supplements(user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, emoji, dose, times FROM supplements WHERE user_id=$1",
            user_id
        )
        return [dict(r) | {"times": json.loads(r["times"])} for r in rows]


@app.post("/api/supplements")
async def add_supplement(s: SupplementIn, user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO supplements (user_id, name, emoji, dose, times) VALUES ($1,$2,$3,$4,$5) RETURNING id",
            user_id, s.name, s.emoji, s.dose, json.dumps(s.times)
        )
    return {"status": "ok", "id": row["id"]}


@app.delete("/api/supplements/{supp_id}")
async def delete_supplement(supp_id: int, user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM supplements WHERE id=$1 AND user_id=$2", supp_id, user_id)
    return {"status": "ok"}


@app.get("/api/supplement_checks")
async def get_supp_checks(user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date, supp_name, time_slot, checked FROM supplement_checks WHERE user_id=$1",
            user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/supplement_checks")
async def toggle_supp_check(c: SuppCheckIn, user_id: int = Header(..., alias="X-User-Id")):
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
# BODY (weight + calories)
# ════════════════════════════════════════════════════════════════
@app.get("/api/body/weight")
async def get_body_weight(user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date, weight FROM body_weight WHERE user_id=$1 ORDER BY date", user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/body/weight")
async def upsert_body_weight(b: BodyWeightIn, user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO body_weight (user_id, date, weight) VALUES ($1,$2,$3)
            ON CONFLICT (user_id, date) DO UPDATE SET weight=$3
        """, user_id, b.date, b.weight)
    return {"status": "ok"}


@app.get("/api/body/calories")
async def get_body_calories(user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date, calories FROM body_calories WHERE user_id=$1 ORDER BY date", user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/body/calories")
async def upsert_body_calories(b: BodyCalIn, user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO body_calories (user_id, date, calories) VALUES ($1,$2,$3)
            ON CONFLICT (user_id, date) DO UPDATE SET calories=$3
        """, user_id, b.date, b.calories)
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════
# FULL EXPORT — для бэкапа
# ════════════════════════════════════════════════════════════════
@app.get("/api/export")
async def export_all(user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        workouts = await conn.fetch("SELECT date, muscle, exercise, sets, reps, weight FROM workouts WHERE user_id=$1", user_id)
        tasks = await conn.fetch("SELECT id, text, prio, dl, cat, done FROM tasks WHERE user_id=$1", user_id)
        supps = await conn.fetch("SELECT name, emoji, dose, times FROM supplements WHERE user_id=$1", user_id)
        checks = await conn.fetch("SELECT date, supp_name, time_slot, checked FROM supplement_checks WHERE user_id=$1", user_id)
        weight = await conn.fetch("SELECT date, weight FROM body_weight WHERE user_id=$1", user_id)
        cal = await conn.fetch("SELECT date, calories FROM body_calories WHERE user_id=$1", user_id)

    return {
        "exported_at": str(date.today()),
        "user_id": user_id,
        "workouts": [dict(r) for r in workouts],
        "tasks": [dict(r) for r in tasks],
        "supplements": [dict(r) | {"times": json.loads(r["times"])} for r in supps],
        "supplement_checks": [dict(r) for r in checks],
        "body_weight": [dict(r) for r in weight],
        "body_calories": [dict(r) for r in cal],
    }


@app.get("/")
async def root():
    return {"status": "ok", "service": "planner-api"}
