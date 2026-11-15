"""DriveTelegramBot: send files to Telegram bot -> saved to a Google Drive folder.

Setup:
  pip install -r requirements.txt
  env BOT_TOKEN=<from @BotFather>
  credentials.json = Google OAuth client (Desktop app), next to bot.py
  python bot.py   (first run opens browser for Drive login -> token.json)

Flow:
  - send 1+ files (albums / many at once ok) -> bot asks name for each (/skip, /skipall = default names)
  - after upload: button to rename the Drive file
  - status: /status, "📊 الحالة" button
  - /newfolder <name> creates + switches folder, /folder shows link
Telegram bot download limit: 20MB per file.
"""
import io, os, re, datetime, mimetypes
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

BOT_TOKEN = os.environ["BOT_TOKEN"]
DEFAULT_FOLDER = os.environ.get("FOLDER_NAME", "Telegram Uploads")
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
ID_FILE = "folder_id.txt"
STATUS_BTN = "📊 الحالة"


# ---------- Drive ----------
def drive_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES).run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


drive = drive_service()


def make_folder(name):
    r = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"}, fields="id"
    ).execute()
    with open(ID_FILE, "w") as f:
        f.write(r["id"])
    return r["id"]


def folder_id():
    if os.path.exists(ID_FILE):
        return open(ID_FILE).read().strip()
    return make_folder(DEFAULT_FOLDER)


def folder_url():
    return f"https://drive.google.com/drive/folders/{folder_id()}"


def clean(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def with_ext(name, ext):
    name = clean(name)
    return name if (not ext or name.lower().endswith(ext.lower())) else name + ext


# ---------- UI helpers ----------
def kb_after_upload(file_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ غيّر الاسم", callback_data=f"ren:{file_id}"),
        InlineKeyboardButton(STATUS_BTN, callback_data="status"),
    ]])


def kb_status():
    return InlineKeyboardMarkup([[InlineKeyboardButton(STATUS_BTN, callback_data="status")]])


def status_text(ud):
    q = ud.get("queue", [])
    up = ud.get("uploaded", [])
    lines = [f"⏳ بالانتظار: {len(q)}", f"✅ انرفع: {len(up)}"]
    if q:
        lines.append(f"التالي: {q[0]['label']}")
    if up:
        lines.append(f"آخر ملف: {up[-1]['name']}")
    lines.append(f"📁 {folder_url()}")
    return "\n".join(lines)


async def ask_next(ctx, chat_id, ud):
    q = ud.get("queue", [])
    if not q:
        return
    await ctx.bot.send_message(
        chat_id,
        f"شو الاسم يلي بدك ياه للملف؟\n«{q[0]['label']}» (بالانتظار: {len(q)})\n"
        "/skip = اسم افتراضي · /skipall = الكل افتراضي",
        reply_markup=kb_status(),
    )


# ---------- upload ----------
async def upload_item(ctx, chat_id, ud, item, chosen_name):
    tg_file = await ctx.bot.get_file(item["file_id"])
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    buf.seek(0)
    name = with_ext(chosen_name, item["ext"]) if chosen_name else item["default_name"]
    media = MediaIoBaseUpload(buf, mimetype=item["mime"], resumable=True)
    r = drive.files().create(
        body={"name": name, "parents": [folder_id()]}, media_body=media, fields="id"
    ).execute()
    ud.setdefault("uploaded", []).append({"id": r["id"], "name": name})
    await ctx.bot.send_message(chat_id, f"✅ انرفع: {name}", reply_markup=kb_after_upload(r["id"]))


# ---------- handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ابعتلي أي ملف (أو كذا ملف سوا) وبرفعو على Drive.",
        reply_markup=ReplyKeyboardMarkup([[STATUS_BTN]], resize_keyboard=True),
    )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(status_text(ctx.user_data))


async def newfolder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = " ".join(ctx.args) or DEFAULT_FOLDER
    fid = make_folder(name)
    await update.message.reply_text(f"📁 {name}\nhttps://drive.google.com/drive/folders/{fid}")


async def folder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(folder_url())


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    ud = ctx.user_data
    att = m.effective_attachment
    if isinstance(att, (list, tuple)):  # photo -> biggest size
        att = att[-1]
    if not hasattr(att, "file_id"):
        return

    mime = getattr(att, "mime_type", None)
    orig = getattr(att, "file_name", None)
    if m.photo:
        mime, ext, kind = "image/jpeg", ".jpg", "صورة"
    elif m.voice:
        mime, ext, kind = mime or "audio/ogg", ".ogg", "فويس"
    elif m.video_note:
        mime, ext, kind = "video/mp4", ".mp4", "فيديو"
    else:
        ext = (os.path.splitext(orig)[1] if orig else "") or mimetypes.guess_extension(mime or "") or ""
        kind = "ملف"
    mime = mime or "application/octet-stream"

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    u = m.from_user
    base = os.path.splitext(orig)[0] if orig else "file"
    item = {
        "file_id": att.file_id,
        "mime": mime,
        "ext": ext,
        "label": orig or f"{kind} {m.message_id}",
        "default_name": clean(f"{ts}_{u.username or u.id}_{m.message_id}_{base}{ext}"),
    }
    queue = ud.setdefault("queue", [])
    was_empty = not queue
    queue.append(item)

    if was_empty and not ud.get("renaming"):
        await ask_next(ctx, m.chat_id, ud)
    elif not m.media_group_id or ud.get("last_group") != m.media_group_id:
        ud["last_group"] = m.media_group_id
        await m.reply_text(f"➕ انضاف للطابور (بالانتظار: {len(queue)})", reply_markup=kb_status())


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    chat_id = update.message.chat_id
    text = update.message.text

    # rename an already-uploaded file
    if ud.get("renaming"):
        fid = ud.pop("renaming")
        cur = drive.files().get(fileId=fid, fields="name").execute()["name"]
        new = with_ext(text, os.path.splitext(cur)[1])
        drive.files().update(fileId=fid, body={"name": new}).execute()
        for x in ud.get("uploaded", []):
            if x["id"] == fid:
                x["name"] = new
        await update.message.reply_text(f"✏️ الاسم الجديد: {new}", reply_markup=kb_after_upload(fid))
        await ask_next(ctx, chat_id, ud)
        return

    # name for the next queued file
    q = ud.get("queue", [])
    if not q:
        return
    item = q.pop(0)
    await upload_item(ctx, chat_id, ud, item, text)
    await ask_next(ctx, chat_id, ud)


async def skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    q = ud.get("queue", [])
    if not q:
        return
    await upload_item(ctx, update.message.chat_id, ud, q.pop(0), None)
    await ask_next(ctx, update.message.chat_id, ud)


async def skipall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    q = ud.get("queue", [])
    while q:
        await upload_item(ctx, update.message.chat_id, ud, q.pop(0), None)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if cq.data == "status":
        await cq.message.reply_text(status_text(ctx.user_data))
    elif cq.data.startswith("ren:"):
        ctx.user_data["renaming"] = cq.data[4:]
        await cq.message.reply_text("شو الاسم الجديد للملف؟")


app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("status", status))
app.add_handler(CommandHandler("newfolder", newfolder))
app.add_handler(CommandHandler("folder", folder))
app.add_handler(CommandHandler("skip", skip))
app.add_handler(CommandHandler("skipall", skipall))
app.add_handler(CallbackQueryHandler(on_button))
app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(STATUS_BTN)}$"), status))
app.add_handler(MessageHandler(filters.ATTACHMENT, on_file))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
app.run_polling()
