"""
FastAPI backend для Telegram Mini App "Мой планировщик"
"""

import os
import json
import httpx
from datetime import date
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "")
FATSECRET_CLIENT_ID = os.environ.get("FATSECRET_CLIENT_ID", "")
FATSECRET_CLIENT_SECRET = os.environ.get("FATSECRET_CLIENT_SECRET", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

pool: Optional[asyncpg.Pool] = None
fs_token_cache: dict = {}  # {"token": "...", "expires_at": timestamp}


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

class FoodLogIn(BaseModel):
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

class BulkImportIn(BaseModel):
    workouts: list[WorkoutIn] = []


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
    allow_origins=["*"],
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

            CREATE TABLE IF NOT EXISTS food_log (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
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
        """)


# ════════════════════════════════════════════════════════════════
# WORKOUTS
# ════════════════════════════════════════════════════════════════
@app.get("/api/workouts")
async def get_workouts(user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, date, muscle, exercise, sets, reps, weight FROM workouts WHERE user_id=$1 ORDER BY date DESC, id DESC",
            user_id
        )
        return [dict(r) for r in rows]


@app.post("/api/workouts")
async def add_workout(w: "WorkoutIn", user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO workouts (user_id, date, muscle, exercise, sets, reps, weight) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
            user_id, w.date, w.muscle, w.exercise, w.sets, w.reps, w.weight
        )
    return {"status": "ok", "id": row["id"]}


@app.delete("/api/workouts/{workout_id}")
async def delete_workout(workout_id: int, user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM workouts WHERE id=$1 AND user_id=$2",
            workout_id, user_id
        )
    if result == "DELETE 0":
        raise HTTPException(404, "Workout not found")
    return {"status": "ok"}


@app.post("/api/workouts/bulk")
async def bulk_import_workouts(payload: "BulkImportIn", user_id: int = Header(..., alias="X-User-Id")):
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
async def upsert_task(t: "TaskIn", user_id: int = Header(..., alias="X-User-Id")):
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
async def add_supplement(s: "SupplementIn", user_id: int = Header(..., alias="X-User-Id")):
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
async def toggle_supp_check(c: "SuppCheckIn", user_id: int = Header(..., alias="X-User-Id")):
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
async def upsert_body_weight(b: "BodyWeightIn", user_id: int = Header(..., alias="X-User-Id")):
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
async def upsert_body_calories(b: "BodyCalIn", user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO body_calories (user_id, date, calories) VALUES ($1,$2,$3)
            ON CONFLICT (user_id, date) DO UPDATE SET calories=$3
        """, user_id, b.date, b.calories)
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════
# FATSECRET — поиск и лог питания
# ════════════════════════════════════════════════════════════════
async def get_fatsecret_token() -> str:
    """Получаем OAuth2 токен FatSecret (client_credentials). Кешируем."""
    import time
    now = time.time()
    if fs_token_cache.get("token") and fs_token_cache.get("expires_at", 0) > now + 60:
        return fs_token_cache["token"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth.fatsecret.com/connect/token",
            data={
                "grant_type": "client_credentials",
                "scope": "basic",
            },
            auth=(FATSECRET_CLIENT_ID, FATSECRET_CLIENT_SECRET),
        )
        resp.raise_for_status()
        data = resp.json()
        fs_token_cache["token"] = data["access_token"]
        fs_token_cache["expires_at"] = now + data.get("expires_in", 86400)
        return fs_token_cache["token"]


@app.get("/api/food/search")
async def search_food(q: str, user_id: int = Header(..., alias="X-User-Id")):
    """Поиск еды через FatSecret API."""
    if not FATSECRET_CLIENT_ID:
        raise HTTPException(503, "FatSecret not configured")
    try:
        token = await get_fatsecret_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://platform.fatsecret.com/rest/server.api",
                params={
                    "method": "foods.search",
                    "search_expression": q,
                    "format": "json",
                    "max_results": 10,
                    "language": "ru",
                    "region": "RU",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        # Обрабатываем разные форматы ответа
        foods_data = data.get("foods", {})
        if not foods_data:
            return {"results": []}
        
        foods = foods_data.get("food", [])
        if isinstance(foods, dict):
            foods = [foods]
        if not foods:
            return {"results": []}

        results = []
        for item in foods:
            desc = item.get("food_description", "")
            nutrition = {"calories": 0, "fat": 0, "carbs": 0, "protein": 0}
            
            # Парсим "Per 100g - Calories: 89kcal | Fat: 0.33g | Carbs: 23g | Protein: 1.09g"
            try:
                for part in desc.split("|"):
                    part = part.strip()
                    if "Calories:" in part:
                        val = part.split("Calories:")[-1].replace("kcal", "").strip()
                        nutrition["calories"] = round(float(val), 1)
                    elif "Fat:" in part:
                        val = part.split("Fat:")[-1].replace("g", "").strip()
                        nutrition["fat"] = round(float(val), 1)
                    elif "Carbs:" in part:
                        val = part.split("Carbs:")[-1].replace("g", "").strip()
                        nutrition["carbs"] = round(float(val), 1)
                    elif "Protein:" in part:
                        val = part.split("Protein:")[-1].replace("g", "").strip()
                        nutrition["protein"] = round(float(val), 1)
            except (ValueError, IndexError):
                pass

            results.append({
                "food_id": str(item.get("food_id", "")),
                "food_name": item.get("food_name", ""),
                "food_type": item.get("food_type", ""),
                "brand_name": item.get("brand_name", ""),
                "serving_desc": desc,
                **nutrition,
            })

        return {"results": results}
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"FatSecret API error: {e.response.text}")
    except Exception as e:
        raise HTTPException(500, f"FatSecret error: {str(e)}")


@app.get("/api/food/barcode")
async def search_by_barcode(barcode: str, user_id: int = Header(..., alias="X-User-Id")):
    """Поиск еды по штрихкоду через FatSecret API."""
    if not FATSECRET_CLIENT_ID:
        raise HTTPException(503, "FatSecret not configured")
    try:
        token = await get_fatsecret_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://platform.fatsecret.com/rest/server.api",
                params={
                    "method": "food.find_id_for_barcode",
                    "barcode": barcode,
                    "format": "json",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()
        
        food_id = data.get("food_id", {}).get("value")
        if not food_id:
            return {"results": []}
        
        # Получаем детали продукта
        async with httpx.AsyncClient(timeout=15) as client:
            resp2 = await client.get(
                "https://platform.fatsecret.com/rest/server.api",
                params={
                    "method": "food.get.v4",
                    "food_id": food_id,
                    "format": "json",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            resp2.raise_for_status()
            food_data = resp2.json()
        
        food = food_data.get("food", {})
        servings = food.get("servings", {}).get("serving", [])
        if isinstance(servings, dict):
            servings = [servings]
        
        # Берём первый вариант порции (обычно 100г)
        s = servings[0] if servings else {}
        
        return {"results": [{
            "food_id": str(food_id),
            "food_name": food.get("food_name", ""),
            "brand_name": food.get("brand_name", ""),
            "serving_desc": s.get("serving_description", "100г"),
            "calories": round(float(s.get("calories", 0)), 1),
            "protein": round(float(s.get("protein", 0)), 1),
            "fat": round(float(s.get("fat", 0)), 1),
            "carbs": round(float(s.get("carbohydrate", 0)), 1),
        }]}
    except Exception as e:
        raise HTTPException(500, f"Barcode error: {str(e)}")


@app.get("/api/food/log")
async def get_food_log(log_date: Optional[str] = None, user_id: int = Header(..., alias="X-User-Id")):
    """Получить лог питания за день."""
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
async def add_food_log(entry: "FoodLogIn", user_id: int = Header(..., alias="X-User-Id")):
    """Добавить еду в дневник."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO food_log (user_id, date, meal_type, food_id, food_name, serving_desc,
                                  calories, protein, fat, carbs, amount)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id
        """, user_id, entry.date, entry.meal_type, entry.food_id, entry.food_name,
            entry.serving_desc, entry.calories, entry.protein, entry.fat, entry.carbs, entry.amount)
    return {"status": "ok", "id": row["id"]}


@app.delete("/api/food/log/{entry_id}")
async def delete_food_log(entry_id: int, user_id: int = Header(..., alias="X-User-Id")):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM food_log WHERE id=$1 AND user_id=$2", entry_id, user_id)
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════
# ИИ АНАЛИЗ — прогресс и плато через Claude
# ════════════════════════════════════════════════════════════════
@app.get("/api/ai/analysis")
async def ai_analysis(user_id: int = Header(..., alias="X-User-Id")):
    """Анализ прогресса, плато и рекомендации через Groq (Llama 3.3 70B)."""
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


# ════════════════════════════════════════════════════════════════
# EXPORT
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
        food = await conn.fetch("SELECT date, meal_type, food_name, calories, protein, fat, carbs, amount FROM food_log WHERE user_id=$1", user_id)

    return {
        "exported_at": str(date.today()),
        "user_id": user_id,
        "workouts": [dict(r) for r in workouts],
        "tasks": [dict(r) for r in tasks],
        "supplements": [dict(r) | {"times": json.loads(r["times"])} for r in supps],
        "supplement_checks": [dict(r) for r in checks],
        "body_weight": [dict(r) for r in weight],
        "body_calories": [dict(r) for r in cal],
        "food_log": [dict(r) for r in food],
    }


@app.get("/")
async def root():
    return {"status": "ok", "service": "planner-api"}
