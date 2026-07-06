"""
FastAPI backend для Telegram Mini App "Мой планировщик"
"""

import os
import json
import httpx
import asyncio
from datetime import date, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TZ = ZoneInfo("Europe/Moscow")

pool: Optional[asyncpg.Pool] = None
import logging


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
    # Запускаем планировщик уведомлений
    task = asyncio.create_task(notification_scheduler())
    yield
    task.cancel()
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

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                notif_morning TEXT DEFAULT '08:00',
                notif_morning_on BOOLEAN DEFAULT TRUE,
                notif_workout TEXT DEFAULT '10:00',
                notif_workout_on BOOLEAN DEFAULT TRUE,
                notif_evening TEXT DEFAULT '21:00',
                notif_evening_on BOOLEAN DEFAULT TRUE,
                notif_weekly_on BOOLEAN DEFAULT TRUE,
                timezone TEXT DEFAULT 'Europe/Moscow',
                updated_at TIMESTAMPTZ DEFAULT now()
            );
        """)


async def ensure_user_settings(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_settings (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            user_id
        )


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


async def notification_scheduler():
    """Планировщик — каждую минуту проверяет нужно ли слать уведомления."""
    logging.info("Notification scheduler started")
    while True:
        try:
            await asyncio.sleep(60)  # проверяем каждую минуту
            now = datetime.now(TZ)
            current_time = now.strftime("%H:%M")
            current_weekday = now.weekday()  # 0=Пн, 6=Вс
            
            async with pool.acquire() as conn:
                users = await conn.fetch("SELECT * FROM user_settings")
            
            for u in users:
                uid = u["user_id"]
                
                # Утренние добавки
                if u["notif_morning_on"] and u["notif_morning"] == current_time:
                    async with pool.acquire() as conn:
                        supps = await conn.fetch(
                            "SELECT name FROM supplements WHERE user_id=$1 AND times::text ILIKE '%утро%'", uid
                        )
                    if supps:
                        names = ", ".join(s["name"] for s in supps[:3])
                        await send_telegram(uid, "☀️ <b>Доброе утро!</b>\n\nНе забудь принять добавки: " + names)
                
                # Напоминание о тренировке
                if u["notif_workout_on"] and u["notif_workout"] == current_time:
                    # Проверяем был ли уже подход сегодня
                    async with pool.acquire() as conn:
                        today_workouts = await conn.fetchval(
                            "SELECT COUNT(*) FROM workouts WHERE user_id=$1 AND date=$2",
                            uid, now.date()
                        )
                    if today_workouts == 0:
                        days = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
                        splits = {0:"Ноги 🦵",1:"Грудь + Трицепс 💪",3:"Спина + Бицепс 🏋️",4:"Плечи 🎯"}
                        workout_today = splits.get(current_weekday, "Тренировка")
                        await send_telegram(uid, "💪 <b>Сегодня " + days[current_weekday] + "</b>\n\n" + workout_today + " — не пропусти!")
                
                # Вечерние добавки
                if u["notif_evening_on"] and u["notif_evening"] == current_time:
                    async with pool.acquire() as conn:
                        supps = await conn.fetch(
                            "SELECT name FROM supplements WHERE user_id=$1 AND times::text ILIKE '%вечер%'", uid
                        )
                    if supps:
                        names = ", ".join(s["name"] for s in supps[:3])
                        await send_telegram(uid, "🌙 <b>Вечерние добавки</b>\n\nПора принять: " + names + "\n\nХорошего сна!")
                
                # Еженедельный отчёт — воскресенье 19:00
                if u["notif_weekly_on"] and current_weekday == 6 and current_time == "19:00":
                    await send_weekly_report(uid)
        
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
# WORKOUTS
# ════════════════════════════════════════════════════════════════
@app.get("/api/workouts")
async def get_workouts(user_id: int = Header(..., alias="X-User-Id")):
    await ensure_user_settings(user_id)
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
async def search_food(q: str, user_id: int = Header(..., alias="X-User-Id")):
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
async def search_by_barcode(barcode: str, user_id: int = Header(..., alias="X-User-Id")):
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
