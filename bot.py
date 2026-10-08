import os
import json
import logging
import datetime
from threading import Thread
from flask import Flask

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

# ---------------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
JOB_CHANNEL_ID = os.getenv("JOB_CHANNEL_ID", "@HorizonTutorsHub")

raw_admins = os.getenv("ADMIN_CHAT_ID", "")
ADMIN_CHAT_IDS = [int(aid.strip()) for aid in raw_admins.split(",") if aid.strip().isdigit()]

SPREADSHEET_NAME = "Adama Tutors Database"

# ---------------------------------------------------------------------------
# GOOGLE SHEETS SETUP & AUTO-RESIZE
# ---------------------------------------------------------------------------
def format_and_fit_columns(doc):
    """Auto-resizes and expands column widths across all sheets."""
    column_widths = {
        "Clients": [180, 180, 150, 140, 220, 160, 120, 130, 150, 140],
        "Tutors": [180, 180, 150, 200, 240, 160, 120, 180, 150],
        "Finances": [180, 180, 150, 140, 160, 200],
        "Feedback": [180, 140, 180, 100, 300],
        "Jobs": [120, 320, 140, 180]
    }

    requests = []
    for sheet_title, widths in column_widths.items():
        try:
            ws = doc.worksheet(sheet_title)
            requests.append({
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": len(widths)
                    }
                }
            })
            for col_idx, width in enumerate(widths):
                requests.append({
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": col_idx,
                            "endIndex": col_idx + 1
                        },
                        "properties": {"pixelSize": width},
                        "fields": "pixelSize"
                    }
                })
        except Exception as e:
            logger.error(f"Error preparing column widths for {sheet_title}: {e}")

    if requests:
        try:
            doc.batch_update({"requests": requests})
            logger.info("Successfully formatted Google Sheet column widths.")
        except Exception as e:
            logger.error(f"Failed to batch update sheet formatting: {e}")

def get_gsheet():
    """Authenticates and returns the Google Spreadsheet instance."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    google_json_env = os.getenv("GOOGLE_JSON")
    
    try:
        if google_json_env:
            creds_dict = json.loads(google_json_env)
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
        elif os.path.exists("service_account.json"):
            creds = Credentials.from_service_account_file("service_account.json", scopes=scope)
        else:
            logger.error("No valid Google credentials found.")
            return None
        
        client = gspread.authorize(creds)
        return client.open(SPREADSHEET_NAME)
    except Exception as e:
        logger.error(f"GSpread Auth Error: {e}")
        return None

# ---------------------------------------------------------------------------
# FLASK KEEP-ALIVE SERVER
# ---------------------------------------------------------------------------
app = Flask('')

@app.route('/')
def home():
    return "Horizon Tutors Hub is online."

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

Thread(target=run_flask, daemon=True).start()

# ---------------------------------------------------------------------------
# STATES
# ---------------------------------------------------------------------------
(
    T_NAME, T_PHONE, T_DEGREE, T_SUBJECTS, T_CITY, T_LOCATION, T_ID,
    C_NAME, C_PHONE, C_CLASS, C_SUBJECTS, C_CITY, C_LOCATION, C_BUDGET, C_ID
) = range(15)

def get_main_keyboard():
    return ReplyKeyboardMarkup([
        ["👨‍🏫 Register as a Tutor", "📚 Request a Tutor"],
        ["ℹ️ About Us", "📞 Contact Support"]
    ], resize_keyboard=True)

# ---------------------------------------------------------------------------
# COMMAND HANDLERS
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🌟 *Welcome to Horizon Tutors Hub* 🌟\n\n"
        "Connecting qualified tutors with students in Adama & Bishoftu.\n"
        "Please select an option below to proceed:"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_keyboard())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Action cancelled.", reply_markup=get_main_keyboard())
    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# TUTOR REGISTRATION FLOW
# ---------------------------------------------------------------------------
async def tutor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("👨‍🏫 *Tutor Registration*\n\nEnter your **Full Name**:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return T_NAME

async def t_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_name'] = update.message.text
    await update.message.reply_text("📞 Enter your **Phone Number**:")
    return T_PHONE

async def t_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_phone'] = update.message.text
    await update.message.reply_text("🎓 Enter your **Educational Background / Degree**:")
    return T_DEGREE

async def t_degree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_degree'] = update.message.text
    await update.message.reply_text("📚 What **Subjects** do you teach?")
    return T_SUBJECTS

async def t_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_subjects'] = update.message.text
    await update.message.reply_text("🏙️ Select your city:", reply_markup=ReplyKeyboardMarkup([["Adama", "Bishoftu"]], resize_keyboard=True, one_time_keyboard=True))
    return T_CITY

async def t_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_city'] = update.message.text
    await update.message.reply_text("📍 Enter your specific neighborhood/location:", reply_markup=ReplyKeyboardRemove())
    return T_LOCATION

async def t_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_location'] = update.message.text
    await update.message.reply_text("🪪 Upload a clear photo of your **National or Kebele ID**:")
    return T_ID

async def t_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d = context.user_data
    
    doc = get_gsheet()
    if doc:
        try:
            sheet = doc.worksheet("Tutors")
            sheet.append_row([now, d['t_name'], d['t_phone'], d['t_degree'], d['t_subjects'], d['t_location'], d['t_city'], "ID Uploaded", "Pending Approval"])
            logger.info(f"Logged Tutor {d['t_name']} into Sheets.")
        except Exception as e:
            logger.error(f"Failed to log Tutor: {e}")

    admin_msg = (
        f"🆕 *NEW TUTOR APPLICATION*\n"
        f"👤 Name: {d['t_name']}\n"
        f"📞 Phone: {d['t_phone']}\n"
        f"🎓 Degree: {d['t_degree']}\n"
        f"📚 Subjects: {d['t_subjects']}\n"
        f"📍 Location: {d['t_location']} ({d['t_city']})\n"
        f"🪪 ID Status: Provided"
    )
    btns = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve Tutor", callback_data=f"app_tutor_{update.effective_user.id}")]])
    
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_msg, parse_mode="Markdown", reply_markup=btns)
        except Exception as e:
            logger.error(f"Failed sending to admin {admin_id}: {e}")

    await update.message.reply_text("✅ Application submitted successfully! Our team will review your application.", reply_markup=get_main_keyboard())
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# CLIENT REQUEST FLOW
# ---------------------------------------------------------------------------
async def client_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("📚 *Request a Tutor*\n\nEnter your **Full Name** (Parent/Student):", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return C_NAME

async def c_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_name'] = update.message.text
    await update.message.reply_text("📞 Enter your active **Phone Number**:")
    return C_PHONE

async def c_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_phone'] = update.message.text
    await update.message.reply_text("👨‍🎓 Enter the student's **Grade Level / Class**:")
    return C_CLASS

async def c_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_class'] = update.message.text
    await update.message.reply_text("📚 What **Subject(s)** are needed?")
    return C_SUBJECTS

async def c_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_subjects'] = update.message.text
    await update.message.reply_text("🏙️ Select your city:", reply_markup=ReplyKeyboardMarkup([["Adama", "Bishoftu"]], resize_keyboard=True, one_time_keyboard=True))
    return C_CITY

async def c_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_city'] = update.message.text
    await update.message.reply_text("📍 Enter your specific neighborhood/location:", reply_markup=ReplyKeyboardRemove())
    return C_LOCATION

async def c_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_location'] = update.message.text
    await update.message.reply_text("💰 Enter your estimated **Monthly Budget (ETB)**:")
    return C_BUDGET

async def c_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_budget'] = update.message.text
    await update.message.reply_text("🪪 Upload a photo of your **National or Kebele ID** to verify your request:")
    return C_ID

async def c_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d = context.user_data
    
    doc = get_gsheet()
    if doc:
        try:
            sheet = doc.worksheet("Clients")
            sheet.append_row([
                now, d['c_name'], d['c_phone'], d['c_class'],
                d['c_subjects'], d['c_location'], d['c_city'],
                d['c_budget'], "ID Verified", "Pending Match"
            ])
            logger.info(f"Logged Client {d['c_name']} into Sheets.")
        except Exception as e:
            logger.error(f"Failed to log Client: {e}")

    admin_msg = (
        f"🆕 *NEW CLIENT REQUEST*\n"
        f"👤 Name: {d['c_name']}\n"
        f"📞 Phone: {d['c_phone']}\n"
        f"🎓 Class: {d['c_class']}\n"
        f"📚 Subjects: {d['c_subjects']}\n"
        f"📍 Location: {d['c_location']} ({d['c_city']})\n"
        f"💰 Budget: {d['c_budget']} ETB\n"
        f"🪪 ID Status: Provided"
    )
    btns = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Post Decorated Job", callback_data=f"postjob_{d['c_class'][:10]}_{d['c_city']}")
    ]])

    for admin_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_msg, parse_mode="Markdown", reply_markup=btns)
        except Exception as e:
            logger.error(f"Failed sending to admin {admin_id}: {e}")

    await update.message.reply_text("✅ Request received & verified! Our admins will contact you shortly.", reply_markup=get_main_keyboard())
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# ADMIN CALLBACK HANDLER & DECORATED BROADCAST
# ---------------------------------------------------------------------------
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("app_tutor_"):
        await query.edit_message_text(f"{query.message.text}\n\n✅ *APPROVED BY ADMIN*")

    elif data.startswith("postjob_"):
        lines = query.message.text.split("\n")
        c_class = next((l.split(":")[1].strip() for l in lines if "Class:" in l), "N/A")
        c_subj = next((l.split(":")[1].strip() for l in lines if "Subjects:" in l), "N/A")
        c_loc = next((l.split(":")[1].strip() for l in lines if "Location:" in l), "N/A")
        c_budg = next((l.split(":")[1].strip() for l in lines if "Budget:" in l), "N/A")

        decorated_post = (
            "🌟 𝐇𝐎𝐑𝐈𝐙𝐎𝐍 𝐓𝐔𝐓𝐎𝐑𝐒 𝐇𝐔𝐁 | 𝐍𝐄𝐖 𝐎𝐏𝐏𝐎𝐑𝐓𝐔𝐍𝐈𝐓𝐘 🌟\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎓 *Grade/Class:* {c_class}\n"
            f"📚 *Subject(s):* {c_subj}\n"
            f"📍 *Location:* {c_loc}\n"
            f"💰 *Budget:* {c_budg}\n\n"
            "⚖️ *Terms:* Standard 1-week trial applies. Placement commission is processed after the first month.\n\n"
            "🚀 *How to Apply:*\n"
            "Send your verified Horizon Tutor ID and apply directly via @HorizonTutorsHubBot!\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━"
        )

        try:
            await context.bot.send_message(chat_id=JOB_CHANNEL_ID, text=decorated_post, parse_mode="Markdown")
            await query.edit_message_text(f"{query.message.text}\n\n🚀 *DECORATED JOB POSTED TO CHANNEL*")
        except Exception as e:
            await query.message.reply_text(f"⚠️ Failed to post job to channel: {e}")

# ---------------------------------------------------------------------------
# MAIN APPLICATION
# ---------------------------------------------------------------------------
def main():
    doc = get_gsheet()
    if doc:
        format_and_fit_columns(doc)

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    tutor_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^👨‍🏫 Register as a Tutor$"), tutor_start)],
        states={
            T_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_name)],
            T_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_phone)],
            T_DEGREE: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_degree)],
            T_SUBJECTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_subjects)],
            T_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_city)],
            T_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_location)],
            T_ID: [MessageHandler(filters.PHOTO | filters.Document.ALL, t_finish)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    client_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📚 Request a Tutor$"), client_start)],
        states={
            C_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_name)],
            C_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_phone)],
            C_CLASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_class)],
            C_SUBJECTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_subjects)],
            C_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_city)],
            C_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_location)],
            C_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_budget)],
            C_ID: [MessageHandler(filters.PHOTO | filters.Document.ALL, c_finish)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(tutor_handler)
    application.add_handler(client_handler)
    application.add_handler(CallbackQueryHandler(admin_callback))

    application.run_polling()

if __name__ == "__main__":
    main()