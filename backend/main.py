"""
WhatsApp-to-ESP32 Smart Thermal Receipt Printer — FastAPI Backend
=================================================================
Receives WhatsApp messages (text & voice), classifies them via Gemini,
manages habits / dated tasks / print queue in SQLite, schedules jobs with
APScheduler (persisted to jobs.db), and exposes endpoints for the ESP32
printer + NFC / presence triggers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import textwrap
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

# --- Google GenAI ----------------------------------------------------------
from google import genai
from google.genai import types as genai_types

# --- SQLAlchemy ------------------------------------------------------------
from sqlalchemy import Column, Integer, String, Text, create_engine, select, update
from sqlalchemy.orm import Session, declarative_base

# --- APScheduler -----------------------------------------------------------
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

log = logging.getLogger("printer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


USER_NAME = os.getenv("USER_NAME", "Amar Abdul Rehman").strip().upper()
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
ALLOWED_PHONE_NUMBER = os.getenv("ALLOWED_PHONE_NUMBER", "").lstrip("+").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
PRINTER_API_KEY = os.getenv("PRINTER_API_KEY", "")

MORNING_HOUR = int(os.getenv("MORNING_HOUR", "7"))
MORNING_MINUTE = int(os.getenv("MORNING_MINUTE", "0"))
CITY_NAME = os.getenv("CITY_NAME", "Islamabad")
WEATHER_LAT = os.getenv("WEATHER_LAT", "33.6844")
WEATHER_LON = os.getenv("WEATHER_LON", "73.0479")

# The demo/simulator page and /api/demo/* endpoints are unauthenticated.
# Keep this OFF whenever the server is reachable from the internet.
DEMO_ENABLED = env_bool("DEMO_ENABLED", False)

WIDTH = 32  # characters per line on 58 mm thermal paper
TITLE_MAX = 60  # long titles simply wrap on the receipt
PKT = ZoneInfo("Asia/Karachi")

# A receipt handed to the printer is re-sent if it is not acknowledged
# within PRINT_LEASE_SECONDS, at most PRINT_MAX_ATTEMPTS times.
PRINT_LEASE_SECONDS = 90
PRINT_MAX_ATTEMPTS = 3

if not PRINTER_API_KEY:
    log.warning("PRINTER_API_KEY is empty — printer endpoints will reject every request.")
if not ALLOWED_PHONE_NUMBER:
    log.warning("ALLOWED_PHONE_NUMBER is empty — all WhatsApp messages will be ignored.")
if not WHATSAPP_APP_SECRET:
    log.warning("WHATSAPP_APP_SECRET is empty — webhook signatures are NOT verified.")


def now_pkt() -> datetime:
    return datetime.now(PKT)


# ---------------------------------------------------------------------------
# Database (synchronous SQLite — fine for a single-user system)
# ---------------------------------------------------------------------------
engine = create_engine(f"sqlite:///{os.path.join(BASE_DIR, 'printer.db')}", echo=False)
Base = declarative_base()


class Habit(Base):
    __tablename__ = "habits"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(60), nullable=False)
    active = Column(Integer, default=1)


class DatedTask(Base):
    __tablename__ = "dated_tasks"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(60), nullable=False)
    due_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    completed = Column(Integer, default=0)


class PrintQueue(Base):
    __tablename__ = "print_queue"
    id = Column(Integer, primary_key=True, autoincrement=True)
    receipt_text = Column(Text, nullable=False)
    created_at = Column(String(30), nullable=False)
    printed = Column(Integer, default=0)  # 0 = pending, 1 = printed, 3 = gave up


class ProcessedMessage(Base):
    """WhatsApp message ids already handled (Meta re-delivers webhooks)."""
    __tablename__ = "processed_messages"
    wa_id = Column(String(128), primary_key=True)
    received_at = Column(String(30), nullable=False)


Base.metadata.create_all(engine)

# ---------------------------------------------------------------------------
# APScheduler (persistent job store → jobs.db)
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{os.path.join(BASE_DIR, 'jobs.db')}")},
    timezone=PKT,
    # If the server was down/asleep when a job was due, still run it
    # (once) when it comes back, as long as it's within 6 hours.
    job_defaults={"coalesce": True, "misfire_grace_time": 6 * 3600},
)

# ---------------------------------------------------------------------------
# 32-Character Receipt Formatter
# ---------------------------------------------------------------------------
_ASCII_REPLACEMENTS = {
    "°": " ", "•": "*", "·": "*", "–": "-", "—": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", "→": "->", "←": "<-", "✓": "x", "✔": "x",
    "×": "x", "₨": "Rs", "€": "EUR", "£": "GBP",
}


def to_ascii(text: str) -> str:
    """The printer only understands plain ASCII — map or drop everything else."""
    for src, dst in _ASCII_REPLACEMENTS.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text


def center(text: str, width: int = WIDTH) -> str:
    return to_ascii(text).upper()[:width].center(width).rstrip()


def divider(char: str = "=") -> str:
    return char * WIDTH


def section_header(title: str) -> str:
    """e.g. '--- [ DAILY HABITS ] -----------' exactly 32 chars"""
    tag = f" [ {to_ascii(title).upper()} ] "
    if len(tag) >= WIDTH - 3:
        return ("-" + tag)[:WIDTH]
    return "---" + tag + "-" * (WIDTH - len(tag) - 3)


def format_checkbox_item(text: str, width: int = WIDTH) -> list[str]:
    """
    [ ] Complete the ESP32 circuit
        design and PCB layout
    """
    clean = text.strip()
    for prefix in ("[ ]", "[]", "[x]", "[X]"):
        if clean.startswith(prefix):
            clean = clean[len(prefix):].strip()
            break
    else:
        clean = clean.lstrip("*- ").strip()
    return textwrap.wrap(clean or "-", width=width, initial_indent="[ ] ", subsequent_indent="    ")


def format_body_line(text: str, width: int = WIDTH) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    if clean.startswith("---") or clean.startswith("==="):
        return [clean[:width]]
    return textwrap.wrap(clean, width=width)


def format_receipt(
    title: str,
    body_lines: list[str],
    subtitle: str | None = None,
    badge: str = "OFFICIAL DISPATCH",
) -> str:
    lines: list[str] = [
        divider("="),
        center(USER_NAME),
        center(subtitle or "PERSONAL NEWSPAPER"),
        divider("="),
        center(title),
        divider("-"),
        center(now_pkt().strftime("%a %b %d, %Y | %H:%M")),
        divider("-"),
    ]

    for raw in body_lines:
        line = to_ascii(raw).strip()
        if not line:
            continue
        if line.startswith("[SECTION:"):
            lines.append(section_header(line[len("[SECTION:"):].rstrip("]").strip()))
        elif line.startswith(("[ ]", "[]", "* ", "- ")):
            lines.extend(format_checkbox_item(line))
        else:
            lines.extend(format_body_line(line))

    lines += [divider("-"), center(badge), divider("=")]
    return "\n".join(l[:WIDTH] for l in lines)


# ---------------------------------------------------------------------------
# Print queue
# ---------------------------------------------------------------------------
# In-memory lease bookkeeping: receipt id -> (last handed out, attempts)
_print_leases: dict[int, tuple[datetime, int]] = {}


def enqueue_receipt(receipt_text: str) -> int:
    with Session(engine) as s:
        row = PrintQueue(receipt_text=receipt_text, created_at=now_pkt().isoformat(), printed=0)
        s.add(row)
        s.commit()
        return row.id


# ---------------------------------------------------------------------------
# WhatsApp Cloud API
# ---------------------------------------------------------------------------
async def wa_reply(to: str, body: str) -> None:
    url = f"https://graph.facebook.com/v25.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body[:4096]},
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code >= 400:
                log.error("WhatsApp reply failed (%s): %s", r.status_code, r.text[:300])
    except httpx.HTTPError as exc:
        log.error("WhatsApp reply failed: %s", exc)


async def download_wa_media(media_id: str) -> bytes:
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://graph.facebook.com/v25.0/{media_id}", headers=headers, timeout=15)
        r.raise_for_status()
        r2 = await client.get(r.json()["url"], headers=headers, timeout=30)
        r2.raise_for_status()
        return r2.content


def verify_meta_signature(raw_body: bytes, signature_header: str | None) -> bool:
    if not WHATSAPP_APP_SECRET:
        return True  # not configured (warned at startup)
    expected = "sha256=" + hmac.new(WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header or "", expected)


def mark_message_seen(wa_id: str) -> bool:
    """Returns True the first time an id is seen, False for re-deliveries."""
    with Session(engine) as s:
        if s.get(ProcessedMessage, wa_id) is not None:
            return False
        s.add(ProcessedMessage(wa_id=wa_id, received_at=now_pkt().isoformat()))
        s.commit()
        return True


# ---------------------------------------------------------------------------
# Gemini structured output schema
# ---------------------------------------------------------------------------
class ActionName(str, Enum):
    add_daily = "add_daily"
    add_dated = "add_dated"
    complete_task = "complete_task"
    remove_habit = "remove_habit"
    print_now = "print_now"
    print_morning = "print_morning"
    schedule_print = "schedule_print"
    list_tasks = "list_tasks"


class TaskItem(BaseModel):
    title: str
    due_date: Optional[str] = Field(None, description="YYYY-MM-DD, only for add_dated")


class GeminiDecision(BaseModel):
    action: ActionName
    items: list[TaskItem] = Field(default_factory=list, description="Habit/task items")
    receipt_title: Optional[str] = Field(None, description="Title for print_now / schedule_print receipt")
    receipt_lines: list[str] = Field(default_factory=list, description="Body lines for the receipt")
    schedule_iso: Optional[str] = Field(None, description="ISO datetime in Asia/Karachi for schedule_print")
    reply: str = Field(..., description="Short confirmation message for the user")


_gemini_client: genai.Client | None = None


def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not configured in backend/.env.")
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def get_system_context(t: datetime) -> str:
    return (
        f"You are the personal assistant behind {USER_NAME}'s thermal receipt printer. "
        f"Current date: {t.strftime('%Y-%m-%d')} ({t.strftime('%A')}). "
        f"Current time: {t.strftime('%H:%M')} Asia/Karachi (PKT).\n\n"
        "Turn each WhatsApp text or voice note into exactly ONE action:\n"
        "- add_daily: a recurring daily habit. Put each habit in 'items' (short, <= 60 chars).\n"
        "- add_dated: a task with a deadline. 'items' with title (short, <= 60 chars) and due_date "
        "(YYYY-MM-DD; resolve 'tomorrow', 'Friday', etc. from the current date; default today).\n"
        "- complete_task: the user finished / wants to delete dated task(s). Put the task "
        "name(s) as they said them in 'items'.\n"
        "- remove_habit: stop / delete habit(s). Put the habit name(s) in 'items'.\n"
        "- print_now: print something right now (list, note, reminder, plan). Fill "
        "'receipt_title' and 'receipt_lines'.\n"
        "- print_morning: the user wants today's morning paper printed again now.\n"
        "- schedule_print: print something later. Compute an absolute Asia/Karachi ISO 8601 "
        "timestamp with offset (+05:00) in 'schedule_iso' and fill 'receipt_title' and "
        "'receipt_lines'.\n"
        "- list_tasks: the user asks what habits/tasks they have.\n\n"
        "Receipt rules (58mm paper, 32 characters per line):\n"
        "- receipt_title: UPPERCASE, at most 26 characters.\n"
        "- Every actionable item / checklist / shopping item line starts with '[ ] '.\n"
        "- A line '[SECTION:NAME]' starts a section. Plain lines are notes.\n"
        "- Keep lines short; never drop details, quantities, times or deadlines.\n"
        "- Plain English ASCII only: no emoji, no Urdu script (transliterate to Roman Urdu).\n\n"
        "reply: one short, friendly WhatsApp confirmation of what you did."
    )


async def _generate(contents) -> GeminiDecision:
    client = get_gemini_client()
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=genai_types.GenerateContentConfig(
            system_instruction=get_system_context(now_pkt()),
            response_mime_type="application/json",
            response_schema=GeminiDecision,
        ),
    )
    return GeminiDecision(**json.loads(response.text))


async def classify_message(user_text: str) -> GeminiDecision:
    return await _generate(user_text)


async def classify_voice(audio_bytes: bytes, mime_type: str) -> GeminiDecision:
    audio_part = genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type.split(";")[0])
    return await _generate([audio_part, f"Transcribe and act on this voice note from {USER_NAME}."])


# ---------------------------------------------------------------------------
# Action executors
# ---------------------------------------------------------------------------
def _clean_title(title: str) -> str:
    """ASCII, at most TITLE_MAX chars, cut at a word boundary."""
    clean = " ".join(to_ascii(title).split())
    if len(clean) > TITLE_MAX:
        clean = clean[:TITLE_MAX + 1].rsplit(" ", 1)[0] if " " in clean[:TITLE_MAX] else clean[:TITLE_MAX]
    return clean or "Untitled"


def execute_add_daily(items: list[TaskItem]) -> str:
    titles = [_clean_title(i.title) for i in items]
    with Session(engine) as s:
        for t in titles:
            s.add(Habit(title=t, active=1))
        s.commit()
    return "Added daily habit(s):\n" + "\n".join(f"- {t}" for t in titles)


def _valid_date(value: str | None) -> str:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return now_pkt().strftime("%Y-%m-%d")


def execute_add_dated(items: list[TaskItem]) -> str:
    rows = [(_clean_title(i.title), _valid_date(i.due_date)) for i in items]
    with Session(engine) as s:
        for title, due in rows:
            s.add(DatedTask(title=title, due_date=due, completed=0))
        s.commit()
    return "Added task(s):\n" + "\n".join(f"- {t} (due {d})" for t, d in rows)


_STOPWORDS = {"the", "a", "an", "my", "to", "of", "and", "on", "for", "with", "in", "at", "done", "task", "habit"}


def _words(text: str) -> list[str]:
    return [w for w in "".join(ch if ch.isalnum() else " " for ch in to_ascii(text).lower()).split()
            if len(w) >= 3 and w not in _STOPWORDS]


def _match(query: str, candidates: list, attr: str = "title"):
    """Fuzzy match: exact title, else the candidate sharing the most words
    (a word matches if one is a prefix of the other: 'reading' ~ 'read')."""
    q = to_ascii(query).strip().lower()
    if not q:
        return None
    for c in candidates:
        if getattr(c, attr).lower() == q:
            return c
    q_words = _words(q)
    best, best_score = None, 0
    for c in candidates:
        c_words = _words(getattr(c, attr))
        score = sum(1 for qw in q_words if any(qw.startswith(cw) or cw.startswith(qw) for cw in c_words))
        if score > best_score:
            best, best_score = c, score
    return best


def execute_complete_task(items: list[TaskItem]) -> str:
    done, missing = [], []
    with Session(engine) as s:
        open_tasks = s.execute(select(DatedTask).where(DatedTask.completed == 0)
                               .order_by(DatedTask.due_date)).scalars().all()
        for item in items:
            task = _match(item.title, open_tasks)
            if task:
                task.completed = 1
                open_tasks.remove(task)
                done.append(task.title)
            else:
                missing.append(item.title)
        s.commit()
    out = []
    if done:
        out.append("Marked done:\n" + "\n".join(f"- {t}" for t in done))
    if missing:
        out.append("Couldn't find: " + ", ".join(missing) + ". Send 'list' to see open tasks.")
    return "\n".join(out) or "Nothing to mark done."


def execute_remove_habit(items: list[TaskItem]) -> str:
    removed, missing = [], []
    with Session(engine) as s:
        habits = s.execute(select(Habit).where(Habit.active == 1)).scalars().all()
        for item in items:
            habit = _match(item.title, habits)
            if habit:
                habit.active = 0
                habits.remove(habit)
                removed.append(habit.title)
            else:
                missing.append(item.title)
        s.commit()
    out = []
    if removed:
        out.append("Removed habit(s):\n" + "\n".join(f"- {t}" for t in removed))
    if missing:
        out.append("Couldn't find: " + ", ".join(missing) + ". Send 'list' to see habits.")
    return "\n".join(out) or "Nothing removed."


def execute_print_now(title: str | None, lines: list[str]) -> str:
    enqueue_receipt(format_receipt(title or "NOTE", lines))
    return "Sent to the printer."


def _scheduled_print_job(title: str, lines: list[str]) -> None:
    """Called by APScheduler at the scheduled time (must stay importable as main:_scheduled_print_job)."""
    enqueue_receipt(format_receipt(title or "REMINDER", lines))


def execute_schedule_print(title: str | None, lines: list[str], iso_dt: str | None) -> str:
    t = now_pkt()
    try:
        run_at = datetime.fromisoformat(iso_dt) if iso_dt else t
    except ValueError:
        return f"Couldn't understand the time '{iso_dt}'. Please say it again, e.g. 'at 7:30 PM'."
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=PKT)
    run_at = run_at.astimezone(PKT)

    if run_at <= t + timedelta(seconds=30):
        _scheduled_print_job(title, lines)
        return "That time is now/past, so it's printing right away."

    scheduler.add_job(
        _scheduled_print_job,
        trigger="date",
        run_date=run_at,
        args=[title or "REMINDER", lines],
        id=f"sched_{t.timestamp()}",
        replace_existing=True,
    )
    return f"Will print at {run_at.strftime('%a %d %b, %I:%M %p')} PKT."


def execute_list_tasks() -> str:
    today = now_pkt().strftime("%Y-%m-%d")
    with Session(engine) as s:
        habits = s.execute(select(Habit).where(Habit.active == 1)).scalars().all()
        tasks = s.execute(select(DatedTask).where(DatedTask.completed == 0)
                          .order_by(DatedTask.due_date)).scalars().all()
    lines: list[str] = []
    if habits:
        lines.append("*Daily habits:*")
        lines += [f"  - {h.title}" for h in habits]
    if tasks:
        lines.append("*Open tasks:*")
        for tk in tasks:
            flag = " (OVERDUE)" if tk.due_date < today else ""
            lines.append(f"  - {tk.title} - {tk.due_date}{flag}")
    return "\n".join(lines) or "No habits or tasks yet. Send me some!"


async def run_decision(decision: GeminiDecision) -> str:
    """Execute a decision and return the WhatsApp reply text."""
    a = decision.action
    if a == ActionName.add_daily:
        return execute_add_daily(decision.items)
    if a == ActionName.add_dated:
        return execute_add_dated(decision.items)
    if a == ActionName.complete_task:
        return execute_complete_task(decision.items)
    if a == ActionName.remove_habit:
        return execute_remove_habit(decision.items)
    if a == ActionName.print_now:
        execute_print_now(decision.receipt_title, decision.receipt_lines)
        return decision.reply or "Sent to the printer."
    if a == ActionName.print_morning:
        await morning_newspaper()
        return "Printing today's morning paper."
    if a == ActionName.schedule_print:
        return execute_schedule_print(decision.receipt_title, decision.receipt_lines, decision.schedule_iso)
    if a == ActionName.list_tasks:
        return execute_list_tasks()
    return decision.reply


# ---------------------------------------------------------------------------
# Morning newspaper
# ---------------------------------------------------------------------------
_WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow", 80: "Showers",
    81: "Showers", 82: "Heavy showers", 95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}


async def _fetch_weather() -> list[str]:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        "&current=temperature_2m,weather_code"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Asia%2FKarachi&forecast_days=1"
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
        cur, day = data["current"], data["daily"]
        desc = _WEATHER_CODES.get(cur.get("weather_code"), "")
        return [
            f"{CITY_NAME}: {round(cur['temperature_2m'])}C {desc}".strip(),
            f"High {round(day['temperature_2m_max'][0])}C / Low {round(day['temperature_2m_min'][0])}C"
            f" | Rain {day['precipitation_probability_max'][0]}%",
        ]
    except Exception as exc:  # weather is nice-to-have, never block the paper
        log.warning("Weather fetch failed: %s", exc)
        return [f"{CITY_NAME}: weather unavailable"]


async def morning_newspaper() -> None:
    t = now_pkt()
    today = t.strftime("%Y-%m-%d")
    weather = await _fetch_weather()

    with Session(engine) as s:
        habits = s.execute(select(Habit).where(Habit.active == 1)).scalars().all()
        tasks = s.execute(select(DatedTask).where(DatedTask.completed == 0, DatedTask.due_date <= today)
                          .order_by(DatedTask.due_date)).scalars().all()

    body: list[str] = ["[SECTION:WEATHER]", *weather, "[SECTION:DAILY HABITS]"]
    if habits:
        body += [f"[ ] {h.title}" for h in habits]
    else:
        body.append("No habits yet - add some on WhatsApp.")

    body.append("[SECTION:TODAY'S TASKS]")
    due_today = [tk for tk in tasks if tk.due_date == today]
    overdue = [tk for tk in tasks if tk.due_date < today]
    body += [f"[ ] {tk.title}" for tk in due_today] or ["Nothing due today."]
    if overdue:
        body.append("[SECTION:OVERDUE]")
        body += [f"[ ] {tk.title} ({tk.due_date[5:]})" for tk in overdue]

    enqueue_receipt(format_receipt("MORNING EDITION", body, subtitle="DAILY BRIEF", badge="OWN THE DAY"))
    log.info("Morning newspaper queued.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
_last_presence_trigger: Optional[datetime] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    # Always (re)register so a changed MORNING_HOUR/MINUTE takes effect.
    scheduler.add_job(
        morning_newspaper,
        trigger="cron",
        hour=MORNING_HOUR,
        minute=MORNING_MINUTE,
        id="morning_newspaper",
        replace_existing=True,
    )
    log.info("Morning newspaper scheduled daily at %02d:%02d PKT.", MORNING_HOUR, MORNING_MINUTE)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Thermal Printer Backend", lifespan=lifespan)


def require_api_key(x_api_key: str | None) -> None:
    if not PRINTER_API_KEY or not x_api_key or not hmac.compare_digest(x_api_key, PRINTER_API_KEY):
        raise HTTPException(status_code=403, detail="Invalid API key")


def require_demo() -> None:
    if not DEMO_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")


@app.get("/health")
async def health():
    job = scheduler.get_job("morning_newspaper")
    return {"status": "ok", "next_morning_paper": str(job.next_run_time) if job else None}


# ---------------------------------------------------------------------------
# WhatsApp Webhook
# ---------------------------------------------------------------------------
@app.get("/webhook/whatsapp")
async def wa_verify(request: Request):
    p = request.query_params
    if WHATSAPP_VERIFY_TOKEN and p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(content=p.get("hub.challenge", ""), status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")


async def handle_wa_message(message: dict) -> None:
    sender = message.get("from", "")
    try:
        mtype = message.get("type")
        if mtype == "text":
            decision = await classify_message(message["text"]["body"])
        elif mtype == "audio":
            audio = message["audio"]
            audio_bytes = await download_wa_media(audio["id"])
            decision = await classify_voice(audio_bytes, audio.get("mime_type", "audio/ogg"))
        else:
            await wa_reply(sender, "I can only handle text and voice notes.")
            return
        await wa_reply(sender, await run_decision(decision))
    except Exception as exc:
        log.exception("Failed to handle WhatsApp message")
        await wa_reply(sender, f"Sorry, something went wrong: {str(exc)[:200]}")


@app.post("/webhook/whatsapp")
async def wa_incoming(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not verify_meta_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="Bad signature")
    try:
        body = json.loads(raw)
    except ValueError:
        return {"status": "ignored"}

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            for message in change.get("value", {}).get("messages", []) or []:
                if message.get("from") != ALLOWED_PHONE_NUMBER:
                    continue
                if not mark_message_seen(message.get("id", "")):
                    continue  # Meta re-delivery — already handled
                # Reply to Meta immediately; Gemini work happens after the response.
                background.add_task(handle_wa_message, message)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# NFC / shortcut trigger endpoints
# ---------------------------------------------------------------------------
@app.post("/api/trigger/nfc/{zone}")
async def nfc_trigger(zone: str, x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    t = now_pkt()

    if zone == "desk":
        end = t + timedelta(minutes=45)
        body = [
            f"Window: {t.strftime('%H:%M')} -> {end.strftime('%H:%M')}",
            "[SECTION:FOCUS PROTOCOL]",
            "[ ] Single high-value objective",
            "[ ] Phone on Do Not Disturb",
            "[ ] Close all unrelated tabs",
            "[ ] Hydrate & maintain posture",
            "[ ] 5-min post-sprint debrief",
        ]
        receipt = format_receipt("DEEP WORK SPRINT", body, subtitle="FOCUS TICKET (45 MIN)", badge="ZERO DISTRACTIONS")

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
        receipt = format_receipt("POCKET CHECK", body, subtitle="DEPARTURE MANIFEST", badge="SAFE TRAVELS")

    elif zone == "bedside":
        tomorrow = (t + timedelta(days=1)).strftime("%Y-%m-%d")
        with Session(engine) as s:
            tasks = s.execute(select(DatedTask).where(DatedTask.completed == 0, DatedTask.due_date == tomorrow)).scalars().all()
        body = [f"Target Date: {tomorrow}", "[SECTION:TOMORROW'S AGENDA]"]
        body += [f"[ ] {tk.title}" for tk in tasks] or ["No fixed tasks scheduled."]
        body += [
            "[SECTION:NIGHTLY RECOVERY]",
            "[ ] Set wake-up alarm",
            "[ ] Phone docked away from bed",
            "[ ] Target 8h restorative sleep",
        ]
        receipt = format_receipt("TOMORROW PREVIEW", body, subtitle="NIGHTLY BRIEFING", badge="REST DEEPLY")

    else:
        raise HTTPException(status_code=404, detail=f"Unknown zone: {zone}")

    enqueue_receipt(receipt)
    return {"status": "queued", "zone": zone}


@app.post("/api/trigger/presence")
async def presence_trigger(x_api_key: str = Header(None), force: bool = Query(False)):
    global _last_presence_trigger
    require_api_key(x_api_key)
    t = now_pkt()

    if not force:
        if t.hour < 16 or t.hour >= 23:
            return {"status": "outside_window"}
        if _last_presence_trigger and (t - _last_presence_trigger) < timedelta(hours=4):
            return {"status": "cooldown"}
    _last_presence_trigger = t

    today = t.strftime("%Y-%m-%d")
    with Session(engine) as s:
        habits = s.execute(select(Habit).where(Habit.active == 1)).scalars().all()
        tasks = s.execute(select(DatedTask).where(DatedTask.completed == 0, DatedTask.due_date <= today)).scalars().all()

    body = [f"Time Log: {t.strftime('%H:%M')} PKT", "[SECTION:REMAINING TASKS]"]
    body += [f"[ ] {tk.title}" for tk in tasks] or ["All tasks completed!"]
    if habits:
        body.append("[SECTION:DAILY HABITS CHECK]")
        body += [f"[ ] {h.title}" for h in habits]
    body += [
        "[SECTION:EVENING PROTOCOL]",
        "[ ] Log daily accomplishments",
        "[ ] Review tomorrow's priorities",
        "[ ] Digital wind-down",
    ]
    enqueue_receipt(format_receipt("EVENING CHECK-IN", body, subtitle="WELCOME BACK", badge="WRAP UP STRONG"))
    return {"status": "queued"}


# ---------------------------------------------------------------------------
# ESP32 print endpoints
# ---------------------------------------------------------------------------
@app.get("/api/next-print")
async def next_print(x_api_key: str = Header(None)):
    """
    Returns the oldest unprinted receipt with its id in the X-Print-Id header.
    It is NOT marked printed until the ESP32 calls POST /api/ack/{id}; if no
    ack arrives within PRINT_LEASE_SECONDS it is handed out again (max
    PRINT_MAX_ATTEMPTS times) so a Wi-Fi blip never loses a receipt.
    """
    require_api_key(x_api_key)
    t = now_pkt()

    with Session(engine) as s:
        rows = s.execute(select(PrintQueue).where(PrintQueue.printed == 0).order_by(PrintQueue.id.asc())).scalars().all()
        for row in rows:
            leased_at, attempts = _print_leases.get(row.id, (None, 0))
            if leased_at and (t - leased_at).total_seconds() < PRINT_LEASE_SECONDS:
                continue  # currently out for printing
            if attempts >= PRINT_MAX_ATTEMPTS:
                s.execute(update(PrintQueue).where(PrintQueue.id == row.id).values(printed=3))
                s.commit()
                _print_leases.pop(row.id, None)
                log.warning("Receipt %s never acknowledged — giving up.", row.id)
                continue
            _print_leases[row.id] = (t, attempts + 1)
            return PlainTextResponse(content=row.receipt_text, headers={"X-Print-Id": str(row.id)})

    return PlainTextResponse(content="", status_code=200)


@app.post("/api/ack/{print_id}")
async def ack_print(print_id: int, x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    with Session(engine) as s:
        s.execute(update(PrintQueue).where(PrintQueue.id == print_id).values(printed=1))
        s.commit()
    _print_leases.pop(print_id, None)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Demo / web simulator (only when DEMO_ENABLED=true)
# ---------------------------------------------------------------------------
class SimulateWhatsAppRequest(BaseModel):
    text: str


@app.get("/")
@app.get("/demo")
async def demo_page():
    require_demo()
    index_path = os.path.join(BASE_DIR, "static", "index.html")
    with open(index_path, encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html.replace("__PRINTER_API_KEY__", PRINTER_API_KEY))


@app.post("/api/demo/morning")
async def trigger_morning_demo():
    require_demo()
    await morning_newspaper()
    return {"status": "queued", "message": "Morning newspaper enqueued"}


@app.post("/api/demo/simulate-whatsapp")
async def simulate_whatsapp_demo(req: SimulateWhatsAppRequest):
    require_demo()
    try:
        decision = await classify_message(req.text)
    except Exception as exc:
        log.warning("Gemini unavailable in demo (%s) — using keyword fallback.", exc)
        decision = _fallback_decision(req.text)
    reply = await run_decision(decision)
    return {"decision": decision.model_dump(), "reply": reply}


def _fallback_decision(txt: str) -> GeminiDecision:
    """Very rough keyword classifier so the demo works without a Gemini key."""
    txt = txt.strip()
    lower = txt.lower()
    if lower.startswith("print"):
        clean = txt[5:].strip(": ")
        parts = [p.strip() for p in clean.replace("\n", ",").split(",") if p.strip()] or [clean]
        return GeminiDecision(action=ActionName.print_now, receipt_title="MANIFEST",
                              receipt_lines=["[SECTION:ITEMS]"] + [f"[ ] {p}" for p in parts],
                              reply="Printed.")
    if lower.startswith("done"):
        return GeminiDecision(action=ActionName.complete_task, items=[TaskItem(title=txt[4:].strip(": "))], reply="")
    if "habit" in lower:
        title = lower.replace("add habit", "").replace("habit:", "").strip() or "Daily habit"
        return GeminiDecision(action=ActionName.add_daily, items=[TaskItem(title=title)], reply="")
    if "list" in lower:
        return GeminiDecision(action=ActionName.list_tasks, reply="")
    if "task" in lower:
        title = lower.replace("add task", "").replace("task:", "").strip() or "Task"
        return GeminiDecision(action=ActionName.add_dated, items=[TaskItem(title=title)], reply="")
    return GeminiDecision(action=ActionName.print_now, receipt_title="MEMO",
                          receipt_lines=[l for l in txt.split("\n") if l.strip()], reply="Printed.")


@app.get("/api/demo/latest")
def demo_latest(after: int = 0):
    """Newest receipt with id > after — lets the demo page preview prints
    WITHOUT taking them away from the real printer."""
    require_demo()
    with Session(engine) as s:
        row = s.execute(select(PrintQueue).where(PrintQueue.id > after)
                        .order_by(PrintQueue.id.desc()).limit(1)).scalar_one_or_none()
    return {"id": row.id, "text": row.receipt_text} if row else {"id": after, "text": ""}


@app.get("/api/demo/queue-status")
def get_queue_status():
    require_demo()
    with Session(engine) as s:
        pending = s.execute(select(PrintQueue).where(PrintQueue.printed == 0)).scalars().all()
        habits = s.execute(select(Habit).where(Habit.active == 1)).scalars().all()
        tasks = s.execute(select(DatedTask).where(DatedTask.completed == 0)).scalars().all()
        return {
            "pending_count": len(pending),
            "habits": [h.title for h in habits],
            "tasks": [{"title": t.title, "due_date": t.due_date} for t in tasks],
            "server_time": now_pkt().strftime("%Y-%m-%d %H:%M:%S PKT"),
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
