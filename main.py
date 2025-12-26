import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import List, Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, OWNER_ID, EMPLOYEES_CSV

# =========================
# Settings
# =========================
DATA_DIR = "data"
ADMINS_CSV = os.path.join(DATA_DIR, "admins.csv")

MAX_LINKS_PER_USER = 75
LINK_EXPIRE_SECONDS = 5 * 60

REQUEST_COOLDOWN_SECONDS = 60          # link aane ke 1 min baad REQUEST allow
CANCEL_ACTIVE_SECONDS = 5 * 60         # 5 min: Cancel + Expire(Remove) buttons

# =========================
# Global Link Pool (FIFO) with metadata
# each item: {"url": str, "by_id": int, "by_name": str}
# =========================
link_pool: List[Dict[str, Any]] = []

# Per-sender contribution stats (owner/admin)
# sender_stats[user_id] = {"name": str, "added": int, "copied": int, "cancelled": int, "expired": int}
sender_stats: Dict[int, Dict[str, Any]] = {}

# =========================
# Helpers
# =========================
def ensure_dir(path: str):
    if not path:
        return
    os.makedirs(path, exist_ok=True)

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def sheet_date_str(d: date | None = None) -> str:
    if d is None:
        d = date.today()
    return d.strftime("%Y-%m-%d")

def parse_ddmmyy(s: str) -> date:
    return datetime.strptime(s, "%d/%m/%y").date()

def sender_display(user_id: int, fallback_name: str = "") -> str:
    if user_id == OWNER_ID:
        return "OWNER"
    adm = load_admins()
    if user_id in adm:
        return f"ADMIN {adm[user_id]}"
    return fallback_name or "UNKNOWN"

def get_sender_stats(uid: int, name: str):
    if uid not in sender_stats:
        sender_stats[uid] = {"name": name, "added": 0, "copied": 0, "cancelled": 0, "expired": 0}
    else:
        sender_stats[uid]["name"] = name
    return sender_stats[uid]

async def notify_sender(context: ContextTypes.DEFAULT_TYPE, sender_id: int, text: str):
    try:
        await context.bot.send_message(sender_id, text)
    except:
        pass

# =========================
# Daily CSV
# =========================
def daily_csv_path(d: date | None = None) -> str:
    ensure_dir(DATA_DIR)
    return os.path.join(DATA_DIR, f"{sheet_date_str(d)}.csv")

DAILY_HEADERS = [
    "date", "employee_name", "employee_id",
    "link", "status",
    "sent_time", "expiry_time", "done_time", "cancelled_time",
    "expired_time", "note",
    "by_name", "by_id"
]

def ensure_daily_csv(d: date | None = None) -> str:
    path = daily_csv_path(d)
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(DAILY_HEADERS)
    return path

def append_daily_row(employee_name: str, employee_id: int, link: str, status: str,
                     sent_time: str = "", expiry_time: str = "",
                     done_time: str = "", cancelled_time: str = "",
                     expired_time: str = "", note: str = "",
                     by_name: str = "", by_id: int | str = ""):
    path = ensure_daily_csv()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            sheet_date_str(), employee_name, employee_id,
            link, status,
            sent_time, expiry_time, done_time, cancelled_time,
            expired_time, note,
            by_name, by_id
        ])

# =========================
# Employees CSV
# =========================
def ensure_employees_csv():
    ensure_dir(DATA_DIR)
    if not os.path.exists(EMPLOYEES_CSV):
        with open(EMPLOYEES_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "telegram_id", "status"])

def load_employees() -> dict[int, str]:
    ensure_employees_csv()
    employees: dict[int, str] = {}
    try:
        with open(EMPLOYEES_CSV, "r", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                if row.get("status", "active") == "active":
                    employees[int(row["telegram_id"])] = row["name"]
    except:
        pass
    return employees

def save_employees(employees: dict[int, str]):
    ensure_employees_csv()
    with open(EMPLOYEES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "telegram_id", "status"])
        for tid, name in employees.items():
            w.writerow([name, tid, "active"])

# =========================
# Admins CSV
# =========================
def ensure_admins_csv():
    ensure_dir(DATA_DIR)
    if not os.path.exists(ADMINS_CSV):
        with open(ADMINS_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "telegram_id", "status"])

def load_admins() -> dict[int, str]:
    ensure_admins_csv()
    admins: dict[int, str] = {}
    try:
        with open(ADMINS_CSV, "r", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                if row.get("status", "active") == "active":
                    admins[int(row["telegram_id"])] = row["name"]
    except:
        pass
    return admins

def save_admins(admins: dict[int, str]):
    ensure_admins_csv()
    with open(ADMINS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "telegram_id", "status"])
        for tid, name in admins.items():
            w.writerow([name, tid, "active"])

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or (user_id in load_admins())

# =========================
# State
# =========================
@dataclass
class PendingLink:
    url: str
    by_id: int
    by_name: str
    sent_time: datetime
    expiry_time: datetime
    request_after: datetime
    actions_until: datetime

pending_by_user: dict[int, PendingLink] = {}
stats_by_user: dict[int, dict[str, int]] = {}

def get_stats(user_id: int) -> dict[str, int]:
    if user_id not in stats_by_user:
        stats_by_user[user_id] = {"sent": 0, "copied": 0, "cancelled": 0, "expired": 0}
    return stats_by_user[user_id]

def employee_name(user_id: int) -> str:
    return load_employees().get(user_id, "Unknown")

# =========================
# Keyboards
# =========================
def build_employee_keyboard(user_id: int) -> InlineKeyboardMarkup:
    pl = pending_by_user.get(user_id)
    btns: list[list[InlineKeyboardButton]] = []

    if not pl:
        btns.append([InlineKeyboardButton("➡️ REQUEST LINK", callback_data="request_link")])
        return InlineKeyboardMarkup(btns)

    btns.append([InlineKeyboardButton("📋 COPY LINK", callback_data="copy_link")])

    if datetime.now() <= pl.actions_until:
        btns.append([
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_link"),
            InlineKeyboardButton("🗑 Expire/Remove", callback_data="expire_manual"),
        ])
    else:
        btns.append([
            InlineKeyboardButton("❌ Cancel expired", callback_data="noop"),
            InlineKeyboardButton("🗑 Expire expired", callback_data="noop"),
        ])

    if datetime.now() < pl.request_after:
        btns.append([InlineKeyboardButton("➡️ REQUEST (wait 1 min)", callback_data="noop")])
    else:
        btns.append([InlineKeyboardButton("➡️ REQUEST LINK", callback_data="request_link")])

    return InlineKeyboardMarkup(btns)

async def send_employee_panel(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    stats = get_stats(user_id)
    if stats["sent"] >= MAX_LINKS_PER_USER:
        await context.bot.send_message(user_id, "🎉 Congrats! 75 links complete! Kal milte hain 🎯")
        return

    pl = pending_by_user.get(user_id)
    if pl:
        left = int((pl.expiry_time - datetime.now()).total_seconds())
        if left < 0: left = 0
        allow_in = int((pl.request_after - datetime.now()).total_seconds())
        if allow_in < 0: allow_in = 0
        text = (
            f"🔗 Active Link (By {pl.by_name}):
<code>{pl.url}</code>

"
            f"⏳ Expire in: {left}s
"
            f"🕒 REQUEST in: {allow_in}s
"
            f"📊 {stats['sent']}/{MAX_LINKS_PER_USER} | Copied: {stats['copied']}"
        )
    else:
        text = (
            "✅ Ready!
REQUEST LINK dabao

"
            f"📊 {stats['sent']}/{MAX_LINKS_PER_USER}"
        )

    await context.bot.send_message(
        chat_id=user_id,
        text=text,
        reply_markup=build_employee_keyboard(user_id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

# =========================
# Commands
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_daily_csv()
    ensure_employees_csv()
    ensure_admins_csv()

    user_id = update.effective_user.id
    first = update.effective_user.first_name or "User"

    if is_admin(user_id):
        emp = load_employees()
        adm = load_admins()
        await update.message.reply_text(
            f"👑 Admin/Owner Panel

"
            f"👥 Employees: {len(emp)}
"
            f"🛡 Admins: {len(adm)}
"
            f"📦 Pool: {len(link_pool)} links

"
            "📋 Commands:
"
            "/totallinksend - Employees stats
"
            "/contributors - Owner/Admin link stats
"
            "/remove <name>
"
            "/sheet 16/10/25
"
            "/adminlist

"
            "💡 Send HTTP links → POOL!"
        )
        return

    emp = load_employees()
    if user_id in emp:
        await update.message.reply_text("👋 Welcome back!")
        await send_employee_panel(context, user_id)
        return

    # New employee approval -> owner
    keyboard = [[
        InlineKeyboardButton("✅ Accept", callback_data=f"req_emp_accept|{user_id}|{first}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"req_emp_reject|{user_id}|{first}")
    ]]
    await context.bot.send_message(
        chat_id=OWNER_ID,
        text=f"🔔 New EMPLOYEE: {first} (ID: {user_id})",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    await update.message.reply_text("🔄 Owner approval pending...")

async def admin_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    name = user.first_name or "User"

    if is_admin(uid):
        return await update.message.reply_text("✅ Aap already admin/owner ho.")

    kb = [[
        InlineKeyboardButton("✅ Make Admin", callback_data=f"req_admin_accept|{uid}|{name}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"req_admin_reject|{uid}|{name}")
    ]]
    await context.bot.send_message(
        chat_id=OWNER_ID,
        text=f"🆕 Admin request: {name} (ID: {uid})",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    await update.message.reply_text("🔄 Admin request owner ko bhej di. Approval ka wait karo.")

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Owner/Admin only!")

    admins = load_admins()
    text = "👑 Admins List

"
    text += f"OWNER: {OWNER_ID}
"
    for uid, nm in admins.items():
        text += f"- {nm} (ID: {uid})
"
    await update.message.reply_text(text)

async def contributors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Owner/Admin only!")

    # Ensure owner shows even if never added (optional)
    owner_label = "OWNER"
    get_sender_stats(OWNER_ID, owner_label)

    lines = ["📦 CONTRIBUTOR STATS (Owner/Admin)
"]
    # show OWNER first then admins
    order = [OWNER_ID] + [uid for uid in load_admins().keys() if uid != OWNER_ID]

    for uid in order:
        st = sender_stats.get(uid)
        if not st:
            continue
        name = st.get("name", sender_display(uid))
        lines.append(
            f"{name}:
"
            f"  • Links added: {st['added']}
"
            f"  • Copied: {st['copied']}
"
            f"  • Cancelled: {st['cancelled']}
"
            f"  • Expired/Removed: {st['expired']}
"
        )

    await update.message.reply_text("
".join(lines))

async def totallinksend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Owner/Admin only!")

    emp = load_employees()
    text = f"📊 EMPLOYEE STATS
Pool: {len(link_pool)}

"
    for uid, nm in emp.items():
        st = get_stats(uid)
        text += (
            f"👤 {nm}
"
            f"Sent: {st['sent']} | Copied: {st['copied']}
"
            f"Cancel: {st['cancelled']} | Expire: {st['expired']}

"
        )
    await update.message.reply_text(text)

async def remove_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1:
        return await update.message.reply_text("Usage: /remove Irfan")

    target = context.args[0].strip().lower()
    emp = load_employees()
    for uid, nm in list(emp.items()):
        if nm.lower() == target:
            del emp[uid]
            save_employees(emp)
            pending_by_user.pop(uid, None)
            await context.bot.send_message(uid, "❌ Removed")
            return await update.message.reply_text(f"✅ {nm} removed")
    await update.message.reply_text(f"❌ {target} not found")

async def sheet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1:
        return await update.message.reply_text("Usage: /sheet 16/10/25")

    try:
        d = parse_ddmmyy(context.args[0])
        path = os.path.join(DATA_DIR, f"{sheet_date_str(d)}.csv")
        if not os.path.exists(path):
            return await update.message.reply_text(f"❌ No data for {context.args[0]}")

        with open(path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_user.id,
                document=f,
                filename=f"sheet_{context.args[0]}.csv",
                caption=f"📄 {context.args[0]}"
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# =========================
# Owner/Admin link message (POOL)
# =========================
async def owner_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    text = (update.message.text or "").strip()
    if not text.startswith("http"):
        return

    sender_id = update.effective_user.id
    sender_name = sender_display(sender_id, update.effective_user.first_name or "Admin")

    link_pool.append({"url": text, "by_id": sender_id, "by_name": sender_name})

    st = get_sender_stats(sender_id, sender_name)
    st["added"] += 1

    await update.message.reply_text(
        f"✅ Link added to POOL!
"
        f"👤 By: {sender_name}
"
        f"📦 Total: {len(link_pool)}
"
        f"💡 Employees REQUEST karega tab milega!"
    )

# =========================
# Assign Link
# =========================
async def assign_link_to_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, item: Dict[str, Any]):
    name = employee_name(user_id)
    sent_time = datetime.now()
    expiry_time = sent_time + timedelta(seconds=LINK_EXPIRE_SECONDS)

    url = item["url"]
    by_id = int(item["by_id"])
    by_name = str(item["by_name"])

    pending_by_user[user_id] = PendingLink(
        url=url,
        by_id=by_id,
        by_name=by_name,
        sent_time=sent_time,
        expiry_time=expiry_time,
        request_after=sent_time + timedelta(seconds=REQUEST_COOLDOWN_SECONDS),
        actions_until=sent_time + timedelta(seconds=CANCEL_ACTIVE_SECONDS),
    )

    get_stats(user_id)["sent"] += 1

    append_daily_row(
        name, user_id, url, "pending",
        sent_time=now_str(),
        expiry_time=expiry_time.strftime("%Y-%m-%d %H:%M:%S"),
        note="Assigned",
        by_name=by_name,
        by_id=by_id
    )

    # notify owner + sender who contributed the link
    await context.bot.send_message(OWNER_ID, f"✅ {name} → got link (By {by_name})")
    if by_id != OWNER_ID:
        await notify_sender(context, by_id, f"📌 Your link assigned to: {name} (ID {user_id})
{url}")

    await send_employee_panel(context, user_id)

    context.job_queue.run_once(
        expire_job,
        when=LINK_EXPIRE_SECONDS,
        data={"user_id": user_id, "url": url, "by_id": by_id, "by_name": by_name},
    )

# =========================
# Jobs
# =========================
async def expire_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    user_id = data.get("user_id")
    url = data.get("url")
    by_id = int(data.get("by_id"))
    by_name = str(data.get("by_name"))

    pl = pending_by_user.get(user_id)
    if not pl or pl.url != url:
        return

    name = employee_name(user_id)
    st = get_stats(user_id)
    st["expired"] += 1

    # back to pool on timer expire
    link_pool.append({"url": url, "by_id": by_id, "by_name": by_name})

    # contributor stats
    get_sender_stats(by_id, by_name)["expired"] += 1

    append_daily_row(
        name, user_id, url, "expired",
        expired_time=now_str(),
        note="Timer expired → pool",
        by_name=by_name,
        by_id=by_id
    )

    pending_by_user.pop(user_id, None)
    await context.bot.send_message(user_id, f"⌛ Expired! REQUEST new.
By: {by_name}")
    await context.bot.send_message(OWNER_ID, f"♻️ {name} expired (back to pool)
By: {by_name}")
    if by_id != OWNER_ID:
        await notify_sender(context, by_id, f"♻️ Expired: {name} (ID {user_id})
Link back to pool
{url}")

# =========================
# Callbacks
# =========================
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data or ""

    # ---- Owner approvals ----
    if data.startswith("req_emp_") or data.startswith("req_admin_"):
        if not is_owner(user_id):
            return

        parts = data.split("|")
        action, uid_str, name = parts
        target_id = int(uid_str)

        if action == "req_emp_accept":
            emp = load_employees()
            emp[target_id] = name
            save_employees(emp)
            await context.bot.send_message(target_id, "🎉 Approved as EMPLOYEE! /start karo.")
            await query.edit_message_text(f"✅ EMPLOYEE {name} approved")
            await send_employee_panel(context, target_id)
            return

        if action == "req_emp_reject":
            await context.bot.send_message(target_id, "❌ Employee request rejected")
            await query.edit_message_text(f"❌ EMPLOYEE {name} rejected")
            return

        if action == "req_admin_accept":
            admins = load_admins()
            admins[target_id] = name
            save_admins(admins)
            await context.bot.send_message(
                target_id,
                "🎉 Congrats! Ab aap ADMIN ho.
"
                "HTTP links bhejo → POOL
"
                "Commands: /contributors /totallinksend /sheet /remove"
            )
            await query.edit_message_text(f"✅ ADMIN {name} approved")
            return

        if action == "req_admin_reject":
            await context.bot.send_message(target_id, "❌ Admin request rejected")
            await query.edit_message_text(f"❌ ADMIN {name} rejected")
            return

    if data == "noop":
        return

    # employee only callbacks
    if user_id not in load_employees():
        return await context.bot.send_message(user_id, "❌ Not approved employee!")

    pl = pending_by_user.get(user_id)
    name = employee_name(user_id)
    st = get_stats(user_id)

    # REQUEST LINK
    if data == "request_link":
        if pl and datetime.now() < pl.request_after:
            wait = int((pl.request_after - datetime.now()).total_seconds())
            if wait < 0: wait = 0
            return await context.bot.send_message(user_id, f"⏳ Wait {wait}s, then REQUEST again.")

        if len(link_pool) == 0:
            return await context.bot.send_message(user_id, "⏳ No links! Owner/Admin bheje!")

        # if old link active and user requests after cooldown: mark done
        if pl:
            st["copied"] += 1
            get_sender_stats(pl.by_id, pl.by_name)["copied"] += 1

            append_daily_row(
                name, user_id, pl.url, "done",
                done_time=now_str(),
                note="Requested new after cooldown",
                by_name=pl.by_name,
                by_id=pl.by_id
            )
            pending_by_user.pop(user_id, None)

        item = link_pool.pop(0)
        await assign_link_to_user(context, user_id, item)
        return

    # COPY LINK
    if data == "copy_link":
        if not pl:
            return await context.bot.send_message(user_id, "⚠️ No active link!")

        st["copied"] += 1
        get_sender_stats(pl.by_id, pl.by_name)["copied"] += 1

        append_daily_row(
            name, user_id, pl.url, "done",
            done_time=now_str(),
            note="Copied ✅",
            by_name=pl.by_name,
            by_id=pl.by_id
        )

        pending_by_user.pop(user_id, None)

        await query.edit_message_text(
            f"✅ LINK COPIED!
<code>{pl.url}</code>

"
            f"By: {pl.by_name}
"
            f"💚 Total: {st['copied']}/{MAX_LINKS_PER_USER}
"
            f"➡️ 1 minute baad REQUEST available hoga."
        )

        # notify owner + sender
        await context.bot.send_message(OWNER_ID, f"📋 COPIED: {name}
By: {pl.by_name}
{pl.url}")
        if pl.by_id != OWNER_ID:
            await notify_sender(
                context,
                pl.by_id,
                f"✅ COPIED by {name} (ID {user_id})
"
                f"Employee total copied: {st['copied']}/{MAX_LINKS_PER_USER}
"
                f"{pl.url}"
            )

        await send_employee_panel(context, user_id)
        return

    # CANCEL (5 min)
    if data == "cancel_link":
        if not pl or datetime.now() > pl.actions_until:
            return await context.bot.send_message(user_id, "❌ Cancel time out!")

        st["cancelled"] += 1
        get_sender_stats(pl.by_id, pl.by_name)["cancelled"] += 1

        append_daily_row(
            name, user_id, pl.url, "cancelled",
            cancelled_time=now_str(),
            note="Cancelled → pool",
            by_name=pl.by_name,
            by_id=pl.by_id
        )

        # return to pool
        link_pool.append({"url": pl.url, "by_id": pl.by_id, "by_name": pl.by_name})
        pending_by_user.pop(user_id, None)

        await context.bot.send_message(user_id, f"❌ Cancelled. Link pool me wapas.
By: {pl.by_name}")
        await context.bot.send_message(OWNER_ID, f"🔁 CANCEL: {name}
By: {pl.by_name}
{pl.url}")
        if pl.by_id != OWNER_ID:
            await notify_sender(
                context,
                pl.by_id,
                f"🔁 CANCELLED by {name} (ID {user_id})
"
                f"Employee total taken: {st['sent']}/{MAX_LINKS_PER_USER}
"
                f"{pl.url}"
            )

        await send_employee_panel(context, user_id)
        return

    # EXPIRE/REMOVE manually (5 min) -> not returned to pool
    if data == "expire_manual":
        if not pl or datetime.now() > pl.actions_until:
            return await context.bot.send_message(user_id, "❌ Expire time out!")

        st["expired"] += 1
        get_sender_stats(pl.by_id, pl.by_name)["expired"] += 1

        append_daily_row(
            name, user_id, pl.url, "expired",
            expired_time=now_str(),
            note="Manual remove (not returned)",
            by_name=pl.by_name,
            by_id=pl.by_id
        )

        removed_url = pl.url
        by_name = pl.by_name
        by_id = pl.by_id
        pending_by_user.pop(user_id, None)

        await context.bot.send_message(user_id, f"🗑 Removed/Expired.
By: {by_name}")
        await context.bot.send_message(OWNER_ID, f"🗑 EXPIRE: {name}
By: {by_name}
{removed_url}")
        if by_id != OWNER_ID:
            await notify_sender(
                context,
                by_id,
                f"🗑 EXPIRED/REMOVED by {name} (ID {user_id})
"
                f"Employee total taken: {st['sent']}/{MAX_LINKS_PER_USER}
"
                f"{removed_url}"
            )

        await send_employee_panel(context, user_id)
        return

# =========================
# Main
# =========================
def main():
    ensure_daily_csv()
    ensure_employees_csv()
    ensure_admins_csv()
    print("🚀 Bot ready! Owner+Admins with contributor tracking")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("adminamit", admin_request))
    app.add_handler(CommandHandler("adminlist", admin_list))

    app.add_handler(CommandHandler("totallinksend", totallinksend))   # employee stats
    app.add_handler(CommandHandler("contributors", contributors))     # owner/admin stats
    app.add_handler(CommandHandler("remove", remove_employee))
    app.add_handler(CommandHandler("sheet", sheet_cmd))

    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, owner_link_message))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()