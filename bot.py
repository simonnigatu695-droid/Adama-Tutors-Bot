import os
import logging
import threading
from datetime import datetime
from flask import Flask
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from text import TEXTS

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# 0. FLASK HEALTH CHECK SERVER (For Render)
# ==========================================
app_flask = Flask(__name__)

@app_flask.route("/")
def health_check():
    return "Horizon Tutors Hub Bot is active and running 24/7!", 200

def run_flask_server():
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Health check server listening on port {port}")
    app_flask.run(host="0.0.0.0", port=port, use_reloader=False)

# ==========================================
# 1. GOOGLE SHEETS SETUP
# ==========================================
def get_google_sheet(sheet_name="Adama Tutors Database"):
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ]
        if os.path.exists("credentials.json"):
            creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
            client = gspread.authorize(creds)
            return client.open(sheet_name)
    except Exception as e:
        logger.error(f"GSpread Auth Error: {e}")
    return None

def get_next_job_code():
    try:
        sheet = get_google_sheet()
        if sheet:
            ws = sheet.worksheet("Settings")
            records = ws.get_all_records()
            current_counter = 0
            row_index = 2
            for idx, r in enumerate(records, start=2):
                if str(r.get("Key")).strip() == "job_counter":
                    current_counter = int(r.get("Value", 0))
                    row_index = idx
                    break
            new_counter = current_counter + 1
            ws.update_cell(row_index, 2, new_counter)
            return f"JOB-{new_counter:04d}"
    except Exception as e:
        logger.error(f"Error fetching/updating job code: {e}")
    return f"JOB-{int(datetime.now().timestamp())}"

# ==========================================
# 2. CONVERSATION STATES
# ==========================================
(
    CHOOSE_LANG,
    MAIN_MENU,
    CLIENT_NAME,
    CLIENT_PHONE,
    CLIENT_SUBJECT,
    CLIENT_GRADE,
    CLIENT_CITY,
    CLIENT_PLACE,
    CLIENT_GENDER_PREF,
    CLIENT_DURATION,
    CLIENT_BUDGET,
    TUTOR_NAME,
    TUTOR_PHONE,
    TUTOR_GENDER,
    TUTOR_SUBJECTS,
    TUTOR_GRADES,
    TUTOR_LOCATION,
    TUTOR_QUALIFICATION,
    TUTOR_ID_VERIFY
) = range(19)

# ==========================================
# 3. HANDLERS
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("English 🇬🇧", callback_data="lang_en"),
            InlineKeyboardButton("አማርኛ 🇪🇹", callback_data="lang_am"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = (
        "🌟 *Welcome to Horizon Tutors Hub* (Adama & Bishoftu)! 🌟\n"
        "Please select your preferred language:\n\n"
        "እንኳን ወደ ሆራይዝን የቱተር ማዕከል (አዳማ እና ቢሾፍቱ) በደህና መጡ!\n"
        "እባክዎ የሚፈልጉትን ቋንቋ ይምረጡ:"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")
    return CHOOSE_LANG

async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = "en" if query.data == "lang_en" else "am"
    context.user_data["lang"] = lang
    t = TEXTS[lang]

    reply_keyboard = [
        [KeyboardButton(t["btn_request"]), KeyboardButton(t["btn_register"])],
        [KeyboardButton(t["btn_about"]), KeyboardButton(t["btn_support"])]
    ]
    reply_markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)
    await query.message.delete()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=t["menu_prompt"],
        reply_markup=reply_markup
    )
    return MAIN_MENU

async def menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    lang = context.user_data.get("lang", "en")
    t = TEXTS[lang]

    if text in ["📚 Request a Tutor", "📚 አስተማሪ እፈልጋለሁ"]:
        await update.message.reply_text(t["cl_name"])
        return CLIENT_NAME
    elif text in ["🧑‍🏫 Register as a Tutor", "🧑‍🏫 እንደ አስተማሪ ይመዝገቡ"]:
        await update.message.reply_text(t["tu_name"])
        return TUTOR_NAME
    elif text in ["ℹ️ About Us", "ℹ️ ስለ እኛ"]:
        await update.message.reply_text(t["about_text"], parse_mode="Markdown")
        return MAIN_MENU
    elif text in ["📞 Contact Support", "📞 እርዳታ/ድጋፍ"]:
        await update.message.reply_text(t["support_text"])
        return MAIN_MENU
    else:
        await update.message.reply_text(t["menu_prompt"])
        return MAIN_MENU

# --- CLIENT FLOW ---
async def client_name_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_name"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["cl_phone"])
    return CLIENT_PHONE

async def client_phone_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_phone"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["cl_subject"])
    return CLIENT_SUBJECT

async def client_subject_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_subject"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["cl_grade"])
    return CLIENT_GRADE

async def client_grade_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_grade"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    
    reply_keyboard = [[KeyboardButton(t["city_adama"]), KeyboardButton(t["city_bishoftu")]]]
    reply_markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(t["cl_city"], reply_markup=reply_markup)
    return CLIENT_CITY

async def client_city_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_city"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["cl_place"])
    return CLIENT_PLACE

async def client_place_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_place"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    
    reply_keyboard = [[KeyboardButton(t["gender_any"]), KeyboardButton(t["gender_male"]), KeyboardButton(t["gender_female"])]]
    reply_markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(t["cl_gender"], reply_markup=reply_markup)
    return CLIENT_GENDER_PREF

async def client_gender_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_gender_pref"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["cl_duration"])
    return CLIENT_DURATION

async def client_duration_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_duration"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["cl_budget"])
    return CLIENT_BUDGET

async def client_budget_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    budget_raw = update.message.text
    context.user_data["client_budget"] = budget_raw
    lang = context.user_data.get("lang", "en")
    t = TEXTS[lang]
    
    job_code = get_next_job_code()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user = update.effective_user
    
    try:
        sheet = get_google_sheet()
        if sheet:
            ws = sheet.worksheet("Client_Requests")
            ws.append_row([
                timestamp,
                job_code,
                str(user.id),
                f"@{user.username}" if user.username else "",
                context.user_data.get("client_name"),
                context.user_data.get("client_phone"),
                context.user_data.get("client_subject"),
                context.user_data.get("client_grade"),
                context.user_data.get("client_city"),
                context.user_data.get("client_place"),
                context.user_data.get("client_gender_pref"),
                context.user_data.get("client_duration"),
                budget_raw,
                "Pending",
                "Open",
                ""
            ])
    except Exception as e:
        logger.error(f"Error saving Client Request: {e}")

    await update.message.reply_text(t["cl_success"].format(job_code=job_code), parse_mode="Markdown")
    return ConversationHandler.END

# --- TUTOR FLOW ---
async def tutor_name_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_name"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["tu_phone"])
    return TUTOR_PHONE

async def tutor_phone_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_phone"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    
    reply_keyboard = [[KeyboardButton(t["gender_male"]), KeyboardButton(t["gender_female"])]]
    reply_markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(t["tu_gender"], reply_markup=reply_markup)
    return TUTOR_GENDER

async def tutor_gender_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_gender"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["tu_subjects"])
    return TUTOR_SUBJECTS

async def tutor_subjects_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_subjects"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["tu_grades"])
    return TUTOR_GRADES

async def tutor_grades_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_grades"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    
    reply_keyboard = [[KeyboardButton("Adama"), KeyboardButton("Bishoftu")], [KeyboardButton("Both / ሁለቱም")]]
    reply_markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(t["tu_location"], reply_markup=reply_markup)
    return TUTOR_LOCATION

async def tutor_location_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_location"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["tu_qualification"])
    return TUTOR_QUALIFICATION

async def tutor_qualification_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_qualification"] = update.message.text
    t = TEXTS[context.user_data.get("lang", "en")]
    await update.message.reply_text(t["tu_id"])
    return TUTOR_ID_VERIFY

async def tutor_id_verify_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    id_status = update.message.text
    lang = context.user_data.get("lang", "en")
    t = TEXTS[lang]
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user = update.effective_user

    try:
        sheet = get_google_sheet()
        if sheet:
            ws = sheet.worksheet("Tutors")
            ws.append_row([
                timestamp,
                str(user.id),
                f"@{user.username}" if user.username else "",
                context.user_data.get("tutor_name"),
                context.user_data.get("tutor_phone"),
                context.user_data.get("tutor_gender"),
                context.user_data.get("tutor_subjects"),
                context.user_data.get("tutor_grades"),
                context.user_data.get("tutor_location"),
                context.user_data.get("tutor_qualification"),
                id_status,
                "Pending"
            ])
    except Exception as e:
        logger.error(f"Error saving Tutor profile: {e}")

    await update.message.reply_text(t["tu_success"])
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled. Send /start to begin again.")
    return ConversationHandler.END

def main():
    server_thread = threading.Thread(target=run_flask_server, daemon=True)
    server_thread.start()

    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        logger.error("No TELEGRAM_TOKEN found!")
        return

    application = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_LANG: [CallbackQueryHandler(language_callback, pattern="^lang_")],
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_choice)],
            
            CLIENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_name_rec)],
            CLIENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_phone_rec)],
            CLIENT_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_subject_rec)],
            CLIENT_GRADE: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_grade_rec)],
            CLIENT_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_city_rec)],
            CLIENT_PLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_place_rec)],
            CLIENT_GENDER_PREF: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_gender_rec)],
            CLIENT_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_duration_rec)],
            CLIENT_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_budget_rec)],
            
            TUTOR_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_name_rec)],
            TUTOR_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_phone_rec)],
            TUTOR_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_gender_rec)],
            TUTOR_SUBJECTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_subjects_rec)],
            TUTOR_GRADES: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_grades_rec)],
            TUTOR_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_location_rec)],
            TUTOR_QUALIFICATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_qualification_rec)],
            TUTOR_ID_VERIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_id_verify_rec)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    logger.info("Bot starting polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
