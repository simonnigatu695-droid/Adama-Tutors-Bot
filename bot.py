import os
import logging
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

# Flask App for Render & Better Stack Keep-Alive
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
        # Check Render secret files path first, fallback to local
        cred_path = "/etc/secrets/credentials.json" if os.path.exists("/etc/secrets/credentials.json") else "credentials.json"
        gc = gspread.service_account(filename=cred_path)
        return gc.open("Adama Tutors Database")
    except Exception as e:
        logger.error(f"Google Sheets connection error: {e}")
        return None

# States
LANG, MENU, TUTOR_NAME, TUTOR_PHONE, TUTOR_SUBJECT = range(5)

# Handlers
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
        text = "Horizon Tutors Hub connects top-tier tutors with eager students in Adama and Bishoftu." if lang == "en" else "ሆራይዘን የቱተርስ ሃብ በአዳማ እና በቢሾፍቱ ጥራት ያላቸው መምህራንን ከ ተማሪዎች ጋር ያገናኛል።"
        kb = [[InlineKeyboardButton("🔙 Back / ተመለስ", callback_data="back_home")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    elif query.data == "support":
        text = "Contact support at @HorizonSupport" if lang == "en" else "ለእርዳታ በ @HorizonSupport ያግኙን።"
        kb = [[InlineKeyboardButton("🔙 Back / ተመለስ", callback_data="back_home")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    elif query.data == "back_home":
        return await start(update, context)
    
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

    # Start Flask keep-alive server in a background thread
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

    # Initialize Bot Application
    application = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            LANG: [CallbackQueryHandler(language_handler, pattern="^lang_")],
            MENU: [CallbackQueryHandler(menu_callback)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    logger.info("Starting bot polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
