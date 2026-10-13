"""
Horizon Tutors Hub - Telegram bot (v2)

Flow
----
Tutor   : /start -> register (+ National/Kebele ID) -> admin approves -> tutor can apply to jobs
Client  : /start -> request a tutor -> admin verifies -> job is posted to the channel
          with an "Apply" button -> approved tutors apply -> admin sends the tutor's
          profile to the client (phone numbers are never shared) -> admin marks the job Filled

Data is stored in a Google Sheet (tabs are created automatically).
See SETUP.md for environment variables and deployment.
"""
import asyncio
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from texts import LANGUAGES, t

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps the bot token out of the logs
logger = logging.getLogger("horizon")

# ----------------------------------------------------------------------------
# Configuration (environment variables)
# ----------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")          # where approval requests are sent
JOB_CHANNEL_ID = os.environ.get("JOB_CHANNEL_ID")        # public job channel
GOOGLE_JSON = os.environ.get("GOOGLE_JSON")              # service-account JSON (one line)
SHEET_NAME = os.environ.get("SHEET_NAME", "Horizon Tutors Hub")
REQUIRE_CLIENT_ID = os.environ.get("REQUIRE_CLIENT_ID", "false").strip().lower() == "true"
TRIAL_THRESHOLD = float(os.environ.get("TRIAL_THRESHOLD", "4000"))  # budget above this -> 7-day trial
MIN_BUDGET = float(os.environ.get("MIN_BUDGET", "300"))
CONVERSATION_TIMEOUT = int(os.environ.get("CONVERSATION_TIMEOUT", "1800"))  # seconds
TZ = ZoneInfo("Africa/Addis_Ababa")


def _parse_admin_ids() -> set:
    """ADMIN_IDS = comma-separated Telegram user IDs allowed to press admin buttons."""
    raw = os.environ.get("ADMIN_IDS") or ADMIN_CHAT_ID or ""
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit() and int(part) > 0:
            ids.add(int(part))
    return ids


ADMIN_IDS = _parse_admin_ids()

# ----------------------------------------------------------------------------
# Conversation states
# ----------------------------------------------------------------------------
(
    LANG, ROLE,
    T_NAME, T_PHONE, T_GENDER, T_SUBJECTS, T_GRADES, T_LOCATION, T_QUAL, T_ID,
    C_NAME, C_PHONE, C_SUBJECT, C_GRADE, C_CITY, C_PLACE, C_GENDER, C_DURATION, C_BUDGET, C_ID,
) = range(20)

LANG_PROMPT = "🌐 Choose your language / ቋንቋ ይምረጡ / Afaan filadhu"
GENDER_PREFS = {"female": "Female Tutor", "male": "Male Tutor", "any": "Any Tutor"}

# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def esc(value) -> str:
    """Escape user text so it is safe inside Telegram HTML messages."""
    return html.escape(str(value or ""), quote=False)


def clip(text, limit: int = 120) -> str:
    """Trim whitespace and cap the length of user input."""
    return " ".join((text or "").split())[:limit]


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def uname(user) -> str:
    return f"@{user.username}" if user.username else "N/A"


PHONE_RE = re.compile(r"^(?:\+?251|0)?([79]\d{8})$")


def normalize_phone(text: str):
    """'+251 911-23-45-67' -> '0911234567'. Returns None if it isn't an Ethiopian mobile number."""
    cleaned = re.sub(r"[\s\-()]", "", text or "")
    match = PHONE_RE.match(cleaned)
    return "0" + match.group(1) if match else None


def parse_budget(text: str) -> float:
    """'5,500 ETB' -> 5500, '5.5k' -> 5500, '4000-6000' -> 4000 (first number). 0 if none."""
    cleaned = (text or "").lower().replace(",", "").replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)(k)?", cleaned)
    if not match:
        return 0.0
    value = float(match.group(1))
    return value * 1000 if match.group(2) else value


def fmt_etb(row: dict) -> str:
    try:
        return f"{float(row.get('Budget Number')):,.0f} ETB"
    except (TypeError, ValueError):
        return esc(row.get("Budget"))


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


async def reply(message, text, markup=None):
    return await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


def lang_of(context) -> str:
    return context.user_data.get("lang", "en")


def form(context) -> dict:
    return context.user_data.setdefault("form", {})


def get_file(message):
    """Returns (file_id, kind) for a photo or image document, else (None, None)."""
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.document:
        return message.document.file_id, "document"
    return None, None


# ----------------------------------------------------------------------------
# Google Sheets layer
# ----------------------------------------------------------------------------
HEADERS = {
    "Tutors": [
        "Timestamp", "Telegram ID", "Username", "Name", "Phone", "Gender", "Subjects",
        "Grades", "Location", "Qualification", "ID Photo", "Status", "Language",
    ],
    "Client_Requests": [
        "Timestamp", "Job Code", "Telegram ID", "Username", "Client Name", "Phone", "Subject",
        "Grade", "City", "Place", "Tutor Gender Pref", "Duration", "Budget", "Budget Number",
        "ID Photo", "Trial Terms", "Verification", "Job Status", "Channel Msg ID", "Language",
    ],
    "Applications": [
        "Timestamp", "Job Code", "Tutor Telegram ID", "Tutor Name", "Tutor Phone", "Status",
    ],
    "Settings": ["Key", "Value"],
}
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class DBError(Exception):
    """Raised when Google Sheets can't be reached."""


class Sheets:
    """
    Thin wrapper around gspread. All calls go through `call()`, which
      - runs one sheet operation at a time (so job codes can never be duplicated),
      - reconnects automatically if the connection went stale.
    Use it from async code via `await db(sheets.some_method, args...)`.
    """

    def __init__(self):
        self._ss = None
        self._lock = threading.Lock()

    def _connect(self):
        if not GOOGLE_JSON:
            raise DBError("GOOGLE_JSON is not set")
        creds = Credentials.from_service_account_info(json.loads(GOOGLE_JSON), scopes=SCOPES)
        ss = gspread.authorize(creds).open(SHEET_NAME)
        existing = {ws.title for ws in ss.worksheets()}
        for name, headers in HEADERS.items():
            if name in existing:
                tab = ss.worksheet(name)
                if tab.row_values(1) == headers:
                    continue
                # An older version of the sheet with different columns: keep its data under a
                # new name instead of mixing it with the new layout.
                backup = f"{name}_old_{datetime.now(TZ):%Y%m%d_%H%M}"
                tab.update_title(backup)
                logger.warning("Tab '%s' had different columns - renamed to '%s' and created a fresh one", name, backup)
            ws = ss.add_worksheet(title=name, rows=1000, cols=len(headers))
            ws.append_row(headers)
            if name == "Settings":
                ws.append_row(["job_counter", 0])
        self._ss = ss

    def call(self, fn, *args):
        with self._lock:
            last_error = None
            for _ in range(2):
                try:
                    if self._ss is None:
                        self._connect()
                    return fn(*args)
                except DBError:
                    raise
                except Exception as exc:  # network hiccup, expired token, quota...
                    last_error = exc
                    logger.warning("Sheets error (will retry once): %s", exc)
                    self._ss = None
            raise DBError(str(last_error))

    # ---- operations (always call through `db()`) ----
    def rows(self, sheet: str) -> list:
        values = self._ss.worksheet(sheet).get_all_values()
        if not values:
            return []
        headers = values[0]
        result = []
        for number, vals in enumerate(values[1:], start=2):
            record = dict(zip(headers, vals))
            record["_row"] = number
            result.append(record)
        return result

    def append(self, sheet: str, row: list):
        self._ss.worksheet(sheet).append_row(row, value_input_option="RAW")

    def find_last(self, sheet: str, header: str, value):
        for record in reversed(self.rows(sheet)):
            if record.get(header, "").strip() == str(value):
                return record
        return None

    def update(self, sheet: str, row_number: int, fields: dict):
        ws = self._ss.worksheet(sheet)
        for header, value in fields.items():
            ws.update_cell(row_number, HEADERS[sheet].index(header) + 1, value)

    def next_job_code(self) -> str:
        ws = self._ss.worksheet("Settings")
        for number, row in enumerate(ws.get_all_values(), start=1):
            if row and row[0] == "job_counter":
                try:
                    count = int(row[1]) + 1
                except (IndexError, ValueError):
                    count = 1
                ws.update_cell(number, 2, count)
                return f"#R{count:03d}"
        ws.append_row(["job_counter", 1])
        return "#R001"

    def has_application(self, job_code: str, tutor_id) -> bool:
        return any(
            r.get("Job Code") == job_code and r.get("Tutor Telegram ID") == str(tutor_id)
            for r in self.rows("Applications")
        )

    def set_application_status(self, job_code: str, tutor_id, status: str) -> bool:
        for r in reversed(self.rows("Applications")):
            if r.get("Job Code") == job_code and r.get("Tutor Telegram ID") == str(tutor_id):
                self.update("Applications", r["_row"], {"Status": status})
                return True
        return False


sheets = Sheets()


async def db(fn, *args):
    """Run a (blocking) Sheets operation without freezing the bot."""
    return await asyncio.to_thread(sheets.call, fn, *args)


# ----------------------------------------------------------------------------
# Admin notification helpers
# ----------------------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def notify_admin(context, text, markup=None, file_id=None, kind=None):
    """Send a message (optionally with the ID photo) to the admin chat. Never raises."""
    if not ADMIN_CHAT_ID:
        logger.error("ADMIN_CHAT_ID is not set - cannot notify admin")
        return None
    bot, chat = context.bot, ADMIN_CHAT_ID
    try:
        if file_id and len(text) <= 1000:  # Telegram caption limit is 1024
            send = bot.send_photo if kind == "photo" else bot.send_document
            field = {"photo": file_id} if kind == "photo" else {"document": file_id}
            return await send(chat, caption=text, parse_mode=ParseMode.HTML, reply_markup=markup, **field)
        if file_id:
            send = bot.send_photo if kind == "photo" else bot.send_document
            field = {"photo": file_id} if kind == "photo" else {"document": file_id}
            await send(chat, **field)
        return await bot.send_message(chat, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:
        logger.exception("Could not notify admin")
        return None


async def mark_done(query, status_line: str):
    """Append a status line to the admin message and remove its buttons."""
    msg = query.message
    if msg.caption is not None:
        await query.edit_message_caption(caption=f"{msg.caption_html}\n\n{status_line}", parse_mode=ParseMode.HTML)
    else:
        await query.edit_message_text(text=f"{msg.text_html}\n\n{status_line}", parse_mode=ParseMode.HTML)


async def tell_user(context, user_id, lang, key, **kwargs) -> bool:
    try:
        await context.bot.send_message(int(user_id), t(lang, key, **kwargs), parse_mode=ParseMode.HTML)
        return True
    except Exception:
        logger.warning("Could not message user %s", user_id)
        return False


# ----------------------------------------------------------------------------
# Start, language and role selection
# ----------------------------------------------------------------------------
def lang_keyboard():
    return kb([("English", "lang:en"), ("አማርኛ", "lang:am"), ("Afaan Oromoo", "lang:om")])


async def show_menu(message, context):
    lang = lang_of(context)
    await reply(
        message,
        f"{t(lang, 'welcome')}\n\n{t(lang, 'choose_role')}",
        kb([(t(lang, "btn_tutor"), "role:tutor")], [(t(lang, "btn_client"), "role:client")]),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args and args[0].startswith("apply_"):          # deep link from a channel post
        await handle_apply(update, context, args[0][len("apply_"):])
        return ConversationHandler.END
    context.user_data.pop("form", None)
    if "lang" in context.user_data:
        await show_menu(update.message, context)
        return ROLE
    await reply(update.message, LANG_PROMPT, lang_keyboard())
    return LANG


async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("lang", None)
    context.user_data.pop("form", None)
    await reply(update.message, LANG_PROMPT, lang_keyboard())
    return LANG


async def lang_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]
    context.user_data["lang"] = choice if choice in LANGUAGES else "en"
    await query.edit_message_reply_markup(None)
    await show_menu(query.message, context)
    return ROLE


async def role_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(None)
    lang, user = lang_of(context), update.effective_user
    context.user_data["form"] = {}

    if query.data == "role:tutor":
        try:
            existing = await db(sheets.find_last, "Tutors", "Telegram ID", user.id)
        except DBError:
            existing = None
        if existing:
            status = existing.get("Status")
            if status == "Approved":
                await reply(query.message, t(lang, "tutor_already_approved"))
                return ConversationHandler.END
            if status == "Pending":
                await reply(query.message, t(lang, "tutor_pending"))
                return ConversationHandler.END
        await reply(query.message, t(lang, "ask_name"))
        return T_NAME

    await reply(query.message, t(lang, "c_ask_name"))
    return C_NAME


# ----------------------------------------------------------------------------
# Generic step builders (keep the registration flows short and readable)
# ----------------------------------------------------------------------------
def text_step(field: str, next_prompt: str, next_state: int, limit: int = 120):
    """Save the user's text in form[field], ask the next question."""
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        form(context)[field] = clip(update.message.text, limit)
        await reply(update.message, t(lang_of(context), next_prompt))
        return next_state
    return handler


def phone_step(next_prompt: str, next_state: int, keyboard_fn=None, retry_state: int = None):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        lang = lang_of(context)
        phone = normalize_phone(update.message.text)
        if not phone:
            await reply(update.message, t(lang, "bad_phone"))
            return retry_state
        form(context)["phone"] = phone
        await reply(update.message, t(lang, next_prompt), keyboard_fn(lang) if keyboard_fn else None)
        return next_state
    return handler


def need_photo(state: int):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await reply(update.message, t(lang_of(context), "send_photo"))
        return state
    return handler


def use_buttons(state: int):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await reply(update.message, t(lang_of(context), "use_buttons"))
        return state
    return handler


# ----------------------------------------------------------------------------
# Tutor registration
# ----------------------------------------------------------------------------
def gender_keyboard(lang):
    return kb([(t(lang, "btn_male"), "g:Male"), (t(lang, "btn_female"), "g:Female")])


async def tutor_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(None)
    form(context)["gender"] = query.data.split(":")[1]
    await reply(query.message, t(lang_of(context), "ask_subjects"))
    return T_SUBJECTS


async def tutor_id_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, lang, f = update.effective_user, lang_of(context), form(context)
    file_id, kind = get_file(update.message)

    row = [
        now_str(), str(user.id), uname(user), f.get("name", ""), f.get("phone", ""),
        f.get("gender", ""), f.get("subjects", ""), f.get("grades", ""), f.get("location", ""),
        f.get("qual", ""), file_id, "Pending", lang,
    ]
    saved = True
    try:
        await db(sheets.append, "Tutors", row)
    except DBError:
        saved = False

    text = (
        "🆕 <b>NEW TUTOR REGISTRATION</b>\n\n"
        f"👤 <b>Name:</b> {esc(f.get('name'))}\n"
        f"📞 <b>Phone:</b> {esc(f.get('phone'))}\n"
        f"⚧ <b>Gender:</b> {esc(f.get('gender'))}\n"
        f"📚 <b>Subjects:</b> {esc(f.get('subjects'))}\n"
        f"🎓 <b>Grades:</b> {esc(f.get('grades'))}\n"
        f"📍 <b>Location:</b> {esc(f.get('location'))}\n"
        f"📜 <b>Qualification:</b> {esc(clip(f.get('qual'), 250))}\n"
        f"💬 <b>Telegram:</b> {esc(uname(user))} (ID: <code>{user.id}</code>)"
    )
    if not saved:
        text += "\n\n⚠️ Could not save to Google Sheets - please record this tutor manually."
    markup = kb([("✅ Approve", f"adm:app_tutor:{user.id}"), ("❌ Reject", f"adm:rej_tutor:{user.id}")])
    await notify_admin(context, text, markup, file_id, kind)

    await reply(update.message, t(lang, "tutor_done"))
    context.user_data.pop("form", None)
    return ConversationHandler.END


# ----------------------------------------------------------------------------
# Client request
# ----------------------------------------------------------------------------
async def client_grade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = lang_of(context)
    form(context)["grade"] = clip(update.message.text, 60)
    await reply(update.message, t(lang, "c_ask_city"), kb([("Adama", "city:Adama"), ("Bishoftu", "city:Bishoftu")]))
    return C_CITY


async def client_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(None)
    form(context)["city"] = query.data.split(":")[1]
    await reply(query.message, t(lang_of(context), "c_ask_place"))
    return C_PLACE


async def client_place(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = lang_of(context)
    form(context)["place"] = clip(update.message.text, 100)
    await reply(
        update.message,
        t(lang, "c_ask_gender_pref"),
        kb(
            [(t(lang, "btn_pref_female"), "gp:female"), (t(lang, "btn_pref_male"), "gp:male")],
            [(t(lang, "btn_pref_any"), "gp:any")],
        ),
    )
    return C_GENDER


async def client_gender_pref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(None)
    form(context)["gender_pref"] = GENDER_PREFS.get(query.data.split(":")[1], "Any Tutor")
    await reply(query.message, t(lang_of(context), "c_ask_duration"))
    return C_DURATION


async def client_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = lang_of(context)
    text = clip(update.message.text, 40)
    value = parse_budget(text)
    if value < MIN_BUDGET:
        await reply(update.message, t(lang, "bad_budget"))
        return C_BUDGET
    form(context)["budget"] = text
    form(context)["budget_value"] = value
    if REQUIRE_CLIENT_ID:
        await reply(update.message, t(lang, "ask_id"))
        return C_ID
    await finalize_client(update, context)
    return ConversationHandler.END


async def client_id_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id, kind = get_file(update.message)
    await finalize_client(update, context, file_id, kind)
    return ConversationHandler.END


async def finalize_client(update, context, file_id=None, kind=None):
    """Save the request, send it to the admin for verification. It is NOT posted publicly yet."""
    user, lang, f = update.effective_user, lang_of(context), form(context)

    try:
        job = await db(sheets.next_job_code)
    except DBError:
        job = f"#R{int(time.time()) % 100000}"

    value = f.get("budget_value", 0)
    trial_key = "trial_long" if value > TRIAL_THRESHOLD else "trial_short"

    row = [
        now_str(), job, str(user.id), uname(user), f.get("name", ""), f.get("phone", ""),
        f.get("subject", ""), f.get("grade", ""), f.get("city", ""), f.get("place", ""),
        f.get("gender_pref", ""), f.get("duration", ""), f.get("budget", ""), value,
        file_id or "", t("en", trial_key), "Pending", "Pending", "", lang,
    ]
    saved = True
    try:
        await db(sheets.append, "Client_Requests", row)
    except DBError:
        saved = False

    text = (
        "🆕 <b>NEW TUTOR REQUEST</b>\n\n"
        f"📌 <b>Job Code:</b> <code>{esc(job)}</code>\n"
        f"👤 <b>Client:</b> {esc(f.get('name'))}\n"
        f"📞 <b>Phone:</b> {esc(f.get('phone'))}\n"
        f"📚 <b>Subject:</b> {esc(f.get('subject'))}\n"
        f"🎓 <b>Grade:</b> {esc(f.get('grade'))}\n"
        f"📍 <b>Location:</b> {esc(f.get('city'))} - {esc(f.get('place'))}\n"
        f"⚧ <b>Tutor gender:</b> {esc(f.get('gender_pref'))}\n"
        f"⏱ <b>Duration:</b> {esc(f.get('duration'))}\n"
        f"💰 <b>Budget:</b> {value:,.0f} ETB\n"
        f"⏳ <b>Policy:</b> {esc(t('en', trial_key))}\n"
        f"💬 <b>Telegram:</b> {esc(uname(user))} (ID: <code>{user.id}</code>)"
    )
    if not REQUIRE_CLIENT_ID:
        text += "\n\n☎️ Call the client to verify before approving."
    if not saved:
        text += "\n\n⚠️ Could not save to Google Sheets - please record this request manually."
    code = job.lstrip("#")
    markup = kb([("✅ Approve & Publish", f"adm:app_client:{code}"), ("❌ Reject", f"adm:rej_client:{code}")])
    await notify_admin(context, text, markup, file_id, kind)

    await reply(update.message, t(lang, "client_done", job=esc(job), trial=t(lang, trial_key)))
    context.user_data.pop("form", None)


# ----------------------------------------------------------------------------
# Job post (channel)
# ----------------------------------------------------------------------------
def build_job_post(row: dict, status: str = "Open") -> str:
    head = "🚨 Open" if status == "Open" else "🔒 Filled"
    return (
        f"{head}\n\n"
        f"📖 {esc(row.get('Tutor Gender Pref'))} 📖\n\n"
        f"Job code: {esc(row.get('Job Code'))}\n\n"
        f"✅ Grade : {esc(row.get('Grade'))}\n"
        f"✅ City : {esc(row.get('City'))}\n"
        f"✅ Place : {esc(row.get('Place'))}\n"
        f"✅ Subject : {esc(row.get('Subject'))}\n"
        f"✅ Duration : {esc(row.get('Duration'))}\n"
        f"✅ Salary : {fmt_etb(row)}\n\n"
        f"📖 We need highly committed, punctual and talented tutor"
    )


def apply_markup(context, code: str) -> InlineKeyboardMarkup:
    url = f"https://t.me/{context.bot.username}?start=apply_{code}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("📩 Apply for this job", url=url)]])


# ----------------------------------------------------------------------------
# Tutors applying to a job (deep link: /start apply_R012)
# ----------------------------------------------------------------------------
async def handle_apply(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    message, user = update.message, update.effective_user
    lang = lang_of(context)
    code = code.upper()
    if not re.fullmatch(r"R\d{1,6}", code):
        await reply(message, t(lang, "job_closed"))
        return
    job_code = "#" + code

    try:
        tutor = await db(sheets.find_last, "Tutors", "Telegram ID", user.id)
        job = await db(sheets.find_last, "Client_Requests", "Job Code", job_code)
        if tutor and tutor.get("Language") and "lang" not in context.user_data:
            lang = tutor["Language"]
            context.user_data["lang"] = lang

        if not tutor or tutor.get("Status") != "Approved":
            await reply(message, t(lang, "not_tutor"))
            return
        if not job or job.get("Verification") != "Approved" or job.get("Job Status") != "Open":
            await reply(message, t(lang, "job_closed"))
            return
        pref, gender = job.get("Tutor Gender Pref"), tutor.get("Gender")
        if (pref == "Female Tutor" and gender != "Female") or (pref == "Male Tutor" and gender != "Male"):
            await reply(message, t(lang, "gender_mismatch"))
            return
        if await db(sheets.has_application, job_code, user.id):
            await reply(message, t(lang, "already_applied", job=esc(job_code)))
            return
        await db(
            sheets.append, "Applications",
            [now_str(), job_code, str(user.id), tutor.get("Name", ""), tutor.get("Phone", ""), "Applied"],
        )
    except DBError:
        await reply(message, t(lang, "error"))
        return

    text = (
        f"📩 <b>NEW APPLICATION</b> for <code>{esc(job_code)}</code>\n\n"
        f"🧑‍🏫 <b>Name:</b> {esc(tutor.get('Name'))}\n"
        f"📞 <b>Phone:</b> {esc(tutor.get('Phone'))}\n"
        f"⚧ <b>Gender:</b> {esc(tutor.get('Gender'))}\n"
        f"📚 <b>Subjects:</b> {esc(tutor.get('Subjects'))}\n"
        f"🎓 <b>Grades:</b> {esc(tutor.get('Grades'))}\n"
        f"📍 <b>Location:</b> {esc(tutor.get('Location'))}\n"
        f"📜 <b>Qualification:</b> {esc(clip(tutor.get('Qualification'), 250))}\n"
        f"💬 {esc(uname(user))} (ID: <code>{user.id}</code>)\n\n"
        f"<b>Job:</b> {esc(job.get('Subject'))}, {esc(job.get('Grade'))} - "
        f"{esc(job.get('City'))} {esc(job.get('Place'))} - {fmt_etb(job)}"
    )
    markup = kb([("📨 Send profile to client", f"adm:send:{code}:{user.id}")])
    await notify_admin(context, text, markup)
    await reply(message, t(lang, "applied_ok", job=esc(job_code)))


# ----------------------------------------------------------------------------
# Admin buttons
# ----------------------------------------------------------------------------
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = query.from_user
    if not is_admin(admin.id):
        await query.answer("⛔ Not authorized", show_alert=True)
        return
    await query.answer()

    parts = query.data.split(":")
    action, who = parts[1], esc(admin.first_name)
    try:
        if action in ("app_tutor", "rej_tutor"):
            await decide_tutor(context, query, action == "app_tutor", int(parts[2]), who)
        elif action in ("app_client", "rej_client"):
            await decide_client(context, query, action == "app_client", parts[2], who)
        elif action == "send":
            await send_profile(context, query, parts[2], int(parts[3]), who)
    except DBError:
        await query.message.reply_text(
            "⚠️ Google Sheets is unavailable right now. Nothing was changed - please tap the button again in a minute."
        )


async def decide_tutor(context, query, approve: bool, tutor_id: int, who: str):
    row = await db(sheets.find_last, "Tutors", "Telegram ID", tutor_id)
    if row and row.get("Status") != "Pending":
        await mark_done(query, f"ℹ️ Already {esc(row.get('Status'))}.")
        return
    status = "Approved" if approve else "Rejected"
    if row:
        await db(sheets.update, "Tutors", row["_row"], {"Status": status})
    lang = (row or {}).get("Language") or "en"
    delivered = await tell_user(context, tutor_id, lang, "tutor_approved" if approve else "tutor_rejected")

    line = f"{'✅ APPROVED' if approve else '❌ REJECTED'} by {who}"
    if not row:
        line += "\n⚠️ Not found in the sheet - update it manually."
    if not delivered:
        line += "\n⚠️ Could not message the tutor (they may have blocked the bot)."
    await mark_done(query, line)


async def decide_client(context, query, approve: bool, code: str, who: str):
    job_code = "#" + code
    row = await db(sheets.find_last, "Client_Requests", "Job Code", job_code)
    if not row:
        await mark_done(query, "⚠️ Job not found in the sheet.")
        return
    if row.get("Verification") != "Pending":
        await mark_done(query, f"ℹ️ Already {esc(row.get('Verification'))}.")
        return

    lang, client_id = row.get("Language") or "en", row.get("Telegram ID")

    if not approve:
        await db(sheets.update, "Client_Requests", row["_row"], {"Verification": "Rejected", "Job Status": "Rejected"})
        delivered = await tell_user(context, client_id, lang, "client_rejected", job=esc(job_code))
        line = f"❌ REJECTED by {who}" + ("" if delivered else "\n⚠️ Could not message the client.")
        await mark_done(query, line)
        return

    # Approve first in the sheet, then publish (so a retry can never double-post).
    await db(sheets.update, "Client_Requests", row["_row"], {"Verification": "Approved", "Job Status": "Open"})
    note = ""
    if JOB_CHANNEL_ID:
        try:
            posted = await context.bot.send_message(
                JOB_CHANNEL_ID, build_job_post(row, "Open"),
                parse_mode=ParseMode.HTML, reply_markup=apply_markup(context, code),
            )
            try:
                await db(sheets.update, "Client_Requests", row["_row"], {"Channel Msg ID": posted.message_id})
            except DBError:
                note += "\n⚠️ Posted, but couldn't save the channel message ID (/filled won't edit the post)."
        except Exception:
            logger.exception("Could not post to the channel")
            note += "\n⚠️ Could not post to the channel - is the bot an admin there with permission to post?"
    else:
        note += "\n⚠️ JOB_CHANNEL_ID is not set - nothing was posted."

    delivered = await tell_user(context, client_id, lang, "client_approved", job=esc(job_code))
    if not delivered:
        note += "\n⚠️ Could not message the client."
    await mark_done(query, f"✅ APPROVED & PUBLISHED by {who}{note}")


async def send_profile(context, query, code: str, tutor_id: int, who: str):
    job_code = "#" + code
    job = await db(sheets.find_last, "Client_Requests", "Job Code", job_code)
    tutor = await db(sheets.find_last, "Tutors", "Telegram ID", tutor_id)
    if not job or not tutor:
        await query.message.reply_text("⚠️ Job or tutor not found in the sheet.")
        return
    delivered = await tell_user(
        context, job.get("Telegram ID"), job.get("Language") or "en", "profile_for_client",
        job=esc(job_code), name=esc(tutor.get("Name")), subjects=esc(tutor.get("Subjects")),
        grades=esc(tutor.get("Grades")), qual=esc(clip(tutor.get("Qualification"), 250)),
    )
    if not delivered:
        await query.message.reply_text("⚠️ Could not message the client (they may have blocked the bot).")
        return
    await db(sheets.set_application_status, job_code, tutor_id, "Sent to client")
    await mark_done(query, f"📨 Profile sent to the client by {who}")


# ----------------------------------------------------------------------------
# Admin commands
# ----------------------------------------------------------------------------
async def filled_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/filled R012 - mark a job as filled and update the channel post."""
    if not is_admin(update.effective_user.id):
        return
    arg = (context.args[0] if context.args else "").upper().lstrip("#")
    if not re.fullmatch(r"R\d{1,6}", arg):
        await update.message.reply_text("Usage: /filled R012")
        return
    job_code = "#" + arg
    try:
        row = await db(sheets.find_last, "Client_Requests", "Job Code", job_code)
        if not row:
            await update.message.reply_text(f"{job_code} was not found.")
            return
        await db(sheets.update, "Client_Requests", row["_row"], {"Job Status": "Filled"})
    except DBError:
        await update.message.reply_text("⚠️ Google Sheets is unavailable. Try again in a minute.")
        return

    edited = False
    if JOB_CHANNEL_ID and row.get("Channel Msg ID"):
        try:
            await context.bot.edit_message_text(
                chat_id=JOB_CHANNEL_ID, message_id=int(row["Channel Msg ID"]),
                text=build_job_post(row, "Filled"), parse_mode=ParseMode.HTML,
            )
            edited = True
        except Exception:
            logger.exception("Could not edit the channel post")
    await update.message.reply_text(
        f"🔒 {job_code} marked as Filled." + ("" if edited else " (The channel post could not be edited - please edit it manually.)")
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats - quick numbers for the admin."""
    if not is_admin(update.effective_user.id):
        return
    try:
        tutors = await db(sheets.rows, "Tutors")
        jobs = await db(sheets.rows, "Client_Requests")
        apps = await db(sheets.rows, "Applications")
    except DBError:
        await update.message.reply_text("⚠️ Google Sheets is unavailable. Try again in a minute.")
        return
    latest = {r.get("Telegram ID"): r.get("Status") for r in tutors}   # last row per tutor wins
    count = lambda rows, key, val: sum(1 for r in rows if r.get(key) == val)
    await update.message.reply_text(
        "📊 Horizon Tutors Hub\n\n"
        f"Tutors: {sum(1 for s in latest.values() if s == 'Approved')} approved, "
        f"{sum(1 for s in latest.values() if s == 'Pending')} pending\n"
        f"Jobs: {count(jobs, 'Job Status', 'Open')} open, {count(jobs, 'Job Status', 'Pending')} pending, "
        f"{count(jobs, 'Job Status', 'Filled')} filled\n"
        f"Applications: {len(apps)}"
    )


# ----------------------------------------------------------------------------
# Misc handlers
# ----------------------------------------------------------------------------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update.message, t(lang_of(context), "help"))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update.message, t(lang_of(context), "cancelled"))
    context.user_data.pop("form", None)
    return ConversationHandler.END


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A form button was tapped after the session ended (timeout or bot restart)."""
    await update.callback_query.answer(t(lang_of(context), "timeout"), show_alert=True)


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("form", None)
    message = update.effective_message
    if message:
        await reply(message, t(lang_of(context), "timeout"))


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text sent outside any form."""
    lang = lang_of(context)
    key = "apply_hint" if re.search(r"#?R\d{2,}", update.message.text or "", re.I) else "send_start"
    await reply(update.message, t(lang, key))


_last_error_ping = 0.0


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    global _last_error_ping
    logger.error("Unhandled error", exc_info=context.error)
    if ADMIN_CHAT_ID and time.time() - _last_error_ping > 300:   # at most one ping per 5 minutes
        _last_error_ping = time.time()
        try:
            await context.bot.send_message(ADMIN_CHAT_ID, f"⚠️ Bot error: {type(context.error).__name__} (see logs)")
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Tiny health-check web server (needed if Render runs this as a free "Web Service")
# ----------------------------------------------------------------------------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


def start_health_server():
    port = os.environ.get("PORT")
    if not port:
        return
    server = HTTPServer(("0.0.0.0", int(port)), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server listening on port %s", port)


# ----------------------------------------------------------------------------
# App wiring
# ----------------------------------------------------------------------------
async def post_init(app):
    try:
        await asyncio.to_thread(sheets.call, lambda: None)   # connects + creates missing tabs
        logger.info("Google Sheet connected")
    except DBError as exc:
        logger.error("Could not connect to Google Sheets at startup: %s", exc)
    await app.bot.set_my_commands([
        ("start", "Register or request a tutor"),
        ("language", "Change language"),
        ("help", "Help"),
        ("cancel", "Cancel the current form"),
    ])


def build_conversation() -> ConversationHandler:
    private = filters.ChatType.PRIVATE
    text = filters.TEXT & ~filters.COMMAND & private
    images = (filters.PHOTO | filters.Document.IMAGE) & private
    not_images = ~(filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND & private

    def on_text(callback):
        return MessageHandler(text, callback)

    def on_button(callback, pattern):
        return CallbackQueryHandler(callback, pattern=pattern)

    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start, filters=private),
            CommandHandler("language", language_cmd, filters=private),
        ],
        states={
            LANG: [on_button(lang_chosen, r"^lang:"), on_text(use_buttons(LANG))],
            ROLE: [on_button(role_chosen, r"^role:"), on_text(use_buttons(ROLE))],
            # ---- tutor registration ----
            T_NAME: [on_text(text_step("name", "ask_phone", T_PHONE, 80))],
            T_PHONE: [on_text(phone_step("ask_gender", T_GENDER, gender_keyboard, T_PHONE))],
            T_GENDER: [on_button(tutor_gender, r"^g:"), on_text(use_buttons(T_GENDER))],
            T_SUBJECTS: [on_text(text_step("subjects", "ask_grades", T_GRADES))],
            T_GRADES: [on_text(text_step("grades", "ask_location", T_LOCATION))],
            T_LOCATION: [on_text(text_step("location", "ask_qual", T_QUAL))],
            T_QUAL: [on_text(text_step("qual", "ask_id", T_ID, 300))],
            T_ID: [MessageHandler(images, tutor_id_photo), MessageHandler(not_images, need_photo(T_ID))],
            # ---- client request ----
            C_NAME: [on_text(text_step("name", "ask_phone", C_PHONE, 80))],
            C_PHONE: [on_text(phone_step("c_ask_subject", C_SUBJECT, None, C_PHONE))],
            C_SUBJECT: [on_text(text_step("subject", "c_ask_grade", C_GRADE))],
            C_GRADE: [on_text(client_grade)],
            C_CITY: [on_button(client_city, r"^city:"), on_text(use_buttons(C_CITY))],
            C_PLACE: [on_text(client_place)],
            C_GENDER: [on_button(client_gender_pref, r"^gp:"), on_text(use_buttons(C_GENDER))],
            C_DURATION: [on_text(text_step("duration", "c_ask_budget", C_BUDGET, 80))],
            C_BUDGET: [on_text(client_budget)],
            C_ID: [MessageHandler(images, client_id_photo), MessageHandler(not_images, need_photo(C_ID))],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, on_timeout), CallbackQueryHandler(on_timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel, filters=private),
            CommandHandler("start", start, filters=private),
            CommandHandler("language", language_cmd, filters=private),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT,
    )


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN is missing!")
    if not ADMIN_CHAT_ID:
        logger.warning("ADMIN_CHAT_ID is not set - approval requests cannot be delivered.")
    if not ADMIN_IDS:
        logger.warning("No admin user IDs found - set ADMIN_IDS (comma-separated Telegram user IDs).")

    start_health_server()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(build_conversation())
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(stale_button, pattern=r"^(lang|role|g|city|gp):"))
    app.add_handler(CommandHandler("filled", filled_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("help", help_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, fallback_text))
    app.add_error_handler(on_error)

    logger.info("Bot starting (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
