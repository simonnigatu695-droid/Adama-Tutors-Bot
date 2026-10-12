"""
Horizon Tutors Hub - Telegram bot (English only)
Telegram + Google Sheets. Hosted on Render.
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

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("horizon")

# ----------------------------------------------------------------------------
# Environment variables
# ----------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
JOB_CHANNEL_ID = os.environ.get("JOB_CHANNEL_ID", "@HorizonTutorsHub")
GOOGLE_JSON = os.environ.get("GOOGLE_JSON")
SHEET_NAME = os.environ.get("SHEET_NAME", "Horizon Tutors Hub")
TRIAL_THRESHOLD = float(os.environ.get("TRIAL_THRESHOLD", "4000"))
MIN_BUDGET = float(os.environ.get("MIN_BUDGET", "300"))
CONVERSATION_TIMEOUT = int(os.environ.get("CONVERSATION_TIMEOUT", "1800"))
# true = clients must upload an ID. false = skip it (you phone the client to verify instead).
REQUIRE_CLIENT_ID = os.environ.get("REQUIRE_CLIENT_ID", "true").strip().lower() == "true"
TZ = ZoneInfo("Africa/Addis_Ababa")
CHANNEL_URL = "https://t.me/HorizonTutorsHub"


def _parse_admin_ids() -> set:
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
    ROLE,
    T_NAME, T_PHONE, T_GENDER, T_SUBJECTS, T_GRADES, T_LOCATION, T_QUAL, T_ID,
    C_NAME, C_PHONE, C_SUBJECT, C_GRADE, C_CITY, C_PLACE, C_GENDER, C_DURATION, C_BUDGET, C_ID,
) = range(19)

GENDER_PREFS = {"female": "Female Tutor", "male": "Male Tutor", "any": "Any Tutor"}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


def clip(text, limit: int = 120) -> str:
    return " ".join((text or "").split())[:limit]


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def uname(user) -> str:
    return f"@{user.username}" if user.username else "N/A"


PHONE_RE = re.compile(r"^(?:\+?251|0)?([79]\d{8})$")


def normalize_phone(text: str):
    cleaned = re.sub(r"[\s\-()]", "", text or "")
    match = PHONE_RE.match(cleaned)
    return "0" + match.group(1) if match else None


def parse_budget(text: str) -> float:
    cleaned = (text or "").lower().replace(",", "").replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)(k)?", cleaned)
    if not match:
        return 0.0
    value = float(match.group(1))
    return value * 1000 if match.group(2) else value


def get_commission_terms(budget: float) -> str:
    if budget > TRIAL_THRESHOLD:
        return "1-Week Trial applies. Commission is due after the 1-week trial ends."
    return "1-Week Trial applies (for satisfaction check). Commission must be paid by the tutor on the FIRST DAY of the job."


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


def channel_markup(label: str = "📢 Join Channel (@HorizonTutorsHub)") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=CHANNEL_URL)]])


async def reply(message, text, markup=None):
    return await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


def form(context) -> dict:
    return context.user_data.setdefault("form", {})


def get_file(message):
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
        "Grades", "Location", "Qualification", "ID Photo", "Status",
    ],
    "Client_Requests": [
        "Timestamp", "Job Code", "Telegram ID", "Username", "Client Name", "Phone", "Subject",
        "Grade", "City", "Place", "Tutor Gender Pref", "Duration", "Budget", "Budget Number",
        "ID Photo", "Commission Terms", "Verification", "Job Status", "Channel Msg ID",
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
                # Older layout: keep its data under a new name and start a fresh tab.
                backup = f"{name}_old_{datetime.now(TZ):%Y%m%d_%H%M}"
                tab.update_title(backup)
                logger.warning("Tab '%s' had different columns - renamed to '%s'", name, backup)
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
                except Exception as exc:
                    last_error = exc
                    logger.warning("Sheets error (will retry once): %s", exc)
                    self._ss = None
            raise DBError(str(last_error))

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
    """Run a blocking Sheets operation without freezing the bot."""
    return await asyncio.to_thread(sheets.call, fn, *args)


# ----------------------------------------------------------------------------
# Admin helpers
# ----------------------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def notify_admin(context, text, markup=None, file_id=None, kind=None):
    if not ADMIN_CHAT_ID:
        logger.error("ADMIN_CHAT_ID not set")
        return None
    bot, chat = context.bot, ADMIN_CHAT_ID
    try:
        if file_id and len(text) <= 1000:
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
    msg = query.message
    if msg.caption is not None:
        await query.edit_message_caption(caption=f"{msg.caption_html}\n\n{status_line}", parse_mode=ParseMode.HTML)
    else:
        await query.edit_message_text(text=f"{msg.text_html}\n\n{status_line}", parse_mode=ParseMode.HTML)


async def tell_user(context, user_id, text, markup=None) -> bool:
    try:
        await context.bot.send_message(int(user_id), text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return True
    except Exception:
        logger.warning("Could not message user %s", user_id)
        return False


# ----------------------------------------------------------------------------
# Start and role selection
# ----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args and args[0].startswith("apply_"):
        await handle_apply(update, context, args[0][len("apply_"):])
        return ConversationHandler.END
    context.user_data.pop("form", None)

    markup = kb([("🎓 Register as Tutor", "role:tutor")], [("📖 Request a Tutor", "role:client")])
    await reply(
        update.message,
        "Welcome to <b>Horizon Tutors Hub</b>! 👋\n\n"
        "Your information (including ID photos) is used only to verify and match tutors and families, "
        "and is never shared publicly.\n\nPlease select an option below:",
        markup,
    )
    return ROLE


async def role_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(None)
    user = update.effective_user
    context.user_data["form"] = {}

    if query.data == "role:tutor":
        try:
            existing = await db(sheets.find_last, "Tutors", "Telegram ID", user.id)
        except DBError:
            existing = None
        if existing:
            status = existing.get("Status")
            if status == "Approved":
                await reply(
                    query.message,
                    "✅ Your tutor profile is already approved! Make sure you are subscribed to our channel to view job opportunities:",
                    channel_markup("📢 Join Channel"),
                )
                return ConversationHandler.END
            if status == "Pending":
                await reply(query.message, "⏳ Your tutor registration and National ID are currently pending Admin review.")
                return ConversationHandler.END

        await reply(query.message, "Great! Let's get you registered as a tutor.\n\nPlease enter your <b>Full Name</b>:")
        return T_NAME

    await reply(query.message, "Great! Let's find you a tutor.\n\nPlease enter your <b>Full Name</b> (Parent or Student):")
    return C_NAME


def text_step(field: str, next_prompt: str, next_state: int, limit: int = 120):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        form(context)[field] = clip(update.message.text, limit)
        await reply(update.message, next_prompt)
        return next_state
    return handler


def phone_step(next_prompt: str, next_state: int, keyboard_fn=None, retry_state: int = None):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        phone = normalize_phone(update.message.text)
        if not phone:
            await reply(update.message, "⚠️ Invalid phone format. Please enter a valid Ethiopian mobile number (e.g., 0911234567 or +251911234567):")
            return retry_state
        form(context)["phone"] = phone
        await reply(update.message, next_prompt, keyboard_fn() if keyboard_fn else None)
        return next_state
    return handler


async def use_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Someone typed text where a button tap was expected."""
    await reply(update.message, "👆 Please tap one of the buttons above, or send /cancel to stop.")


async def need_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Someone sent text where an ID photo was expected."""
    await reply(update.message, "📷 Please send a clear <b>photo</b> (or image file) of your ID card, or send /cancel to stop.")


# ----------------------------------------------------------------------------
# Tutor registration
# ----------------------------------------------------------------------------
async def tutor_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(None)
    form(context)["gender"] = query.data.split(":")[1]
    await reply(query.message, "Which <b>Subjects</b> do you teach? (e.g., Mathematics, Physics):")
    return T_SUBJECTS


async def tutor_id_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, f = update.effective_user, form(context)
    file_id, kind = get_file(update.message)
    if not file_id:
        await need_photo(update, context)
        return T_ID

    row = [
        now_str(), str(user.id), uname(user), f.get("name", ""), f.get("phone", ""),
        f.get("gender", ""), f.get("subjects", ""), f.get("grades", ""), f.get("location", ""),
        f.get("qual", ""), file_id, "Pending",
    ]
    saved = True
    try:
        await db(sheets.append, "Tutors", row)
    except DBError as exc:
        logger.error("COULD NOT SAVE tutor to Google Sheets: %s", exc)
        saved = False

    text = (
        "🆕 <b>NEW TUTOR REGISTRATION & ID SUBMISSION</b>\n\n"
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
    markup = kb([("✅ Approve ID", f"adm:app_tutor:{user.id}"), ("❌ Reject ID", f"adm:rej_tutor:{user.id}")])
    await notify_admin(context, text, markup, file_id, kind)

    await reply(
        update.message,
        "✅ <b>Registration & National ID Submitted!</b>\n\n"
        "Your profile is now under Admin review. Meanwhile, please join our Telegram channel to stay updated on new job postings:",
        channel_markup(),
    )
    context.user_data.pop("form", None)
    return ConversationHandler.END


# ----------------------------------------------------------------------------
# Client request
# ----------------------------------------------------------------------------
async def client_grade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    form(context)["grade"] = clip(update.message.text, 60)
    markup = kb([("Adama", "city:Adama"), ("Bishoftu", "city:Bishoftu")])
    await reply(update.message, "Select your <b>City</b>:", markup)
    return C_CITY


async def client_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(None)
    form(context)["city"] = query.data.split(":")[1]
    await reply(query.message, "Enter your <b>Specific Location / Kebele / Neighborhood</b> (e.g., Kebele 10):")
    return C_PLACE


async def client_place(update: Update, context: ContextTypes.DEFAULT_TYPE):
    form(context)["place"] = clip(update.message.text, 100)
    markup = kb(
        [("Female Tutor", "gp:female"), ("Male Tutor", "gp:male")],
        [("Any Tutor", "gp:any")],
    )
    await reply(update.message, "Select your preference for <b>Tutor Gender</b>:", markup)
    return C_GENDER


async def client_gender_pref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(None)
    form(context)["gender_pref"] = GENDER_PREFS.get(query.data.split(":")[1], "Any Tutor")
    await reply(query.message, "Enter the <b>Schedule / Duration</b> (e.g., 3 days and 2 hours per week):")
    return C_DURATION


async def client_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = clip(update.message.text, 40)
    value = parse_budget(text)
    if value < MIN_BUDGET:
        await reply(update.message, f"⚠️ Please enter a realistic budget amount (minimum {MIN_BUDGET:,.0f} ETB):")
        return C_BUDGET
    form(context)["budget"] = text
    form(context)["budget_value"] = value
    if REQUIRE_CLIENT_ID:
        await reply(update.message, "🪪 Please upload a clear photo or document of your <b>National / Kebele ID</b> for verification:")
        return C_ID
    await finalize_client(update, context)
    return ConversationHandler.END


async def client_id_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id, kind = get_file(update.message)
    if not file_id:
        await need_photo(update, context)
        return C_ID
    await finalize_client(update, context, file_id, kind)
    return ConversationHandler.END


async def finalize_client(update, context, file_id=None, kind=None):
    """Save the request and send it to the admin. It is NOT posted publicly until approved."""
    user, f = update.effective_user, form(context)
    try:
        job = await db(sheets.next_job_code)
    except DBError as exc:
        logger.error("Could not get job code from Settings tab: %s", exc)
        job = f"#R{int(time.time()) % 100000}"

    budget_val = f.get("budget_value", 0)
    terms = get_commission_terms(budget_val)

    row = [
        now_str(), job, str(user.id), uname(user), f.get("name", ""), f.get("phone", ""),
        f.get("subject", ""), f.get("grade", ""), f.get("city", ""), f.get("place", ""),
        f.get("gender_pref", ""), f.get("duration", ""), f.get("budget", ""), budget_val,
        file_id or "", terms, "Pending", "Pending", "",
    ]
    saved = True
    try:
        await db(sheets.append, "Client_Requests", row)
    except DBError as exc:
        logger.error("COULD NOT SAVE client request %s to Google Sheets: %s", job, exc)
        saved = False

    text = (
        "🆕 <b>NEW CLIENT REQUEST</b>\n\n"
        f"📌 <b>Job Code:</b> <code>{esc(job)}</code>\n"
        f"👤 <b>Client:</b> {esc(f.get('name'))}\n"
        f"📞 <b>Phone:</b> {esc(f.get('phone'))}\n"
        f"📚 <b>Subject:</b> {esc(f.get('subject'))}\n"
        f"🎓 <b>Grade:</b> {esc(f.get('grade'))}\n"
        f"📍 <b>Location:</b> {esc(f.get('city'))} - {esc(f.get('place'))}\n"
        f"⚧ <b>Tutor Gender Pref:</b> {esc(f.get('gender_pref'))}\n"
        f"⏱ <b>Duration:</b> {esc(f.get('duration'))}\n"
        f"💰 <b>Salary:</b> {budget_val:,.0f} ETB\n"
        f"📋 <b>Commission Policy:</b> {esc(terms)}\n"
        f"💬 <b>Telegram:</b> {esc(uname(user))} (ID: <code>{user.id}</code>)"
    )
    if not REQUIRE_CLIENT_ID:
        text += "\n\n☎️ No ID requested - call the client to verify before approving."
    if not saved:
        text += "\n\n⚠️ Could not save to Google Sheets - please record this request manually."
    code = job.lstrip("#")
    markup = kb([("✅ Approve & Publish", f"adm:app_client:{code}"), ("❌ Reject", f"adm:rej_client:{code}")])
    await notify_admin(context, text, markup, file_id, kind)

    await reply(
        update.message,
        f"✅ <b>Tutor Request Received!</b>\n\n"
        f"📌 <b>Job Code:</b> <code>{esc(job)}</code>\n"
        f"⚖️ <b>Applied Policy:</b> {esc(terms)}\n\n"
        + ("Your ID is under Admin verification. " if REQUIRE_CLIENT_ID else "Our team will call you to verify the request. ")
        + "Once approved, your request will be published to our channel.",
        channel_markup(),
    )
    context.user_data.pop("form", None)


# ----------------------------------------------------------------------------
# Job post (channel)
# ----------------------------------------------------------------------------
def build_job_post(row: dict, status: str = "Open") -> str:
    head = "🚨 Open" if status == "Open" else "🔒 Filled"
    budget_num = row.get("Budget Number") or row.get("Budget")
    try:
        salary_str = f"{float(budget_num):,.0f}"
    except (ValueError, TypeError):
        salary_str = str(budget_num)

    return (
        f"{head}\n\n"
        f"📖 {esc(row.get('Tutor Gender Pref'))} 📖\n\n"
        f"Job code: {esc(row.get('Job Code'))}\n\n"
        f"✅ Grade : {esc(row.get('Grade'))}\n"
        f"✅ City : {esc(row.get('City'))}\n"
        f"✅ Place : {esc(row.get('Place'))}\n"
        f"✅ Subject : {esc(row.get('Subject'))}\n"
        f"✅ Duration : {esc(row.get('Duration'))}\n"
        f"✅ Salary : {esc(salary_str)}\n\n"
        f"📖 We need highly committed, punctual and talented tutor\n\n"
        f"Tap the button below to apply."
    )


def apply_markup(context, code: str) -> InlineKeyboardMarkup:
    url = f"https://t.me/{context.bot.username}?start=apply_{code}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("📩 Apply for this job", url=url)]])


# ----------------------------------------------------------------------------
# Tutor applies for a job (deep link from the channel button)
# ----------------------------------------------------------------------------
async def handle_apply(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    message, user = update.message, update.effective_user
    code = code.upper()
    if not re.fullmatch(r"R\d{1,6}", code):
        await reply(message, "⚠️ Invalid or expired job link.")
        return
    job_code = "#" + code

    try:
        tutor = await db(sheets.find_last, "Tutors", "Telegram ID", user.id)
        job = await db(sheets.find_last, "Client_Requests", "Job Code", job_code)

        if not tutor or tutor.get("Status") != "Approved":
            await reply(message, "⚠️ You must register and have your National ID approved as a tutor before applying for jobs. Send /start to register.")
            return
        if not job or job.get("Verification") != "Approved" or job.get("Job Status") != "Open":
            await reply(message, "⚠️ This job posting is no longer active.")
            return
        pref, gender = job.get("Tutor Gender Pref"), tutor.get("Gender")
        if (pref == "Female Tutor" and gender != "Female") or (pref == "Male Tutor" and gender != "Male"):
            await reply(message, f"⚠️ This job specifically requests a {esc(pref)}.")
            return
        if await db(sheets.has_application, job_code, user.id):
            await reply(message, f"⚠️ You have already applied for job <code>{esc(job_code)}</code>.")
            return

        await db(
            sheets.append, "Applications",
            [now_str(), job_code, str(user.id), tutor.get("Name", ""), tutor.get("Phone", ""), "Applied"],
        )
    except DBError as exc:
        logger.error("Application failed: %s", exc)
        await reply(message, "⚠️ System temporarily unavailable. Please try again in a few moments.")
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
        f"💬 {esc(uname(user))} (ID: <code>{user.id}</code>)"
    )
    markup = kb([("📨 Send profile to client", f"adm:send:{code}:{user.id}")])
    await notify_admin(context, text, markup)
    await reply(message, f"✅ Application submitted successfully for <code>{esc(job_code)}</code>! Our team will contact you if matched.")


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
    except DBError as exc:
        logger.error("Admin action failed: %s", exc)
        await query.message.reply_text("⚠️ Google Sheets unavailable. Retry in a moment.")


async def decide_tutor(context, query, approve: bool, tutor_id: int, who: str):
    row = await db(sheets.find_last, "Tutors", "Telegram ID", tutor_id)
    if row and row.get("Status") != "Pending":
        await mark_done(query, f"ℹ️ Already {esc(row.get('Status'))}.")
        return
    status = "Approved" if approve else "Rejected"
    if row:
        await db(sheets.update, "Tutors", row["_row"], {"Status": status})

    if approve:
        msg = "🎉 <b>Congratulations!</b> Your National ID and tutor profile have been approved. Join our channel to apply for open job postings:"
        delivered = await tell_user(context, tutor_id, msg, channel_markup())
    else:
        msg = "⚠️ Your tutor National ID verification was not approved. Please contact support or re-register with a clearer ID photo."
        delivered = await tell_user(context, tutor_id, msg)

    line = f"{'✅ APPROVED' if approve else '❌ REJECTED'} by {who}"
    if not row:
        line += "\n⚠️ Not found in the sheet - nothing was updated there."
    if not delivered:
        line += "\n⚠️ Could not deliver message to user."
    await mark_done(query, line)


async def decide_client(context, query, approve: bool, code: str, who: str):
    job_code = "#" + code
    row = await db(sheets.find_last, "Client_Requests", "Job Code", job_code)
    if not row:
        await mark_done(query, "⚠️ Job not found in sheet.")
        return
    if row.get("Verification") != "Pending":
        await mark_done(query, f"ℹ️ Already {esc(row.get('Verification'))}.")
        return

    client_id = row.get("Telegram ID")

    if not approve:
        await db(sheets.update, "Client_Requests", row["_row"], {"Verification": "Rejected", "Job Status": "Rejected"})
        await tell_user(context, client_id, f"⚠️ Your verification for Job Code <code>{esc(job_code)}</code> was not approved. Contact support for help.")
        await mark_done(query, f"❌ REJECTED by {who}")
        return

    await db(sheets.update, "Client_Requests", row["_row"], {"Verification": "Approved", "Job Status": "Open"})
    note = ""
    if JOB_CHANNEL_ID:
        try:
            posted = await context.bot.send_message(
                JOB_CHANNEL_ID, build_job_post(row, "Open"),
                parse_mode=ParseMode.HTML, reply_markup=apply_markup(context, code),
            )
            await db(sheets.update, "Client_Requests", row["_row"], {"Channel Msg ID": posted.message_id})
        except Exception as e:
            logger.exception("Could not post to channel: %s", e)
            note += "\n⚠️ Channel posting error."

    await tell_user(
        context, client_id,
        f"✅ Your request <code>{esc(job_code)}</code> is verified and now published to our channel!",
        channel_markup("📢 Channel Link"),
    )
    await mark_done(query, f"✅ APPROVED & PUBLISHED by {who}{note}")


async def send_profile(context, query, code: str, tutor_id: int, who: str):
    job_code = "#" + code
    job = await db(sheets.find_last, "Client_Requests", "Job Code", job_code)
    tutor = await db(sheets.find_last, "Tutors", "Telegram ID", tutor_id)
    if not job or not tutor:
        await query.message.reply_text("⚠️ Job or tutor not found in sheet.")
        return

    profile_text = (
        f"🧑‍🏫 <b>RECOMMENDED TUTOR PROFILE</b> for Job <code>{esc(job_code)}</code>\n\n"
        f"👤 <b>Name:</b> {esc(tutor.get('Name'))}\n"
        f"⚧ <b>Gender:</b> {esc(tutor.get('Gender'))}\n"
        f"📚 <b>Subjects:</b> {esc(tutor.get('Subjects'))}\n"
        f"🎓 <b>Grades:</b> {esc(tutor.get('Grades'))}\n"
        f"📍 <b>Location:</b> {esc(tutor.get('Location'))}\n"
        f"📜 <b>Qualifications:</b> {esc(clip(tutor.get('Qualification'), 250))}\n\n"
        f"Our team will assist you with setting up the trial schedule!"
    )
    delivered = await tell_user(context, job.get("Telegram ID"), profile_text)
    if delivered:
        await db(sheets.set_application_status, job_code, tutor_id, "Sent to client")
        await mark_done(query, f"📨 Profile dispatched to client by {who}")
    else:
        await query.message.reply_text("⚠️ Could not message client.")


# ----------------------------------------------------------------------------
# Admin commands
# ----------------------------------------------------------------------------
async def catchup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/catchup - pending tutors and client requests."""
    if not is_admin(update.effective_user.id):
        return
    try:
        tutors = await db(sheets.rows, "Tutors")
        clients = await db(sheets.rows, "Client_Requests")
    except DBError:
        await reply(update.message, "⚠️ Unable to fetch data from Google Sheets.")
        return
    pending_t = [r for r in tutors if r.get("Status") == "Pending"]
    pending_c = [r for r in clients if r.get("Verification") == "Pending"]

    msg = "🔄 <b>CATCHUP REPORT</b>\n\n"
    msg += f"🎓 <b>Pending Tutors ({len(pending_t)}):</b>\n"
    for r in pending_t[:5]:
        msg += f"• {esc(r.get('Name'))} ({esc(r.get('Phone'))})\n"
    msg += f"\n📖 <b>Pending Client Requests ({len(pending_c)}):</b>\n"
    for r in pending_c[:5]:
        msg += f"• {esc(r.get('Job Code'))}: {esc(r.get('Client Name'))} ({esc(r.get('City'))})\n"
    await reply(update.message, msg)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats - overview numbers."""
    if not is_admin(update.effective_user.id):
        return
    try:
        tutors = await db(sheets.rows, "Tutors")
        jobs = await db(sheets.rows, "Client_Requests")
        apps = await db(sheets.rows, "Applications")
    except DBError:
        await reply(update.message, "⚠️ Sheets unavailable.")
        return
    approved_t = sum(1 for r in tutors if r.get("Status") == "Approved")
    pending_t = sum(1 for r in tutors if r.get("Status") == "Pending")
    open_j = sum(1 for r in jobs if r.get("Job Status") == "Open")
    filled_j = sum(1 for r in jobs if r.get("Job Status") == "Filled")
    await reply(
        update.message,
        "📊 <b>HORIZON TUTORS HUB METRICS</b>\n\n"
        f"🎓 <b>Tutors:</b> {approved_t} Approved | {pending_t} Pending\n"
        f"📖 <b>Jobs:</b> {open_j} Open | {filled_j} Filled | {len(jobs)} Total\n"
        f"📩 <b>Applications:</b> {len(apps)} Total",
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search <text> - find tutors and jobs by name, phone or job code."""
    if not is_admin(update.effective_user.id):
        return
    query = " ".join(context.args).strip().lower()
    if not query:
        await reply(update.message, "Usage: <code>/search</code> followed by a name, phone number or job code.")
        return
    try:
        tutors = await db(sheets.rows, "Tutors")
        jobs = await db(sheets.rows, "Client_Requests")
    except DBError:
        await reply(update.message, "⚠️ Google Sheets search error.")
        return

    t_matches = [r for r in tutors if query in r.get("Name", "").lower() or query in r.get("Phone", "").lower()]
    j_matches = [
        r for r in jobs
        if query in r.get("Job Code", "").lower() or query in r.get("Client Name", "").lower() or query in r.get("Phone", "").lower()
    ]
    res = f"🔎 <b>SEARCH RESULTS FOR '{esc(query)}'</b>\n\n"
    if t_matches:
        res += f"<b>Tutors ({len(t_matches)}):</b>\n"
        for r in t_matches[:3]:
            res += f"• {esc(r.get('Name'))} - {esc(r.get('Phone'))} [{esc(r.get('Status'))}]\n"
    if j_matches:
        res += f"\n<b>Client Jobs ({len(j_matches)}):</b>\n"
        for r in j_matches[:3]:
            res += f"• {esc(r.get('Job Code'))}: {esc(r.get('Client Name'))} - {esc(r.get('Phone'))} [{esc(r.get('Job Status'))}]\n"
    if not t_matches and not j_matches:
        res += "No matching records found."
    await reply(update.message, res)


async def testblur_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/testblur - health check, also tests the Google Sheet connection."""
    if not is_admin(update.effective_user.id):
        return
    try:
        await db(sheets.rows, "Client_Requests")
        sheet_state = "✅ Google Sheet connected"
    except DBError as exc:
        sheet_state = f"❌ Google Sheet problem: {esc(str(exc)[:200])}"
    await reply(update.message, f"🧪 <b>System Check:</b> Bot is active.\n{sheet_state}")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast <message> - announcement to all approved tutors."""
    if not is_admin(update.effective_user.id):
        return
    msg_text = " ".join(context.args).strip()
    if not msg_text:
        await reply(update.message, "Usage: <code>/broadcast</code> followed by your message.")
        return
    try:
        tutors = await db(sheets.rows, "Tutors")
    except DBError:
        await reply(update.message, "⚠️ Broadcast error accessing database.")
        return
    count = 0
    for r in tutors:
        if r.get("Status") == "Approved" and r.get("Telegram ID"):
            if await tell_user(context, r["Telegram ID"], f"📢 <b>ANNOUNCEMENT FROM HORIZON TUTORS HUB:</b>\n\n{esc(msg_text)}"):
                count += 1
            await asyncio.sleep(0.05)
    await reply(update.message, f"✅ Broadcast delivered to {count} approved tutors.")


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
            await update.message.reply_text(f"{job_code} not found.")
            return
        await db(sheets.update, "Client_Requests", row["_row"], {"Job Status": "Filled"})
    except DBError:
        await update.message.reply_text("⚠️ Google Sheets error.")
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
            logger.exception("Could not edit channel post")

    await update.message.reply_text(
        f"🔒 Job {job_code} marked as Filled." + ("" if edited else " (Channel post update failed)")
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("form", None)
    await reply(update.message, "Operation cancelled. Send /start to begin again.")
    return ConversationHandler.END


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("form", None)
    try:
        await reply(update.effective_message, "⌛ This session expired. Send /start to begin again.")
    except Exception:
        pass
    return ConversationHandler.END


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Something went wrong. Please send /start and try again.")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Health server (Render / uptime pinger)
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
    logger.info("Health check server listening on port %s", port)


# ----------------------------------------------------------------------------
# Application setup
# ----------------------------------------------------------------------------
async def post_init(app):
    try:
        await asyncio.to_thread(sheets.call, lambda: None)
        logger.info("Google Sheet connected and ready")
    except DBError as exc:
        logger.error("STARTUP: Google Sheet check FAILED: %s", exc)
    if not ADMIN_IDS:
        logger.error("STARTUP: ADMIN_IDS / ADMIN_CHAT_ID not set - admin buttons will not work")

    await app.bot.set_my_commands([
        ("start", "Register or request a tutor"),
        ("cancel", "Cancel what you are doing"),
    ])


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN is missing!")

    start_health_server()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    private = filters.ChatType.PRIVATE
    text_filter = filters.TEXT & ~filters.COMMAND & private
    images_filter = (filters.PHOTO | filters.Document.ALL) & private

    def text_in(handler):
        return MessageHandler(text_filter, handler)

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start, filters=private)],
        states={
            ROLE: [CallbackQueryHandler(role_chosen, pattern=r"^role:"), text_in(use_buttons)],
            # ---- tutor registration ----
            T_NAME: [text_in(text_step("name", "Enter your <b>Phone Number</b> (e.g., 0911234567):", T_PHONE, 80))],
            T_PHONE: [text_in(phone_step("Select your <b>Gender</b>:", T_GENDER, lambda: kb([("Male", "g:Male"), ("Female", "g:Female")]), T_PHONE))],
            T_GENDER: [CallbackQueryHandler(tutor_gender, pattern=r"^g:"), text_in(use_buttons)],
            T_SUBJECTS: [text_in(text_step("subjects", "Which <b>Grade Levels</b> do you teach? (e.g., Grade 6 & 9):", T_GRADES))],
            T_GRADES: [text_in(text_step("grades", "Enter your <b>City/Location</b> (e.g., Adama - Kebele 10):", T_LOCATION))],
            T_LOCATION: [text_in(text_step("location", "State your <b>Qualifications & Experience</b>:", T_QUAL, 300))],
            T_QUAL: [text_in(text_step("qual", "🪪 Please upload a clear photo or document of your <b>National ID or Kebele ID</b>:", T_ID, 300))],
            T_ID: [MessageHandler(images_filter, tutor_id_photo), text_in(need_photo)],
            # ---- client request ----
            C_NAME: [text_in(text_step("name", "Enter your <b>Phone Number</b>:", C_PHONE, 80))],
            C_PHONE: [text_in(phone_step("Which <b>Subject(s)</b> do you need tutoring for?:", C_SUBJECT, None, C_PHONE))],
            C_SUBJECT: [text_in(text_step("subject", "What is the <b>Student's Grade Level</b>?:", C_GRADE))],
            C_GRADE: [text_in(client_grade)],
            C_CITY: [CallbackQueryHandler(client_city, pattern=r"^city:"), text_in(use_buttons)],
            C_PLACE: [text_in(client_place)],
            C_GENDER: [CallbackQueryHandler(client_gender_pref, pattern=r"^gp:"), text_in(use_buttons)],
            C_DURATION: [text_in(text_step("duration", "Enter the <b>Offered Monthly Salary / Budget</b> (e.g., 5500 ETB):", C_BUDGET))],
            C_BUDGET: [text_in(client_budget)],
            C_ID: [MessageHandler(images_filter, client_id_photo), text_in(need_photo)],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, on_timeout)],
        },
        fallbacks=[
            CommandHandler("start", start, filters=private),    # /start always works, even mid-form
            CommandHandler("cancel", cancel, filters=private),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT,
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))

    for name, fn in [
        ("catchup", catchup_cmd), ("stats", stats_cmd), ("search", search_cmd),
        ("testblur", testblur_cmd), ("broadcast", broadcast_cmd), ("filled", filled_cmd),
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_error_handler(on_error)

    logger.info("Bot starting (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
