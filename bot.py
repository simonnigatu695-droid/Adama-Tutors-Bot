import os
import logging
from datetime import datetime
import random
from flask import Flask
from threading import Thread
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# Flask App for Render Keep-Alive
app = Flask('')

@app.route('/')
def home():
    return "Horizon Tutors Hub Bot is alive and running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# Google Sheets Setup
def get_gspread_client():
    try:
        cred_path = "/etc/secrets/credentials.json" if os.path.exists("/etc/secrets/credentials.json") else "credentials.json"
        gc = gspread.service_account(filename=cred_path)
        return gc.open("Adama Tutors Database")
    except Exception as e:
        logger.error(f"Google Sheets connection error: {e}")
        return None

# Conversation States
(
    LANG, MENU, 
    TUTOR_NAME, TUTOR_PHONE, TUTOR_SUBJECT, TUTOR_CITY,
    REQ_NAME, REQ_PHONE, REQ_SUBJECT, REQ_GRADE, REQ_CITY
) = range(11)

# --- Core Handlers ---

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
        "Connecting qualified tutors with students.\n\n"
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
        ]
    else:
        text = "እንኳን ደህና መጡ! ዛሬ ምን ማድረግ ይፈልጋሉ?"
        kb = [
            [InlineKeyboardButton("👨‍🏫 መምህር ሆኖ ለመመዝገብ", callback_data="reg_tutor")],
            [InlineKeyboardButton("📚 መምህር ማግኘት እፈልጋለሁ", callback_data="req_tutor")],
            [InlineKeyboardButton("ℹ️ ስለ እኛ", callback_data="about")],
            [InlineKeyboardButton("📞 የዕርዳታ ማዕከል", callback_data="support")],
        ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return MENU

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    if query.data == "about":
        text = "Horizon Tutors Hub connects top-tier tutors with eager students in Adama and Bishoftu." if lang == "en" else "ሆራይዘን የቱተርስ ሃብ በአዳማ እና በቢሾፍቱ ጥራት ያላቸው መምህራንን ከተማሪዎች ጋር ያገናኛል።"
        kb = [[InlineKeyboardButton("🔙 Back / ተመለስ", callback_data="back_home")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
        return MENU
    elif query.data == "support":
        text = "Contact support at @HorizonSupport" if lang == "en" else "ለእርዳታ በ @HorizonSupport ያግኙን።"
        kb = [[InlineKeyboardButton("🔙 Back / ተመለስ", callback_data="back_home")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
        return MENU
    elif query.data == "back_home":
        return await start(update, context)
    
    return MENU

# --- Tutor Registration Flow (Writes to 'Tutors' tab) ---

async def reg_tutor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    text = "Please enter your full name:" if lang == "en" else "እባክዎ ሙሉ ስምዎን ያስገቡ:"
    await query.message.edit_text(text)
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
    ]
    text = "Select your city:" if lang == "en" else "ከተማዎን ይምረጡ:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_CITY

async def tutor_city_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")
    
    city = "Adama" if query.data == "city_adama" else "Bishoftu"
    
    sheet = get_gspread_client()
    if sheet:
        try:
            worksheet = sheet.worksheet("Tutors")
            worksheet.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(query.from_user.id),
                query.from_user.username or "",
                context.user_data.get("tutor_name"),
                context.user_data.get("tutor_phone"),
                context.user_data.get("tutor_subject"),
                city
            ])
        except Exception as e:
            logger.error(f"Failed to write to Tutors sheet: {e}")

    text = (
        "✅ **Registration Successful!**\nWe have received your details and will contact you soon."
        if lang == "en" else
        "✅ **ምዝገባዎ ተሳክቷል!**\nመረጃዎ ደርሶናል፤ በቅርቡ እናነጋግርዎታለን።"
    )
    
    kb = [[InlineKeyboardButton("🔙 Main Menu / ዋና ምናሌ", callback_data="back_home")]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return MENU


# --- Student Request Flow (Writes to 'Client_Requests' tab) ---

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

    # Save to Client_Requests worksheet matching your exact columns:
    # Timestamp, Job Code, Telegram ID, Username, Client Name, Phone, Subject, Grade, City...
    sheet = get_gspread_client()
    if sheet:
        try:
            worksheet = sheet.worksheet("Client_Requests")
            worksheet.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), # Timestamp
                job_code,                                    # Job Code
                str(query.from_user.id),                     # Telegram ID
                query.from_user.username or "",              # Username
                context.user_data.get("req_name"),           # Client Name
                context.user_data.get("req_phone"),          # Phone
                context.user_data.get("req_subject"),        # Subject
                context.user_data.get("req_grade"),          # Grade
                city,                                        # City
                "", "", "", "", "", "", ""                   # Empty placeholders for remaining columns (Place, Budget, etc.)
            ])
        except Exception as e:
            logger.error(f"Failed to write to Client_Requests sheet: {e}")

    text = (
        f"✅ **Request Submitted Successfully!**\nYour Job Code is `{job_code}`. We will match you with a qualified tutor soon."
        if lang == "en" else
        f"✅ **ጥያቄዎ በተሳካ ሁኔታ ተመዝግቧል!**\nየሥራ ኮድዎ (Job Code): `{job_code}` ነው። በቅርቡ ብቁ መምህር እናገናኝዎታለን።"
    )
    
    kb = [[InlineKeyboardButton("🔙 Main Menu / ዋና ምናሌ", callback_data="back_home")]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Operation cancelled. Type /start to begin again."
    if update.message:
        await update.message.reply_text(text)
    return ConversationHandler.END


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        logger.error("No TELEGRAM_TOKEN found in environment variables!")
        return

    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

    application = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            LANG: [CallbackQueryHandler(language_handler, pattern="^lang_")],
            MENU: [
                CallbackQueryHandler(menu_callback, pattern="^(about|support|back_home)$"),
                CallbackQueryHandler(reg_tutor_start, pattern="^reg_tutor$"),
                CallbackQueryHandler(req_tutor_start, pattern="^req_tutor$"),
            ],
            TUTOR_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_name_received)],
            TUTOR_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_phone_received)],
            TUTOR_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_subject_received)],
            TUTOR_CITY: [CallbackQueryHandler(tutor_city_received, pattern="^city_")],
            
            REQ_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_name_received)],
            REQ_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_phone_received)],
            REQ_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_subject_received)],
            REQ_GRADE: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_grade_received)],
            REQ_CITY: [CallbackQueryHandler(req_city_received, pattern="^reqcity_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    logger.info("Starting bot polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
