from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)
import os
import json


# ENV VARS
BOT_TOKEN = os.getenv("7231687781:AAE8qTH7orpwdwnD0z_gMwCIqn47oe17bcA")
ADMIN_CHAT_ID = os.getenv("6811664913")

# Local Orders
orders = {}

# ==== USER ORDER SEND MESSAGE FORM ====
async def order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text

    # Format Example:
    # /order items=Chips,20rs name=Rohit phone=12345 address=xyz pin=111222
    try:
        data = text.replace("/order ", "")
        data = data.split(" ")
        
        order_data = {}
        for item in data:
            k, v = item.split("=")
            order_data[k] = v

        order_id = str(len(orders) + 1)

        order_data["user_id"] = user_id
        orders[order_id] = order_data

        # Send confirmation msg to USER
        await update.message.reply_text("🛒 Order Placed.\nAdmin Review Pending.")

        # Send details to ADMIN
        keyboard = [
            [InlineKeyboardButton("✔ ACCEPT", callback_data=f"accept_{order_id}")],
            [InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{order_id}")]
        ]

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"🛒 NEW ORDER\n\n"
                f"📦 Item: {order_data['items']}\n"
                f"👤 Name: {order_data['name']}\n"
                f"📱 Phone: {order_data['phone']}\n"
                f"🏠 Address: {order_data['address']}\n"
                f"📮 PIN: {order_data['pin']}"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except:
        await update.message.reply_text(
            "❗ Wrong Format.\n\nUse:\n/order items=Chips name=Ram phone=9876 address=City pin=100102"
        )


# ==== CALLBACK HANDLING ====
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, order_id = query.data.split("_")
    order = orders.get(order_id)

    if not order:
        await query.answer("Order not found.")
        return

    # Reject
    if action == "reject":
        await context.bot.send_message(
            chat_id=order["user_id"],
            text="❌ Your order is rejected."
        )
        await query.edit_message_text("🚫 ORDER REJECTED")
        return

    # Accept
    if action == "accept":
        await context.bot.send_message(
            chat_id=order["user_id"],
            text="✔ Your order is accepted.\n⌛ Delivery in about 40 minutes."
        )

        keyboard = [
            [InlineKeyboardButton("⏳ Delay", callback_data=f"delay_{order_id}")],
            [InlineKeyboardButton("🚚 Delivered", callback_data=f"delivered_{order_id}")]
        ]

        await query.edit_message_text(
            "📦 ORDER ACCEPTED",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Delay
    if action == "delay":
        await context.bot.send_message(
            chat_id=order["user_id"],
            text="⏳ Sorry sir, your delivery may be delayed. Our team will contact you."
        )
        await query.edit_message_text("⚠ Delivery Delayed")
        return

    # Delivered
    if action == "delivered":
        await context.bot.send_message(
            chat_id=order["user_id"],
            text="🎉 Your Order is Successfully Delivered.\nThanks for shopping 🙏"
        )
        await query.edit_message_text("🎉 ORDER COMPLETED")
        return


# ==== START ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 BOT ACTIVE!")


# ==== MAIN RUN ====
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("order", order))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("Bot Running...")
    app.run_polling()


if __name__ == "__main__":
    main()

