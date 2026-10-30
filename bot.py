import os
import logging
from datetime import datetime
import random
import json
import cv2
import numpy as np
import io
from flask import Flask
from threading import Thread
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
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

def upload_id_to_drive(file_bytes, file_name):
    try:
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

        env_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if env_creds:
            creds_dict = json.loads(env_creds)
            creds = service_account.Credentials.from_service_account_info(
                creds_dict, scopes=['https://www.googleapis.com/auth/drive']
            )
        elif cred_path:
            creds = service_account.Credentials.from_service_account_file(
                cred_path, scopes=['https://www.googleapis.com/auth/drive']
            )
        else:
            logger.error("❌ Drive Upload Error: No credentials found.")
            return None

        service = build('drive', 'v3', credentials=creds)
        folder_id = "1eMruxEtVhLLi1607zwbZIeerr-MxVAE4"

        file_metadata = {
            'name': file_name,
            'parents': [folder_id]
        }
        
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype='image/jpeg', resumable=True)
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink'
        ).execute()
        
        logger.info(f"✅ ID successfully uploaded to Google Drive: {file.get('webViewLink')}")
        return file.get('webViewLink')
    except Exception as e:
        logger.error(f"❌ Failed to upload ID to Google Drive: {str(e)}")
        return None

# Conversation States
(
    LANG, MENU, 
    TUTOR_NAME, TUTOR_PHONE, TUTOR_GENDER, TUTOR_SUBJECT, TUTOR_GRADE_CAT, TUTOR_GRADE_SPECIFIC, TUTOR_CITY, TUTOR_SPECIFIC_LOC, TUTOR_ID,
    REQ_NAME, REQ_PHONE, REQ_SUBJECT, REQ_GRADE_CAT, REQ_GRADE_SPECIFIC, REQ_CITY, REQ_SPECIFIC_LOC, REQ_BUDGET
) = range(19)

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
    if context.args:
        arg = context.args[0]
        if arg.startswith("apply_"):
            job_code = arg.replace("apply_", "")
            context.user_data["target_job"] = job_code
            await update.message.reply_text(
                f"🎯 **Applying for Job #{job_code}**\n"
                f"Welcome! To apply for this vacancy, please register as a tutor or contact our admin with your Job Code.",
                parse_mode="Markdown"
            )

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
            "qualified, verified professional tutors across Adama and Bishoftu.\n\n"
            "💡 **Agency Policy:** Enjoy a 1-day trial session. Commission is 50% after successful trial."
            if lang == "en" else
            "ℹ️ **ስለ ሆራይዘን የቱተርስ ሃብ**\n"
            "እኛ በአዳማ እና በቢሾፍቱ ከተሞች ጥራት ያላቸውን እና ብቁ መምህራንን ከተማሪዎች ጋር የምናገናኝ ድርጅት ነን።\n\n"
            "💡 **የድርጅቱ ህግ:** የ 1 ቀን የሙከራ ትምህርት (Trial) አለ። የኮሚሽን ክፍያ 50% የሚከፈለው ከሙከራው በኋላ ነው።"
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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **Horizon Tutors Hub - Help Guide**\n\n"
        "👤 **User Commands:**\n"
        "• /start - Open main menu & choose language\n"
        "• /cancel - Abort current operation\n\n"
        "🛠️ **Admin Commands:**\n"
        "• /stats - View live agency database counts\n"
        "• /search [Keyword] - Search approved tutors\n"
        "• /catchup - View active pending items in memory\n"
        "• /broadcast [Job Code] - Broadcast a job vacancy to channel\n"
        "• /testblur - Test sharpness score of an ID photo"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheet = get_gspread_client()
    if sheet:
        try:
            tutors_count = len(sheet.worksheet("Tutors").get_all_values()) - 1
            requests_count = len(sheet.worksheet("Client_Requests").get_all_values()) - 1
            await update.message.reply_text(
                f"📊 **Live Agency Stats (Horizon Tutors Hub)**\n\n"
                f"👨‍🏫 Approved Tutors: `{max(0, tutors_count)}`\n"
                f"📚 Total Student Requests: `{max(0, requests_count)}`\n"
                f"📍 Active Cities: Adama & Bishoftu",
                parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error fetching stats from Google Sheets: {e}")
    else:
        await update.message.reply_text("❌ Database connection error.")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("🔍 Usage: `/search [keyword]`", parse_mode="Markdown")
        return
    query_keyword = " ".join(context.args).lower()
    sheet = get_gspread_client()
    if not sheet:
        await update.message.reply_text("❌ Database connection error.")
        return
    try:
        ws = sheet.worksheet("Tutors")
        rows = ws.get_all_values()
        matches = []
        for row in rows[1:]:
            if query_keyword in " ".join(row).lower():
                matches.append(f"👤 **{row[3]}**\n📞 `{row[4]}`\n📚 {row[6]} ({row[7]})\n📍 {row[8]}\n")
        if matches:
            await update.message.reply_text(f"🔍 **Search Results:**\n\n" + "\n".join(matches[:10]), parse_mode="Markdown")
        else:
            await update.message.reply_text(f"🔍 No matching tutors found.")
    except Exception as e:
        await update.message.reply_text(f"❌ Search error: {e}")

async def catchup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending_items = [k for k in context.bot_data.keys() if k.startswith("pending_")]
    if not pending_items:
        await update.message.reply_text("📋 No pending items in memory.")
        return
    await update.message.reply_text(f"📋 **Pending Items ({len(pending_items)}):**\n" + "\n".join(f"• `{item}`" for item in pending_items), parse_mode="Markdown")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("📢 Usage: `/broadcast [Job Code]`", parse_mode="Markdown")
        return
    job_code_target = context.args[0].replace("#", "")
    job_channel_id = os.environ.get("JOB_CHANNEL_ID")
    sheet = get_gspread_client()
    if not sheet or not job_channel_id:
        await update.message.reply_text("❌ Configuration or database error.")
        return
    try:
        rows = sheet.worksheet("Client_Requests").get_all_values()
        found_row = next((r for r in rows[1:] if len(r) > 1 and r[1] == job_code_target), None)
        if not found_row:
            await update.message.reply_text(f"❌ Job Code `#{job_code_target}` not found.", parse_mode="Markdown")
            return
        channel_msg = (
            f"🟢 **OPEN VACANCY**\n\n"
            f"🏫 **Grade:** {found_row[7]}\n"
            f"🏙️ **City:** {found_row[8]} ({found_row[9]})\n"
            f"📚 **Subject:** {found_row[6]}\n"
            f"💰 **Budget:** {found_row[10]}\n"
            f"💬 **Job code:** #{job_code_target}\n\n"
            f"💡 *Policy: 1-day trial session. Agency commission (50%) applies after successful trial.*"
        )
        bot_username = context.bot.username
        channel_kb = [[InlineKeyboardButton("Apply Now", url=f"https://t.me/{bot_username}?start=apply_{job_code_target}")]]
        await context.bot.send_message(chat_id=job_channel_id, text=channel_msg, reply_markup=InlineKeyboardMarkup(channel_kb), parse_mode="Markdown")
        await update.message.reply_text(f"✅ Successfully broadcasted `#{job_code_target}`!", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Broadcast error: {e}")

async def testblur_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("📷 Reply to an ID photo with `/testblur`.", parse_mode="Markdown")
        return
    photo_file = await update.message.reply_to_message.photo[-1].get_file()
    nparr = np.frombuffer(await photo_file.download_as_bytearray(), np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
    sharpness = round(cv2.Laplacian(img, cv2.CV_64F).var(), 2) if img is not None else 0.0
    await update.message.reply_text(f"📐 Sharpness Score: `{sharpness}`", parse_mode="Markdown")

# --- Tutor Registration Flow ---

async def reg_tutor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")
    await query.message.edit_text("Please enter your full name:" if lang == "en" else "እባክዎ ሙሉ ስምዎን ያስገቡ:")
    return TUTOR_NAME

async def tutor_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_name"] = update.message.text
    lang = context.user_data.get("lang", "en")
    contact_keyboard = ReplyKeyboardMarkup([[KeyboardButton("📱 Share Phone", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Please share your contact phone number:" if lang == "en" else "እባክዎ ስልክ ቁጥርዎን ያጋሩ:", reply_markup=contact_keyboard)
    return TUTOR_PHONE

async def tutor_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_phone"] = update.message.contact.phone_number if update.message.contact else update.message.text
    kb = [
        [InlineKeyboardButton("👨 Male / ወንድ", callback_data="tgen_male")],
        [InlineKeyboardButton("👩 Female / ሴት", callback_data="tgen_female")],
    ]
    await update.message.reply_text("Select gender / ጾታዎን ይምረጡ:", reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_GENDER

async def tutor_gender_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["tutor_gender"] = "Male" if query.data == "tgen_male" else "Female"
    kb = [
        [InlineKeyboardButton("📚 All Subjects", callback_data="tsubj_all")],
        [InlineKeyboardButton("🔢 Mathematics", callback_data="tsubj_math")],
        [InlineKeyboardButton("⚗️ Science", callback_data="tsubj_science")],
        [InlineKeyboardButton("📝 English", callback_data="tsubj_english")],
    ]
    await query.message.edit_text("Select subject:", reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_SUBJECT

async def tutor_subject_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mapping = {"tsubj_all": "All Subjects", "tsubj_math": "Mathematics", "tsubj_science": "Physics & Chemistry", "tsubj_english": "English"}
    context.user_data["tutor_subject"] = mapping.get(query.data, "Custom")
    kb = [
        [InlineKeyboardButton("Kindergarten (KG)", callback_data="tcat_kg")],
        [InlineKeyboardButton("Primary", callback_data="tcat_primary")],
        [InlineKeyboardButton("Secondary", callback_data="tcat_secondary")],
    ]
    await query.message.edit_text("Select grade category:", reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_GRADE_CAT

async def tutor_grade_cat_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = "Kindergarten (KG)" if query.data == "tcat_kg" else ("Primary" if query.data == "tcat_primary" else "Secondary")
    context.user_data["tutor_grade_cat"] = category
    
    if category == "Kindergarten (KG)":
        kb = [[InlineKeyboardButton("KG 1", callback_data="tgrade_kg1"), InlineKeyboardButton("KG 2", callback_data="tgrade_kg2")]]
    elif category == "Primary":
        kb = [[InlineKeyboardButton("Grade 1", callback_data="tgrade_1"), InlineKeyboardButton("Grade 8", callback_data="tgrade_8")]]
    else:
        kb = [[InlineKeyboardButton("Grade 9", callback_data="tgrade_9"), InlineKeyboardButton("Grade 12", callback_data="tgrade_12")]]
    await query.message.edit_text(f"Select exact grade under {category}:", reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_GRADE_SPECIFIC

async def tutor_grade_specific_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["tutor_grade_specific"] = query.data.replace("tgrade_", "").title()
    kb = [
        [InlineKeyboardButton("Adama", callback_data="city_adama")],
        [InlineKeyboardButton("Bishoftu", callback_data="city_bishoftu")],
    ]
    await query.message.edit_text("Select your city:", reply_markup=InlineKeyboardMarkup(kb))
    return TUTOR_CITY

async def tutor_city_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["tutor_city"] = "Adama" if query.data == "city_adama" else "Bishoftu"
    await query.message.edit_text("📍 Enter your specific neighborhood or Kebele:")
    return TUTOR_SPECIFIC_LOC

async def tutor_specific_loc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tutor_specific_loc"] = update.message.text
    context.user_data["tutor_budget"] = "N/A"
    await update.message.reply_text("📷 Please upload a clear photo of your National ID card:")
    return TUTOR_ID

async def tutor_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please upload an image file of your ID.")
        return TUTOR_ID

    photo_file = await update.message.photo[-1].get_file()
    file_bytes = await photo_file.download_as_bytearray()
    
    user = update.effective_user
    full_name = context.user_data.get('tutor_name', 'Tutor')
    safe_name = full_name.replace(" ", "_")
    username = f"@{user.username}" if user.username else f"id_{user.id}"
    
    file_name = f"{safe_name}_{username}.jpg"
    
    drive_link = upload_id_to_drive(file_bytes, file_name)
    context.user_data["tutor_drive_link"] = drive_link or "N/A"
    context.user_data["tutor_username"] = username
    context.user_data["telegram_id"] = user.id

    nparr = np.frombuffer(file_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
    sharpness = round(cv2.Laplacian(img, cv2.CV_64F).var(), 2) if img is not None else 0.0

    session_key = f"pending_tutor_{user.id}_{int(datetime.now().timestamp())}"
    context.bot_data[session_key] = context.user_data.copy()

    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
    if admin_chat_id:
        review_text = (
            f"🔔 **New Tutor Review**\n\n"
            f"👤 Name: {full_name}\n"
            f"🏷️ Username: {username}\n"
            f"📞 Phone: {context.user_data.get('tutor_phone')}\n"
            f"⚧️ Gender: {context.user_data.get('tutor_gender')}\n"
            f"📚 Subject: {context.user_data.get('tutor_subject')}\n"
            f"🏫 Grade: {context.user_data.get('tutor_grade_cat')} - {context.user_data.get('tutor_grade_specific')}\n"
            f"🏙️ Location: {context.user_data.get('tutor_city')} ({context.user_data.get('tutor_specific_loc')})\n"
            f"📁 [Secure Drive ID Link ({file_name})]({drive_link})\n"
            f"📐 Sharpness: `{sharpness}`"
        )
        kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"app_tutor_{session_key}"), InlineKeyboardButton("❌ Reject", callback_data=f"rej_tutor_{session_key}")]]
        await context.bot.send_photo(chat_id=admin_chat_id, photo=photo_file.file_id, caption=review_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    channel_link = os.environ.get("CHANNEL_INVITE_LINK", "https://t.me/HorizonTutorsHub")
    kb = [[InlineKeyboardButton("📢 Join Channel", url=channel_link)], [InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]
    await update.message.reply_text("✅ Registration submitted successfully! We will review your ID and contact you.", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return MENU

# --- Student Request Flow ---

async def req_tutor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("Please enter student's or parent's full name:")
    return REQ_NAME

async def req_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_name"] = update.message.text
    contact_keyboard = ReplyKeyboardMarkup([[KeyboardButton("📱 Share Phone", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Please share your contact phone number:", reply_markup=contact_keyboard)
    return REQ_PHONE

async def req_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_phone"] = update.message.contact.phone_number if update.message.contact else update.message.text
    kb = [[InlineKeyboardButton("Mathematics", callback_data="rsubj_math")], [InlineKeyboardButton("All Subjects", callback_data="rsubj_all")]]
    await update.message.reply_text("Choose subject:", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("Select subject:", reply_markup=InlineKeyboardMarkup(kb))
    return REQ_SUBJECT

async def req_subject_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["req_subject"] = "Mathematics" if query.data == "rsubj_math" else "All Subjects"
    kb = [[InlineKeyboardButton("Primary", callback_data="rcat_primary")], [InlineKeyboardButton("Secondary", callback_data="rcat_secondary")]]
    await query.message.edit_text("Select grade category:", reply_markup=InlineKeyboardMarkup(kb))
    return REQ_GRADE_CAT

async def req_grade_cat_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["req_grade_cat"] = "Primary" if query.data == "rcat_primary" else "Secondary"
    kb = [[InlineKeyboardButton("Grade 5", callback_data="rgrade_5")], [InlineKeyboardButton("Grade 9", callback_data="rgrade_9")]]
    await query.message.edit_text("Select exact grade:", reply_markup=InlineKeyboardMarkup(kb))
    return REQ_GRADE_SPECIFIC

async def req_grade_specific_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["req_grade_specific"] = query.data.replace("rgrade_", "").title()
    kb = [[InlineKeyboardButton("Adama", callback_data="reqcity_adama")], [InlineKeyboardButton("Bishoftu", callback_data="reqcity_bishoftu")]]
    await query.message.edit_text("Select city:", reply_markup=InlineKeyboardMarkup(kb))
    return REQ_CITY

async def req_city_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["req_city"] = "Adama" if query.data == "reqcity_adama" else "Bishoftu"
    await query.message.edit_text("📍 Enter specific neighborhood or Kebele:")
    return REQ_SPECIFIC_LOC

async def req_specific_loc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_specific_loc"] = update.message.text
    kb = [[InlineKeyboardButton("🤝 Negotiable", callback_data="rbudget_negotiable")]]
    await update.message.reply_text("💰 Click Negotiable or type your budget:", reply_markup=InlineKeyboardMarkup(kb))
    return REQ_BUDGET

async def req_budget_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["req_budget"] = "Negotiable 🤝"
    return await finalize_client_request(query.message, context)

async def req_budget_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["req_budget"] = update.message.text
    return await finalize_client_request(update.message, context)

async def finalize_client_request(message_obj, context):
    job_code = f"HRZ-{random.randint(1000, 9999)}"
    context.user_data["job_code"] = job_code
    session_key = f"pending_req_{message_obj.chat_id}_{int(datetime.now().timestamp())}"
    context.bot_data[session_key] = context.user_data.copy()

    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
    if admin_chat_id:
        review_text = (
            f"🔔 **New Student Request**\n\n"
            f"🆔 Job Code: `{job_code}`\n"
            f"👤 Client: {context.user_data.get('req_name')}\n"
            f"📞 Phone: {context.user_data.get('req_phone')}\n"
            f"📚 Subject: {context.user_data.get('req_subject')}\n"
            f"🏫 Grade: {context.user_data.get('req_grade_cat')} - {context.user_data.get('req_grade_specific')}\n"
            f"🏙️ Location: {context.user_data.get('req_city')} ({context.user_data.get('req_specific_loc')})\n"
            f"💰 Budget: {context.user_data.get('req_budget')}"
        )
        kb = [[InlineKeyboardButton("✅ Approve & Post", callback_data=f"app_req_{session_key}"), InlineKeyboardButton("❌ Reject", callback_data=f"rej_req_{session_key}")]]
        await context.bot.send_message(chat_id=admin_chat_id, text=review_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    channel_link = os.environ.get("CHANNEL_INVITE_LINK", "https://t.me/HorizonTutorsHub")
    kb = [[InlineKeyboardButton("📢 Join Channel", url=channel_link)], [InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]
    text = f"✅ Request submitted! Your Job Code is `#{job_code}`."
    if hasattr(message_obj, "edit_text"):
        await message_obj.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await message_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return MENU

# --- Admin Approval Handler ---

async def admin_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("app_tutor_"):
        session_key = data.replace("app_tutor_", "")
        tutor_data = context.bot_data.get(session_key)
        if tutor_data:
            full_location = f"{tutor_data.get('tutor_city')} - {tutor_data.get('tutor_specific_loc')}"
            db_add_tutor([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(tutor_data.get('telegram_id')),
                tutor_data.get('tutor_username', 'N/A'),
                tutor_data.get('tutor_name'),
                str(tutor_data.get('tutor_phone')),
                tutor_data.get('tutor_gender'),
                tutor_data.get('tutor_subject'),
                f"{tutor_data.get('tutor_grade_cat')} - {tutor_data.get('tutor_grade_specific')}",
                full_location,
                "N/A",
                "Approved",
                tutor_data.get('tutor_drive_link', 'N/A')
            ])
            await query.edit_message_caption(caption=query.message.caption + "\n\n✅ **APPROVED & SAVED (Username & Drive Linked)**", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=tutor_data.get('telegram_id'), text="🎉 Your tutor registration has been approved!")
            except Exception:
                pass
    elif data.startswith("rej_tutor_"):
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ **REJECTED**", parse_mode="Markdown")
    elif data.startswith("app_req_"):
        session_key = data.replace("app_req_", "")
        req_data = context.bot_data.get(session_key)
        if req_data:
            job_code = req_data.get('job_code')
            full_grade = f"{req_data.get('req_grade_cat')} - {req_data.get('req_grade_specific')}"
            db_add_client_request([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                job_code, "", "", req_data.get('req_name'), req_data.get('req_phone'),
                req_data.get('req_subject'), full_grade, req_data.get('req_city'),
                req_data.get('req_specific_loc'), req_data.get('req_budget'),
                "", "", "", "", "Approved", "Open", ""
            ])
            job_channel_id = os.environ.get("JOB_CHANNEL_ID")
            if job_channel_id:
                channel_msg = (
                    f"🟢 **OPEN VACANCY**\n\n"
                    f"🏫 **Grade:** {full_grade}\n"
                    f"🏙️ **City:** {req_data.get('req_city')} ({req_data.get('req_specific_loc')})\n"
                    f"📚 **Subject:** {req_data.get('req_subject')}\n"
                    f"💰 **Budget:** {req_data.get('req_budget')}\n"
                    f"💬 **Job code:** #{job_code}\n\n"
                    f"💡 *Policy: 1-day trial session. Agency commission (50%) applies after successful trial.*"
                )
                bot_username = context.bot.username
                channel_kb = [[InlineKeyboardButton("Apply Now", url=f"https://t.me/{bot_username}?start=apply_{job_code}")]]
                await context.bot.send_message(chat_id=job_channel_id, text=channel_msg, reply_markup=InlineKeyboardMarkup(channel_kb), parse_mode="Markdown")
            await query.edit_message_text(text=query.message.text + "\n\n✅ **APPROVED & POSTED TO CHANNEL**", parse_mode="Markdown")
    elif data.startswith("rej_req_"):
        await query.edit_message_text(text=query.message.text + "\n\n❌ **REJECTED**", parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Operation cancelled. Type /start to begin again.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("Operation cancelled. Type /start to begin again.")
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
            TUTOR_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_name_received)],
            TUTOR_PHONE: [MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, tutor_phone_received)],
            TUTOR_GENDER: [CallbackQueryHandler(tutor_gender_received, pattern="^tgen_")],
            TUTOR_SUBJECT: [CallbackQueryHandler(tutor_subject_received, pattern="^tsubj_")],
            TUTOR_GRADE_CAT: [CallbackQueryHandler(tutor_grade_cat_received, pattern="^tcat_")],
            TUTOR_GRADE_SPECIFIC: [CallbackQueryHandler(tutor_grade_specific_received, pattern="^tgrade_")],
            TUTOR_CITY: [CallbackQueryHandler(tutor_city_received, pattern="^city_")],
            TUTOR_SPECIFIC_LOC: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_specific_loc_received)],
            TUTOR_ID: [MessageHandler(filters.PHOTO & ~filters.COMMAND, tutor_id_received)],
            
            REQ_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_name_received)],
            REQ_PHONE: [MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, req_phone_received)],
            REQ_SUBJECT: [CallbackQueryHandler(req_subject_received, pattern="^rsubj_")],
            REQ_GRADE_CAT: [CallbackQueryHandler(req_grade_cat_received, pattern="^rcat_")],
            REQ_GRADE_SPECIFIC: [CallbackQueryHandler(req_grade_specific_received, pattern="^rgrade_")],
            REQ_CITY: [CallbackQueryHandler(req_city_received, pattern="^reqcity_")],
            REQ_SPECIFIC_LOC: [MessageHandler(filters.TEXT & ~filters.COMMAND, req_specific_loc_received)],
            REQ_BUDGET: [
                CallbackQueryHandler(req_budget_callback, pattern="^rbudget_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, req_budget_text)
            ],
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
