#!/usr/bin/env python3
"""
Horizon Tutors Hub - Telegram bot (Adama & Bishoftu, Ethiopia)

Single-file, deploy-ready for Render.
Backend: Google Sheets ("Adama Tutors Database": tabs Tutors, Client_Requests)
         + Google Drive (Tutor_Uploads folder) for ID photos.

Required env vars : TELEGRAM_TOKEN, GOOGLE_CREDENTIALS_JSON (or credentials.json),
                    ADMIN_CHAT_ID, JOB_CHANNEL_ID
Optional env vars : DRIVE_FOLDER_ID, SPREADSHEET_NAME, SPREADSHEET_ID,
                    GOOGLE_CREDENTIALS_FILE, BLUR_THRESHOLD, STARTUP_WRITE_TEST,
                    RENDER_EXTERNAL_URL (auto-set by Render), PORT (auto-set by Render)
"""

import asyncio
import base64
import html
import io
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import gspread
import numpy as np
import requests
from flask import Flask
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
logger = logging.getLogger("horizon")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
ADMIN_CHAT_ID_RAW = os.environ.get("ADMIN_CHAT_ID", "").strip()
JOB_CHANNEL_ID_RAW = os.environ.get("JOB_CHANNEL_ID", "").strip()

DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1TPt6gHg4ff3PQIbj64ZiLUfIi3IZjQkP").strip()
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Adama Tutors Database").strip()
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()  # optional, more reliable than name
BLUR_THRESHOLD = float(os.environ.get("BLUR_THRESHOLD", "50"))
MIN_IMAGE_SIDE = 480
MAX_IMAGE_BYTES = 15 * 1024 * 1024
STARTUP_WRITE_TEST = os.environ.get("STARTUP_WRITE_TEST", "1") != "0"

ADMIN_CHAT_ID: int = 0           # set in validate_config()
JOB_CHANNEL_ID = None            # int or "@name", set in validate_config()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
EAT = timezone(timedelta(hours=3))  # East Africa Time


class ConfigError(Exception):
    pass


class SheetsError(Exception):
    pass


class DriveError(Exception):
    pass


def validate_config():
    """Fail fast with a clear message if required settings are missing."""
    global ADMIN_CHAT_ID, JOB_CHANNEL_ID
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not ADMIN_CHAT_ID_RAW:
        missing.append("ADMIN_CHAT_ID")
    if not JOB_CHANNEL_ID_RAW:
        missing.append("JOB_CHANNEL_ID")
    if missing:
        logger.critical("Missing required environment variables: %s", ", ".join(missing))
        raise SystemExit(1)
    try:
        ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
    except ValueError:
        logger.critical("ADMIN_CHAT_ID must be a number (got %r). Send /myid to the bot to find yours.",
                        ADMIN_CHAT_ID_RAW)
        raise SystemExit(1)
    raw = JOB_CHANNEL_ID_RAW
    if re.fullmatch(r"-?\d+", raw):
        JOB_CHANNEL_ID = int(raw)
    else:
        JOB_CHANNEL_ID = raw if raw.startswith("@") else "@" + raw
    logger.info("Config OK | admin=%s | channel=%s | drive_folder=%s | sheet=%s",
                ADMIN_CHAT_ID, JOB_CHANNEL_ID, DRIVE_FOLDER_ID, SPREADSHEET_ID or SPREADSHEET_NAME)


# --------------------------------------------------------------------------- #
# Google credentials (env JSON string -> base64 -> file fallbacks)
# --------------------------------------------------------------------------- #
def _parse_json_blob(raw: str, source: str) -> dict:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        raw = raw[1:-1].strip()
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except Exception:
            raise ConfigError(f"{source} is neither JSON nor base64-encoded JSON "
                              f"(first chars: {raw[:12]!r}...).")
    try:
        # strict=False tolerates literal newlines inside the private_key string
        return json.loads(raw, strict=False)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{source} is not valid JSON: {e.msg} (line {e.lineno}, col {e.colno}). "
                          "Paste the ENTIRE service-account file contents without extra quotes.")


def load_service_account_info() -> dict:
    tried = []
    info = None
    source = ""

    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw:
        if not raw.startswith("{") and len(raw) < 512 and Path(raw).is_file():
            source = f"file {raw} (from GOOGLE_CREDENTIALS_JSON)"
            info = _parse_json_blob(Path(raw).read_text(encoding="utf-8"), source)
        else:
            source = "env GOOGLE_CREDENTIALS_JSON"
            info = _parse_json_blob(raw, source)
    else:
        tried.append("env GOOGLE_CREDENTIALS_JSON (not set)")
        candidates = [
            os.environ.get("GOOGLE_CREDENTIALS_FILE", "").strip() or None,
            str(Path(__file__).resolve().with_name("credentials.json")),
            str(Path.cwd() / "credentials.json"),
            "/etc/secrets/credentials.json",  # Render "Secret Files" location
        ]
        for cand in candidates:
            if not cand:
                continue
            if Path(cand).is_file():
                source = f"file {cand}"
                info = _parse_json_blob(Path(cand).read_text(encoding="utf-8"), source)
                break
            tried.append(f"{cand} (not found)")

    if info is None:
        raise ConfigError("No Google credentials found. Tried: " + "; ".join(tried))

    if not isinstance(info, dict):
        raise ConfigError(f"{source}: expected a JSON object.")
    required = ["type", "client_email", "private_key"]
    absent = [k for k in required if not info.get(k)]
    if absent:
        raise ConfigError(f"{source}: missing keys {absent}. Use a SERVICE ACCOUNT key file.")
    if info["type"] != "service_account":
        raise ConfigError(f"{source}: type is {info['type']!r}, expected 'service_account'.")
    info["private_key"] = info["private_key"].replace("\\n", "\n")
    logger.info("Google credentials loaded from %s | service account: %s", source, info["client_email"])
    return info


_creds = None
_creds_lock = threading.Lock()


def get_credentials() -> Credentials:
    global _creds
    with _creds_lock:
        if _creds is None:
            info = load_service_account_info()
            try:
                _creds = Credentials.from_service_account_info(info, scopes=SCOPES)
            except Exception as e:
                raise ConfigError(f"Service-account key rejected by google-auth: {e}")
        return _creds


def service_account_email() -> str:
    return get_credentials().service_account_email


# --------------------------------------------------------------------------- #
# Google Sheets layer (Aligned with existing sheet headers)
# --------------------------------------------------------------------------- #
TUTOR_HEADERS = [
    "Timestamp", "Tutor Code", "Telegram ID", "Username", "Name", "Phone", "Gender",
    "Subjects", "Grades", "Location", "Qualification", "ID Verification Status",
    "Status", "ID File ID"
]
CLIENT_HEADERS = [
    "Timestamp", "Job Code", "Telegram ID", "Username", "Client Name", "Phone",
    "Subject", "Grade", "City", "Place", "Tutor Gender Pref", "Duration",
    "Budget", "Budget Number", "Commission Terms", "Verification", "Job Status",
    "Channel Msg ID"
]
TABS = {
    "Tutors": {"headers": TUTOR_HEADERS, "code": "Tutor Code"},
    "Client_Requests": {"headers": CLIENT_HEADERS, "code": "Job Code"},
}

SHEETS_LOCK = threading.RLock()
_spreadsheet = None


def _sheets_hint(err: str) -> str:
    low = err.lower()
    if "has not been used" in low or "accessnotconfigured" in low or "service_disabled" in low:
        return " HINT: enable the Google Sheets API and Google Drive API in your Google Cloud project."
    if "permission" in low or "403" in low:
        return f" HINT: share the Google Sheet with {service_account_email()} as Editor."
    return ""


def get_spreadsheet():
    global _spreadsheet
    with SHEETS_LOCK:
        if _spreadsheet is None:
            gc = gspread.authorize(get_credentials())
            try:
                _spreadsheet = gc.open_by_key(SPREADSHEET_ID) if SPREADSHEET_ID else gc.open(SPREADSHEET_NAME)
            except gspread.SpreadsheetNotFound:
                raise SheetsError(
                    f"Spreadsheet '{SPREADSHEET_ID or SPREADSHEET_NAME}' not found or not shared. "
                    f"Share it with {service_account_email()} as Editor (check the exact name).")
            except Exception as e:
                raise SheetsError(f"Could not open spreadsheet: {e!r}." + _sheets_hint(str(e)))
        return _spreadsheet


def get_worksheet(tab: str):
    ss = get_spreadsheet()
    try:
        return ss.worksheet(tab)
    except gspread.WorksheetNotFound:
        found = [w.title for w in ss.worksheets()]
        raise SheetsError(f"Tab '{tab}' not found. Tabs present: {found}. Tab names are case-sensitive.")


def _reset_sheet_connection():
    global _spreadsheet
    _spreadsheet = None


def _read_headers(ws, tab: str) -> list:
    headers = ws.row_values(1)
    if not headers:
        raise SheetsError(f"Tab '{tab}' has an empty header row (row 1).")
    missing = [h for h in TABS[tab]["headers"] if h not in headers]
    if missing:
        raise SheetsError(f"Tab '{tab}' is missing header columns: {missing}. Found: {headers}")
    return headers


def append_with_code(tab: str, prefix: str, data: dict):
    """Atomically generate a unique code and append the row. Returns (code, updated_range)."""
    cfg = TABS[tab]
    code_header = cfg["code"]
    with SHEETS_LOCK:
        try:
            ws = get_worksheet(tab)
            headers = _read_headers(ws, tab)
            existing = set(ws.col_values(headers.index(code_header) + 1))
            code = None
            for attempt in range(60):
                digits = 4 if attempt < 30 else 5
                lo, hi = 10 ** (digits - 1), 10 ** digits - 1
                candidate = f"#{prefix}-{random.randint(lo, hi)}"
                if candidate not in existing:
                    code = candidate
                    break
            if code is None:
                raise SheetsError("Could not generate a unique code.")
            row_data = dict(data)
            row_data[code_header] = code
            row = [str(row_data.get(h, "")) for h in headers]
            resp = ws.append_row(row, value_input_option="RAW")
            updated = (resp or {}).get("updates", {}).get("updatedRange", "?")
            logger.info("Sheets append OK | tab=%s | code=%s | range=%s", tab, code, updated)
            return code, updated
        except SheetsError:
            raise
        except Exception as e:
            _reset_sheet_connection()
            raise SheetsError(f"Append to '{tab}' failed: {e!r}." + _sheets_hint(str(e)))


def find_record(tab: str, code: str):
    """Returns (row_number, {header: value}) or None."""
    cfg = TABS[tab]
    with SHEETS_LOCK:
        try:
            ws = get_worksheet(tab)
            headers = _read_headers(ws, tab)
            codes = ws.col_values(headers.index(cfg["code"]) + 1)
            if code not in codes:
                return None
            row_no = codes.index(code) + 1
            values = ws.row_values(row_no)
            values += [""] * (len(headers) - len(values))
            return row_no, dict(zip(headers, values))
        except SheetsError:
            raise
        except Exception as e:
            _reset_sheet_connection()
            raise SheetsError(f"Lookup in '{tab}' failed: {e!r}." + _sheets_hint(str(e)))


def set_status(tab: str, row_no: int, status: str):
    with SHEETS_LOCK:
        try:
            ws = get_worksheet(tab)
            headers = _read_headers(ws, tab)
            # Find status column ("Status" for Tutors, "Verification" or "Job Status" for Client Requests)
            status_col_name = "Status" if tab == "Tutors" else "Verification"
            if status_col_name not in headers:
                status_col_name = "Job Status"
            ws.update_cell(row_no, headers.index(status_col_name) + 1, status)
            logger.info("Sheets status updated | tab=%s | row=%s | status=%s", tab, row_no, status)
        except SheetsError:
            raise
        except Exception as e:
            _reset_sheet_connection()
            raise SheetsError(f"Status update in '{tab}' failed: {e!r}." + _sheets_hint(str(e)))


# --------------------------------------------------------------------------- #
# Google Drive layer
# --------------------------------------------------------------------------- #
def _drive_service():
    return build("drive", "v3", credentials=get_credentials(), cache_discovery=False)


def _http_error_info(e: HttpError):
    try:
        body = json.loads(e.content.decode("utf-8"))
        err = body.get("error", {})
        reasons = [x.get("reason") for x in err.get("errors", [])]
        return e.resp.status, reasons, err.get("message", "")
    except Exception:
        return getattr(e.resp, "status", "?"), [], str(e)


def _drive_hint(status, reasons, message) -> str:
    email = service_account_email()
    if "storageQuotaExceeded" in reasons or "storage quota" in message.lower():
        return ("Service accounts have NO storage quota in a normal 'My Drive' folder. Move the "
                f"Tutor_Uploads folder into a SHARED DRIVE and add {email} as Content manager.")
    if status == 404:
        return f"Folder not found or not shared with {email}. Check DRIVE_FOLDER_ID and sharing."
    if "accessNotConfigured" in reasons or "SERVICE_DISABLED" in message:
        return "The Google Drive API is not enabled in your Google Cloud project."
    if status == 403:
        return f"{email} lacks permission. Give it Editor (My Drive folder) or Content manager (Shared Drive)."
    return ""


def upload_to_drive(data: bytes, filename: str, mimetype: str) -> dict:
    try:
        svc = _drive_service()
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
        meta = {"name": filename, "parents": [DRIVE_FOLDER_ID]}
        f = svc.files().create(body=meta, media_body=media, fields="id,webViewLink",
                               supportsAllDrives=True).execute()
        logger.info("Drive upload OK | name=%s | id=%s", filename, f.get("id"))
        return f
    except HttpError as e:
        status, reasons, message = _http_error_info(e)
        raise DriveError(f"Drive upload failed: HTTP {status} {reasons} {message} | "
                         f"{_drive_hint(status, reasons, message)}")
    except Exception as e:
        raise DriveError(f"Drive upload failed: {e!r}")


# --------------------------------------------------------------------------- #
# Image sharpness (OpenCV)
# --------------------------------------------------------------------------- #
def check_sharpness(data: bytes):
    """Returns (ok, score, reason). Laplacian variance on a normalized-size grayscale copy."""
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return False, 0.0, "not_image"
    h, w = img.shape[:2]
    if min(h, w) < MIN_IMAGE_SIDE:
        return False, 0.0, "too_small"
    longest = max(h, w)
    if longest > 1024:
        scale = 1024 / longest
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    score = float(cv2.Laplacian(img, cv2.CV_64F).var())
    logger.info("Sharpness score=%.1f (threshold=%.1f) size=%dx%d", score, BLUR_THRESHOLD, w, h)
    return score >= BLUR_THRESHOLD, score, "blurry" if score < BLUR_THRESHOLD else "ok"


# --------------------------------------------------------------------------- #
# Startup self-checks (Sheets + Drive)
# --------------------------------------------------------------------------- #
def run_startup_checks() -> list:
    problems = []

    try:
        email = service_account_email()
    except ConfigError as e:
        logger.critical("CREDENTIALS PROBLEM: %s", e)
        return [f"Credentials: {e}"]

    # Sheets
    try:
        ss = get_spreadsheet()
        existing = {w.title for w in ss.worksheets()}
        for tab, cfg in TABS.items():
            if tab not in existing:
                logger.warning("Tab '%s' missing - creating it.", tab)
                ss.add_worksheet(title=tab, rows=1000, cols=len(cfg["headers"]))
            ws = ss.worksheet(tab)
            if not ws.row_values(1):
                ws.insert_row(cfg["headers"], 1, value_input_option="RAW")
                logger.warning("Tab '%s' had no header row - wrote default headers.", tab)
            try:
                _read_headers(ws, tab)
            except SheetsError as e:
                problems.append(str(e))
        logger.info("Sheets check done. Tabs: %s", sorted(existing | set(TABS)))
    except Exception as e:
        _reset_sheet_connection()
        msg = f"Sheets check failed: {e}" + (_sheets_hint(str(e)) if not isinstance(e, SheetsError) else "")
        logger.error(msg)
        problems.append(msg)

    # Drive
    try:
        svc = _drive_service()
        meta = svc.files().get(
            fileId=DRIVE_FOLDER_ID,
            fields="id,name,mimeType,driveId,capabilities(canAddChildren)",
            supportsAllDrives=True).execute()
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            problems.append(f"DRIVE_FOLDER_ID points to a non-folder ({meta.get('mimeType')}).")
        elif not meta.get("capabilities", {}).get("canAddChildren"):
            problems.append(f"{email} can see the Drive folder but cannot add files (needs Editor).")
        elif not meta.get("driveId"):
            logger.warning("Drive folder '%s' is in a personal My Drive. Uploads by service accounts "
                           "often fail with storageQuotaExceeded there; a Shared Drive is recommended.",
                           meta.get("name"))
        if STARTUP_WRITE_TEST and not problems:
            test = upload_to_drive(b"horizon healthcheck", "_horizon_healthcheck.txt", "text/plain")
            svc.files().delete(fileId=test["id"], supportsAllDrives=True).execute()
            logger.info("Drive write test OK (test file created and removed).")
        logger.info("Drive check done. Folder: %s", meta.get("name"))
    except HttpError as e:
        status, reasons, message = _http_error_info(e)
        msg = f"Drive check failed: HTTP {status} {reasons} {message} | {_drive_hint(status, reasons, message)}"
        logger.error(msg)
        problems.append(msg)
    except Exception as e:
        msg = f"Drive check failed: {e}"
        logger.error(msg)
        problems.append(msg)

    return problems


# --------------------------------------------------------------------------- #
# Texts (English / Amharic)
# --------------------------------------------------------------------------- #
TEXT = {
    "en": {
        "menu": "Welcome to Horizon Tutors Hub! 🎓\nWe connect students with verified tutors in Adama and Bishoftu.\n\nWhat would you like to do?",
        "btn_tutor": "👨‍🏫 Register as Tutor",
        "btn_client": "🔎 Find a Tutor",
        "ask_name": "Please enter your full name:",
        "ask_phone": "Enter your phone number (e.g. 0912345678):",
        "bad_phone": "That doesn't look like a valid Ethiopian phone number. Try again (e.g. 0912345678 or +251912345678).",
        "ask_gender": "Select your gender:",
        "male": "Male", "female": "Female",
        "ask_subject": "Which subject(s) do you teach?",
        "ask_grade": "Which grade level(s) can you teach? (e.g. Grade 9-12)",
        "ask_city": "Select your city:",
        "adama": "Adama", "bishoftu": "Bishoftu",
        "ask_hood": "Enter your neighborhood / area:",
        "ask_id": "Please send a clear photo of your National ID (front side).\nTip: send it as a File (no compression) for best quality.",
        "not_image": "Please send an image (photo) of your ID.",
        "too_large": "That file is too large. Please send an image under 15 MB.",
        "blurry": "⚠️ The photo looks blurry or too small to read. Please retake it in good light, hold steady, and send again.",
        "dl_failed": "Couldn't download that file. Please try sending it again.",
        "processing": "⏳ Processing, please wait…",
        "tutor_done": "✅ Thank you! Your registration ({code}) was received and is awaiting admin verification.",
        "save_failed_photo": "❌ Sorry, a technical problem prevented us from saving your registration. Nothing was recorded. Send the photo again to retry, or /cancel.",
        "save_failed_text": "❌ Sorry, a technical problem prevented us from saving your request. Nothing was recorded. Send your budget again to retry, or /cancel.",
        "tutor_approved": "🎉 Your tutor profile {code} has been verified and approved!",
        "tutor_rejected": "Unfortunately your registration {code} was not approved. Please contact support or re-register with a clear ID photo.",
        "ask_student_grade": "Which grade is the student in?",
        "ask_c_subject": "Which subject(s) does the student need help with?",
        "ask_req": "Describe your requirements (schedule, days per week, tutor gender preference, etc.):",
        "ask_budget": "What is your budget per month (ETB)?",
        "client_done": "✅ Your request {code} was received and is awaiting approval. Once approved we'll share it with our tutors.",
        "client_approved": "🎉 Your request {code} is approved and has been shared with our tutors. We'll connect you soon.",
        "client_rejected": "Your request {code} was not approved. Please contact support for details.",
        "cancelled": "Cancelled. Send /start to begin again.",
        "hint": "Please send /start to begin.",
        "error": "Something went wrong. Please try again with /start.",
    },
    "am": {
        "menu": "እንኳን ወደ ሆራይዘን ቱተርስ ሃብ በደህና መጡ! 🎓\nበአዳማ እና ቢሾፍቱ ተማሪዎችን ከተረጋገጡ አስተማሪዎች ጋር እናገናኛለን።\n\nምን ማድረግ ይፈልጋሉ?",
        "btn_tutor": "👨‍🏫 እንደ አስተማሪ ይመዝገቡ",
        "btn_client": "🔎 አስተማሪ ይፈልጉ",
        "ask_name": "እባክዎ ሙሉ ስምዎን ያስገቡ፦",
        "ask_phone": "ስልክ ቁጥርዎን ያስገቡ (ለምሳሌ 0912345678)፦",
        "bad_phone": "ትክክለኛ የኢትዮጵያ ስልክ ቁጥር አይመስልም። እንደገና ይሞክሩ (ለምሳሌ 0912345678 ወይም +251912345678)።",
        "ask_gender": "ጾታዎን ይምረጡ፦",
        "male": "ወንድ", "female": "ሴት",
        "ask_subject": "የሚያስተምሩት ትምህርት(ቶች) ምንድን ነው?",
        "ask_grade": "የትኛውን የክፍል ደረጃ ማስተማር ይችላሉ? (ለምሳሌ ከ9-12ኛ ክፍል)",
        "ask_city": "ከተማዎን ይምረጡ፦",
        "adama": "አዳማ", "bishoftu": "ቢሾፍቱ",
        "ask_hood": "ሰፈርዎን / አካባቢዎን ያስገቡ፦",
        "ask_id": "እባክዎ የብሔራዊ መታወቂያዎን (የፊት ገጽ) ግልጽ ፎቶ ይላኩ።\nለተሻለ ጥራት እንደ ፋይል (File) ይላኩ።",
        "not_image": "እባክዎ የመታወቂያዎን ምስል (ፎቶ) ይላኩ።",
        "too_large": "ፋይሉ በጣም ትልቅ ነው። እባክዎ ከ15 ሜጋባይት በታች የሆነ ምስል ይላኩ።",
        "blurry": "⚠️ ፎቶው ደብዛዛ ወይም ለማንበብ በጣም ትንሽ ነው። እባክዎ በጥሩ ብርሃን ቀጥ አድርገው እንደገና አንስተው ይላኩ።",
        "dl_failed": "ፋይሉን ማውረድ አልተቻለም። እባክዎ እንደገና ይላኩ።",
        "processing": "⏳ በማስኬድ ላይ፣ እባክዎ ይጠብቁ…",
        "tutor_done": "✅ እናመሰግናለን! ምዝገባዎ ({code}) ተቀብለናል፤ የአስተዳዳሪ ማረጋገጫ በመጠበቅ ላይ ነው።",
        "save_failed_photo": "❌ ይቅርታ፣ በቴክኒክ ችግር ምክንያት ምዝገባዎን ማስቀመጥ አልቻልንም። ምንም አልተመዘገበም። ለመድገም ፎቶውን እንደገና ይላኩ ወይም /cancel ይበሉ።",
        "save_failed_text": "❌ ይቅርታ፣ በቴክኒክ ችግር ምክንያት ጥያቄዎን ማስቀመጥ አልቻልንም። ምንም አልተመዘገበም። ለመድገም በጀትዎን እንደገና ይላኩ ወይም /cancel ይበሉ።",
        "tutor_approved": "🎉 የአስተማሪ መገለጫዎ {code} ተረጋግጦ ጸድቋል!",
        "tutor_rejected": "ይቅርታ፣ ምዝገባዎ {code} አልጸደቀም። እባክዎ ድጋፍን ያነጋግሩ ወይም በግልጽ የመታወቂያ ፎቶ እንደገና ይመዝገቡ።",
        "ask_student_grade": "ተማሪው ስንተኛ ክፍል ነው?",
        "ask_c_subject": "ተማሪው በየትኛው ትምህርት(ቶች) እገዛ ይፈልጋል?",
        "ask_req": "መስፈርቶችዎን ይግለጹ (የጊዜ ሰሌዳ፣ በሳምንት ስንት ቀን፣ የአስተማሪ ጾታ ምርጫ ወዘተ)፦",
        "ask_budget": "ወርሃዊ በጀትዎ ስንት ነው (በብር)?",
        "client_done": "✅ ጥያቄዎ ({code}) ተቀብለናል፤ ማረጋገጫ በመጠበቅ ላይ ነው። ከጸደቀ ለአስተማሪዎቻችን እናሳውቃለን።",
        "client_approved": "🎉 ጥያቄዎ {code} ጸድቆ ለአስተማሪዎቻችን ተሰራጭቷል። በቅርቡ እናገናኝዎታለን።",
        "client_rejected": "ጥያቄዎ {code} አልጸደቀም። ለዝርዝር ድጋፍን ያነጋግሩ።",
        "cancelled": "ተሰርዟል። እንደገና ለመጀመር /start ይላኩ።",
        "hint": "ለመጀመር እባክዎ /start ይላኩ።",
        "error": "የሆነ ችግር ተፈጥሯል። እባክዎ በ /start እንደገና ይሞክሩ።",
    },
}


def lang_of(context) -> str:
    return context.user_data.get("lang", "en")


def tr(context, key: str, **kw) -> str:
    s = TEXT[lang_of(context)][key]
    return s.format(**kw) if kw else s


def tr_lang(lang: str, key: str, **kw) -> str:
    lang = lang if lang in TEXT else "en"
    s = TEXT[lang][key]
    return s.format(**kw) if kw else s


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def esc(v) -> str:
    return html.escape(str(v), quote=False)


def clean(text: str, maxlen: int) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:maxlen]


def normalize_phone(s: str):
    s = re.sub(r"[\s\-().]", "", s or "")
    m = re.fullmatch(r"(?:\+?251|0)?([79]\d{8})", s)
    return "+251" + m.group(1) if m else None


def now_str() -> str:
    return datetime.now(EAT).strftime("%Y-%m-%d %H:%M:%S")


def user_tag(user) -> str:
    return f"@{user.username}" if user.username else ""


def mention(user) -> str:
    name = esc(user.full_name or "user")
    return f'<a href="tg://user?id={user.id}">{name}</a>' + (f" ({esc(user_tag(user))})" if user.username else "")


def menu_keyboard(context):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(context, "btn_tutor"), callback_data="menu:tutor")],
        [InlineKeyboardButton(tr(context, "btn_client"), callback_data="menu:client")],
    ])


def gender_keyboard(context):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(tr(context, "male"), callback_data="gender:Male"),
        InlineKeyboardButton(tr(context, "female"), callback_data="gender:Female"),
    ]])


def city_keyboard(context):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(tr(context, "adama"), callback_data="city:Adama"),
        InlineKeyboardButton(tr(context, "bishoftu"), callback_data="city:Bishoftu"),
    ]])


async def notify_admin(context, text: str, **kwargs):
    """Send to admin; never raise, but log loudly if it fails."""
    try:
        return await context.bot.send_message(ADMIN_CHAT_ID, text, **kwargs)
    except Exception:
        logger.exception("ADMIN NOTIFICATION FAILED (check ADMIN_CHAT_ID and that the admin pressed /start). "
                         "Message was: %s", text[:300])
        return None


# --------------------------------------------------------------------------- #
# Conversation states
# --------------------------------------------------------------------------- #
(LANG, MENU,
 T_NAME, T_PHONE, T_GENDER, T_SUBJECT, T_GRADE, T_CITY, T_HOOD, T_ID,
 C_NAME, C_PHONE, C_GRADE, C_SUBJECT, C_CITY, C_HOOD, C_REQ, C_BUDGET) = range(18)

PRIVATE = filters.ChatType.PRIVATE
TEXT_ONLY = filters.TEXT & ~filters.COMMAND & PRIVATE


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Chat ID: {update.effective_chat.id}\nYour user ID: {update.effective_user.id}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Please choose your language / እባክዎ ቋንቋ ይምረጡ",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
            InlineKeyboardButton("🇪🇹 አማርኛ", callback_data="lang:am"),
        ]]))
    return LANG


async def set_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["lang"] = q.data.split(":")[1]
    await q.message.reply_text(tr(context, "menu"), reply_markup=menu_keyboard(context))
    return MENU


async def menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    flow = q.data.split(":")[1]
    context.user_data["flow"] = flow
    await q.message.reply_text(tr(context, "ask_name"))
    return T_NAME if flow == "tutor" else C_NAME


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = lang_of(context)
    context.user_data.clear()
    await update.effective_message.reply_text(tr_lang(lang, "cancelled"))
    return ConversationHandler.END


def text_step(state, field, next_state, prompt_key, *, maxlen=120, validator=None,
              error_key=None, markup_fn=None):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        value = clean(update.message.text, maxlen)
        if validator:
            value = validator(value)
            if value is None:
                await update.message.reply_text(tr(context, error_key))
                return state
        elif not value:
            return state
        context.user_data[field] = value
        await update.message.reply_text(
            tr(context, prompt_key),
            reply_markup=markup_fn(context) if markup_fn else None)
        return next_state
    return handler


def choice_step(field, next_state, prompt_key):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        context.user_data[field] = q.data.split(":")[1]
        await q.message.reply_text(tr(context, prompt_key))
        return next_state
    return handler


# --------------------------------------------------------------------------- #
# Tutor flow: final step (ID photo -> Drive -> Sheet -> admin)
# --------------------------------------------------------------------------- #
EXT_BY_MIME = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
               "image/webp": "webp", "image/heic": "heic"}


async def tutor_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, ud, user = update.message, context.user_data, update.effective_user
    if ud.get("busy"):
        return T_ID

    if msg.photo:
        tg_obj, mime, kind = msg.photo[-1], "image/jpeg", "photo"
    else:
        tg_obj, mime, kind = msg.document, msg.document.mime_type or "image/jpeg", "document"
    ext = EXT_BY_MIME.get(mime, "jpg")

    if tg_obj.file_size and tg_obj.file_size > MAX_IMAGE_BYTES:
        await msg.reply_text(tr(context, "too_large"))
        return T_ID

    ud["busy"] = True
    try:
        # 1) download
        try:
            tg_file = await tg_obj.get_file()
            raw = bytes(await tg_file.download_as_bytearray())
        except Exception:
            logger.exception("Telegram file download failed | user=%s", user.id)
            await msg.reply_text(tr(context, "dl_failed"))
            return T_ID

        # 2) sharpness check
        ok, score, reason = await asyncio.to_thread(check_sharpness, raw)
        if reason == "not_image":
            await msg.reply_text(tr(context, "not_image"))
            return T_ID
        if not ok:
            await msg.reply_text(tr(context, "blurry"))
            return T_ID

        await msg.reply_text(tr(context, "processing"))

        # 3) Drive upload (skipped on retry if it already succeeded)
        if not ud.get("id_link"):
            fname = f"TutorID_{user.id}_{datetime.now(EAT).strftime('%Y%m%d_%H%M%S')}.{ext}"
            try:
                f = await asyncio.to_thread(upload_to_drive, raw, fname, mime)
                ud["id_link"] = f.get("webViewLink") or f"https://drive.google.com/file/d/{f.get('id')}/view"
            except Exception as e:
                logger.error("TUTOR SAVE FAILED at DRIVE step | user=%s | %s", user.id, e, exc_info=True)
                await notify_admin(context, f"⚠️ Drive upload FAILED for tutor {user.id}:\n{e}")
                await msg.reply_text(tr(context, "save_failed_photo"))
                return T_ID

        # 4) Sheet append matching sheet headers
        data = {
            "Timestamp": now_str(),
            "Tutor Code": "",
            "Telegram ID": user.id,
            "Username": user_tag(user),
            "Name": ud["name"],
            "Phone": ud["phone"],
            "Gender": ud["gender"],
            "Subjects": ud["subject"],
            "Grades": ud["grade"],
            "Location": f"{ud['hood']}, {ud['city']}",
            "Qualification": "Verified via Telegram",
            "ID Verification Status": "Pending",
            "Status": "Pending",
            "ID File ID": ud["id_link"],
        }
        try:
            code, rng = await asyncio.to_thread(append_with_code, "Tutors", "TUT", data)
        except Exception as e:
            logger.error("TUTOR SAVE FAILED at SHEETS step (ID already on Drive: %s) | user=%s | %s",
                         ud["id_link"], user.id, e, exc_info=True)
            await notify_admin(context, f"⚠️ Sheets append FAILED for tutor {user.id}:\n{e}\n"
                                        f"ID photo already on Drive: {ud['id_link']}")
            await msg.reply_text(tr(context, "save_failed_photo"))
            return T_ID

        # 5) Admin notification with Approve / Reject
        caption = (
            f"🆕 <b>Tutor registration {esc(code)}</b>\n"
            f"👤 {esc(ud['name'])}  |  {esc(ud['gender'])}\n"
            f"📞 {esc(ud['phone'])}\n"
            f"📚 {esc(ud['subject'])}  |  🎓 {esc(ud['grade'])}\n"
            f"📍 {esc(ud['hood'])}, {esc(ud['city'])}\n"
            f"🔗 <a href=\"{esc(ud['id_link'])}\">ID on Drive</a>\n"
            f"Telegram: {mention(user)}"
        )
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"tA:{code}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"tR:{code}"),
        ]])
        try:
            if kind == "photo":
                await context.bot.send_photo(ADMIN_CHAT_ID, tg_obj.file_id, caption=caption,
                                             parse_mode=ParseMode.HTML, reply_markup=buttons)
            else:
                await context.bot.send_document(ADMIN_CHAT_ID, tg_obj.file_id, caption=caption,
                                                parse_mode=ParseMode.HTML, reply_markup=buttons)
        except Exception:
            logger.exception("Admin photo notification failed, falling back to text | code=%s", code)
            sent = await notify_admin(context, caption, parse_mode=ParseMode.HTML, reply_markup=buttons)
            if sent is None:
                logger.error("ADMIN NEVER NOTIFIED about tutor %s - approve manually in the sheet.", code)

        await msg.reply_text(tr(context, "tutor_done", code=code))
        ud.clear()
        return ConversationHandler.END
    finally:
        ud.pop("busy", None)


async def tutor_id_wrong(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(tr(context, "not_image"))
    return T_ID


# --------------------------------------------------------------------------- #
# Client flow: final step (budget -> Sheet -> admin)
# --------------------------------------------------------------------------- #
async def client_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud, user = context.user_data, update.effective_user
    if ud.get("busy"):
        return C_BUDGET
    budget = clean(update.message.text, 60)
    if not budget:
        return C_BUDGET
    ud["budget"] = budget
    ud["busy"] = True
    try:
        await update.message.reply_text(tr(context, "processing"))
        data = {
            "Timestamp": now_str(),
            "Job Code": "",
            "Telegram ID": user.id,
            "Username": user_tag(user),
            "Client Name": ud["name"],
            "Phone": ud["phone"],
            "Subject": ud["subject"],
            "Grade": ud["grade"],
            "City": ud["city"],
            "Place": ud["hood"],
            "Tutor Gender Pref": "Any",
            "Duration": "1 Month",
            "Budget": budget,
            "Budget Number": ''.join(filter(str.isdigit, budget)) or "0",
            "Commission Terms": "50%",
            "Verification": "Pending",
            "Job Status": "Open",
            "Channel Msg ID": "",
        }
        try:
            code, rng = await asyncio.to_thread(append_with_code, "Client_Requests", "HRZ", data)
        except Exception as e:
            logger.error("CLIENT SAVE FAILED at SHEETS step | user=%s | %s", user.id, e, exc_info=True)
            await notify_admin(context, f"⚠️ Sheets append FAILED for client request from {user.id}:\n{e}")
            await update.message.reply_text(tr(context, "save_failed_text"))
            return C_BUDGET

        text = (
            f"🆕 <b>Client request {esc(code)}</b>\n"
            f"👤 {esc(ud['name'])}\n"
            f"📞 {esc(ud['phone'])}\n"
            f"🎓 Student grade: {esc(ud['grade'])}\n"
            f"📚 Subject: {esc(ud['subject'])}\n"
            f"📍 {esc(ud['hood'])}, {esc(ud['city'])}\n"
            f"💰 Budget: {esc(budget)} ETB\n"
            f"📝 {esc(ud['req'])}\n"
            f"Telegram: {mention(user)}"
        )
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"cA:{code}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"cR:{code}"),
        ]])
        sent = await notify_admin(context, text, parse_mode=ParseMode.HTML, reply_markup=buttons)
        if sent is None:
            logger.error("ADMIN NEVER NOTIFIED about client request %s - approve manually in the sheet.", code)

        await update.message.reply_text(tr(context, "client_done", code=code))
        ud.clear()
        return ConversationHandler.END
    finally:
        ud.pop("busy", None)


# --------------------------------------------------------------------------- #
# Admin approve / reject
# --------------------------------------------------------------------------- #
ADMIN_LOCK = asyncio.Lock()


def _is_admin(q) -> bool:
    return q.from_user.id == ADMIN_CHAT_ID or (q.message is not None and q.message.chat.id == ADMIN_CHAT_ID)


async def _mark_admin_message(q, suffix: str):
    msg = q.message
    try:
        if msg.caption is not None or msg.photo or msg.document:
            await q.edit_message_caption(caption=(msg.caption_html or "") + suffix,
                                         parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await q.edit_message_text(text=(msg.text_html or "") + suffix,
                                      parse_mode=ParseMode.HTML, reply_markup=None)
    except Exception:
        logger.exception("Could not edit admin message")


def _channel_post(data: dict, code: str, bot_username: str):
    text = (
        f"📢 <b>NEW TUTOR JOB {esc(code)}</b>\n\n"
        f"📚 Subject / ትምህርት: {esc(data.get('Subject', ''))}\n"
        f"🎓 Grade / ክፍል: {esc(data.get('Grade', ''))}\n"
        f"📍 Area / አካባቢ: {esc(data.get('Place', ''))}, {esc(data.get('City', ''))}\n"
        f"💰 Budget / በጀት: {esc(data.get('Budget', ''))} ETB"
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("📩 Apply / ያመልክቱ", url=f"https://t.me/{bot_username}")
    ]])
    return text, markup


async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q):
        await q.answer("Not authorized.", show_alert=True)
        logger.warning("Unauthorized admin-button press by user %s", q.from_user.id)
        return
    await q.answer()

    action, code = q.data.split(":", 1)
    is_tutor = action in ("tA", "tR")
    approve = action.endswith("A")
    tab = "Tutors" if is_tutor else "Client_Requests"

    async with ADMIN_LOCK:
        try:
            rec = await asyncio.to_thread(find_record, tab, code)
        except Exception as e:
            logger.error("Admin decision: sheet lookup failed | %s | %s", code, e, exc_info=True)
            await q.message.reply_text(f"❌ Sheets lookup failed for {code}:\n{e}")
            return
        if rec is None:
            await q.message.reply_text(f"❌ {code} not found in '{tab}'. Was the row deleted?")
            return
        row_no, data = rec
        
        status_field = "Status" if is_tutor else "Verification"
        if status_field not in data:
            status_field = "Job Status"
            
        current = (data.get(status_field) or "").strip()
        if current.lower() not in ("pending", ""):
            await _mark_admin_message(q, f"\n\nℹ️ Already <b>{esc(current)}</b>.")
            return

        # Broadcast first (client approvals) so a failed post leaves the request Pending for retry
        if approve and not is_tutor:
            try:
                text, markup = _channel_post(data, code, context.bot.username)
                await context.bot.send_message(JOB_CHANNEL_ID, text, parse_mode=ParseMode.HTML,
                                               reply_markup=markup)
                logger.info("Broadcast OK | %s -> %s", code, JOB_CHANNEL_ID)
            except Exception as e:
                logger.error("CHANNEL BROADCAST FAILED | %s | channel=%s | %s", code, JOB_CHANNEL_ID, e,
                             exc_info=True)
                await q.message.reply_text(
                    f"❌ Could not post {code} to the channel ({e}).\n"
                    "Make sure the bot is an ADMIN of the channel with permission to post. "
                    "The request is still Pending - press Approve again after fixing it.")
                return

        new_status = "Approved" if approve else "Rejected"
        try:
            await asyncio.to_thread(set_status, tab, row_no, new_status)
        except Exception as e:
            logger.error("Admin decision: status update failed | %s | %s", code, e, exc_info=True)
            warn = "⚠️ It WAS posted to the channel but the sheet status could not be updated. " if (
                approve and not is_tutor) else ""
            await q.message.reply_text(f"❌ Could not update status for {code}:\n{e}\n{warn}Update it manually.")
            return

    # Tell the user
    lang = data.get("Language", "en")
    key = ("tutor_" if is_tutor else "client_") + ("approved" if approve else "rejected")
    try:
        await context.bot.send_message(int(data["Telegram ID"]), tr_lang(lang, key, code=code))
    except Exception:
        logger.exception("Could not notify user %s about %s", data.get("Telegram ID"), code)
        await q.message.reply_text(f"⚠️ {code} was {new_status.lower()}, but the user could not be notified "
                                   "(they may have blocked the bot).")

    icon = "✅" if approve else "❌"
    await _mark_admin_message(q, f"\n\n{icon} <b>{new_status}</b> by {esc(q.from_user.full_name)}")


# --------------------------------------------------------------------------- #
# Misc handlers
# --------------------------------------------------------------------------- #
async def fallback_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TEXT["en"]["hint"] + "\n" + TEXT["am"]["hint"])


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            lang = (context.user_data or {}).get("lang", "en") if context.user_data is not None else "en"
            await update.effective_message.reply_text(tr_lang(lang, "error"))
        except Exception:
            pass


def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start, filters=PRIVATE)],
        states={
            LANG: [CallbackQueryHandler(set_lang, pattern=r"^lang:(en|am)$")],
            MENU: [CallbackQueryHandler(menu_choice, pattern=r"^menu:(tutor|client)$")],

            T_NAME: [MessageHandler(TEXT_ONLY, text_step(T_NAME, "name", T_PHONE, "ask_phone"))],
            T_PHONE: [MessageHandler(TEXT_ONLY, text_step(
                T_PHONE, "phone", T_GENDER, "ask_gender", maxlen=30,
                validator=normalize_phone, error_key="bad_phone", markup_fn=gender_keyboard))],
            T_GENDER: [CallbackQueryHandler(choice_step("gender", T_SUBJECT, "ask_subject"),
                                            pattern=r"^gender:(Male|Female)$")],
            T_SUBJECT: [MessageHandler(TEXT_ONLY, text_step(T_SUBJECT, "subject", T_GRADE, "ask_grade"))],
            T_GRADE: [MessageHandler(TEXT_ONLY, text_step(
                T_GRADE, "grade", T_CITY, "ask_city", markup_fn=city_keyboard))],
            T_CITY: [CallbackQueryHandler(choice_step("city", T_HOOD, "ask_hood"),
                                          pattern=r"^city:(Adama|Bishoftu)$")],
            T_HOOD: [MessageHandler(TEXT_ONLY, text_step(T_HOOD, "hood", T_ID, "ask_id"))],
            T_ID: [
                MessageHandler((filters.PHOTO | filters.Document.IMAGE) & PRIVATE, tutor_id),
                MessageHandler(filters.ALL & ~filters.COMMAND & PRIVATE, tutor_id_wrong),
            ],

            C_NAME: [MessageHandler(TEXT_ONLY, text_step(C_NAME, "name", C_PHONE, "ask_phone"))],
            C_PHONE: [MessageHandler(TEXT_ONLY, text_step(
                C_PHONE, "phone", C_GRADE, "ask_student_grade", maxlen=30,
                validator=normalize_phone, error_key="bad_phone"))],
            C_GRADE: [MessageHandler(TEXT_ONLY, text_step(C_GRADE, "grade", C_SUBJECT, "ask_c_subject"))],
            C_SUBJECT: [MessageHandler(TEXT_ONLY, text_step(
                C_SUBJECT, "subject", C_CITY, "ask_city", markup_fn=city_keyboard))],
            C_CITY: [CallbackQueryHandler(choice_step("city", C_HOOD, "ask_hood"),
                                          pattern=r"^city:(Adama|Bishoftu)$")],
            C_HOOD: [MessageHandler(TEXT_ONLY, text_step(C_HOOD, "hood", C_REQ, "ask_req"))],
            C_REQ: [MessageHandler(TEXT_ONLY, text_step(C_REQ, "req", C_BUDGET, "ask_budget", maxlen=600))],
            C_BUDGET: [MessageHandler(TEXT_ONLY, client_budget)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start, filters=PRIVATE),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(admin_decision, pattern=r"^(tA|tR|cA|cR):"))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & PRIVATE, fallback_hint))
    app.add_error_handler(on_error)
    return app


# --------------------------------------------------------------------------- #
# Flask keep-alive (Render needs an open HTTP port)
# --------------------------------------------------------------------------- #
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "Horizon Tutors Hub bot is running.", 200


@flask_app.route("/health")
def health():
    return "ok", 200


def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    logger.info("Flask keep-alive listening on 0.0.0.0:%s", port)
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def keep_alive_pinger():
    url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        logger.info("RENDER_EXTERNAL_URL not set; self-ping disabled.")
        return
    while True:
        time.sleep(600)
        try:
            requests.get(url.rstrip("/") + "/health", timeout=15)
        except Exception as e:
            logger.warning("Keep-alive ping failed: %s", e)


# --------------------------------------------------------------------------- #
# Main (manual asyncio lifecycle: works on Python 3.11 - 3.14)
# --------------------------------------------------------------------------- #
async def run_bot():
    problems = await asyncio.to_thread(run_startup_checks)

    app = build_application()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    me = await app.bot.get_me()
    logger.info("Bot online as @%s (python %s)", me.username, sys.version.split()[0])

    status = ("⚠️ <b>Horizon bot started with PROBLEMS</b>\n" + "\n".join(f"• {esc(p)}" for p in problems)
              if problems else "✅ <b>Horizon bot online.</b> Sheets and Drive checks passed.")
    try:
        await app.bot.send_message(ADMIN_CHAT_ID, status[:4000], parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Could not send startup status to ADMIN_CHAT_ID=%s. Open the bot and press /start "
                         "from the admin account, then check the ID with /myid.", ADMIN_CHAT_ID)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()

    logger.info("Shutting down...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


def main():
    validate_config()
    try:
        get_credentials()  # fail fast, with a precise message, if credentials are unusable
    except ConfigError as e:
        logger.critical("GOOGLE CREDENTIALS ERROR: %s", e)
        raise SystemExit(1)
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive_pinger, daemon=True).start()
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
