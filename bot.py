#!/usr/bin/env python3
"""
Horizon Tutors Hub - Telegram bot (Adama & Bishoftu, Ethiopia)

Google Sheet "Adama Tutors Database" tabs used:
  Tutors, Client_Requests, Matches, Applications, Settings

ID photos are kept as Telegram file_id by default (USE_DRIVE=0). Set USE_DRIVE=1 only if the
Drive folder is inside a Google Workspace Shared Drive.

Required env vars : TELEGRAM_TOKEN, GOOGLE_CREDENTIALS_JSON (or credentials.json),
                    ADMIN_CHAT_ID, JOB_CHANNEL_ID
Optional env vars : USE_DRIVE, DRIVE_FOLDER_ID, SPREADSHEET_NAME, SPREADSHEET_ID,
                    GOOGLE_CREDENTIALS_FILE, BLUR_THRESHOLD, STARTUP_WRITE_TEST,
                    RENDER_EXTERNAL_URL (auto-set by Render), PORT (auto-set by Render)
"""

import asyncio
import base64
import functools
import html
import io
import json
import logging
import os
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
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
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

USE_DRIVE = os.environ.get("USE_DRIVE", "0").strip() == "1"
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1TPt6gHg4ff3PQIbj64ZiLUfIi3IZjQkP").strip()
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Adama Tutors Database").strip()
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
BLUR_THRESHOLD = float(os.environ.get("BLUR_THRESHOLD", "50"))
MIN_IMAGE_SIDE = 480
MAX_IMAGE_BYTES = 15 * 1024 * 1024
STARTUP_WRITE_TEST = os.environ.get("STARTUP_WRITE_TEST", "1") != "0"
JOB_PREFIX = "HRZ"
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/HorizonTutorsHub").strip()

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


class BusinessError(Exception):
    """A rule-based refusal (not a technical failure); message is safe to show to the admin."""


def validate_config():
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
    logger.info("Config OK | admin=%s | channel=%s | use_drive=%s | sheet=%s",
                ADMIN_CHAT_ID, JOB_CHANNEL_ID, USE_DRIVE, SPREADSHEET_ID or SPREADSHEET_NAME)


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
    absent = [k for k in ("type", "client_email", "private_key") if not info.get(k)]
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
# Sheet layout (must match row 1 of each tab; extra columns are fine)
#   Optional extra columns filled automatically if present: "Language", "Requirements"
# --------------------------------------------------------------------------- #
TUTOR_HEADERS = [
    "Timestamp", "Telegram ID", "Username", "Name", "Phone", "Gender",
    "Subjects", "Grades", "Location", "Qualification", "ID Verification Status",
    "Status", "ID File ID",
]
CLIENT_HEADERS = [
    "Timestamp", "Job Code", "Telegram ID", "Username", "Client Name", "Phone",
    "Subject", "Grade", "City", "Place", "Tutor Gender Pref", "Duration",
    "Budget", "Budget Number", "Commission Terms", "Verification", "Job Status",
    "Channel Msg ID",
]
MATCH_HEADERS = [
    "Logged Date", "Job Code", "Client Name", "Client Phone", "Tutor Name", "Tutor Phone",
    "Budget Number", "Trial End Date", "Commission Terms", "Payment Status",
]
APPLICATION_HEADERS = [
    "Timestamp", "Job Code", "Tutor Telegram", "Tutor Name", "Tutor Phone", "Status", "Admin Notes",
]
SETTINGS_HEADERS = ["Key", "Value", "Description"]

TABS = {
    "Tutors": {"headers": TUTOR_HEADERS, "code": "Timestamp"},
    "Client_Requests": {"headers": CLIENT_HEADERS, "code": "Job Code"},
    "Matches": {"headers": MATCH_HEADERS},
    "Applications": {"headers": APPLICATION_HEADERS},
    "Settings": {"headers": SETTINGS_HEADERS},
}

SETTINGS_DEFAULTS = {
    "job_counter": ("0", "Tracks the latest sequential job number"),
    "commission_rate": ("50%", "Default agency commission terms"),
    "support_handle": ("@simon_1919", "Customer support Telegram handle"),
    "support_phone": ("+251 90 088 2373", "Customer support phone number"),
    "bot_status": ("active", "Controls whether the bot is accepting submissions"),
}

SHEETS_LOCK = threading.RLock()
_spreadsheet = None


def _sheets_hint(err: str) -> str:
    low = err.lower()
    if "has not been used" in low or "accessnotconfigured" in low or "service_disabled" in low:
        return " HINT: enable the Google Sheets API (and Drive API) in your Google Cloud project."
    if "permission" in low or "403" in low:
        return f" HINT: share the Google Sheet with {service_account_email()} as Editor."
    return ""


def _reset_sheet_connection():
    global _spreadsheet
    _spreadsheet = None


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


def _read_headers(ws, tab: str) -> list:
    headers = ws.row_values(1)
    if not headers:
        raise SheetsError(f"Tab '{tab}' has an empty header row (row 1).")
    missing = [h for h in TABS[tab]["headers"] if h not in headers]
    if missing:
        raise SheetsError(f"Tab '{tab}' is missing header columns: {missing}. Found: {headers}")
    return headers


def sheets_guard(label: str):
    """Serialize sheet access and turn unexpected exceptions into SheetsError with hints."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with SHEETS_LOCK:
                try:
                    return fn(*a, **kw)
                except (SheetsError, BusinessError):
                    raise
                except Exception as e:
                    _reset_sheet_connection()
                    raise SheetsError(f"{label} failed: {e!r}." + _sheets_hint(str(e)))
        return wrapper
    return deco


def digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


@sheets_guard("Reading rows")
def read_rows(tab: str):
    """Returns [(row_number, {header: value}), ...] for non-empty data rows."""
    ws = get_worksheet(tab)
    headers = _read_headers(ws, tab)
    vals = ws.get_all_values()
    rows = []
    for i, v in enumerate(vals[1:], start=2):
        if not any(c.strip() for c in v):
            continue
        v = v + [""] * (len(headers) - len(v))
        rows.append((i, dict(zip(headers, v))))
    return rows


@sheets_guard("Sheets append")
def append_dict(tab: str, data: dict):
    ws = get_worksheet(tab)
    headers = _read_headers(ws, tab)
    row = [str(data.get(h, "")) for h in headers]
    resp = ws.append_row(row, value_input_option="RAW")
    updated = (resp or {}).get("updates", {}).get("updatedRange", "?")
    logger.info("Sheets append OK | tab=%s | range=%s", tab, updated)
    return updated


@sheets_guard("Cell update")
def set_cells(tab: str, row_no: int, updates: dict):
    ws = get_worksheet(tab)
    headers = _read_headers(ws, tab)
    for col, val in updates.items():
        if col in headers:
            ws.update_cell(row_no, headers.index(col) + 1, val)
        else:
            logger.warning("set_cells: column '%s' not in tab '%s' - skipped", col, tab)
    logger.info("Sheets cells updated | tab=%s | row=%s | %s", tab, row_no, list(updates))


@sheets_guard("Record lookup")
def find_record(tab: str, code: str):
    """Returns (row_number, {header: value}) or None. Only for Tutors / Client_Requests."""
    code_header = TABS[tab]["code"]
    for row_no, d in read_rows(tab):
        if d.get(code_header) == code:
            return row_no, d
    return None


@sheets_guard("Job code generation")
def append_with_code(tab: str, data: dict):
    """Tutors: code = Timestamp (made unique). Client_Requests: sequential #HRZ-0001 from Settings.job_counter."""
    code_header = TABS[tab]["code"]
    ws = get_worksheet(tab)
    headers = _read_headers(ws, tab)
    existing = set(ws.col_values(headers.index(code_header) + 1))
    counter_n = None

    if tab == "Client_Requests":
        sws = get_worksheet("Settings")
        svals = sws.get_all_values()
        shead = svals[0] if svals else SETTINGS_HEADERS
        kcol = shead.index("Key") if "Key" in shead else 0
        vcol = shead.index("Value") if "Value" in shead else 1
        counter_row = None
        n = 0
        for i, r in enumerate(svals[1:], start=2):
            r = r + [""] * (max(kcol, vcol) + 1 - len(r))
            if r[kcol].strip().lower() == "job_counter":
                counter_row = i
                n = int(digits(r[vcol]) or 0)
                break
        n += 1
        while f"#{JOB_PREFIX}-{n:04d}" in existing:
            n += 1
        code = f"#{JOB_PREFIX}-{n:04d}"
        counter_n = (sws, counter_row, vcol, n)
    else:
        base = str(data.get("Timestamp") or now_str())
        code, k = base, 2
        while code in existing:
            code = f"{base}-{k}"
            k += 1

    row_data = dict(data)
    row_data[code_header] = code
    row = [str(row_data.get(h, "")) for h in headers]
    resp = ws.append_row(row, value_input_option="RAW")
    updated = (resp or {}).get("updates", {}).get("updatedRange", "?")
    logger.info("Sheets append OK | tab=%s | code=%s | range=%s", tab, code, updated)

    if counter_n:
        sws, counter_row, vcol, n = counter_n
        try:
            if counter_row:
                sws.update_cell(counter_row, vcol + 1, n)
            else:
                sws.append_row(["job_counter", n, SETTINGS_DEFAULTS["job_counter"][1]], value_input_option="RAW")
            invalidate_settings()
        except Exception:
            logger.exception("job_counter update failed (job %s saved; uniqueness is still enforced)", code)
    return code, updated


def set_status(tab: str, row_no: int, status: str):
    """Tutors: Status + ID Verification Status. Client_Requests: Verification (+Job Status on reject)."""
    if tab == "Tutors":
        updates = {"Status": status,
                   "ID Verification Status": "Verified" if status == "Approved" else "Rejected"}
    else:
        updates = {"Verification": status}
        if status == "Rejected":
            updates["Job Status"] = "Rejected"
    set_cells(tab, row_no, updates)


# ---- Settings (with short cache) ------------------------------------------ #
_settings_cache = {"ts": 0.0, "data": None}


def invalidate_settings():
    _settings_cache["ts"] = 0.0


@sheets_guard("Reading Settings")
def read_settings() -> dict:
    ws = get_worksheet("Settings")
    vals = ws.get_all_values()
    out = {}
    for r in vals[1:]:
        if len(r) >= 2 and r[0].strip():
            out[r[0].strip()] = r[1].strip()
    return out


def get_settings_sync() -> dict:
    now = time.time()
    if _settings_cache["data"] is not None and now - _settings_cache["ts"] < 45:
        return dict(_settings_cache["data"])
    data = {k: v[0] for k, v in SETTINGS_DEFAULTS.items()}
    try:
        data.update(read_settings())
        _settings_cache.update(ts=now, data=data)
    except Exception:
        logger.exception("Could not read Settings tab - using last known/default values.")
        if _settings_cache["data"] is not None:
            data = dict(_settings_cache["data"])
    return dict(data)


async def get_settings() -> dict:
    return await asyncio.to_thread(get_settings_sync)


async def bot_active() -> bool:
    s = await get_settings()
    return s.get("bot_status", "active").strip().lower() == "active"


# ---- Tutors / Applications / Matches -------------------------------------- #
@sheets_guard("Tutor lookup")
def tutor_map() -> dict:
    """Latest Tutors row per Telegram ID: {tid_str: row_dict}."""
    out = {}
    for _, d in read_rows("Tutors"):
        t = digits(d.get("Telegram ID"))
        if t:
            out[t] = d
    return out


@sheets_guard("Tutor lookup")
def find_tutor_by_tid(tid):
    best = None
    for row_no, d in read_rows("Tutors"):
        if digits(d.get("Telegram ID")) == str(tid):
            best = (row_no, d)
    return best


@sheets_guard("Application lookup")
def find_application(code: str, tid):
    for row_no, d in read_rows("Applications"):
        if d.get("Job Code") == code and digits(d.get("Tutor Telegram")) == str(tid):
            return row_no, d
    return None


def _eligibility(tid, code: str) -> dict:
    tutor = find_tutor_by_tid(tid)
    out = {}
    if tutor and (tutor[1].get("Language") or "").strip() in ("en", "am"):
        out["lang"] = tutor[1]["Language"].strip()
    if tutor is None:
        out["error"] = "apply_not_registered"
        return out
    status = (tutor[1].get("Status") or "").strip()
    if status.lower() != "approved":
        out.update(error="apply_not_approved", status=status or "Pending")
        return out
    job = find_record("Client_Requests", code)
    if (job is None or (job[1].get("Verification") or "").strip().lower() != "approved"
            or (job[1].get("Job Status") or "").strip().lower() != "open"):
        out["error"] = "apply_job_closed"
        return out
    if find_application(code, tid):
        out["error"] = "apply_already"
        return out
    out.update(tutor=tutor[1], job=job[1])
    return out


@sheets_guard("Application check")
def check_apply(tid, code: str) -> dict:
    return _eligibility(tid, code)


@sheets_guard("Application submit")
def submit_application(tid, code: str) -> dict:
    out = _eligibility(tid, code)
    if "error" in out:
        return out
    t = out["tutor"]
    data = {
        "Timestamp": now_str(), "Job Code": code, "Tutor Telegram": str(tid),
        "Tutor Name": t.get("Name", ""), "Tutor Phone": t.get("Phone", ""),
        "Status": "Pending", "Admin Notes": "",
    }
    append_dict("Applications", data)
    out["app"] = data
    return out


@sheets_guard("Application listing")
def list_applications(code=None, tid=None, pending_only=False):
    tmap = tutor_map()
    out = []
    for row_no, d in read_rows("Applications"):
        if code and d.get("Job Code") != code:
            continue
        t = digits(d.get("Tutor Telegram"))
        if tid and t != str(tid):
            continue
        if pending_only and (d.get("Status") or "").strip().lower() != "pending":
            continue
        d = dict(d)
        d["_row"] = row_no
        d["_tutor"] = tmap.get(t)
        out.append(d)
    return out


@sheets_guard("Open jobs listing")
def list_open_jobs():
    counts = {}
    for _, a in read_rows("Applications"):
        c = a.get("Job Code")
        n = counts.setdefault(c, [0, 0])
        n[0] += 1
        if (a.get("Status") or "").strip().lower() == "pending":
            n[1] += 1
    out = []
    for _, j in read_rows("Client_Requests"):
        if ((j.get("Verification") or "").strip().lower() == "approved"
                and (j.get("Job Status") or "").strip().lower() == "open"):
            out.append((j, counts.get(j.get("Job Code"), [0, 0])))
    return out


@sheets_guard("Record search")
def search_rows(tab: str, query: str):
    # Search Tutors / Client_Requests by phone (any format), name/username, or (clients) job code.
    q = (query or "").strip()
    if not q:
        return []
    qd = digits(q)
    has_letters = bool(re.search(r"[^\W\d_]", q))
    is_client = tab == "Client_Requests"
    mode, needle = "name", None
    if not has_letters and len(qd) >= 7:
        mode, needle = "phone", qd[-9:]
    elif is_client and (re.fullmatch(r"#?hrz-?\d+", q, re.I) or (not has_letters and qd and len(qd) <= 6)):
        mode, needle = "code", normalize_job_code(q)
    tokens = q.lower().lstrip("@").split()
    name_col = "Client Name" if is_client else "Name"
    out = []
    for row_no, d in read_rows(tab):
        if mode == "phone":
            ok = digits(d.get("Phone")).endswith(needle)
        elif mode == "code":
            ok = needle is not None and d.get("Job Code") == needle
        else:
            hay = f"{d.get(name_col, '')} {d.get('Username', '')}".lower()
            ok = bool(tokens) and all(t in hay for t in tokens)
        if ok:
            out.append((row_no, d))
    out.reverse()  # newest first
    if tab == "Tutors" and out:
        by_tid = {}
        try:
            for _, a in read_rows("Applications"):
                by_tid.setdefault(digits(a.get("Tutor Telegram")), []).append(
                    f"{a.get('Job Code')} ({a.get('Status')})")
        except Exception:
            logger.warning("Could not read Applications while searching tutors", exc_info=True)
        for _, d in out:
            d["_apps"] = by_tid.get(digits(d.get("Telegram ID")), [])
    return out


@sheets_guard("Select tutor")
def select_application(code: str, tid) -> dict:
    found = find_application(code, tid)
    if not found:
        raise BusinessError("Application not found.")
    app_row, app = found
    if (app.get("Status") or "").strip().lower() != "pending":
        raise BusinessError(f"This application is already: {app.get('Status')}.")
    job_rec = find_record("Client_Requests", code)
    if job_rec is None:
        raise BusinessError(f"{code} not found in Client_Requests.")
    job_row, job = job_rec
    if (job.get("Job Status") or "").strip().lower() != "open":
        raise BusinessError(f"{code} is no longer open (Job Status: {job.get('Job Status')}).")

    commission = job.get("Commission Terms") or get_settings_sync().get("commission_rate", "50%")
    append_dict("Matches", {
        "Logged Date": now_str(), "Job Code": code,
        "Client Name": job.get("Client Name", ""), "Client Phone": job.get("Phone", ""),
        "Tutor Name": app.get("Tutor Name", ""), "Tutor Phone": app.get("Tutor Phone", ""),
        "Budget Number": job.get("Budget Number", ""), "Trial End Date": "",
        "Commission Terms": commission, "Payment Status": "Pending",
    })
    tmap = tutor_map()
    others = []
    try:
        set_cells("Applications", app_row, {"Status": "Selected"})
        set_cells("Client_Requests", job_row, {"Job Status": "Matched"})
        for row_no, d in read_rows("Applications"):
            if (d.get("Job Code") == code and row_no != app_row
                    and (d.get("Status") or "").strip().lower() == "pending"):
                set_cells("Applications", row_no, {"Status": "Not selected"})
                d = dict(d)
                d["_tutor"] = tmap.get(digits(d.get("Tutor Telegram")))
                others.append(d)
    except Exception as e:
        raise SheetsError(f"The match WAS logged in 'Matches', but updating statuses failed: {e!r}. "
                          "Please fix Applications/Client_Requests statuses manually.")
    return {"job": job, "app": app, "tutor": tmap.get(str(tid)), "others": others}


@sheets_guard("Reject application")
def reject_application(code: str, tid) -> dict:
    found = find_application(code, tid)
    if not found:
        raise BusinessError("Application not found.")
    app_row, app = found
    if (app.get("Status") or "").strip().lower() != "pending":
        raise BusinessError(f"This application is already: {app.get('Status')}.")
    set_cells("Applications", app_row, {"Status": "Rejected"})
    return {"app": app, "tutor": tutor_map().get(str(tid))}


# --------------------------------------------------------------------------- #
# Google Drive layer (only used when USE_DRIVE=1)
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
        return ("Service accounts have NO storage quota in a normal 'My Drive' folder. Either move the "
                f"folder into a Workspace SHARED DRIVE (add {email} as Content manager) or set USE_DRIVE=0.")
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


def download_drive_file(file_id: str):
    """Downloads a file from Drive with the service account. Returns (bytes, mimetype, name)."""
    try:
        svc = _drive_service()
        meta = svc.files().get(fileId=file_id, fields="name,mimeType", supportsAllDrives=True).execute()
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id, supportsAllDrives=True))
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue(), meta.get("mimeType", ""), meta.get("name", "id_photo.jpg")
    except HttpError as e:
        status, reasons, message = _http_error_info(e)
        raise DriveError(f"Drive download failed: HTTP {status} {reasons} {message} | "
                         f"{_drive_hint(status, reasons, message)}")
    except Exception as e:
        raise DriveError(f"Drive download failed: {e!r}")


# --------------------------------------------------------------------------- #
# Image sharpness (OpenCV)
# --------------------------------------------------------------------------- #
def check_sharpness(data: bytes):
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
# Startup self-checks
# --------------------------------------------------------------------------- #
def run_startup_checks() -> list:
    problems = []
    try:
        email = service_account_email()
    except ConfigError as e:
        logger.critical("CREDENTIALS PROBLEM: %s", e)
        return [f"Credentials: {e}"]

    try:
        ss = get_spreadsheet()
        existing = {w.title for w in ss.worksheets()}
        for tab, cfg in TABS.items():
            created = False
            if tab not in existing:
                logger.warning("Tab '%s' missing - creating it.", tab)
                ss.add_worksheet(title=tab, rows=1000, cols=max(len(cfg["headers"]), 5))
                created = True
            ws = ss.worksheet(tab)
            if not ws.row_values(1):
                ws.insert_row(cfg["headers"], 1, value_input_option="RAW")
                logger.warning("Tab '%s' had no header row - wrote default headers (created=%s).", tab, created)
            try:
                headers = _read_headers(ws, tab)
                if tab in ("Tutors", "Client_Requests"):
                    for opt in ("Language", "Requirements"):
                        if opt not in headers:
                            logger.info("Tab '%s': optional column '%s' not present (add it to store this data).",
                                        tab, opt)
            except SheetsError as e:
                problems.append(str(e))
        # Settings defaults
        try:
            current = read_settings()
            have = {k.lower() for k in current}
            for key, (val, desc) in SETTINGS_DEFAULTS.items():
                if key.lower() not in have:
                    ss.worksheet("Settings").append_row([key, val, desc], value_input_option="RAW")
                    logger.warning("Settings key '%s' was missing - added default '%s'.", key, val)
        except Exception as e:
            problems.append(f"Settings check failed: {e}")
        logger.info("Sheets check done. Tabs: %s", sorted(existing | set(TABS)))
    except Exception as e:
        _reset_sheet_connection()
        msg = f"Sheets check failed: {e}" + (_sheets_hint(str(e)) if not isinstance(e, SheetsError) else "")
        logger.error(msg)
        problems.append(msg)

    if not USE_DRIVE:
        logger.info("Drive disabled (USE_DRIVE != 1): ID photos are stored as Telegram file_id.")
        return problems

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
            problems.append("Drive folder is in a personal My Drive: service-account uploads will fail with "
                            "storageQuotaExceeded. Use a Workspace Shared Drive or set USE_DRIVE=0.")
        if STARTUP_WRITE_TEST and not problems:
            test = upload_to_drive(b"horizon healthcheck", "_horizon_healthcheck.txt", "text/plain")
            svc.files().delete(fileId=test["id"], supportsAllDrives=True).execute()
            logger.info("Drive write test OK.")
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
# (key, English label, Amharic label, [(code, English label, Amharic label), ...])
GRADE_SECTIONS = [
    ("KG", "🧸 KG", "🧸 መዋለ ህጻናት (KG)",
     [(f"KG{n}", f"KG {n}", f"KG {n}") for n in (1, 2, 3)]),
    ("PRI", "📘 Primary (Grades 1-8)", "📘 አንደኛ ደረጃ (1-8ኛ ክፍል)",
     [(f"G{n}", f"Grade {n}", f"{n}ኛ ክፍል") for n in range(1, 9)]),
    ("HS", "🏫 High School (Grades 9-12)", "🏫 ሁለተኛ ደረጃ (9-12ኛ ክፍል)",
     [(f"G{n}", f"Grade {n}", f"{n}ኛ ክፍል") for n in range(9, 13)]),
]
UNI_GRADE = ("UNI", "🎓 University / College", "🎓 ዩኒቨርሲቲ / ኮሌጅ")
# grade code -> English label stored in the sheet
GRADE_LABELS = {code: en for _, _, _, grades in GRADE_SECTIONS for code, en, _ in grades}
GRADE_LABELS["UNI"] = "University / College"

TEXT = {
    "en": {
        "menu": "👋 Welcome to Horizon Tutors Hub!\n\nWe connect families in Adama and Bishoftu with trusted, ID-verified tutors, so learning at home is simple, safe and stress-free. 🎓\n\nHow can we help you today?",
        "btn_tutor": "👨‍🏫 I'm a Tutor - I want jobs",
        "btn_client": "🔎 I Need a Tutor",
        "use_buttons": "Please tap one of the buttons above 👆",
        "ask_name": "Let's get started! 😊 What is your full name?",
        "ask_phone": "Thanks! 📱 What's the best number to reach you on?\nTap the button below to share it, or just type it (e.g. 0912345678).",
        "share_phone": "📱 Share my phone number",
        "contact_not_yours": "Please share YOUR OWN phone number using the button below 🙏",
        "phone_saved": "✅ Got it: {phone}",
        "bad_phone": "Hmm, that doesn't look like an Ethiopian mobile number. Tap the button or type it like 0912345678 🙏",
        "ask_gender": "Please select your gender:",
        "male": "Male",
        "female": "Female",
        "ask_subject": "📚 Which subject(s) do you teach? (e.g. Math, Physics, English)",
        "ask_city": "📍 Which city are you in?",
        "adama": "Adama",
        "bishoftu": "Bishoftu",
        "ask_hood": "🏘 Which neighborhood / area?",
        "ask_id": "🪪 Almost done! To keep every family safe, all our tutors are ID-verified. Please send a clear photo of the front of your National ID.\n\n💡 Tip: send it as a File (no compression) and make sure all the text is readable.",
        "not_image": "Please send an image (photo) of your ID.",
        "too_large": "That file is too large. Please send an image under 15 MB.",
        "blurry": "⚠️ That photo looks blurry or too small to read. Please retake it in good light, hold steady, and send it again 📸",
        "dl_failed": "Couldn't download that file. Please try sending it again.",
        "processing": "⏳ One moment, saving your details…",
        "tutor_done": "🎉 Thank you, your registration is in!\n\nOur team is verifying your details now. You'll get a message right here as soon as you're approved, and then you can start applying for jobs.",
        "save_failed_photo": "❌ Sorry, a technical problem prevented us from saving your registration. Nothing was recorded. Send the photo again to retry, or /cancel.",
        "save_failed_text": "❌ Sorry, a technical problem prevented us from saving your request. Nothing was recorded. Send your budget again to retry, or /cancel.",
        "tutor_approved": "🎉 Congratulations, you're verified and approved!\n\nYou can now apply for jobs: just tap “Apply” under any job post in our channel. Good luck! 🍀",
        "tutor_rejected": "Unfortunately your registration was not approved. Please contact support or re-register with a clear ID photo.",
        "ask_student_grade": "🎓 Which level is the student in?",
        "ask_other_grade": "✏️ Please type the grade or level (e.g. Grade 6 & 8):",
        "ask_c_subject": "📚 Which subject(s) does the student need help with?",
        "ask_req": "📝 Anything else the tutor should know? (preferred time, the student's needs, etc.)\nOr tap Skip.",
        "ask_budget": "💰 Last one! What is your monthly budget for the tutor (in ETB)?\nExample: 3000",
        "client_done": "✅ Your request {code} is in!\n\nOur team is reviewing it and will share it with our verified tutors very soon. We'll message you here the moment it goes live.",
        "client_approved": "🎉 Great news! Your request {code} is approved and live. Tutors can see it now, and we'll connect you with the right match soon.",
        "client_rejected": "Your request {code} was not approved. Please contact support for details.",
        "client_matched": "✅ Good news! A tutor has been selected for your request {code}. Our team will call you shortly to arrange everything. 🤝",
        "cancelled": "Cancelled. Send /start to begin again.",
        "hint": "Please send /start to begin.",
        "error": "Something went wrong. Please try again with /start.",
        "paused": "🚧 We're not accepting new submissions right now. Please try again later.",
        "support": "Need help? {handle} | {phone}",
        "help": "ℹ️ Horizon Tutors Hub connects students with verified tutors in Adama and Bishoftu.\n\n/start - register as a tutor or request a tutor\n/cancel - cancel what you're doing\n/myapplications - your job applications (tutors)\n/help - this message\n\nTo apply for a job, tap “Apply” under the job post in our channel.",
        "apply_card": "📌 <b>Job {code}</b>\n🎓 Grade: {grade}\n📚 Subject: {subject}\n📍 Area: {area}\n🕒 Schedule: {duration}\n💰 Salary: {budget}\n\nInterested? Tap Apply and we'll send your application to our team.",
        "btn_apply": "✅ Apply",
        "apply_not_registered": "To apply you must first register as a tutor. Send /start.",
        "apply_not_approved": "Your tutor registration status is: {status}. You can apply once it is approved.",
        "apply_job_closed": "Sorry, this job is no longer open.",
        "apply_already": "You have already applied for this job.",
        "apply_done": "✅ Your application for {code} is in! We'll message you here as soon as a decision is made.",
        "apply_failed": "❌ Sorry, we couldn't save your application. Please try again later.",
        "app_selected": "🎉 Congratulations! You've been selected for job {code}. Our team will contact you shortly with the next steps.",
        "app_not_selected": "Thanks for applying to job {code}. Another tutor was chosen this time, but don't be discouraged! New jobs are posted in our channel regularly 💪",
        "app_rejected": "Your application for job {code} was not accepted.",
        "my_apps_header": "📋 Your applications:",
        "my_apps_empty": "You have no applications yet.",
        "ask_grade_pick": "{section}: which grade?",
        "ask_gender_pref": "👩‍🏫👨‍🏫 Would you prefer a male or a female tutor?",
        "ask_days": "📅 How many days per week would you like lessons?",
        "ask_days_text": "✏️ Type the days you'd like (e.g. weekends only):",
        "ask_hours": "⏰ How long should each lesson be?",
        "ask_hours_text": "✏️ Type the lesson length (e.g. 1 hour 30 minutes):",
        "btn_skip": "⏭ Skip",
        "btn_back": "⬅ Back",
        "btn_type": "✏️ Other (type it)",
        "pref_male": "👨 Male tutor",
        "pref_female": "👩 Female tutor",
        "pref_any": "🤝 No preference",
        "hr_label": "{n} hr",
        "btn_join": "📢 Join our channel",
        "join_prompt": "📢 Join our channel so you never miss a new tutoring job or update 👇",
    },
    "am": {
        "menu": "👋 እንኳን ወደ ሆራይዘን ቱተርስ ሃብ በደህና መጡ!\n\nበአዳማ እና በቢሾፍቱ ለሚገኙ ቤተሰቦች መታወቂያቸው የተረጋገጠ ታማኝ አስተማሪዎችን እናገናኛለን። ትምህርት በቤት ውስጥ ቀላል፣ አስተማማኝ እና ከጭንቀት ነጻ እንዲሆን። 🎓\n\nዛሬ እንዴት ልንረዳዎት እንችላለን?",
        "btn_tutor": "👨‍🏫 አስተማሪ ነኝ - ሥራ እፈልጋለሁ",
        "btn_client": "🔎 አስተማሪ እፈልጋለሁ",
        "use_buttons": "እባክዎ ከላይ ካሉት አማራጮች አንዱን ይጫኑ 👆",
        "ask_name": "እንጀምር! 😊 ሙሉ ስምዎን ያስገቡ፦",
        "ask_phone": "እናመሰግናለን! 📱 የሚደውሉበትን ስልክ ቁጥር ያጋሩ።\nከታች ያለውን ቁልፍ ይጫኑ ወይም ይጻፉ (ለምሳሌ 0912345678)።",
        "share_phone": "📱 ስልክ ቁጥሬን አጋራ",
        "contact_not_yours": "እባክዎ በቁልፉ የራስዎን ስልክ ቁጥር ብቻ ያጋሩ 🙏",
        "phone_saved": "✅ ተቀብለናል፦ {phone}",
        "bad_phone": "ትክክለኛ የኢትዮጵያ ሞባይል ቁጥር አይመስልም። ቁልፉን ይጫኑ ወይም እንደ 0912345678 ብለው ይጻፉ 🙏",
        "ask_gender": "ጾታዎን ይምረጡ፦",
        "male": "ወንድ",
        "female": "ሴት",
        "ask_subject": "📚 የሚያስተምሩት ትምህርት(ቶች) ምንድን ናቸው? (ለምሳሌ ሒሳብ፣ ፊዚክስ፣ እንግሊዝኛ)",
        "ask_city": "📍 በየትኛው ከተማ ነው የሚኖሩት?",
        "adama": "አዳማ",
        "bishoftu": "ቢሾፍቱ",
        "ask_hood": "🏘 የትኛው ሰፈር / አካባቢ ነው?",
        "ask_id": "🪪 ሊጨርሱ ነው! ቤተሰቦችን ለመጠበቅ ሁሉም አስተማሪዎቻችን መታወቂያቸው ይረጋገጣል። እባክዎ የብሔራዊ መታወቂያዎን የፊት ገጽ ግልጽ ፎቶ ይላኩ።\n\n💡 ጠቃሚ ምክር፦ ጽሑፉ በግልጽ እንዲነበብ ፎቶውን እንደ ፋይል (File) ይላኩ።",
        "not_image": "እባክዎ የመታወቂያዎን ምስል (ፎቶ) ይላኩ።",
        "too_large": "ፋይሉ በጣም ትልቅ ነው። እባክዎ ከ15 ሜጋባይት በታች የሆነ ምስል ይላኩ።",
        "blurry": "⚠️ ፎቶው ደብዛዛ ወይም ለማንበብ በጣም ትንሽ ነው። እባክዎ በጥሩ ብርሃን ቀጥ አድርገው እንደገና አንስተው ይላኩ 📸",
        "dl_failed": "ፋይሉን ማውረድ አልተቻለም። እባክዎ እንደገና ይላኩ።",
        "processing": "⏳ ትንሽ ይጠብቁ፣ መረጃዎን በማስቀመጥ ላይ ነን…",
        "tutor_done": "🎉 እናመሰግናለን፣ ምዝገባዎ ደርሶናል!\n\nቡድናችን መረጃዎን በማረጋገጥ ላይ ነው። ሲጸድቁ እዚሁ መልዕክት እንልክልዎታለን፤ ከዚያም ለሥራዎች ማመልከት ይችላሉ።",
        "save_failed_photo": "❌ ይቅርታ፣ በቴክኒክ ችግር ምክንያት ምዝገባዎን ማስቀመጥ አልቻልንም። ምንም አልተመዘገበም። ለመድገም ፎቶውን እንደገና ይላኩ ወይም /cancel ይበሉ።",
        "save_failed_text": "❌ ይቅርታ፣ በቴክኒክ ችግር ምክንያት ጥያቄዎን ማስቀመጥ አልቻልንም። ምንም አልተመዘገበም። ለመድገም በጀትዎን እንደገና ይላኩ ወይም /cancel ይበሉ።",
        "tutor_approved": "🎉 እንኳን ደስ አለዎት፣ ተረጋግጠው ጸድቀዋል!\n\nአሁን ለሥራዎች ማመልከት ይችላሉ፤ በቻናላችን ላይ ባለ ማንኛውም የሥራ ልጥፍ ስር “አመልክት” ይጫኑ። መልካም ዕድል! 🍀",
        "tutor_rejected": "ይቅርታ፣ ምዝገባዎ አልጸደቀም። እባክዎ ድጋፍን ያነጋግሩ ወይም በግልጽ የመታወቂያ ፎቶ እንደገና ይመዝገቡ።",
        "ask_student_grade": "🎓 ተማሪው በየትኛው ደረጃ ነው?",
        "ask_other_grade": "✏️ ክፍሉን / ደረጃውን ይጻፉ (ለምሳሌ 6ኛ እና 8ኛ ክፍል)፦",
        "ask_c_subject": "📚 ተማሪው በየትኛው ትምህርት(ቶች) እገዛ ይፈልጋል?",
        "ask_req": "📝 አስተማሪው ማወቅ ያለበት ሌላ ነገር አለ? (የሚመረጥ ሰዓት፣ የተማሪው ፍላጎት ወዘተ)\nከሌለ “ዝለል” ይጫኑ።",
        "ask_budget": "💰 የመጨረሻው! ለአስተማሪው ወርሃዊ በጀትዎ ስንት ነው (በብር)?\nለምሳሌ፦ 3000",
        "client_done": "✅ ጥያቄዎ ({code}) ደርሶናል!\n\nቡድናችን እየመረመረው ነው፤ ከዚያም ለተረጋገጡ አስተማሪዎቻችን እናሳውቃለን። ሲለቀቅ እዚሁ እናሳውቅዎታለን።",
        "client_approved": "🎉 መልካም ዜና! ጥያቄዎ {code} ጸድቆ ተለቅቋል። አስተማሪዎች አሁን ማየት ይችላሉ፤ ተስማሚውን አስተማሪ በቅርቡ እናገናኝዎታለን።",
        "client_rejected": "ጥያቄዎ {code} አልጸደቀም። ለዝርዝር ድጋፍን ያነጋግሩ።",
        "client_matched": "✅ መልካም ዜና! ለጥያቄዎ {code} አስተማሪ ተመርጧል። ሁሉንም ለማመቻቸት ቡድናችን በቅርቡ ይደውልልዎታል። 🤝",
        "cancelled": "ተሰርዟል። እንደገና ለመጀመር /start ይላኩ።",
        "hint": "ለመጀመር እባክዎ /start ይላኩ።",
        "error": "የሆነ ችግር ተፈጥሯል። እባክዎ በ /start እንደገና ይሞክሩ።",
        "paused": "🚧 በአሁኑ ጊዜ አዳዲስ ጥያቄዎችን አንቀበልም። እባክዎ ቆይተው ይሞክሩ።",
        "support": "እርዳታ ይፈልጋሉ? {handle} | {phone}",
        "help": "ℹ️ ሆራይዘን ቱተርስ ሃብ በአዳማ እና ቢሾፍቱ ተማሪዎችን ከተረጋገጡ አስተማሪዎች ጋር ያገናኛል።\n\n/start - እንደ አስተማሪ ለመመዝገብ ወይም አስተማሪ ለመፈለግ\n/cancel - የጀመሩትን ለመሰረዝ\n/myapplications - የእርስዎ የሥራ ማመልከቻዎች (አስተማሪዎች)\n/help - ይህ መልዕክት\n\nለሥራ ለማመልከት በቻናላችን ላይ ባለው ልጥፍ ስር “አመልክት” ይጫኑ።",
        "apply_card": "📌 <b>ሥራ {code}</b>\n🎓 ክፍል፦ {grade}\n📚 ትምህርት፦ {subject}\n📍 አካባቢ፦ {area}\n🕒 የጊዜ ሰሌዳ፦ {duration}\n💰 ደመወዝ፦ {budget}\n\nፍላጎት አለዎት? “አመልክት” ይጫኑ፤ ማመልከቻዎን ለቡድናችን እንልካለን።",
        "btn_apply": "✅ አመልክት",
        "apply_not_registered": "ለማመልከት መጀመሪያ እንደ አስተማሪ መመዝገብ አለብዎት። /start ይላኩ።",
        "apply_not_approved": "የምዝገባዎ ሁኔታ፦ {status}። ከጸደቀ በኋላ ማመልከት ይችላሉ።",
        "apply_job_closed": "ይቅርታ፣ ይህ ሥራ አሁን ክፍት አይደለም።",
        "apply_already": "ለዚህ ሥራ ቀድሞ አመልክተዋል።",
        "apply_done": "✅ ለ{code} ማመልከቻዎ ደርሶናል! ውሳኔ እንደተሰጠ እዚሁ እናሳውቅዎታለን።",
        "apply_failed": "❌ ይቅርታ፣ ማመልከቻዎን ማስቀመጥ አልተቻለም። እባክዎ ቆይተው ይሞክሩ።",
        "app_selected": "🎉 እንኳን ደስ አለዎት! ለሥራ {code} ተመርጠዋል። ቀጣዩን ሂደት ለማሳወቅ ቡድናችን በቅርቡ ያነጋግርዎታል።",
        "app_not_selected": "ለሥራ {code} ስላመለከቱ እናመሰግናለን። በዚህ ጊዜ ሌላ አስተማሪ ተመርጧል፤ ግን አይሸነፉ! አዳዲስ ሥራዎች በቻናላችን በየጊዜው ይለቀቃሉ 💪",
        "app_rejected": "ለሥራ {code} ያቀረቡት ማመልከቻ አልተቀበለም።",
        "my_apps_header": "📋 የእርስዎ ማመልከቻዎች፦",
        "my_apps_empty": "እስካሁን ምንም ማመልከቻ የለም።",
        "ask_grade_pick": "{section} - የትኛው ክፍል ነው?",
        "ask_gender_pref": "👩‍🏫👨‍🏫 ወንድ ወይስ ሴት አስተማሪ ይመርጣሉ?",
        "ask_days": "📅 በሳምንት ስንት ቀን ትምህርት እንዲሰጥ ይፈልጋሉ?",
        "ask_days_text": "✏️ የሚፈልጓቸውን ቀናት ይጻፉ (ለምሳሌ ቅዳሜና እሁድ ብቻ)፦",
        "ask_hours": "⏰ እያንዳንዱ ትምህርት ስንት ሰዓት ይሁን?",
        "ask_hours_text": "✏️ የትምህርቱን ርዝመት ይጻፉ (ለምሳሌ አንድ ሰዓት ተኩል)፦",
        "btn_skip": "⏭ ዝለል",
        "btn_back": "⬅ ተመለስ",
        "btn_type": "✏️ ሌላ (ራሴ ልጻፍ)",
        "pref_male": "👨 ወንድ አስተማሪ",
        "pref_female": "👩 ሴት አስተማሪ",
        "pref_any": "🤝 ምርጫ የለኝም",
        "hr_label": "{n} ሰዓት",
        "btn_join": "📢 ቻናላችንን ይቀላቀሉ",
        "join_prompt": "📢 አዳዲስ የትምህርት ሥራዎችና መረጃዎች እንዳያመልጥዎ ቻናላችንን ይቀላቀሉ 👇",
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


def default_lang(user) -> str:
    return "am" if (getattr(user, "language_code", "") or "").startswith("am") else "en"


async def support_line(lang: str) -> str:
    s = await get_settings()
    return tr_lang(lang, "support", handle=s.get("support_handle", ""), phone=s.get("support_phone", ""))


async def with_support(lang: str, text: str) -> str:
    return f"{text}\n\n{await support_line(lang)}"


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


def normalize_job_code(s: str):
    s = (s or "").strip().upper().lstrip("#")
    if re.fullmatch(r"\d{1,6}", s):
        s = f"{JOB_PREFIX}-{int(s):04d}"
    return "#" + s if re.fullmatch(rf"{JOB_PREFIX}-\d{{3,6}}", s) else None


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


def grade_keyboard(context):
    """Step 1 of the grade picker: KG / Primary / High School (+ University, + type it)."""
    idx = 2 if lang_of(context) == "am" else 1
    rows = [[InlineKeyboardButton(s[idx], callback_data=f"gsec:{s[0]}")] for s in GRADE_SECTIONS]
    rows.append([InlineKeyboardButton(UNI_GRADE[idx], callback_data="grade:UNI")])
    rows.append([InlineKeyboardButton(tr(context, "btn_type"), callback_data="gsec:TYPE")])
    return InlineKeyboardMarkup(rows)


def grade_pick_keyboard(context, sec_key: str):
    """Step 2: the individual grades inside the chosen section."""
    idx = 2 if lang_of(context) == "am" else 1
    sec = next(s for s in GRADE_SECTIONS if s[0] == sec_key)
    buttons = [InlineKeyboardButton(g[idx], callback_data=f"grade:{g[0]}") for g in sec[3]]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(tr(context, "btn_back"), callback_data="gsec:BACK")])
    return InlineKeyboardMarkup(rows)


def gpref_keyboard(context):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(context, "pref_male"), callback_data="gpref:Male"),
         InlineKeyboardButton(tr(context, "pref_female"), callback_data="gpref:Female")],
        [InlineKeyboardButton(tr(context, "pref_any"), callback_data="gpref:Any")],
    ])


def days_keyboard(context):
    nums = [InlineKeyboardButton(str(n), callback_data=f"days:{n}") for n in range(1, 8)]
    return InlineKeyboardMarkup([nums[:4], nums[4:],
                                 [InlineKeyboardButton(tr(context, "btn_type"), callback_data="days:TYPE")]])


def hours_keyboard(context):
    vals = ["1", "1.5", "2", "3"]
    row = [InlineKeyboardButton(tr(context, "hr_label", n=v), callback_data=f"hrs:{v}") for v in vals]
    return InlineKeyboardMarkup([row[:2], row[2:],
                                 [InlineKeyboardButton(tr(context, "btn_type"), callback_data="hrs:TYPE")]])


def skip_keyboard(context):
    return InlineKeyboardMarkup([[InlineKeyboardButton(tr(context, "btn_skip"), callback_data="skip:req")]])


def join_markup(lang: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton(tr_lang(lang, "btn_join"), url=CHANNEL_LINK)]])


async def send_join_prompt(message, lang: str):
    """Asked at the end of every flow (tutor registration, client request, job application)."""
    try:
        await message.reply_text(tr_lang(lang, "join_prompt"), reply_markup=join_markup(lang))
    except Exception:
        logger.exception("Could not send the join-channel prompt")


def fmt_budget(b) -> str:
    b = clean(str(b or ""), 60)
    if re.fullmatch(r"\d[\d,\s]*", b):
        return f"{int(digits(b)):,} ETB"
    if not b or re.search(r"etb|birr|ብር", b, re.I):
        return b
    return f"{b} ETB"


def duration_text(days, hours) -> str:
    d, h = str(days or "").strip(), str(hours or "").strip()
    if d.isdigit():
        d = f"{d} {'day' if d == '1' else 'days'}/week"
    if re.fullmatch(r"\d+(\.\d+)?", h):
        h = f"{h} {'hour' if h == '1' else 'hours'}/day"
    return " · ".join(x for x in (d, h) if x)


def phone_keyboard(context):
    """Reply keyboard with a 'share phone' button shown right above the typing keyboard."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(tr(context, "share_phone"), request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True, input_field_placeholder="0912345678")


async def notify_admin(context, text: str, **kwargs):
    try:
        return await context.bot.send_message(ADMIN_CHAT_ID, text, **kwargs)
    except Exception:
        logger.exception("ADMIN NOTIFICATION FAILED (check ADMIN_CHAT_ID and that the admin pressed /start). "
                         "Message was: %s", text[:300])
        return None


def _is_admin(q) -> bool:
    return q.from_user.id == ADMIN_CHAT_ID or (q.message is not None and q.message.chat.id == ADMIN_CHAT_ID)


def _is_admin_user(update: Update) -> bool:
    return (update.effective_user is not None and update.effective_user.id == ADMIN_CHAT_ID) or (
        update.effective_chat is not None and update.effective_chat.id == ADMIN_CHAT_ID)


# --------------------------------------------------------------------------- #
# Conversation states
# --------------------------------------------------------------------------- #
(LANG, MENU,
 T_NAME, T_PHONE, T_GENDER, T_SUBJECT, T_CITY, T_HOOD, T_ID,
 C_NAME, C_PHONE, C_GRADE, C_GRADE_TEXT, C_SUBJECT, C_CITY, C_HOOD, C_REQ, C_BUDGET,
 C_GRADE_PICK, C_GENDER, C_DAYS, C_DAYS_TEXT, C_HOURS, C_HOURS_TEXT) = range(24)

PRIVATE = filters.ChatType.PRIVATE
TEXT_ONLY = filters.TEXT & ~filters.COMMAND & PRIVATE
CONTACT_OR_TEXT = (filters.CONTACT | (filters.TEXT & ~filters.COMMAND)) & PRIVATE


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Chat ID: {update.effective_chat.id}\nYour user ID: {update.effective_user.id}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang") or default_lang(update.effective_user)
    await update.effective_message.reply_text(await with_support(lang, tr_lang(lang, "help")))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args and args[0].startswith("apply_"):
        code = normalize_job_code(args[0][len("apply_"):])
        if code:
            await apply_intro(update, context, code)
            return ConversationHandler.END
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
    if not await bot_active():
        await q.message.reply_text(await with_support(lang_of(context), tr(context, "paused")))
        context.user_data.clear()
        return ConversationHandler.END
    flow = q.data.split(":")[1]
    context.user_data["flow"] = flow
    await q.message.reply_text(tr(context, "ask_name"))
    return T_NAME if flow == "tutor" else C_NAME


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang") or default_lang(update.effective_user)
    context.user_data.clear()
    await update.effective_message.reply_text(tr_lang(lang, "cancelled"), reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def use_buttons(state):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(tr(context, "use_buttons"))
        return state
    return handler


def text_step(state, field, next_state, prompt_key, *, maxlen=120, markup_fn=None):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        value = clean(update.message.text, maxlen)
        if not value:
            return state
        context.user_data[field] = value
        await update.message.reply_text(
            tr(context, prompt_key),
            reply_markup=markup_fn(context) if markup_fn else None)
        return next_state
    return handler


def phone_step(state, next_state, next_prompt_key, next_markup_fn):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if msg.contact:
            if msg.contact.user_id and msg.contact.user_id != update.effective_user.id:
                await msg.reply_text(tr(context, "contact_not_yours"), reply_markup=phone_keyboard(context))
                return state
            raw = msg.contact.phone_number or ""
        else:
            raw = msg.text or ""
        phone = normalize_phone(clean(raw, 30))
        if phone is None:
            await msg.reply_text(tr(context, "bad_phone"), reply_markup=phone_keyboard(context))
            return state
        context.user_data["phone"] = phone
        # Remove the share-phone keyboard, then continue with the next question
        await msg.reply_text(tr(context, "phone_saved", phone=phone), reply_markup=ReplyKeyboardRemove())
        await msg.reply_text(tr(context, next_prompt_key), reply_markup=next_markup_fn(context))
        return next_state
    return handler


def choice_step(field, next_state, prompt_key, markup_fn=None):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        context.user_data[field] = q.data.split(":")[1]
        await q.message.reply_text(tr(context, prompt_key),
                                   reply_markup=markup_fn(context) if markup_fn else None)
        return next_state
    return handler


async def _drop_buttons(q):
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def grade_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _drop_buttons(q)
    key = q.data.split(":", 1)[1]
    if key == "TYPE":
        await q.message.reply_text(tr(context, "ask_other_grade"))
        return C_GRADE_TEXT
    sec = next(s for s in GRADE_SECTIONS if s[0] == key)
    label = sec[2] if lang_of(context) == "am" else sec[1]
    await q.message.reply_text(tr(context, "ask_grade_pick", section=label),
                               reply_markup=grade_pick_keyboard(context, key))
    return C_GRADE_PICK


async def grade_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _drop_buttons(q)
    await q.message.reply_text(tr(context, "ask_student_grade"), reply_markup=grade_keyboard(context))
    return C_GRADE


async def grade_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    label = GRADE_LABELS.get(q.data.split(":", 1)[1])
    if not label:
        return None
    await _drop_buttons(q)
    context.user_data["grade"] = label
    await q.message.reply_text(tr(context, "ask_c_subject"))
    return C_SUBJECT


async def days_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _drop_buttons(q)
    val = q.data.split(":", 1)[1]
    if val == "TYPE":
        await q.message.reply_text(tr(context, "ask_days_text"))
        return C_DAYS_TEXT
    context.user_data["days"] = val
    await q.message.reply_text(tr(context, "ask_hours"), reply_markup=hours_keyboard(context))
    return C_HOURS


async def hours_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _drop_buttons(q)
    val = q.data.split(":", 1)[1]
    if val == "TYPE":
        await q.message.reply_text(tr(context, "ask_hours_text"))
        return C_HOURS_TEXT
    context.user_data["hours"] = val
    await q.message.reply_text(tr(context, "ask_req"), reply_markup=skip_keyboard(context))
    return C_REQ


async def req_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _drop_buttons(q)
    context.user_data["req"] = ""
    await q.message.reply_text(tr(context, "ask_budget"))
    return C_BUDGET


# --------------------------------------------------------------------------- #
# Tutor flow: final step (ID photo -> [Drive] -> Sheet -> admin)
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
        try:
            tg_file = await tg_obj.get_file()
            raw = bytes(await tg_file.download_as_bytearray())
        except Exception:
            logger.exception("Telegram file download failed | user=%s", user.id)
            await msg.reply_text(tr(context, "dl_failed"))
            return T_ID

        ok, score, reason = await asyncio.to_thread(check_sharpness, raw)
        if reason == "not_image":
            await msg.reply_text(tr(context, "not_image"))
            return T_ID
        if not ok:
            await msg.reply_text(tr(context, "blurry"))
            return T_ID

        if not await bot_active():
            await msg.reply_text(await with_support(lang_of(context), tr(context, "paused")))
            ud.clear()
            return ConversationHandler.END

        await msg.reply_text(tr(context, "processing"))

        if not USE_DRIVE:
            ud["id_ref"] = tg_obj.file_id
        elif not ud.get("id_ref"):
            fname = f"TutorID_{user.id}_{datetime.now(EAT).strftime('%Y%m%d_%H%M%S')}.{ext}"
            try:
                f = await asyncio.to_thread(upload_to_drive, raw, fname, mime)
                ud["id_ref"] = f.get("webViewLink") or f"https://drive.google.com/file/d/{f.get('id')}/view"
            except Exception as e:
                logger.error("TUTOR SAVE FAILED at DRIVE step | user=%s | %s", user.id, e, exc_info=True)
                await notify_admin(context, f"⚠️ Drive upload FAILED for tutor {user.id}:\n{e}")
                await msg.reply_text(tr(context, "save_failed_photo"))
                return T_ID

        data = {
            "Timestamp": now_str(),
            "Telegram ID": user.id,
            "Username": user_tag(user),
            "Name": ud["name"],
            "Phone": ud["phone"],
            "Gender": ud["gender"],
            "Subjects": ud["subject"],
            "Grades": "",
            "Location": f"{ud['hood']}, {ud['city']}",
            "Qualification": "Not provided",
            "ID Verification Status": "Pending",
            "Status": "Pending",
            "ID File ID": ud["id_ref"],
            "Language": lang_of(context),
        }
        try:
            code, rng = await asyncio.to_thread(append_with_code, "Tutors", data)
        except Exception as e:
            logger.error("TUTOR SAVE FAILED at SHEETS step | user=%s | %s", user.id, e, exc_info=True)
            await notify_admin(context, f"⚠️ Sheets append FAILED for tutor {user.id}:\n{e}")
            await msg.reply_text(tr(context, "save_failed_photo"))
            return T_ID

        caption = (
            f"🆕 <b>Tutor registration</b>\n"
            f"👤 {esc(ud['name'])}  |  {esc(ud['gender'])}\n"
            f"📞 {esc(ud['phone'])}\n"
            f"📚 {esc(ud['subject'])}\n"
            f"📍 {esc(ud['hood'])}, {esc(ud['city'])}\n"
        )
        if USE_DRIVE:
            caption += f"🔗 <a href=\"{esc(ud['id_ref'])}\">ID on Drive</a>\n"
        caption += f"Telegram: {mention(user)}"
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
            logger.exception("Admin photo notification failed, falling back to text")
            sent = await notify_admin(context, caption, parse_mode=ParseMode.HTML, reply_markup=buttons)
            if sent is None:
                logger.error("ADMIN NEVER NOTIFIED about tutor %s - approve manually in the sheet.", code)

        await msg.reply_text(await with_support(lang_of(context), tr(context, "tutor_done")))
        await send_join_prompt(msg, lang_of(context))
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
        if not await bot_active():
            await update.message.reply_text(await with_support(lang_of(context), tr(context, "paused")))
            ud.clear()
            return ConversationHandler.END

        await update.message.reply_text(tr(context, "processing"))
        settings = await get_settings()
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
            "Tutor Gender Pref": ud.get("gpref", "Any"),
            "Duration": duration_text(ud.get("days"), ud.get("hours")),
            "Budget": budget,
            "Budget Number": "".join(filter(str.isdigit, budget)) or "0",
            "Commission Terms": settings.get("commission_rate", "50%"),
            "Verification": "Pending",
            "Job Status": "Open",
            "Channel Msg ID": "",
            "Requirements": ud.get("req", ""),
            "Language": lang_of(context),
        }
        try:
            code, rng = await asyncio.to_thread(append_with_code, "Client_Requests", data)
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
            f"👩‍🏫 Tutor preference: {esc(ud.get('gpref', 'Any'))}\n"
            f"🕒 Schedule: {esc(duration_text(ud.get('days'), ud.get('hours')))}\n"
            f"💰 Budget: {esc(fmt_budget(budget))}\n"
            f"📝 {esc(ud.get('req') or '-')}\n"
            f"Telegram: {mention(user)}"
        )
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"cA:{code}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"cR:{code}"),
        ]])
        sent = await notify_admin(context, text, parse_mode=ParseMode.HTML, reply_markup=buttons)
        if sent is None:
            logger.error("ADMIN NEVER NOTIFIED about client request %s - approve manually in the sheet.", code)

        await update.message.reply_text(await with_support(lang_of(context), tr(context, "client_done", code=code)))
        await send_join_prompt(update.message, lang_of(context))
        ud.clear()
        return ConversationHandler.END
    finally:
        ud.pop("busy", None)


# --------------------------------------------------------------------------- #
# Admin approve / reject (tutors and client requests)
# --------------------------------------------------------------------------- #
ADMIN_LOCK = asyncio.Lock()


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


def _channel_post(data: dict, code: str, bot_username: str, filled: bool = False):
    """Job post for the channel. 🟢 OPEN while looking for a tutor, 🔴 CLOSED once a tutor is matched."""
    pref = (data.get("Tutor Gender Pref") or "").strip().lower()
    title = {"male": "Male Tutor", "female": "Female Tutor"}.get(pref, "Tutor Needed")
    status = "🔴 <b>CLOSED · ተዘግቷል</b>" if filled else "🟢 <b>OPEN · ክፍት</b>"
    lines = [status, "", f"📖 <b>{title}</b> 📖", "", f"Job code: {esc(code)}", ""]
    fields = [
        ("Grade", data.get("Grade")),
        ("City", data.get("City")),
        ("Place", data.get("Place")),
        ("Subject", data.get("Subject")),
        ("Duration", data.get("Duration")),
        ("Salary (per month)", fmt_budget(data.get("Budget"))),
    ]
    for label, value in fields:
        if value:
            lines.append(f"✅ {label} : {esc(value)}")
    note = (data.get("Requirements") or "").strip() or "We need a highly committed, punctual and talented tutor"
    lines += ["", f"📖 {esc(note)}"]
    if filled:
        lines += ["", "✅ This job has been filled and a tutor has been matched. "
                      "Watch the channel for new jobs! / ይህ ሥራ ተሞልቷል።"]
        return "\n".join(lines), None
    payload = code.lstrip("#")
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("📩 Apply Now / ያመልክቱ", url=f"https://t.me/{bot_username}?start=apply_{payload}")
    ]])
    return "\n".join(lines), markup


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
        current = (data.get(status_field) or "").strip()
        if current.lower() not in ("pending", ""):
            await _mark_admin_message(q, f"\n\nℹ️ Already <b>{esc(current)}</b>.")
            return

        sent_channel = None
        if approve and not is_tutor:
            try:
                text, markup = _channel_post(data, code, context.bot.username)
                sent_channel = await context.bot.send_message(JOB_CHANNEL_ID, text, parse_mode=ParseMode.HTML,
                                                              reply_markup=markup)
                logger.info("Broadcast OK | %s -> %s (msg %s)", code, JOB_CHANNEL_ID, sent_channel.message_id)
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
            warn = "⚠️ It WAS posted to the channel but the sheet status could not be updated. " if sent_channel else ""
            await q.message.reply_text(f"❌ Could not update status for {code}:\n{e}\n{warn}Update it manually.")
            return
        if sent_channel is not None:
            try:
                await asyncio.to_thread(set_cells, "Client_Requests", row_no,
                                        {"Channel Msg ID": str(sent_channel.message_id)})
            except Exception:
                logger.exception("Could not store Channel Msg ID for %s", code)

    lang = (data.get("Language") or "en").strip()
    key = ("tutor_" if is_tutor else "client_") + ("approved" if approve else "rejected")
    text_user = tr_lang(lang, key, code=code)
    if not approve:
        text_user = await with_support(lang, text_user)
    try:
        await context.bot.send_message(int(digits(data["Telegram ID"])), text_user)
    except Exception:
        logger.exception("Could not notify user %s about %s", data.get("Telegram ID"), code)
        await q.message.reply_text(f"⚠️ {code} was {new_status.lower()}, but the user could not be notified "
                                   "(they may have blocked the bot).")

    icon = "✅" if approve else "❌"
    await _mark_admin_message(q, f"\n\n{icon} <b>{new_status}</b> by {esc(q.from_user.full_name)}")


# --------------------------------------------------------------------------- #
# Tutor applications: apply (tutor side)
# --------------------------------------------------------------------------- #
async def apply_intro(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    user, msg = update.effective_user, update.effective_message
    lang = context.user_data.get("lang") or default_lang(user)
    if not await bot_active():
        await msg.reply_text(await with_support(lang, tr_lang(lang, "paused")))
        return
    try:
        res = await asyncio.to_thread(check_apply, user.id, code)
    except Exception as e:
        logger.error("Apply check failed | user=%s | %s | %s", user.id, code, e, exc_info=True)
        await msg.reply_text(tr_lang(lang, "error"))
        return
    lang = res.get("lang", lang)
    if "error" in res:
        await msg.reply_text(tr_lang(lang, res["error"], status=res.get("status", "")))
        return
    job = res["job"]
    text = tr_lang(lang, "apply_card", code=esc(code), subject=esc(job.get("Subject", "")),
                   grade=esc(job.get("Grade", "")),
                   area=esc(f"{job.get('Place', '')}, {job.get('City', '')}"),
                   duration=esc(job.get("Duration") or "-"),
                   budget=esc(fmt_budget(job.get("Budget", ""))))
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton(tr_lang(lang, "btn_apply"), callback_data=f"apply:{code.lstrip('#')}")]])
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


def app_card(app: dict, tutor=None) -> str:
    tid = digits(app.get("Tutor Telegram"))
    name = esc(app.get("Tutor Name", ""))
    link = f'<a href="tg://user?id={tid}">{name}</a>' if tid else name
    lines = [f"📥 <b>Application</b> · {esc(app.get('Job Code', ''))}",
             f"👤 {link}",
             f"📞 {esc(app.get('Tutor Phone', ''))}"]
    if tutor:
        lines.append(f"📚 {esc(tutor.get('Subjects', ''))} | 📍 {esc(tutor.get('Location', ''))} | "
                     f"{esc(tutor.get('Gender', ''))}")
    lines.append(f"🕒 {esc(app.get('Timestamp', ''))} · Status: <b>{esc(app.get('Status', ''))}</b>")
    return "\n".join(lines)


def app_buttons(app: dict, tutor=None):
    rows = []
    if (app.get("Status") or "").strip().lower() == "pending":
        code, tid = app.get("Job Code", ""), digits(app.get("Tutor Telegram"))
        rows.append([
            InlineKeyboardButton("✅ Select tutor", callback_data=f"aS:{code}:{tid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"aR:{code}:{tid}"),
        ])
    ts = (tutor or {}).get("Timestamp")
    if ts:
        rows.append([InlineKeyboardButton("🪪 Tutor record + ID", callback_data=f"tv:{ts}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def apply_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    lang = context.user_data.get("lang") or default_lang(user)
    code = normalize_job_code(q.data.split(":", 1)[1])
    if not code:
        return
    if not await bot_active():
        await q.message.reply_text(await with_support(lang, tr_lang(lang, "paused")))
        return
    try:
        res = await asyncio.to_thread(submit_application, user.id, code)
    except Exception as e:
        logger.error("APPLICATION SAVE FAILED | user=%s | %s | %s", user.id, code, e, exc_info=True)
        await notify_admin(context, f"⚠️ Saving an application FAILED ({code}, user {user.id}):\n{e}")
        await q.message.reply_text(tr_lang(lang, "apply_failed"))
        return
    lang = res.get("lang", lang)
    if "error" in res:
        await q.message.reply_text(tr_lang(lang, res["error"], status=res.get("status", "")))
        return
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.message.reply_text(tr_lang(lang, "apply_done", code=code))
    await send_join_prompt(q.message, lang)

    job = res["job"]
    app = dict(res["app"])
    header = (f"📌 <b>{esc(code)}</b> · {esc(job.get('Subject', ''))} · {esc(job.get('Grade', ''))} · "
              f"{esc(job.get('Place', ''))}, {esc(job.get('City', ''))} · {esc(fmt_budget(job.get('Budget', '')))}\n\n")
    sent = await notify_admin(context, header + app_card(app, res["tutor"]), parse_mode=ParseMode.HTML,
                              reply_markup=app_buttons(app, res["tutor"]))
    if sent is None:
        logger.error("ADMIN NEVER NOTIFIED about application %s / %s - see /applications.", code, user.id)


async def cmd_myapplications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = context.user_data.get("lang") or default_lang(user)
    try:
        items = await asyncio.to_thread(list_applications, None, user.id, False)
    except Exception as e:
        logger.error("My applications failed | user=%s | %s", user.id, e, exc_info=True)
        await update.effective_message.reply_text(tr_lang(lang, "error"))
        return
    if not items:
        await update.effective_message.reply_text(tr_lang(lang, "my_apps_empty"))
        return
    t = items[-1].get("_tutor") or {}
    if (t.get("Language") or "").strip() in ("en", "am"):
        lang = t["Language"].strip()
    lines = [tr_lang(lang, "my_apps_header")]
    for it in items[-15:]:
        lines.append(f"• {it.get('Job Code')} - {it.get('Status')}")
    await update.effective_message.reply_text("\n".join(lines))


# --------------------------------------------------------------------------- #
# Applications: retrieve + decide (admin side)
# --------------------------------------------------------------------------- #
async def send_applications(context, chat_id: int, code):
    try:
        items = await asyncio.to_thread(list_applications, code, None, code is None)
    except Exception as e:
        logger.error("Reading applications failed | %s | %s", code, e, exc_info=True)
        await context.bot.send_message(chat_id, f"❌ Could not read applications:\n{e}")
        return
    if not items:
        await context.bot.send_message(chat_id, f"No applications found for {code}." if code
                                       else "No pending applications.")
        return
    limit = 15
    head = (f"📂 Applications for {code}: {len(items)}" if code else f"📂 Pending applications: {len(items)}")
    if len(items) > limit:
        head += f" (showing latest {limit})"
    await context.bot.send_message(chat_id, head)
    for it in items[-limit:]:
        await context.bot.send_message(chat_id, app_card(it, it.get("_tutor")), parse_mode=ParseMode.HTML,
                                       reply_markup=app_buttons(it, it.get("_tutor")))


async def cmd_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin_user(update):
        return
    code = None
    if context.args:
        code = normalize_job_code(context.args[0])
        if not code:
            await update.effective_message.reply_text("Usage: /applications HRZ-0001  (or just /applications)")
            return
    await send_applications(context, update.effective_chat.id, code)


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin_user(update):
        return
    try:
        jobs = await asyncio.to_thread(list_open_jobs)
    except Exception as e:
        logger.error("Listing open jobs failed | %s", e, exc_info=True)
        await update.effective_message.reply_text(f"❌ Could not read jobs:\n{e}")
        return
    if not jobs:
        await update.effective_message.reply_text("No open jobs right now.")
        return
    lines, buttons = ["📋 Open jobs (tap a job to see its applications):"], []
    for j, (total, pending) in jobs[-20:]:
        code = j.get("Job Code", "")
        lines.append(f"{code} · {j.get('Subject', '')} · {j.get('Grade', '')} · "
                     f"{j.get('Place', '')}, {j.get('City', '')} · {j.get('Budget', '')} ETB · "
                     f"{total} applicant(s), {pending} pending")
        if total:
            buttons.append(InlineKeyboardButton(f"📂 {code} ({total})", callback_data=f"jl:{code.lstrip('#')}"))
    markup = InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)]) if buttons else None
    await update.effective_message.reply_text("\n".join(lines)[:4000], reply_markup=markup)


async def jobs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q):
        await q.answer("Not authorized.", show_alert=True)
        return
    await q.answer()
    code = normalize_job_code(q.data.split(":", 1)[1])
    if code:
        await send_applications(context, q.message.chat.id, code)


def full_card(tab: str, d: dict) -> str:
    if tab == "Tutors":
        lines = ["👨‍🏫 <b>Tutor record</b>"]
    else:
        lines = [f"🔎 <b>Client request {esc(d.get('Job Code', ''))}</b>"]
    for k, v in d.items():
        if k.startswith("_"):
            continue
        if k == "ID File ID" and v and not v.startswith("http"):
            v = "(ID photo sent below)"
        lines.append(f"<b>{esc(k)}:</b> {esc(v) if v else '-'}")
    if tab == "Tutors":
        apps = d.get("_apps")
        lines.append("<b>Applications:</b> " + (esc(", ".join(apps)) if apps else "none"))
    tid = digits(d.get("Telegram ID"))
    if tid:
        lines.append(f'<b>Telegram:</b> <a href="tg://user?id={tid}">open chat</a>')
    return "\n".join(lines)[:4000]


def record_buttons(tab: str, d: dict):
    rows = []
    if tab == "Tutors":
        code = d.get("Timestamp", "")
        if (d.get("Status") or "").strip().lower() == "pending" and code:
            rows.append([InlineKeyboardButton("✅ Approve", callback_data=f"tA:{code}"),
                         InlineKeyboardButton("❌ Reject", callback_data=f"tR:{code}")])
    else:
        code = d.get("Job Code", "")
        ver = (d.get("Verification") or "").strip().lower()
        if ver == "pending" and code:
            rows.append([InlineKeyboardButton("✅ Approve", callback_data=f"cA:{code}"),
                         InlineKeyboardButton("❌ Reject", callback_data=f"cR:{code}")])
        elif ver == "approved" and code:
            rows.append([InlineKeyboardButton("📂 Applications", callback_data=f"jl:{code.lstrip('#')}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def send_tutor_id(context, chat_id: int, d: dict):
    ref = (d.get("ID File ID") or "").strip()
    if not ref:
        await context.bot.send_message(chat_id, "🪪 No ID reference is stored for this tutor.")
        return
    caption = f"🪪 ID · {d.get('Name', '')} · {d.get('Phone', '')}"
    if ref.startswith("http"):
        # Drive-stored ID: fetch it with the service account and show it right in the chat
        m = re.search(r"/d/([A-Za-z0-9_-]{20,})|[?&]id=([A-Za-z0-9_-]{20,})", ref)
        fid = (m.group(1) or m.group(2)) if m else None
        if fid:
            try:
                data, mime, fname = await asyncio.to_thread(download_drive_file, fid)
                bio = io.BytesIO(data)
                bio.name = fname
                try:
                    await context.bot.send_photo(chat_id, bio, caption=caption)
                except Exception:
                    bio.seek(0)
                    await context.bot.send_document(chat_id, bio, caption=caption)
                return
            except Exception as e:
                logger.error("Could not fetch ID from Drive | tutor=%s | %s", d.get("Name"), e, exc_info=True)
        await context.bot.send_message(chat_id, f"🪪 ID (Drive link): {ref}")
        return
    try:
        await context.bot.send_photo(chat_id, ref, caption=caption)
    except Exception:
        try:
            await context.bot.send_document(chat_id, ref, caption=caption)
        except Exception as e:
            logger.error("Could not send stored ID image | tutor=%s | %s", d.get("Name"), e, exc_info=True)
            await context.bot.send_message(
                chat_id, "⚠️ Could not load the ID image (the stored file_id is invalid, or it was "
                         "saved by a different bot token). Check the original admin notification for this tutor.")


async def show_record(context, chat_id: int, tab: str, d: dict):
    await context.bot.send_message(chat_id, full_card(tab, d), parse_mode=ParseMode.HTML,
                                   reply_markup=record_buttons(tab, d))
    if tab == "Tutors":
        await send_tutor_id(context, chat_id, d)


async def run_search(context, chat_id: int, tab: str, query: str):
    query = clean(query, 80)
    try:
        results = await asyncio.to_thread(search_rows, tab, query)
    except Exception as e:
        logger.error("Search failed | %s | %r | %s", tab, query, e, exc_info=True)
        await context.bot.send_message(chat_id, f"❌ Search failed:\n{e}")
        return
    if not results:
        hint = ("Try the full phone number (e.g. 0912345678) or the name as registered."
                if tab == "Tutors" else "Try the phone number, the name, or the job code (e.g. HRZ-0001).")
        await context.bot.send_message(chat_id, f"No match for “{query}”. {hint}")
        return
    if len(results) == 1:
        await show_record(context, chat_id, tab, results[0][1])
        return
    buttons = []
    for _, d in results[:8]:
        if tab == "Tutors":
            label = f"{d.get('Name', '')} · {d.get('Phone', '')} · {d.get('Status', '')}"
            cb = f"tv:{d.get('Timestamp', '')}"
        else:
            label = f"{d.get('Job Code', '')} · {d.get('Client Name', '')} · {d.get('Verification', '')}"
            cb = f"cv:{d.get('Job Code', '').lstrip('#')}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=cb)])
    note = f" (showing the 8 newest of {len(results)})" if len(results) > 8 else ""
    await context.bot.send_message(chat_id, f"Found {len(results)} matches for “{query}”{note}. Tap one:",
                                   reply_markup=InlineKeyboardMarkup(buttons))


async def cmd_tutor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin_user(update):
        return
    q = " ".join(context.args or []).strip()
    if not q:
        await update.effective_message.reply_text(
            "Usage: /tutor <phone or full name>\nExample: /tutor 0912345678\n"
            "You can also just type the phone number or name here.")
        return
    await run_search(context, update.effective_chat.id, "Tutors", q)


async def cmd_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin_user(update):
        return
    q = " ".join(context.args or []).strip()
    if not q:
        await update.effective_message.reply_text("Usage: /client <phone, name or job code>\nExample: /client HRZ-0001")
        return
    await run_search(context, update.effective_chat.id, "Client_Requests", q)


async def record_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q):
        await q.answer("Not authorized.", show_alert=True)
        return
    await q.answer()
    kind, ref = q.data.split(":", 1)
    tab = "Tutors" if kind == "tv" else "Client_Requests"
    code = ref if kind == "tv" else "#" + ref
    try:
        rec = await asyncio.to_thread(find_record, tab, code)
    except Exception as e:
        logger.error("Record view failed | %s | %s | %s", tab, code, e, exc_info=True)
        await q.message.reply_text(f"❌ Could not load the record:\n{e}")
        return
    if rec is None:
        await q.message.reply_text(f"❌ {code} not found in '{tab}'.")
        return
    d = dict(rec[1])
    if tab == "Tutors":
        try:
            apps = await asyncio.to_thread(list_applications, None, digits(d.get("Telegram ID")), False)
            d["_apps"] = [f"{a.get('Job Code')} ({a.get('Status')})" for a in apps]
        except Exception:
            logger.warning("Could not load applications for record view", exc_info=True)
    await show_record(context, q.message.chat.id, tab, d)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin: any plain text outside a conversation is a tutor search (phone or name).
    if _is_admin_user(update):
        await run_search(context, update.effective_chat.id, "Tutors", update.message.text or "")
        return
    await fallback_hint(update, context)


async def application_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q):
        await q.answer("Not authorized.", show_alert=True)
        logger.warning("Unauthorized application-button press by user %s", q.from_user.id)
        return
    await q.answer()
    action, rest = q.data.split(":", 1)
    code, tid = rest.rsplit(":", 1)
    select = action == "aS"

    async with ADMIN_LOCK:
        try:
            if select:
                res = await asyncio.to_thread(select_application, code, int(tid))
            else:
                res = await asyncio.to_thread(reject_application, code, int(tid))
        except BusinessError as e:
            await q.message.reply_text(f"ℹ️ {e}")
            return
        except Exception as e:
            logger.error("Application decision failed | %s | %s | %s", code, tid, e, exc_info=True)
            await q.message.reply_text(f"❌ Could not process this application:\n{e}")
            return

    def lang_for(tutor_row):
        l = ((tutor_row or {}).get("Language") or "").strip()
        return l if l in ("en", "am") else "en"

    app, tutor = res["app"], res.get("tutor")
    try:
        await context.bot.send_message(
            int(tid), tr_lang(lang_for(tutor), "app_selected" if select else "app_rejected", code=code))
    except Exception:
        logger.exception("Could not notify tutor %s about %s", tid, code)
        await q.message.reply_text("⚠️ Decision saved, but the tutor could not be notified.")

    if select:
        job = res["job"]
        # tell other applicants the job is filled
        for o in res["others"]:
            try:
                await context.bot.send_message(
                    int(digits(o.get("Tutor Telegram"))),
                    tr_lang(lang_for(o.get("_tutor")), "app_not_selected", code=code))
            except Exception:
                logger.warning("Could not notify other applicant %s about %s", o.get("Tutor Telegram"), code)
        # tell the client
        try:
            await context.bot.send_message(
                int(digits(job.get("Telegram ID"))),
                tr_lang((job.get("Language") or "en").strip(), "client_matched", code=code))
        except Exception:
            logger.warning("Could not notify client of %s", code)
        # mark the channel post as filled
        msg_id = digits(job.get("Channel Msg ID"))
        if msg_id:
            try:
                text, _ = _channel_post(job, code, context.bot.username, filled=True)
                await context.bot.edit_message_text(text, chat_id=JOB_CHANNEL_ID, message_id=int(msg_id),
                                                    parse_mode=ParseMode.HTML, reply_markup=None)
            except Exception:
                logger.exception("Could not mark channel post as filled | %s", code)
        await q.message.reply_text(
            f"✅ Match logged for {code}.\n"
            f"Client: {job.get('Client Name', '')} - {job.get('Phone', '')}\n"
            f"Tutor: {app.get('Tutor Name', '')} - {app.get('Tutor Phone', '')}\n"
            f"Budget: {job.get('Budget', '')} ETB | Commission: {job.get('Commission Terms', '')}\n"
            f"{len(res['others'])} other applicant(s) marked 'Not selected'.")
    icon, word = ("✅", "Selected") if select else ("❌", "Rejected")
    await _mark_admin_message(q, f"\n\n{icon} <b>{word}</b> by {esc(q.from_user.full_name)}")


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
            LANG: [CallbackQueryHandler(set_lang, pattern=r"^lang:(en|am)$"),
                   MessageHandler(TEXT_ONLY, use_buttons(LANG))],
            MENU: [CallbackQueryHandler(menu_choice, pattern=r"^menu:(tutor|client)$"),
                   MessageHandler(TEXT_ONLY, use_buttons(MENU))],

            # ---- tutor: name -> phone -> gender -> subject -> city -> area -> ID photo (no grade) ----
            T_NAME: [MessageHandler(TEXT_ONLY, text_step(T_NAME, "name", T_PHONE, "ask_phone",
                                                         markup_fn=phone_keyboard))],
            T_PHONE: [MessageHandler(CONTACT_OR_TEXT, phone_step(T_PHONE, T_GENDER, "ask_gender", gender_keyboard))],
            T_GENDER: [CallbackQueryHandler(choice_step("gender", T_SUBJECT, "ask_subject"),
                                            pattern=r"^gender:(Male|Female)$"),
                       MessageHandler(TEXT_ONLY, use_buttons(T_GENDER))],
            T_SUBJECT: [MessageHandler(TEXT_ONLY, text_step(T_SUBJECT, "subject", T_CITY, "ask_city",
                                                            markup_fn=city_keyboard))],
            T_CITY: [CallbackQueryHandler(choice_step("city", T_HOOD, "ask_hood"),
                                          pattern=r"^city:(Adama|Bishoftu)$"),
                     MessageHandler(TEXT_ONLY, use_buttons(T_CITY))],
            T_HOOD: [MessageHandler(TEXT_ONLY, text_step(T_HOOD, "hood", T_ID, "ask_id"))],
            T_ID: [
                MessageHandler((filters.PHOTO | filters.Document.IMAGE) & PRIVATE, tutor_id),
                MessageHandler(filters.ALL & ~filters.COMMAND & PRIVATE, tutor_id_wrong),
            ],

            # ---- client: name -> phone -> grade (section > grade) -> subject -> city -> area
            #      -> tutor gender -> days/week -> hours -> notes (skippable) -> budget ----
            C_NAME: [MessageHandler(TEXT_ONLY, text_step(C_NAME, "name", C_PHONE, "ask_phone",
                                                         markup_fn=phone_keyboard))],
            C_PHONE: [MessageHandler(CONTACT_OR_TEXT, phone_step(C_PHONE, C_GRADE, "ask_student_grade", grade_keyboard))],
            C_GRADE: [CallbackQueryHandler(grade_section, pattern=r"^gsec:(KG|PRI|HS|TYPE)$"),
                      CallbackQueryHandler(grade_pick, pattern=r"^grade:UNI$"),
                      MessageHandler(TEXT_ONLY, use_buttons(C_GRADE))],
            C_GRADE_PICK: [CallbackQueryHandler(grade_pick, pattern=r"^grade:[A-Z0-9]+$"),
                           CallbackQueryHandler(grade_back, pattern=r"^gsec:BACK$"),
                           MessageHandler(TEXT_ONLY, use_buttons(C_GRADE_PICK))],
            C_GRADE_TEXT: [MessageHandler(TEXT_ONLY, text_step(C_GRADE_TEXT, "grade", C_SUBJECT, "ask_c_subject"))],
            C_SUBJECT: [MessageHandler(TEXT_ONLY, text_step(C_SUBJECT, "subject", C_CITY, "ask_city",
                                                            markup_fn=city_keyboard))],
            C_CITY: [CallbackQueryHandler(choice_step("city", C_HOOD, "ask_hood"),
                                          pattern=r"^city:(Adama|Bishoftu)$"),
                     MessageHandler(TEXT_ONLY, use_buttons(C_CITY))],
            C_HOOD: [MessageHandler(TEXT_ONLY, text_step(C_HOOD, "hood", C_GENDER, "ask_gender_pref",
                                                         markup_fn=gpref_keyboard))],
            C_GENDER: [CallbackQueryHandler(choice_step("gpref", C_DAYS, "ask_days", days_keyboard),
                                            pattern=r"^gpref:(Male|Female|Any)$"),
                       MessageHandler(TEXT_ONLY, use_buttons(C_GENDER))],
            C_DAYS: [CallbackQueryHandler(days_choice, pattern=r"^days:([1-7]|TYPE)$"),
                     MessageHandler(TEXT_ONLY, use_buttons(C_DAYS))],
            C_DAYS_TEXT: [MessageHandler(TEXT_ONLY, text_step(C_DAYS_TEXT, "days", C_HOURS, "ask_hours",
                                                              maxlen=40, markup_fn=hours_keyboard))],
            C_HOURS: [CallbackQueryHandler(hours_choice, pattern=r"^hrs:(1|1\.5|2|3|TYPE)$"),
                      MessageHandler(TEXT_ONLY, use_buttons(C_HOURS))],
            C_HOURS_TEXT: [MessageHandler(TEXT_ONLY, text_step(C_HOURS_TEXT, "hours", C_REQ, "ask_req",
                                                               maxlen=40, markup_fn=skip_keyboard))],
            C_REQ: [CallbackQueryHandler(req_skip, pattern=r"^skip:req$"),
                    MessageHandler(TEXT_ONLY, text_step(C_REQ, "req", C_BUDGET, "ask_budget", maxlen=600))],
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
    app.add_handler(CallbackQueryHandler(application_decision, pattern=r"^(aS|aR):"))
    app.add_handler(CallbackQueryHandler(apply_confirm, pattern=r"^apply:"))
    app.add_handler(CallbackQueryHandler(jobs_callback, pattern=r"^jl:"))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("myapplications", cmd_myapplications))
    app.add_handler(CommandHandler("applications", cmd_applications))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("tutor", cmd_tutor))
    app.add_handler(CommandHandler("client", cmd_client))
    app.add_handler(CallbackQueryHandler(record_callback, pattern=r"^(tv|cv):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & PRIVATE, text_router))
    app.add_error_handler(on_error)
    return app


async def register_commands(bot):
    """Fills the Telegram 'Menu' button with commands (including /cancel)."""
    en = [BotCommand("start", "Start / restart"),
          BotCommand("cancel", "Cancel what you're doing"),
          BotCommand("myapplications", "My job applications"),
          BotCommand("help", "Help and support")]
    am = [BotCommand("start", "ጀምር / እንደገና ጀምር"),
          BotCommand("cancel", "የጀመሩትን ሰርዝ"),
          BotCommand("myapplications", "የእኔ ማመልከቻዎች"),
          BotCommand("help", "እርዳታ")]
    admin = en + [BotCommand("applications", "Review tutor applications"),
                  BotCommand("jobs", "Open jobs and applicants"),
                  BotCommand("tutor", "Find a tutor: full record + ID (phone or name)"),
                  BotCommand("client", "Find a client request (phone, name, job code)")]
    try:
        await bot.set_my_commands(en)
        await bot.set_my_commands(am, language_code="am")
        await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID))
        logger.info("Bot command menu registered.")
    except Exception:
        logger.exception("Could not register the command menu (non-fatal).")


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
    await register_commands(app.bot)

    status = ("⚠️ <b>Horizon bot started with PROBLEMS</b>\n" + "\n".join(f"• {esc(p)}" for p in problems)
              if problems else "✅ <b>Horizon bot online.</b> Sheets check passed.")
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
        get_credentials()
    except ConfigError as e:
        logger.critical("GOOGLE CREDENTIALS ERROR: %s", e)
        raise SystemExit(1)
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive_pinger, daemon=True).start()
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
