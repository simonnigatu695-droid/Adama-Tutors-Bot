import os
import logging
from datetime import datetime
import random
import json
from flask import Flask
from threading import Thread
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask Keep-Alive Server for Render & Better Stack
app = Flask('')

@app.route('/')
def home():
    return "Horizon Tutors Hub Bot is alive and running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# Google Sheets Setup supporting multiple environment variable keys & fallback paths
def get_gspread_client():
    try:
        # Check both possible environment variable key names
        env_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON") or os.environ.get("credentials.json")
        if env_creds:
            logger.info("✅ Loading credentials from environment variable")
            creds_dict = json.loads(env_creds)
            gc = gspread.service_account_from_dict(creds_dict)
            sheet = gc.open("Adama Tutors Database")
            logger.info("✅ Successfully opened Google Sheet: Adama Tutors Database")
            return sheet

        possible_paths = [
            "/etc/secrets/credentials.json",
            "credentials.json",
            os.path.join(os.getcwd(), "credentials.json")
        ]
        cred_path = None
        for path in possible_paths:
            if os.path.exists(path):
                cred_path = path
                break
        
        if not cred_path:
            logger.error("❌ GSPREAD ERROR: credentials.json could not be found anywhere!")
            return None

        logger.info(f"✅ Found credentials at: {cred_path}")
        gc = gspread.service_account(filename=cred_path)
        sheet = gc.open("Adama Tutors Database")
        logger.info("✅ Successfully opened Google Sheet: Adama Tutors Database")
        return sheet
    except Exception as e:
        logger.error(f"❌ GSPREAD CONNECTION EXCEPTION: {str(e)}")
        return None

def db_add_tutor(data):
    logger.info("Attempting to write tutor data to Google Sheets...")
    sheet = get_gspread_client()
    if sheet:
        try:
            ws = sheet.worksheet("Tutors")
            ws.append_row(data)
            logger.info("🎉 SUCCESS: Tutor row successfully appended to 'Tutors' tab!")
        except Exception as e:
            logger.error(f"❌ FAILED to append to 'Tutors' tab: {str(e)}")
    else:
        logger.error("❌ FAILED to write tutor: Spreadsheet object is None.")

def db_add_client_request(data):
    logger.info("Attempting to write client request data to Google Sheets...")
    sheet = get_gspread_client()
    if sheet:
        try:
            ws = sheet.worksheet("Client_Requests")
            ws.append_row(data)
            logger.info("🎉 SUCCESS: Request row successfully appended to 'Client_Requests' tab!")
        except Exception as e:
            logger.error(f"❌ FAILED to append to 'Client_Requests' tab: {str(e)}")
    else:
        logger.error("❌ FAILED to write request: Spreadsheet object is None.")

# Conversation States
(
    LANG, MENU, 
    TUTOR_NAME, TUTOR_PHONE, TUTOR_SUBJECT, TUTOR_CITY, TUTOR_ID,
    REQ_NAME, REQ_PHONE, REQ_SUBJECT, REQ_GRADE, REQ_CITY
) = range(12)

# --- Core Handlers & Commands Setup ---

async def post_init(application: Application):
    commands = [
        BotCommand("start", "Start or restart the bot / ቦቱን ያስጀምሩ"),
        BotCommand("cancel", "Cancel current operation / ሂደቱን ይሰርዙ"),
    ]
    await application.bot.set_my_commands(commands)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("English 🇬🇧", callback_data="lang_en"),
            InlineKeyboardButton("አማርኛ 🇪🇹", callback_data="lang_am"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        "🌟 **Welcome to Horizon Tutors Hub** 🌟\n"
        "Connecting qualified tutors with students in Adama and Bishoftu.\n\n"
        "Please choose your preferred language:"
    )
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    return LANG

async def language_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    lang = "en" if query.data == "lang_en" else "am"
    context.user_data["lang"] = lang

    if lang == "en":
        text = "Welcome! What would you like to do today?"
        kb = [
            [InlineKeyboardButton("👨‍🏫 Register as a Tutor", callback_data="reg_tutor")],
            [InlineKeyboardButton("📚 Request a Tutor", callback_data="req_tutor")],
            [InlineKeyboardButton("ℹ️ About Us", callback_data="about")],
            [InlineKeyboardButton("📞 Contact Support", callback_data="support")],
            [InlineKeyboardButton("❌ Cancel / Exit", callback_data="cancel_action")],
        ]
    else:
        text = "እንኳን ደህና መጡ! ዛሬ ምን ማድረግ ይፈልጋሉ?"
        kb = [
            [InlineKeyboardButton("👨‍🏫 መምህር ሆኖ ለመመዝገብ", callback_data="reg_tutor")],
            [InlineKeyboardButton("📚 መምህር ማግኘት እፈልጋለሁ", callback_data="req_tutor")],
            [InlineKeyboardButton("ℹ️ ስለ እኛ", callback_data="about")],
            [InlineKeyboardButton("📞 የዕርዳታ ማዕከል", callback_data="support")],
            [InlineKeyboardButton("❌ ሰርዝ / ውጣ", callback_data="cancel_action")],
        ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return MENU

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    if query.data == "about":
        text = (
            "ℹ️ **About Horizon Tutors Hub**\n"
            "We are a premier tutoring agency bridging the gap between eager students and "
            "qualified, verified professional tutors across Adama and Bishoftu. "
            "Our mission is to foster academic excellence and reliable educational support."
            if lang == "en" else
            "ℹ️ **ስለ ሆራይዘን የቱተርስ ሃብ**\n"
            "እኛ በአዳማ እና በቢሾፍቱ ከተሞች ጥራት ያላቸውን እና ብቁ መምህራንን ከተማሪዎች ጋር የምናገናኝ "
            "ታማኝ የትምህርት ድርጅት ነን። ዓላማችን አስተማማኝ የትምህርት ድጋፍ መስጠት ነው።"
        )
        kb = [[InlineKeyboardButton("🔙 Back / ተመለስ", callback_data="back_home")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return MENU
        
    elif query.data == "support":
        text = (
            "📞 **Customer Support & Assistance**\n"
            "For urgent inquiries, reach out to our team:\n\n"
            "👤 Telegram: @simon_1919\n"
            "📱 Phone: +251 90 088 2373"
            if lang == "en" else
            "📞 **የዕርዳታ ማዕከል (Customer Support)**\n"
            "ለማንኛውም ጥያቄ ከዚህ በታች ባሉት አድራሻዎች ያግኙን:\n\n"
            "👤 ቴሌግራም: @simon_1919\n"
            "📱 ስልክ ቁጥር: +251 90 088 2373"
        )
        kb = [[InlineKeyboardButton("🔙 Back / ተመለስ", callback_data="back_home")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return MENU
        
    elif query.data == "cancel_action":
        return await cancel_callback(update, context)
        
    elif query.data == "back_home":
        return await start(update, context)
    
    return MENU

# --- Tutor Registration Flow ('Tutors' Tab) ---

async def reg_tutor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    text = "Please enter your full name:" if lang == "en" else "እባክዎ ሙሉ ስምዎን ያስገቡ:"
    kb = [[InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_NAME

async def tutor_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "en")
    context.user_data["tutor_name"] = update.message.text

    text = "Please enter your phone number (e.g., 0911223344):" if lang == "en" else "እባክዎ ስልክ ቁጥርዎን ያስገቡ (ምሳሌ፦ 0911223344):"
    await update.message.reply_text(text)
    return TUTOR_PHONE

async def tutor_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "en")
    context.user_data["tutor_phone"] = update.message.text

    text = "What subjects or grades can you teach?" if lang == "en" else "ምን አይነት ትምህርቶች ወይም የክፍል ደረጃዎች ማስተማር ይችላሉ?"
    await update.message.reply_text(text)
    return TUTOR_SUBJECT

async def tutor_subject_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "en")
    context.user_data["tutor_subject"] = update.message.text

    kb = [
        [InlineKeyboardButton("Adama / አዳማ", callback_data="city_adama")],
        [InlineKeyboardButton("Bishoftu / ቢሾፍቱ", callback_data="city_bishoftu")],
        [InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")],
    ]
    text = "Select your city:" if lang == "en" else "ከተማዎን ይምረጡ:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_CITY

async def tutor_city_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")
    
    city = "Adama" if query.data == "city_adama" else "Bishoftu"
    context.user_data["tutor_city"] = city

    text = (
        "📷 **ID Verification Required**\nPlease upload a clear photo of the **front of your National ID**:"
        if lang == "en" else
        "📷 **መታወቂያ ማረጋገጫ**\nእባክዎ የመታወቂያዎን (የብሔራዊ መታወቂያ) ፊት ለፊት ግልጽ ፎቶ ይላኩ:"
    )
    
    await query.message.edit_text(text, parse_mode="Markdown")
    return TUTOR_ID

async def tutor_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "en")
    
    if not update.message.photo:
        text = "Please upload an image file of your ID." if lang == "en" else "እባክዎ የመታወቂያዎን ፎቶ ምስል ይላኩ።"
        await update.message.reply_text(text)
        return TUTOR_ID

    photo_file = await update.message.photo[-1].get_file()
    id_file_id = photo_file.file_id

    # Append row to 'Tutors' tab
    db_add_tutor([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), # Timestamp
        str(update.effective_user.id),                # Telegram ID
        update.effective_user.username or "",         # Username
        context.user_data.get("tutor_name"),          # Name
        context.user_data.get("tutor_phone"),         # Phone
        "",                                           # Gender
        context.user_data.get("tutor_subject"),       # Subjects
        "",                                           # Grades
        context.user_data.get("tutor_city"),          # Location
        "",                                           # Qualification
        "Pending",                                    # ID Verification Status
        "Active",                                     # Status
        id_file_id                                    # ID File Reference
    ])

    text = (
        "✅ **Registration & ID Submitted Successfully!**\nWe have received your details and ID. We will review and contact you soon."
        if lang == "en" else
        "✅ **ምዝገባዎ እና መታወቂያዎ በተሳካ ሁኔታ ተልኳል!**\nመረጃዎ እና መታወቂያዎ ደርሶናል፤ አጣርተን በቅርቡ እናነጋግርዎታለን።"
    )
    
    kb = [[InlineKeyboardButton("🔙 Main Menu / ዋና ምናሌ", callback_data="back_home")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return MENU


# --- Student Request Flow ('Client_Requests' Tab) ---

async def req_tutor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    text = "Please enter the student's or parent's full name:" if lang == "en" else "እባክዎ የተማሪውን ወይም የወላጅ ስም ያስገቡ:"
    await query.message.edit_text(text)
    return REQ_NAME

async def req_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_name"] = update.message.text
    lang = context.user_data.get("lang", "en")

    text = "Please enter your contact phone number:" if lang == "en" else "እባክዎ ማነጋገሪያ ስልክ ቁጥር ያስገቡ:"
    await update.message.reply_text(text)
    return REQ_PHONE

async def req_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_phone"] = update.message.text
    lang = context.user_data.get("lang", "en")

    text = "What subject(s) do you need a tutor for?" if lang == "en" else "ምን ዓይነት ትምህርት አስተማሪ ይፈልጋሉ?"
    await update.message.reply_text(text)
    return REQ_SUBJECT

async def req_subject_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_subject"] = update.message.text
    lang = context.user_data.get("lang", "en")

    text = "What is the student's grade level?" if lang == "en" else "የተማሪው የክፍል ደረጃ ስንተኛ ነው?"
    await update.message.reply_text(text)
    return REQ_GRADE

async def req_grade_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_grade"] = update.message.text
    lang = context.user_data.get("lang", "en")

    kb = [
        [InlineKeyboardButton("Adama / አዳማ", callback_data="reqcity_adama")],
        [InlineKeyboardButton("Bishoftu / ቢሾፍቱ", callback_data="reqcity_bishoftu")],
    ]
    text = "Select the city for tutoring:" if lang == "en" else "ትምህርቱ የሚሰጥበትን ከተማ ይምረጡ:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return REQ_CITY

async def req_city_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")
    
    city = "Adama" if query.data == "reqcity_adama" else "Bishoftu"
    job_code = f"HRZ-{random.randint(1000, 9999)}"

    # Append row to 'Client_Requests' tab
    db_add_client_request([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), # Timestamp
        job_code,                                    # Job Code
        str(query.from_user.id),                     # Telegram ID
        query.from_user.username or "",              # Username
        context.user_data.get("req_name"),           # Client Name
        context.user_data.get("req_phone"),          # Phone
        context.user_data.get("req_subject"),        # Subject
        context.user_data.get("req_grade"),          # Grade
        city,                                        # City
        "", "", "", "", "", "", "Pending", "Open", "" # Place, Prefs, Budget, Status, etc.
    ])

    text = (
        f"✅ **Request Submitted Successfully!**\nYour Job Code is `{job_code}`. We will match you with a qualified tutor soon."
        if lang == "en" else
        f"✅ **ጥያቄዎ በተሳካ ሁኔታ ተመዝግቧል!**\nየሥራ ኮድዎ (Job Code): `{job_code}` ነው። በቅርቡ ብቁ መምህር እናገናኝዎታለን።"
    )
    
    kb = [[InlineKeyboardButton("🔙 Main Menu / ዋና ምናሌ", callback_data="back_home")]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return MENU

# --- Cancel Handlers ---

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Operation cancelled. Type /start to begin again."
    if update.message:
        await update.message.reply_text(text)
    return ConversationHandler.END

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "Operation cancelled. Type /start to begin again."
    kb = [[InlineKeyboardButton("🔄 Restart / ጀምር", callback_data="back_home")]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        logger.error("No TELEGRAM_TOKEN found in environment variables!")
        return

    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

    application = Application.builder().token(token).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            LANG: [CallbackQueryHandler(language_handler, pattern="^lang_")],
            MENU: [
                CallbackQueryHandler(menu_callback, pattern="^(about|support|back_home|cancel_action)$"),
                CallbackQueryHandler(reg_tutor_start, pattern="^reg_tutor$"),
                CallbackQueryHandler(req_tutor_start, pattern="^req_tutor$"),
            ],
            TUTOR_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_name_received),
                CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")
            ],
            TUTOR_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_phone_received)],
            TUTOR_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_subject_received)],
            TUTOR_CITY: [
                CallbackQueryHandler(tutor_city_received, pattern="^city_"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")
            ],
            TUTOR_ID: [MessageHandler(filters.PHOTO & ~filters.COMMAND, tutor_id_received)],
            
            REQ_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_name_received)],
            REQ_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_phone_received)],
            REQ_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_subject_received)],
            REQ_GRADE: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_grade_received)],
            REQ_CITY: [CallbackQueryHandler(req_city_received, pattern="^reqcity_")],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_message=False
    )

    application.add_handler(conv_handler)

    logger.info("Starting bot polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
