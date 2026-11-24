"""
WhatsApp-to-ESP32 Smart Thermal Receipt Printer — FastAPI Backend
=================================================================
Receives WhatsApp messages (text & voice), classifies them via Gemini,
manages habits / dated tasks / print queue in SQLite, schedules jobs with
APScheduler (persisted to jobs.db), and exposes endpoints for the ESP32
printer + NFC / presence triggers.
"""

from __future__ import annotations

import json
import os
import textwrap
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date as date_type
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

# --- Google GenAI ----------------------------------------------------------
from google import genai
from google.genai import types as genai_types

# --- SQLAlchemy ------------------------------------------------------------
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    create_engine,
    select,
    update,
)
from sqlalchemy.orm import declarative_base, Session

# --- APScheduler -----------------------------------------------------------
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "printer_secret_2026")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "1428007273647455")
ALLOWED_PHONE_NUMBER = os.getenv("ALLOWED_PHONE_NUMBER", "923315355121")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PRINTER_API_KEY = os.getenv("PRINTER_API_KEY", "esp32_local_secret_99")

WIDTH = 32  # characters per line on 58 mm thermal paper
PKT = ZoneInfo("Asia/Karachi")


def now_pkt() -> datetime:
    return datetime.now(PKT)


# ---------------------------------------------------------------------------
# Database (synchronous SQLite — perfectly fine for a single-user system)
# ---------------------------------------------------------------------------
DATABASE_URL = "sqlite:///printer.db"
engine = create_engine(DATABASE_URL, echo=False)
Base = declarative_base()


class Habit(Base):
    __tablename__ = "habits"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(26), nullable=False)
    active = Column(Integer, default=1)


class DatedTask(Base):
    __tablename__ = "dated_tasks"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(26), nullable=False)
    due_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    completed = Column(Integer, default=0)


class PrintQueue(Base):
    __tablename__ = "print_queue"
    id = Column(Integer, primary_key=True, autoincrement=True)
    receipt_text = Column(Text, nullable=False)
    created_at = Column(String(30), nullable=False)
    printed = Column(Integer, default=0)


Base.metadata.create_all(engine)

# ---------------------------------------------------------------------------
# APScheduler (persistent job store → jobs.db)
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url="sqlite:///jobs.db")},
    timezone=PKT,
)

# ---------------------------------------------------------------------------
# 32-char receipt formatter helpers
# ---------------------------------------------------------------------------


def center(text: str) -> str:
    return text.upper().center(WIDTH)


def divider(char: str = "=") -> str:
    return char * WIDTH


def wrap_line(text: str) -> str:
    """Wrap a single string to WIDTH, preserving leading checkbox."""
    return "\n".join(textwrap.wrap(text, WIDTH))


def checkbox(text: str) -> str:
    prefix = "[ ] "
    max_txt = WIDTH - len(prefix)
    return prefix + text[:max_txt]


def format_receipt(title: str, body_lines: list[str]) -> str:
    """Build a complete receipt string."""
    lines: list[str] = [
        divider("="),
        center(title),
        divider("="),
    ]
    for line in body_lines:
        lines.append(wrap_line(line))
    lines.append(divider("-"))
    stamp = now_pkt().strftime("%Y-%m-%d %H:%M PKT")
    lines.append(stamp.center(WIDTH))
    lines.append(divider("="))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Print queue helper
# ---------------------------------------------------------------------------


def enqueue_receipt(receipt_text: str) -> None:
    with Session(engine) as s:
        s.add(PrintQueue(
            receipt_text=receipt_text,
            created_at=now_pkt().isoformat(),
            printed=0,
        ))
        s.commit()


# ---------------------------------------------------------------------------
# WhatsApp Cloud API — send text reply
# ---------------------------------------------------------------------------
async def wa_reply(to: str, body: str) -> None:
    url = f"https://graph.facebook.com/v25.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body[:4096]},
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload, headers=headers, timeout=15)


# ---------------------------------------------------------------------------
# WhatsApp Cloud API — download media bytes
# ---------------------------------------------------------------------------
async def download_wa_media(media_id: str) -> bytes:
    """Two-step download: get URL, then fetch bytes."""
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient() as client:
        # Step 1 – get media URL
        r = await client.get(
            f"https://graph.facebook.com/v25.0/{media_id}",
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        media_url = r.json()["url"]
        # Step 2 – download bytes
        r2 = await client.get(media_url, headers=headers, timeout=30)
        r2.raise_for_status()
        return r2.content


# ---------------------------------------------------------------------------
# Gemini structured output schema
# ---------------------------------------------------------------------------

class ActionName(str, Enum):
    add_daily = "add_daily"
    add_dated = "add_dated"
    print_now = "print_now"
    schedule_print = "schedule_print"
    list_tasks = "list_tasks"


class TaskItem(BaseModel):
    title: str = Field(..., max_length=26)
    due_date: Optional[str] = Field(None, description="YYYY-MM-DD, only for add_dated")


class GeminiDecision(BaseModel):
    action: ActionName
    items: list[TaskItem] = Field(default_factory=list, description="Habit/task items")
    receipt_title: Optional[str] = Field(None, max_length=26, description="Title for print_now receipt")
    receipt_lines: list[str] = Field(default_factory=list, description="Body lines for print_now receipt")
    schedule_iso: Optional[str] = Field(None, description="ISO datetime in Asia/Karachi for schedule_print")
    reply: str = Field(..., description="Short confirmation message for the user")


# ---------------------------------------------------------------------------
# Gemini classifier
# ---------------------------------------------------------------------------
def get_gemini_client() -> genai.Client:
    api_key = GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not configured in your .env file or environment.")
    return genai.Client(api_key=api_key)


async def classify_message(user_text: str) -> GeminiDecision:
    """Send user message to Gemini and get a structured action decision."""
    client = get_gemini_client()
    t = now_pkt()
    system_context = (
        f"Current date: {t.strftime('%Y-%m-%d')} ({t.strftime('%A')}). "
        f"Current time: {t.strftime('%H:%M')} Asia/Karachi (PKT). "
        "You are an assistant that classifies WhatsApp messages into printer actions. "
        "Actions:\n"
        "• add_daily – add recurring daily habits (populate items with titles, no due_date)\n"
        "• add_dated – add tasks with specific dates (populate items with titles + due_date YYYY-MM-DD)\n"
        "• print_now – immediately print a receipt (populate receipt_title + receipt_lines)\n"
        "• schedule_print – print a receipt at a scheduled time (populate receipt_title + receipt_lines + schedule_iso as Asia/Karachi ISO datetime)\n"
        "• list_tasks – user wants to see their habits & tasks (no items needed)\n"
        "Keep titles ≤ 26 characters. Keep receipt_lines each ≤ 32 characters. "
        "If the user says 'in 45 mins' or 'at 7 PM', compute the absolute ISO datetime from the current time. "
        "Always include a short, friendly 'reply' confirming what you did."
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user_text,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_context,
            response_mime_type="application/json",
            response_schema=GeminiDecision,
        ),
    )
    data = json.loads(response.text)
    return GeminiDecision(**data)


async def classify_voice(audio_bytes: bytes, mime_type: str) -> GeminiDecision:
    """Send inline voice note to Gemini and get a structured action decision."""
    client = get_gemini_client()
    t = now_pkt()
    system_context = (
        f"Current date: {t.strftime('%Y-%m-%d')} ({t.strftime('%A')}). "
        f"Current time: {t.strftime('%H:%M')} Asia/Karachi (PKT). "
        "You are an assistant that classifies WhatsApp voice messages into printer actions. "
        "First transcribe the audio, then classify. "
        "Actions:\n"
        "• add_daily – add recurring daily habits (populate items with titles, no due_date)\n"
        "• add_dated – add tasks with specific dates (populate items with titles + due_date YYYY-MM-DD)\n"
        "• print_now – immediately print a receipt (populate receipt_title + receipt_lines)\n"
        "• schedule_print – print a receipt at a scheduled time (populate receipt_title + receipt_lines + schedule_iso as Asia/Karachi ISO datetime)\n"
        "• list_tasks – user wants to see their habits & tasks (no items needed)\n"
        "Keep titles ≤ 26 characters. Keep receipt_lines each ≤ 32 characters. "
        "If the user says 'in 45 mins' or 'at 7 PM', compute the absolute ISO datetime from the current time. "
        "Always include a short, friendly 'reply' confirming what you did."
    )

    audio_part = genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[audio_part, "Classify this voice message into a printer action."],
        config=genai_types.GenerateContentConfig(
            system_instruction=system_context,
            response_mime_type="application/json",
            response_schema=GeminiDecision,
        ),
    )
    data = json.loads(response.text)
    return GeminiDecision(**data)


# ---------------------------------------------------------------------------
# Action executors
# ---------------------------------------------------------------------------


def execute_add_daily(items: list[TaskItem]) -> str:
    with Session(engine) as s:
        for item in items:
            s.add(Habit(title=item.title[:26], active=1))
        s.commit()
    return f"Added {len(items)} habit(s)."


def execute_add_dated(items: list[TaskItem]) -> str:
    with Session(engine) as s:
        for item in items:
            s.add(DatedTask(
                title=item.title[:26],
                due_date=item.due_date or now_pkt().strftime("%Y-%m-%d"),
                completed=0,
            ))
        s.commit()
    return f"Added {len(items)} dated task(s)."


def execute_print_now(title: str, lines: list[str]) -> str:
    receipt = format_receipt(title or "RECEIPT", lines)
    enqueue_receipt(receipt)
    return "Receipt queued for printing."


def _scheduled_print_job(title: str, lines: list[str]) -> None:
    """Called by APScheduler at the scheduled time."""
    receipt = format_receipt(title or "SCHEDULED", lines)
    enqueue_receipt(receipt)


def execute_schedule_print(title: str, lines: list[str], iso_dt: str) -> str:
    run_at = datetime.fromisoformat(iso_dt)
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=PKT)
    scheduler.add_job(
        _scheduled_print_job,
        trigger="date",
        run_date=run_at,
        args=[title or "SCHEDULED", lines],
        id=f"sched_{now_pkt().timestamp()}",
        replace_existing=True,
    )
    return f"Print scheduled for {run_at.strftime('%Y-%m-%d %H:%M PKT')}."


def execute_list_tasks() -> str:
    with Session(engine) as s:
        habits = s.execute(select(Habit).where(Habit.active == 1)).scalars().all()
        tasks = s.execute(
            select(DatedTask).where(DatedTask.completed == 0)
        ).scalars().all()

    lines: list[str] = []
    if habits:
        lines.append("📋 *Daily Habits:*")
        for h in habits:
            lines.append(f"  • {h.title}")
    if tasks:
        lines.append("📌 *Dated Tasks:*")
        for t in tasks:
            lines.append(f"  • {t.title} ({t.due_date})")
    if not lines:
        lines.append("No habits or tasks yet. Send me some!")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Morning newspaper cron job
# ---------------------------------------------------------------------------


async def _fetch_weather() -> str:
    """Fetch Islamabad weather from Open-Meteo."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=33.6844&longitude=73.0479&current_weather=true"
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=10)
            r.raise_for_status()
            cw = r.json()["current_weather"]
            return f"{cw['temperature']}°C, wind {cw['windspeed']} km/h"
    except Exception:
        return "weather unavailable"


async def morning_newspaper() -> None:
    weather = await _fetch_weather()
    t = now_pkt()

    with Session(engine) as s:
        habits = s.execute(select(Habit).where(Habit.active == 1)).scalars().all()
        today_str = t.strftime("%Y-%m-%d")
        tasks = s.execute(
            select(DatedTask).where(
                DatedTask.completed == 0,
                DatedTask.due_date == today_str,
            )
        ).scalars().all()

    body: list[str] = []
    body.append(f"Date: {t.strftime('%A %b %d, %Y')}")
    body.append(f"Weather: {weather}")
    body.append(divider("-"))

    if habits:
        body.append(center("DAILY HABITS"))
        body.append(divider("-"))
        for h in habits:
            body.append(checkbox(h.title))

    if tasks:
        body.append(divider("-"))
        body.append(center("TODAY'S TASKS"))
        body.append(divider("-"))
        for tk in tasks:
            body.append(checkbox(tk.title))

    if not habits and not tasks:
        body.append("No habits or tasks today.")

    receipt = format_receipt("MORNING EDITION", body)
    enqueue_receipt(receipt)


# ---------------------------------------------------------------------------
# Presence cooldown (in-memory)
# ---------------------------------------------------------------------------
_last_presence_trigger: Optional[datetime] = None

# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Start scheduler + add morning cron if not already there -----------
    scheduler.start()
    if not scheduler.get_job("morning_newspaper"):
        scheduler.add_job(
            morning_newspaper,
            trigger="cron",
            hour=7,
            minute=0,
            id="morning_newspaper",
            replace_existing=True,
        )
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Thermal Printer Backend", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------
def require_api_key(x_api_key: str = Header(None)):
    if x_api_key != PRINTER_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


# ---------------------------------------------------------------------------
# WhatsApp Webhook
# ---------------------------------------------------------------------------


@app.get("/webhook/whatsapp")
async def wa_verify(
    request: Request,
):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook/whatsapp")
async def wa_incoming(request: Request):
    body = await request.json()

    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        message = value["messages"][0]
        sender = message["from"]
    except (KeyError, IndexError):
        # Status update or non-message webhook — acknowledge silently
        return {"status": "ok"}

    # Only process messages from the allowed number
    if sender != ALLOWED_PHONE_NUMBER:
        return {"status": "ignored"}

    # --- Extract user text (or transcribe voice) --------------------------
    try:
        if message["type"] == "text":
            user_text = message["text"]["body"]
            decision = await classify_message(user_text)

        elif message["type"] == "audio":
            media_id = message["audio"]["id"]
            mime = message["audio"].get("mime_type", "audio/ogg")
            audio_bytes = await download_wa_media(media_id)
            decision = await classify_voice(audio_bytes, mime)

        else:
            await wa_reply(sender, "Sorry, I only handle text and voice messages right now.")
            return {"status": "ok"}

        # --- Execute the classified action --------------------------------
        action = decision.action

        if action == ActionName.add_daily:
            execute_add_daily(decision.items)
        elif action == ActionName.add_dated:
            execute_add_dated(decision.items)
        elif action == ActionName.print_now:
            execute_print_now(decision.receipt_title, decision.receipt_lines)
        elif action == ActionName.schedule_print:
            execute_schedule_print(
                decision.receipt_title,
                decision.receipt_lines,
                decision.schedule_iso or now_pkt().isoformat(),
            )
        elif action == ActionName.list_tasks:
            task_list = execute_list_tasks()
            decision.reply = task_list  # override with the full listing

        await wa_reply(sender, decision.reply)

    except Exception as exc:
        await wa_reply(sender, f"⚠️ Error: {exc}")

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# NFC Trigger endpoints
# ---------------------------------------------------------------------------


@app.post("/api/trigger/nfc/{zone}")
async def nfc_trigger(zone: str, x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    t = now_pkt()

    if zone == "desk":
        end = t + timedelta(minutes=45)
        body = [
            f"Start: {t.strftime('%H:%M')}",
            f"End:   {end.strftime('%H:%M')}",
            divider("-"),
            "RULES:",
            "1. Phone on silent",
            "2. Single task only",
            "3. No social media",
            divider("-"),
            center("FOCUS MODE"),
        ]
        receipt = format_receipt("DEEP WORK SPRINT", body)

    elif zone == "door":
        body = [
            checkbox("Keys"),
            checkbox("Wallet"),
            checkbox("University ID"),
            checkbox("Laptop & Charger"),
            divider("-"),
            center("SAFE TRAVELS!"),
        ]
        receipt = format_receipt("POCKET CHECK", body)

    elif zone == "bedside":
        tomorrow = (t + timedelta(days=1)).strftime("%Y-%m-%d")
        with Session(engine) as s:
            tasks = s.execute(
                select(DatedTask).where(
                    DatedTask.completed == 0,
                    DatedTask.due_date == tomorrow,
                )
            ).scalars().all()
        body = [f"Date: {tomorrow}", divider("-")]
        if tasks:
            for tk in tasks:
                body.append(checkbox(tk.title))
        else:
            body.append("No tasks for tomorrow.")
            body.append("Rest well!")
        receipt = format_receipt("TOMORROW PREVIEW", body)

    else:
        raise HTTPException(status_code=404, detail=f"Unknown zone: {zone}")

    enqueue_receipt(receipt)
    return {"status": "queued", "zone": zone}


# ---------------------------------------------------------------------------
# Presence trigger
# ---------------------------------------------------------------------------


@app.post("/api/trigger/presence")
async def presence_trigger(x_api_key: str = Header(None)):
    global _last_presence_trigger
    require_api_key(x_api_key)

    t = now_pkt()
    hour = t.hour

    # Only active between 16:00 and 23:00 PKT
    if hour < 16 or hour >= 23:
        return {"status": "outside_window"}

    # 4-hour cooldown
    if _last_presence_trigger and (t - _last_presence_trigger) < timedelta(hours=4):
        return {"status": "cooldown"}

    _last_presence_trigger = t

    today_str = t.strftime("%Y-%m-%d")
    with Session(engine) as s:
        habits = s.execute(select(Habit).where(Habit.active == 1)).scalars().all()
        tasks = s.execute(
            select(DatedTask).where(
                DatedTask.completed == 0,
                DatedTask.due_date == today_str,
            )
        ).scalars().all()

    body = [f"Time: {t.strftime('%H:%M PKT')}", divider("-")]
    if habits:
        body.append(center("HABITS"))
        body.append(divider("-"))
        for h in habits:
            body.append(checkbox(h.title))
    if tasks:
        body.append(divider("-"))
        body.append(center("REMAINING TASKS"))
        body.append(divider("-"))
        for tk in tasks:
            body.append(checkbox(tk.title))
    if not habits and not tasks:
        body.append("All clear! Enjoy your evening.")

    receipt = format_receipt("EVENING CHECK-IN", body)
    enqueue_receipt(receipt)
    return {"status": "queued"}


# ---------------------------------------------------------------------------
# ESP32 print polling endpoint
# ---------------------------------------------------------------------------


@app.get("/api/next-print")
async def next_print(x_api_key: str = Header(None)):
    require_api_key(x_api_key)

    with Session(engine) as s:
        row = s.execute(
            select(PrintQueue)
            .where(PrintQueue.printed == 0)
            .order_by(PrintQueue.id.asc())
            .limit(1)
        ).scalar_one_or_none()

        if row is None:
            return PlainTextResponse(content="", status_code=200)

        receipt_text = row.receipt_text
        s.execute(
            update(PrintQueue)
            .where(PrintQueue.id == row.id)
            .values(printed=1)
        )
        s.commit()

    return PlainTextResponse(content=receipt_text, status_code=200)


# ---------------------------------------------------------------------------
# Dev entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
