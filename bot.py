import os
import json
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
import gspread
from google.oauth2.service_account import Credentials

# Logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment Variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
JOB_CHANNEL_ID = os.environ.get("JOB_CHANNEL_ID")
GOOGLE_JSON = os.environ.get("GOOGLE_JSON")

# Conversation States
CHOOSING_ROLE = 0
# Tutor registration states
TUTOR_NAME = 1
TUTOR_PHONE = 2
TUTOR_SUBJECTS = 3
TUTOR_GRADES = 4
TUTOR_LOCATION = 5
TUTOR_QUALIFICATION = 6
TUTOR_ID_PHOTO = 7

# Client request states
CLIENT_NAME = 10
CLIENT_PHONE = 11
CLIENT_SUBJECT = 12
CLIENT_GRADE = 13
CLIENT_LOCATION = 14
CLIENT_BUDGET = 15


def append_to_gsheet(sheet_name: str, row_data: list):
    """Appends a row to the specified tab in Google Sheets."""
    if not GOOGLE_JSON:
        logger.error("GOOGLE_JSON environment variable is not set.")
        return False

    try:
        creds_dict = json.loads(GOOGLE_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(credentials)
        
        # Open your Google Sheet titled 'Horizon Tutors Hub'
        sheet = client.open("Horizon Tutors Hub").worksheet(sheet_name)
        sheet.append_row(row_data)
        return True
    except Exception as e:
        logger.error(f"Google Sheets error on sheet '{sheet_name}': {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the bot conversation and displays the main menu."""
    reply_keyboard = [["🎓 Register as Tutor", "📖 Request a Tutor"]]
    await update.message.reply_text(
        "Welcome to *Horizon Tutors Hub*! 👋\n\nPlease select an option below:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return CHOOSING_ROLE


async def role_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Directs user based on their menu selection."""
    text = update.message.text

    if text == "🎓 Register as Tutor":
        await update.message.reply_text(
            "Great! Let's get you registered as a tutor.\n\nFirst, please enter your **Full Name**:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return TUTOR_NAME

    elif text == "📖 Request a Tutor":
        await update.message.reply_text(
            "Great! Let's find you a tutor.\n\nPlease enter your **Full Name** (Parent or Student):",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return CLIENT_NAME

    else:
        await update.message.reply_text("Please choose one of the options using the buttons below.")
        return CHOOSING_ROLE


# --- TUTOR REGISTRATION FLOW ---

async def tutor_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_name"] = update.message.text
    await update.message.reply_text("Enter your **Phone Number** (e.g., 0911234567):", parse_mode="Markdown")
    return TUTOR_PHONE


async def tutor_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_phone"] = update.message.text
    await update.message.reply_text("Which **Subjects** do you teach? (e.g., Mathematics, Physics):", parse_mode="Markdown")
    return TUTOR_SUBJECTS


async def tutor_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_subjects"] = update.message.text
    await update.message.reply_text("Which **Grade Levels** do you teach? (e.g., Grades 9-12):", parse_mode="Markdown")
    return TUTOR_GRADES


async def tutor_grades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_grades"] = update.message.text
    await update.message.reply_text("Enter your **Location** (e.g., Adama - Piazza):", parse_mode="Markdown")
    return TUTOR_LOCATION


async def tutor_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_location"] = update.message.text
    await update.message.reply_text("State your **Qualifications & Experience** (e.g., BSc in Engineering, 2 yrs experience):", parse_mode="Markdown")
    return TUTOR_QUALIFICATION


async def tutor_qualification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_qualification"] = update.message.text
    await update.message.reply_text(
        "📷 For security verification, please upload a clear photo or image of your **ID Card (Kebele or National ID)**:",
        parse_mode="Markdown"
    )
    return TUTOR_ID_PHOTO


async def tutor_id_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles photo/document upload for ID verification, logs data, and completes registration."""
    user = update.effective_user
    file_id = None

    # Check if sent as a compressed photo or document image
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("Please upload an image file or photo of your ID card to continue.")
        return TUTOR_ID_PHOTO

    context.user_data["tutor_id_photo"] = file_id
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Forward ID card photo to Admin Chat
    if ADMIN_CHAT_ID:
        try:
            caption_text = (
                f"🆔 *NEW TUTOR ID SUBMISSION*\n\n"
                f"*Name:* {context.user_data.get('tutor_name')}\n"
                f"*Phone:* {context.user_data.get('tutor_phone')}\n"
                f"*Username:* @{user.username if user.username else 'N/A'}\n"
                f"*Telegram ID:* `{user.id}`"
            )
            if update.message.photo:
                await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=file_id, caption=caption_text, parse_mode="Markdown")
            elif update.message.document:
                await context.bot.send_document(chat_id=ADMIN_CHAT_ID, document=file_id, caption=caption_text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error forwarding ID photo to Admin: {e}")

    # Append entry to Google Sheets 'Tutors' tab
    row_data = [
        now_str,
        str(user.id),
        f"@{user.username}" if user.username else "N/A",
        context.user_data.get("tutor_name", ""),
        context.user_data.get("tutor_phone", ""),
        context.user_data.get("tutor_subjects", ""),
        context.user_data.get("tutor_grades", ""),
        context.user_data.get("tutor_location", ""),
        context.user_data.get("tutor_qualification", ""),
        "Pending Verification",
    ]
    append_to_gsheet("Tutors", row_data)

    await update.message.reply_text(
        "✅ *Registration Submitted Successfully!*\n\n"
        "Our team will review your ID verification and contact you shortly when matching tutoring jobs become available.",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


# --- CLIENT REQUEST FLOW ---

async def client_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_name"] = update.message.text
    await update.message.reply_text("Enter your **Phone Number**:", parse_mode="Markdown")
    return CLIENT_PHONE


async def client_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_phone"] = update.message.text
    await update.message.reply_text("Which **Subject(s)** do you need help with?:", parse_mode="Markdown")
    return CLIENT_SUBJECT


async def client_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_subject"] = update.message.text
    await update.message.reply_text("What is the **Student's Grade Level**? (e.g., Grade 8):", parse_mode="Markdown")
    return CLIENT_GRADE


async def client_grade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_grade"] = update.message.text
    await update.message.reply_text("Enter your **Location** (e.g., Adama - Bole):", parse_mode="Markdown")
    return CLIENT_LOCATION


async def client_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_location"] = update.message.text
    await update.message.reply_text("What is your **Monthly Budget / Offered Rate**? (e.g., 3,000 ETB / month):", parse_mode="Markdown")
    return CLIENT_BUDGET


async def client_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_budget"] = update.message.text
    user = update.effective_user
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Append entry to Google Sheets 'Client_Requests' tab
    row_data = [
        now_str,
        str(user.id),
        f"@{user.username}" if user.username else "N/A",
        context.user_data.get("client_name", ""),
        context.user_data.get("client_phone", ""),
        context.user_data.get("client_subject", ""),
        context.user_data.get("client_grade", ""),
        context.user_data.get("client_location", ""),
        context.user_data.get("client_budget", ""),
        "Pending Match",
    ]
    append_to_gsheet("Client_Requests", row_data)

    # Post Job Announcement to Telegram Channel
    if JOB_CHANNEL_ID:
        try:
            job_post = (
                f"📌 *NEW TUTORING JOB OPPORTUNITY*\n\n"
                f"📚 *Subject:* {context.user_data.get('client_subject')}\n"
                f"🎓 *Grade:* {context.user_data.get('client_grade')}\n"
                f"📍 *Location:* {context.user_data.get('client_location')}\n"
                f"💰 *Budget:* {context.user_data.get('client_budget')}\n\n"
                f"📩 *To apply, contact us via bot:* @HorizonTutorsHubBot"
            )
            await context.bot.send_message(chat_id=JOB_CHANNEL_ID, text=job_post, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error posting job to channel: {e}")

    await update.message.reply_text(
        "✅ *Tutor Request Received!*\n\n"
        "We have received your request and will match you with a qualified tutor shortly.",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled. Type /start to begin again.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END


def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN is missing!")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, role_chosen)],
            # Tutor registration flow
            TUTOR_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_name)],
            TUTOR_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_phone)],
            TUTOR_SUBJECTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_subjects)],
            TUTOR_GRADES: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_grades)],
            TUTOR_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_location)],
            TUTOR_QUALIFICATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_qualification)],
            TUTOR_ID_PHOTO: [MessageHandler(filters.PHOTO | filters.Document.ALL, tutor_id_photo)],
            # Client request flow
            CLIENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_name)],
            CLIENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_phone)],
            CLIENT_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_subject)],
            CLIENT_GRADE: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_grade)],
            CLIENT_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_location)],
            CLIENT_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_budget)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)

    print("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
