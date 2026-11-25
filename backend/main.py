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
from fastapi.responses import PlainTextResponse, FileResponse
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

USER_NAME = os.getenv("USER_NAME", "Amar Abdul Rehman").strip().upper()
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
# Executive 32-Character Receipt Formatter Engine
# ---------------------------------------------------------------------------


def center(text: str, width: int = WIDTH) -> str:
    return text.upper().center(width)


def divider(char: str = "=") -> str:
    return char * WIDTH


def section_header(title: str) -> str:
    """e.g. '--- [ DAILY HABITS ] -----------' exactly 32 chars"""
    tag = f" [ {title.upper()} ] "
    if len(tag) >= WIDTH:
        return tag[:WIDTH]
    left_pad = 3
    right_pad = WIDTH - len(tag) - left_pad
    return ("-" * left_pad) + tag + ("-" * right_pad)


def format_checkbox_item(text: str, width: int = WIDTH) -> list[str]:
    """
    Format actionable tasks with clean hanging indentation:
    [ ] Complete the ESP32 circuit
        design and PCB layout
    """
    clean = text.strip()
    if clean.startswith("[ ]") or clean.startswith("[]") or clean.startswith("[x]") or clean.startswith("[X]"):
        clean = clean.lstrip("[]xX ").strip()
    elif clean.startswith("•") or clean.startswith("-") or clean.startswith("*"):
        clean = clean.lstrip("•-* ").strip()

    prefix = "[ ] "
    indent = "    "
    wrapped = textwrap.fill(clean, width=width, initial_indent=prefix, subsequent_indent=indent)
    return wrapped.split("\n")


def format_body_line(text: str, width: int = WIDTH) -> list[str]:
    """Wrap body text preserving words up to 32 chars."""
    clean = text.strip()
    if not clean:
        return [""]
    if clean.startswith("---") or clean.startswith("==="):
        return [clean[:width]]
    return textwrap.wrap(clean, width=width)


def checkbox(text: str) -> str:
    """Helper for legacy callers returning a single checkbox string."""
    return f"[ ] {text.strip()}"


def format_receipt(
    title: str,
    body_lines: list[str],
    subtitle: str | None = None,
    badge: str = "OFFICIAL DISPATCH"
) -> str:
    """
    Builds a personalized, executive 32-character receipt with dividers,
    timestamps, and proper hanging-indented checkboxes.
    """
    lines: list[str] = [
        divider("="),
        center(USER_NAME),
        center(subtitle or "PERSONAL NEWSPAPER"),
        divider("="),
        center(title),
        divider("-"),
        f"{now_pkt().strftime('%a %b %d, %Y | %H:%M')}".center(WIDTH),
        divider("-"),
    ]

    for line in body_lines:
        sline = line.strip()
        if not sline:
            continue
        if sline.startswith("---") or sline.startswith("==="):
            lines.append(sline[:WIDTH])
        elif sline.startswith("[SECTION:"):
            sec = sline.replace("[SECTION:", "").replace("]", "").strip()
            lines.append(section_header(sec))
        elif sline.startswith("[ ]") or sline.startswith("•") or sline.startswith("- ") or sline.startswith("* "):
            lines.extend(format_checkbox_item(sline))
        else:
            lines.extend(format_body_line(sline))

    lines.append(divider("-"))
    lines.append(center(badge))
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


def get_system_context(t: datetime) -> str:
    return (
        f"You are the elite Executive Chief of Staff and Personal Editor for {USER_NAME}. "
        f"Current Date: {t.strftime('%Y-%m-%d')} ({t.strftime('%A')}). "
        f"Current Time: {t.strftime('%H:%M')} Asia/Karachi (PKT).\n\n"
        f"Your mission: Transform raw text and voice notes from {USER_NAME} into immaculate, "
        "ultra-professional, executive thermal receipt prints. Nothing must be missed or compromised.\n\n"
        "RULES FOR EXCELLENCE:\n"
        f"1. Personalized: Every printout is tailored specifically for {USER_NAME}.\n"
        "2. Completeness: Never drop details, deadlines, counts, or nuances. Structure messy thoughts into "
        "crisp, numbered or bulleted items with clear sections.\n"
        "3. Checkboxes: Every actionable task, shopping item, or checklist item MUST start with '[ ] '.\n"
        "4. 32-Column Thermal Formatting: Keep receipt_title ≤ 26 characters (UPPERCASE, e.g. 'EXECUTIVE DISPATCH', "
        "'GROCERY MANIFEST', 'PROJECT BRIEF'). Keep each item in 'receipt_lines' concise and punchy.\n"
        "5. Categorization:\n"
        "   • add_daily: Recurring daily habits -> populate 'items' with titles (≤26 chars).\n"
        "   • add_dated: Deadlines/tasks -> populate 'items' with title and 'due_date' (YYYY-MM-DD).\n"
        "   • print_now: Immediate physical printout -> populate 'receipt_title' and structured 'receipt_lines' (all starting with '[ ] ').\n"
        "   • schedule_print: Deferred printout -> calculate absolute Asia/Karachi ISO timestamp in 'schedule_iso', populate 'receipt_title' and 'receipt_lines'.\n"
        "   • list_tasks: Inquiries about existing habits/tasks.\n"
        f"6. Replies: Always address {USER_NAME} with utmost executive polish, confirming exactly what was done."
    )


async def classify_message(user_text: str) -> GeminiDecision:
    """Send user message to Gemini and get a structured action decision."""
    client = get_gemini_client()
    t = now_pkt()
    system_context = get_system_context(t)

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
    system_context = get_system_context(t)

    audio_part = genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[audio_part, f"Transcribe and classify this voice dispatch from {USER_NAME}."],
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

    body: list[str] = [
        f"Islamabad Weather: {weather}",
        "[SECTION:DAILY HABITS]",
    ]

    if habits:
        for h in habits:
            body.append(f"[ ] {h.title}")
    else:
        body.append("[ ] Morning 45m Exercise")
        body.append("[ ] Read 10 Pages & Set Goals")
        body.append("[ ] Cold Shower & Hydration")

    body.append("[SECTION:TODAY'S OBJECTIVES]")
    if tasks:
        for tk in tasks:
            body.append(f"[ ] {tk.title}")
    else:
        body.append("[ ] Review High-Priority Tasks")
        body.append("[ ] Complete Deep Work Sprint")

    receipt = format_receipt(
        "MORNING EDITION",
        body,
        subtitle="EXECUTIVE DAILY BRIEF",
        badge="OWN THE DAY • EXECUTE"
    )
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
            f"Window: {t.strftime('%H:%M')} -> {end.strftime('%H:%M PKT')}",
            "[SECTION:FOCUS PROTOCOL]",
            "[ ] Single high-value objective",
            "[ ] Phone on Do Not Disturb",
            "[ ] Close all unrelated tabs",
            "[ ] Hydrate & maintain posture",
            "[ ] 5-min post-sprint debrief",
        ]
        receipt = format_receipt(
            "DEEP WORK SPRINT",
            body,
            subtitle="FOCUS TICKET (45 MIN)",
            badge="ZERO DISTRACTIONS • EXECUTE"
        )

    elif zone == "door":
        body = [
            "[SECTION:DEPARTURE CHECKLIST]",
            "[ ] House & Car Keys",
            "[ ] Wallet, CNIC & Cards",
            "[ ] University Student ID",
            "[ ] Laptop & USB-C Charger",
            "[ ] Earbuds & Power Bank",
            "[ ] Review Today's Appointments",
        ]
        receipt = format_receipt(
            "POCKET CHECK",
            body,
            subtitle="DEPARTURE MANIFEST",
            badge="SAFE TRAVELS • CONQUER TODAY"
        )

    elif zone == "bedside":
        tomorrow = (t + timedelta(days=1)).strftime("%Y-%m-%d")
        with Session(engine) as s:
            tasks = s.execute(
                select(DatedTask).where(
                    DatedTask.completed == 0,
                    DatedTask.due_date == tomorrow,
                )
            ).scalars().all()
        body = [
            f"Target Date: {tomorrow}",
            "[SECTION:TOMORROW'S AGENDA]",
        ]
        if tasks:
            for tk in tasks:
                body.append(f"[ ] {tk.title}")
        else:
            body.append("[ ] No fixed tasks scheduled")
            body.append("[ ] Dedicate morning to core habits")

        body.append("[SECTION:NIGHTLY RECOVERY]")
        body.append("[ ] Set 06:30 AM Wakeup Alarm")
        body.append("[ ] Phone docked away from bed")
        body.append("[ ] Target 8h restorative sleep")

        receipt = format_receipt(
            "TOMORROW PREVIEW",
            body,
            subtitle="NIGHTLY BRIEFING",
            badge="REST DEEPLY • WIN TOMORROW"
        )

    else:
        raise HTTPException(status_code=404, detail=f"Unknown zone: {zone}")

    enqueue_receipt(receipt)
    return {"status": "queued", "zone": zone}


# ---------------------------------------------------------------------------
# Presence trigger
# ---------------------------------------------------------------------------


@app.post("/api/trigger/presence")
async def presence_trigger(x_api_key: str = Header(None), force: bool = Query(False)):
    global _last_presence_trigger
    require_api_key(x_api_key)

    t = now_pkt()
    hour = t.hour

    if not force:
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

    body = [
        f"Time Log: {t.strftime('%H:%M PKT')}",
        "[SECTION:REMAINING OBJECTIVES]",
    ]
    if tasks:
        for tk in tasks:
            body.append(f"[ ] {tk.title}")
    else:
        body.append("[ ] All dated tasks completed!")

    if habits:
        body.append("[SECTION:DAILY HABITS CHECK]")
        for h in habits:
            body.append(f"[ ] {h.title}")

    body.append("[SECTION:EVENING PROTOCOL]")
    body.append("[ ] Log daily accomplishments")
    body.append("[ ] Review tomorrow's priorities")
    body.append("[ ] Digital wind-down")

    receipt = format_receipt(
        "EVENING CHECK-IN",
        body,
        subtitle="PRESENCE DETECTED",
        badge="WRAP UP STRONG • REFLECT"
    )
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
# Interactive Demo & Web Simulator Endpoints
# ---------------------------------------------------------------------------
class SimulateWhatsAppRequest(BaseModel):
    text: str


@app.get("/")
@app.get("/demo")
async def demo_page():
    index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return PlainTextResponse("Demo page not found. Ensure backend/static/index.html exists.")


@app.post("/api/demo/morning")
async def trigger_morning_demo():
    await morning_newspaper()
    return {"status": "queued", "message": "Morning newspaper enqueued"}


@app.post("/api/demo/simulate-whatsapp")
async def simulate_whatsapp_demo(req: SimulateWhatsAppRequest):
    try:
        decision = await classify_message(req.text)
    except Exception:
        # Fallback for demo when GEMINI_API_KEY is not yet populated
        txt = req.text.strip()
        lower = txt.lower()
        if "print" in lower:
            clean = txt.replace("print", "").replace("Print", "").strip(": ")
            if "," in clean:
                raw_items = [item.strip() for item in clean.split(",") if item.strip()]
            elif "\n" in clean:
                raw_items = [item.strip() for item in clean.split("\n") if item.strip()]
            else:
                raw_items = [clean]

            lines = ["[SECTION:ACTION MANIFEST]"]
            for item in raw_items:
                lines.append(f"[ ] {item}")

            decision = GeminiDecision(
                action=ActionName.print_now,
                receipt_title="EXECUTIVE MANIFEST",
                receipt_lines=lines,
                reply=f"🖨️ Formatted & enqueued executive manifest with checkboxes for {USER_NAME}."
            )
        elif "habit" in lower:
            habit_title = txt.replace("add habit", "").replace("habit:", "").strip()[:26]
            h_title = habit_title or "Daily Habit"
            decision = GeminiDecision(
                action=ActionName.add_daily,
                items=[TaskItem(title=h_title)],
                reply=f"✅ Logged habit for {USER_NAME}: \"{h_title}\". It will automatically print in your 07:00 AM Morning Newspaper!"
            )
            execute_print_now(
                "HABIT REGISTERED",
                [f"New Target: {h_title}", divider("-"), "Enqueued into daily habits database."]
            )
        elif "list" in lower or "what" in lower:
            decision = GeminiDecision(
                action=ActionName.list_tasks,
                reply=execute_list_tasks()
            )
        elif "task" in lower or "due" in lower:
            task_title = txt.replace("add task", "").replace("task:", "").strip()[:26]
            t_title = task_title or "Dated Task"
            decision = GeminiDecision(
                action=ActionName.add_dated,
                items=[TaskItem(title=t_title, due_date=now_pkt().strftime("%Y-%m-%d"))],
                reply=f"📌 Logged dated task for {USER_NAME}: \"{t_title}\" (Due: {now_pkt().strftime('%Y-%m-%d')})"
            )
            execute_print_now(
                "TASK REGISTERED",
                [f"Task: {t_title}", f"Due:  {now_pkt().strftime('%Y-%m-%d')}", divider("-"), "Active in dated_tasks agenda."]
            )
        elif "remind" in lower or "schedule" in lower or "at " in lower:
            decision = GeminiDecision(
                action=ActionName.schedule_print,
                receipt_title="REMINDER DISPATCH",
                receipt_lines=[f"[ ] {txt[:32]}"],
                schedule_iso=now_pkt().isoformat(),
                reply=f"⏰ Scheduled high-priority reminder for {USER_NAME}: {txt[:32]}"
            )
            execute_print_now(
                "REMINDER DISPATCH",
                [f"[ ] {txt[:32]}", divider("-"), "Persisted in jobs.db scheduler."]
            )
        else:
            raw_lines = [line.strip() for line in txt.split("\n") if line.strip()] or [txt[:32]]
            formatted = ["[SECTION:NOTES & MEMO]"]
            for r in raw_lines:
                formatted.append(f"[ ] {r}")
            decision = GeminiDecision(
                action=ActionName.print_now,
                receipt_title="EXECUTIVE MEMO",
                receipt_lines=formatted,
                reply=f"🖨️ Formatted & enqueued executive memo for {USER_NAME}."
            )

    # Execute action
    if decision.action == ActionName.add_daily:
        execute_add_daily(decision.items)
    elif decision.action == ActionName.add_dated:
        execute_add_dated(decision.items)
    elif decision.action == ActionName.print_now:
        execute_print_now(decision.receipt_title, decision.receipt_lines)
    elif decision.action == ActionName.schedule_print:
        execute_print_now(decision.receipt_title, decision.receipt_lines)
    elif decision.action == ActionName.list_tasks:
        decision.reply = execute_list_tasks()

    return {"decision": decision.model_dump(), "reply": decision.reply}


@app.get("/api/demo/queue-status")
def get_queue_status():
    with Session(engine) as s:
        pending = s.execute(select(PrintQueue).where(PrintQueue.printed == 0)).scalars().all()
        recent = s.execute(select(PrintQueue).order_by(PrintQueue.id.desc()).limit(6)).scalars().all()
        habits = s.execute(select(Habit).where(Habit.active == 1)).scalars().all()
        tasks = s.execute(select(DatedTask).where(DatedTask.completed == 0)).scalars().all()
        return {
            "pending_count": len(pending),
            "recent_receipts": [{"id": r.id, "text": r.receipt_text, "printed": r.printed, "created_at": r.created_at} for r in recent],
            "habits": [h.title for h in habits],
            "tasks": [{"title": t.title, "due_date": t.due_date} for t in tasks],
            "server_time": now_pkt().strftime("%Y-%m-%d %H:%M:%S PKT")
        }


# ---------------------------------------------------------------------------
# Dev entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
