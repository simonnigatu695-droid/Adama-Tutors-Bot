import os
import logging
from datetime import datetime
import random
import json
import cv2
import numpy as np
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
    sheet = get_gspread_client()
    if sheet:
        try:
            ws = sheet.worksheet("Tutors")
            ws.append_row(data)
            logger.info("🎉 SUCCESS: Tutor row successfully appended to 'Tutors' tab!")
        except Exception as e:
            logger.error(f"❌ FAILED to append to 'Tutors' tab: {str(e)}")

def db_add_client_request(data):
    sheet = get_gspread_client()
    if sheet:
        try:
            ws = sheet.worksheet("Client_Requests")
            ws.append_row(data)
            logger.info("🎉 SUCCESS: Request row successfully appended to 'Client_Requests' tab!")
        except Exception as e:
            logger.error(f"❌ FAILED to append to 'Client_Requests' tab: {str(e)}")

# Conversation States
(
    LANG, MENU, 
    TUTOR_NAME, TUTOR_PHONE, TUTOR_SUBJECT, TUTOR_GRADE_CAT, TUTOR_GRADE_SPECIFIC, TUTOR_CITY, TUTOR_SPECIFIC_LOC, TUTOR_ID,
    REQ_NAME, REQ_PHONE, REQ_SUBJECT, REQ_GRADE_CAT, REQ_GRADE_SPECIFIC, REQ_CITY, REQ_SPECIFIC_LOC
) = range(17)

# --- Core Handlers & Commands Setup ---

async def post_init(application: Application):
    commands = [
        BotCommand("start", "Start or restart the bot / ቦቱን ያስጀምሩ"),
        BotCommand("help", "How to use this bot"),
        BotCommand("broadcast", "Post vacancy with job ID to channel"),
        BotCommand("catchup", "View recent pending applications"),
        BotCommand("stats", "View live agency stats"),
        BotCommand("search", "Search approved tutors by keyword"),
        BotCommand("testblur", "Check sharpness score of an ID photo"),
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
            "qualified, verified professional tutors across Adama and Bishoftu."
            if lang == "en" else
            "ℹ️ **ስለ ሆራይዘን የቱተርስ ሃብ**\n"
            "እኛ በአዳማ እና በቢሾፍቱ ከተሞች ጥራት ያላቸውን እና ብቁ መምህራንን ከተማሪዎች ጋር የምናገናኝ ድርጅት ነን።"
        )
        kb = [[InlineKeyboardButton("🔙 Back / ተመለስ", callback_data="back_home")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return MENU
        
    elif query.data == "support":
        text = (
            "📞 **Customer Support & Assistance**\n"
            "👤 Telegram: @simon_1919\n"
            "📱 Phone: +251 90 088 2373"
            if lang == "en" else
            "📞 **የዕርዳታ ማዕከል**\n"
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

# --- Additional Command Implementations ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📖 Use /start to open the main menu or /cancel to abort any ongoing operation.")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📢 Broadcast feature ready.")

async def catchup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Checking recent pending applications...")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheet = get_gspread_client()
    if sheet:
        try:
            tutors_count = len(sheet.worksheet("Tutors").get_all_values()) - 1
            requests_count = len(sheet.worksheet("Client_Requests").get_all_values()) - 1
            await update.message.reply_text(f"📊 **Live Agency Stats**\n\nTutors: {max(0, tutors_count)}\nRequests: {max(0, requests_count)}", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"Error fetching stats: {e}")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Search feature ready.")

async def testblur_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📷 Send or reply to an ID photo to test its sharpness score.")

# --- Tutor Registration Flow ---

async def reg_tutor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "Please enter your full name:"
    kb = [[InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_NAME

async def tutor_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_name"] = update.message.text
    kb = [[InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")]]
    await update.message.reply_text("Please enter your phone number (e.g., 0911223344):", reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_PHONE

async def tutor_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_phone"] = update.message.text
    kb = [
        [InlineKeyboardButton("📚 All Subjects / ሁሉም", callback_data="tsubj_all")],
        [InlineKeyboardButton("🔢 Mathematics / ሂሳብ", callback_data="tsubj_math")],
        [InlineKeyboardButton("⚗️ Physics & Chemistry / ሳይንስ", callback_data="tsubj_science")],
        [InlineKeyboardButton("📝 English / እንግሊዝኛ", callback_data="tsubj_english")],
        [InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")],
    ]
    await update.message.reply_text("Select your primary teaching subject:", reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_SUBJECT

async def tutor_subject_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mapping = {
        "tsubj_all": "All Subjects",
        "tsubj_math": "Mathematics",
        "tsubj_science": "Physics & Chemistry",
        "tsubj_english": "English"
    }
    context.user_data["tutor_subject"] = mapping.get(query.data, "Custom Subject")

    kb = [
        [InlineKeyboardButton("🎒 Primary / አንደኛ ደረጃ", callback_data="tcat_primary")],
        [InlineKeyboardButton("🏫 Secondary / ሁለተኛ ደረጃ", callback_data="tcat_secondary")],
        [InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")],
    ]
    await query.message.edit_text("Select Grade Category:", reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_GRADE_CAT

async def tutor_grade_cat_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = "Primary" if query.data == "tcat_primary" else "Secondary"
    context.user_data["tutor_grade_cat"] = category

    if category == "Primary":
        kb = [
            [InlineKeyboardButton("Grade 1", callback_data="tgrade_1"), InlineKeyboardButton("Grade 2", callback_data="tgrade_2")],
            [InlineKeyboardButton("Grade 3", callback_data="tgrade_3"), InlineKeyboardButton("Grade 4", callback_data="tgrade_4")],
            [InlineKeyboardButton("Grade 5", callback_data="tgrade_5"), InlineKeyboardButton("Grade 6", callback_data="tgrade_6")],
            [InlineKeyboardButton("Grade 7", callback_data="tgrade_7"), InlineKeyboardButton("Grade 8", callback_data="tgrade_8")],
            [InlineKeyboardButton("⭐ All Primary (1 - 8)", callback_data="tgrade_all_primary")],
            [InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")]
        ]
    else:
        kb = [
            [InlineKeyboardButton("Grade 9", callback_data="tgrade_9"), InlineKeyboardButton("Grade 10", callback_data="tgrade_10")],
            [InlineKeyboardButton("Grade 11", callback_data="tgrade_11"), InlineKeyboardButton("Grade 12", callback_data="tgrade_12")],
            [InlineKeyboardButton("⭐ All Secondary (9 - 12)", callback_data="tgrade_all_secondary")],
            [InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")]
        ]
    await query.message.edit_text(f"Select exact grade under **{category}**:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return TUTOR_GRADE_SPECIFIC

async def tutor_grade_specific_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mapping = {
        "tgrade_1": "Grade 1",
        "tgrade_2": "Grade 2",
        "tgrade_3": "Grade 3",
        "tgrade_4": "Grade 4",
        "tgrade_5": "Grade 5",
        "tgrade_6": "Grade 6",
        "tgrade_7": "Grade 7",
        "tgrade_8": "Grade 8",
        "tgrade_all_primary": "All Primary (1 - 8)",
        "tgrade_9": "Grade 9",
        "tgrade_10": "Grade 10",
        "tgrade_11": "Grade 11",
        "tgrade_12": "Grade 12",
        "tgrade_all_secondary": "All Secondary (9 - 12)"
    }
    context.user_data["tutor_grade_specific"] = mapping.get(query.data, "Specific Grade")

    kb = [
        [InlineKeyboardButton("Adama / አዳማ", callback_data="city_adama")],
        [InlineKeyboardButton("Bishoftu / ቢሾፍቱ", callback_data="city_bishoftu")],
        [InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")],
    ]
    await query.message.edit_text("Select your city / ከተማዎን ይምረጡ:", reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_CITY

async def tutor_city_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["tutor_city"] = "Adama" if query.data == "city_adama" else "Bishoftu"
    kb = [[InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")]]
    await query.message.edit_text("📍 Please enter your specific neighborhood or Kebele:", reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_SPECIFIC_LOC

async def tutor_specific_loc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_specific_loc"] = update.message.text
    text = (
        "📷 **Almost done!** To help us maintain a safe and trusted community for students and parents, "
        "please upload a clear photo of the **front of your National ID**:"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    return TUTOR_ID

async def tutor_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please upload an image file of your ID.")
        return TUTOR_ID

    photo_file = await update.message.photo[-1].get_file()
    id_file_id = photo_file.file_id

    file_bytes = await photo_file.download_as_bytearray()
    nparr = np.frombuffer(file_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
    sharpness = round(cv2.Laplacian(img, cv2.CV_64F).var(), 2) if img is not None else 0.0
    blur_status = "🟢 Clear" if sharpness > 100 else "⚠️ Blurry / Low Quality"

    context.user_data["tutor_id_file_id"] = id_file_id
    context.user_data["tutor_sharpness"] = sharpness

    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
    if admin_chat_id:
        review_text = (
            f"🔔 **New Tutor Registration Review**\n\n"
            f"👤 Name: {context.user_data.get('tutor_name')}\n"
            f"📞 Phone: {context.user_data.get('tutor_phone')}\n"
            f"📚 Subject: {context.user_data.get('tutor_subject')}\n"
            f"🏫 Category: {context.user_data.get('tutor_grade_cat')}\n"
            f"🎒 Exact Grade: {context.user_data.get('tutor_grade_specific')}\n"
            f"🏙️ City: {context.user_data.get('tutor_city')}\n"
            f"📍 Location: {context.user_data.get('tutor_specific_loc')}\n"
            f"📐 ID Sharpness Score: `{sharpness}` ({blur_status})"
        )
        
        user_id = update.effective_user.id
        context.bot_data[f"pending_tutor_{user_id}"] = context.user_data.copy()

        kb = [
            [
                InlineKeyboardButton("✅ Approve Tutor", callback_data=f"app_tutor_{user_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"rej_tutor_{user_id}")
            ]
        ]
        await context.bot.send_photo(
            chat_id=admin_chat_id,
            photo=id_file_id,
            caption=review_text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

    text = "✅ **Registration & ID Submitted for Review!**\nOur admin team is reviewing your details and ID. You will be notified soon."
    kb = [[InlineKeyboardButton("🔙 Main Menu / ዋና ምናሌ", callback_data="back_home")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return MENU


# --- Student Request Flow ---

async def req_tutor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("Please enter the student's or parent's full name:")
    return REQ_NAME

async def req_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_name"] = update.message.text
    await update.message.reply_text("Please enter your contact phone number:")
    return REQ_PHONE

async def req_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_phone"] = update.message.text
    kb = [
        [InlineKeyboardButton("📚 All Subjects / ሁሉም", callback_data="rsubj_all")],
        [InlineKeyboardButton("🔢 Mathematics / ሂሳብ", callback_data="rsubj_math")],
        [InlineKeyboardButton("⚗️ Physics & Chemistry / ሳይንስ", callback_data="rsubj_science")],
        [InlineKeyboardButton("📝 English / እንግሊዝኛ", callback_data="rsubj_english")],
    ]
    await update.message.reply_text("Select subject needed:", reply_markup=InlineKeyboardMarkup(kb))
    return REQ_SUBJECT

async def req_subject_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mapping = {
        "rsubj_all": "All Subjects",
        "rsubj_math": "Mathematics",
        "rsubj_science": "Physics & Chemistry",
        "rsubj_english": "English"
    }
    context.user_data["req_subject"] = mapping.get(query.data, "General Subject")

    kb = [
        [InlineKeyboardButton("🎒 Primary / አንደኛ ደረጃ", callback_data="rcat_primary")],
        [InlineKeyboardButton("🏫 Secondary / ሁለተኛ ደረጃ", callback_data="rcat_secondary")],
    ]
    await query.message.edit_text("Select Grade Category:", reply_markup=InlineKeyboardMarkup(kb))
    return REQ_GRADE_CAT

async def req_grade_cat_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = "Primary" if query.data == "rcat_primary" else "Secondary"
    context.user_data["req_grade_cat"] = category

    if category == "Primary":
        kb = [
            [InlineKeyboardButton("Grade 1", callback_data="rgrade_1"), InlineKeyboardButton("Grade 2", callback_data="rgrade_2")],
            [InlineKeyboardButton("Grade 3", callback_data="rgrade_3"), InlineKeyboardButton("Grade 4", callback_data="rgrade_4")],
            [InlineKeyboardButton("Grade 5", callback_data="rgrade_5"), InlineKeyboardButton("Grade 6", callback_data="rgrade_6")],
            [InlineKeyboardButton("Grade 7", callback_data="rgrade_7"), InlineKeyboardButton("Grade 8", callback_data="rgrade_8")],
            [InlineKeyboardButton("⭐ All Primary (1 - 8)", callback_data="rgrade_all_primary")]
        ]
    else:
        kb = [
            [InlineKeyboardButton("Grade 9", callback_data="rgrade_9"), InlineKeyboardButton("Grade 10", callback_data="rgrade_10")],
            [InlineKeyboardButton("Grade 11", callback_data="rgrade_11"), InlineKeyboardButton("Grade 12", callback_data="rgrade_12")],
            [InlineKeyboardButton("⭐ All Secondary (9 - 12)", callback_data="rgrade_all_secondary")]
        ]
    await query.message.edit_text(f"Select exact grade under **{category}**:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return REQ_GRADE_SPECIFIC

async def req_grade_specific_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mapping = {
        "rgrade_1": "Grade 1",
        "rgrade_2": "Grade 2",
        "rgrade_3": "Grade 3",
        "rgrade_4": "Grade 4",
        "rgrade_5": "Grade 5",
        "rgrade_6": "Grade 6",
        "rgrade_7": "Grade 7",
        "rgrade_8": "Grade 8",
        "rgrade_all_primary": "All Primary (1 - 8)",
        "rgrade_9": "Grade 9",
        "rgrade_10": "Grade 10",
        "rgrade_11": "Grade 11",
        "rgrade_12": "Grade 12",
        "rgrade_all_secondary": "All Secondary (9 - 12)"
    }
    context.user_data["req_grade_specific"] = mapping.get(query.data, "Specific Grade")

    kb = [
        [InlineKeyboardButton("Adama / አዳማ", callback_data="reqcity_adama")],
        [InlineKeyboardButton("Bishoftu / ቢሾፍቱ", callback_data="reqcity_bishoftu")],
    ]
    await query.message.edit_text("Select the city for tutoring:", reply_markup=InlineKeyboardMarkup(kb))
    return REQ_CITY

async def req_city_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["req_city"] = "Adama" if query.data == "reqcity_adama" else "Bishoftu"
    kb = [[InlineKeyboardButton("❌ Cancel / ሰርዝ", callback_data="cancel_action")]]
    await query.message.edit_text("📍 Please enter the specific neighborhood or Kebele where tutoring is needed:", reply_markup=InlineKeyboardMarkup(kb))
    return REQ_SPECIFIC_LOC

async def req_specific_loc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_specific_loc"] = update.message.text
    job_code = f"HRZ-{random.randint(1000, 9999)}"
    context.user_data["job_code"] = job_code

    user_id = update.effective_user.id
    context.bot_data[f"pending_req_{user_id}"] = context.user_data.copy()

    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
    if admin_chat_id:
        review_text = (
            f"🔔 **New Student Request Review**\n\n"
            f"🆔 Job Code: `{job_code}`\n"
            f"👤 Client: {context.user_data.get('req_name')}\n"
            f"📞 Phone: {context.user_data.get('req_phone')}\n"
            f"📚 Subject: {context.user_data.get('req_subject')}\n"
            f"🏫 Category: {context.user_data.get('req_grade_cat')}\n"
            f"🎒 Exact Grade: {context.user_data.get('req_grade_specific')}\n"
            f"🏙️ City: {context.user_data.get('req_city')}\n"
            f"📍 Location: {context.user_data.get('req_specific_loc')}"
        )
        kb = [
            [
                InlineKeyboardButton("✅ Approve & Post", callback_data=f"app_req_{user_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"rej_req_{user_id}")
            ]
        ]
        await context.bot.send_message(chat_id=admin_chat_id, text=review_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    text = f"✅ **Request Submitted for Review!**\nYour Job Code is `{job_code}`. Once reviewed by our team, it will be posted."
    kb = [[InlineKeyboardButton("🔙 Main Menu / ዋና ምናሌ", callback_data="back_home")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return MENU


# --- Admin Approval & Channel Broadcast Handler ---

async def admin_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("app_tutor_"):
        user_id = data.split("_")[2]
        tutor_data = context.bot_data.get(f"pending_tutor_{user_id}")
        if tutor_data:
            full_location = f"{tutor_data.get('tutor_city')} - {tutor_data.get('tutor_specific_loc')}"
            db_add_tutor([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(user_id),
                "",
                tutor_data.get('tutor_name'),
                tutor_data.get('tutor_phone'),
                "",
                tutor_data.get('tutor_subject'),
                f"{tutor_data.get('tutor_grade_cat')} - {tutor_data.get('tutor_grade_specific')}",
                full_location,
                "",
                "Approved",
                "Active",
                tutor_data.get('tutor_id_file_id')
            ])
            await query.edit_message_caption(caption=query.message.caption + "\n\n✅ **APPROVED & SAVED TO SHEET**", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=user_id, text="🎉 Congratulations! Your tutor registration has been approved by admin.")
            except Exception:
                pass
    elif data.startswith("rej_tutor_"):
        user_id = data.split("_")[2]
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ **REJECTED**", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=user_id, text="⚠️ Unfortunately, your tutor registration was rejected. Please contact support.")
        except Exception:
            pass

    elif data.startswith("app_req_"):
        user_id = data.split("_")[2]
        req_data = context.bot_data.get(f"pending_req_{user_id}")
        if req_data:
            job_code = req_data.get('job_code')
            full_grade_str = f"{req_data.get('req_grade_cat')} - {req_data.get('req_grade_specific')}"
            db_add_client_request([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                job_code,
                str(user_id),
                "",
                req_data.get('req_name'),
                req_data.get('req_phone'),
                req_data.get('req_subject'),
                full_grade_str,
                req_data.get('req_city'),
                req_data.get('req_specific_loc'),
                "", "", "", "", "", "Approved", "Open", ""
            ])

            job_channel_id = os.environ.get("JOB_CHANNEL_ID")
            if job_channel_id:
                channel_msg = (
                    f"🟢 **OPEN**\n\n"
                    f"🏫 **Grade:** {full_grade_str}\n"
                    f"🏙️ **City:** {req_data.get('req_city')}\n"
                    f"📍 **Place:** {req_data.get('req_specific_loc')}\n"
                    f"📚 **Subject:** {req_data.get('req_subject')}\n"
                    f"💬 **Job code:** #{job_code}\n\n"
                    f"_We need highly committed, punctual and talented tutor._"
                )
                bot_username = context.bot.username
                channel_kb = [[InlineKeyboardButton("Apply Now", url=f"https://t.me/{bot_username}")]]
                await context.bot.send_message(
                    chat_id=job_channel_id,
                    text=channel_msg,
                    reply_markup=InlineKeyboardMarkup(channel_kb),
                    parse_mode="Markdown"
                )

            await query.edit_message_text(text=query.message.text + "\n\n✅ **APPROVED, SAVED & POSTED TO CHANNEL**", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=user_id, text=f"🎉 Your request has been approved and published! Job Code: `{job_code}`", parse_mode="Markdown")
            except Exception:
                pass
    elif data.startswith("rej_req_"):
        user_id = data.split("_")[2]
        await query.edit_message_text(text=query.message.text + "\n\n❌ **REJECTED**", parse_mode="Markdown")


# --- Cancel Handlers ---

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Operation cancelled. Type /start to begin again.")
    return ConversationHandler.END

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = [[InlineKeyboardButton("🔄 Restart / ጀምር", callback_data="back_home")]]
    await query.message.edit_text("Operation cancelled. Type /start to begin again.", reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        logger.error("No TELEGRAM_TOKEN found!")
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
            TUTOR_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_name_received), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            TUTOR_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_phone_received), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            TUTOR_SUBJECT: [CallbackQueryHandler(tutor_subject_received, pattern="^tsubj_"), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            TUTOR_GRADE_CAT: [CallbackQueryHandler(tutor_grade_cat_received, pattern="^tcat_"), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            TUTOR_GRADE_SPECIFIC: [CallbackQueryHandler(tutor_grade_specific_received, pattern="^tgrade_"), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            TUTOR_CITY: [CallbackQueryHandler(tutor_city_received, pattern="^city_"), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            TUTOR_SPECIFIC_LOC: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_specific_loc_received), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            TUTOR_ID: [MessageHandler(filters.PHOTO & ~filters.COMMAND, tutor_id_received), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            
            REQ_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_name_received), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            REQ_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_phone_received), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            REQ_SUBJECT: [CallbackQueryHandler(req_subject_received, pattern="^rsubj_"), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            REQ_GRADE_CAT: [CallbackQueryHandler(req_grade_cat_received, pattern="^rcat_"), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            REQ_GRADE_SPECIFIC: [CallbackQueryHandler(req_grade_specific_received, pattern="^rgrade_"), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            REQ_CITY: [CallbackQueryHandler(req_city_received, pattern="^reqcity_"), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
            REQ_SPECIFIC_LOC: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_specific_loc_received), CallbackQueryHandler(cancel_callback, pattern="^cancel_action$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_message=False
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(admin_approval_callback, pattern="^(app|rej)_"))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("catchup", catchup_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("testblur", testblur_command))

    logger.info("Starting bot polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
