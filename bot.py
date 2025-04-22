import os
import re
import json
import threading
import asyncio
import time
from collections import defaultdict
from dotenv import load_dotenv
from telegram import (
    Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode

from hyperliquid.info import Info
from hyperliquid.utils import constants
import websocket

# Load environment variables
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

info = Info(constants.MAINNET_API_URL, skip_ws=True)

user_wallets = defaultdict(list)
wallet_to_user = {}
nickname_map = {}
MAX_WALLETS_PER_USER = 2

ws_connection = None
bot_app = None
bot_loop = None
recent_order_coins = {}
last_spot_balance = {}
last_perp_balance = {}

spot_id_to_base_token_name = {}
try:
    spot_meta, asset_ctxs = info.spot_meta_and_asset_ctxs()
    for entry in spot_meta["universe"]:
        spot_id = entry["index"]
        base_token_index = entry["tokens"][0]
        base_token_name = spot_meta["tokens"][base_token_index]["name"]
        spot_id_to_base_token_name[spot_id] = base_token_name
except Exception as e:
    print(f"❌ Failed to load spot asset map: {e}")

# --- Telegram Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 SuperX | Hyperliquid Wallet Tracker\n\n"
        "Monitor Hyperliquid wallets. Send the /add command to track and receive notification for wallet activity.\n\n"
        "Official SuperX channel: @trysuperx"
    )

async def add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="back")]])
    await update.message.reply_text(
        "To track a wallet, respond to this message with each wallet address on a new line. "
        "If you'd like to assign a nickname (40 characters max), include a comma after the address. For example:\n\n"
        "WalletAddress1, Name1\n"
        "WalletAddress2, Name2\n\n"
        "There's is a current wallet limit of 2.",
        reply_markup=keyboard
    )

async def show_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    wallets = user_wallets.get(user_id, [])
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="back")]])

    if not wallets:
        empty_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Wallet", callback_data="add_wallet")],
            [InlineKeyboardButton("← Back", callback_data="back")]
        ])
        await update.message.reply_text(
            "No wallets to show. Please press the Add button below.",
            reply_markup=empty_keyboard
        )
        return

    message_lines = [f"Total wallets: {len(wallets)} / {MAX_WALLETS_PER_USER}"]
    message_lines.append("✅ - Wallet is active\n")

    for address, nickname in wallets:
        label = f"{nickname} ({address})" if nickname else address
        message_lines.append(f"✅ {label}")

    await update.message.reply_text("\n".join(message_lines), reply_markup=keyboard)

async def remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    wallets = user_wallets.get(user_id, [])

    if not wallets:
        await update.message.reply_text("❌ You don't have any wallets to remove.")
        return

    buttons = [
        [InlineKeyboardButton(
            f"{nickname_map.get(addr.lower(), addr)}", callback_data=f"remove:{addr}"
        )]
        for addr, _ in wallets
    ]
    buttons.append([InlineKeyboardButton("← Back", callback_data="back")])
    keyboard = InlineKeyboardMarkup(buttons)

    lines = ["Saved Wallets"]
    for addr, _ in wallets:
        label = nickname_map.get(addr.lower(), addr)
        lines.append(f"✅ {label}")

    await update.message.reply_text("\n".join(lines), reply_markup=keyboard)

async def handle_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("remove:"):
        return

    address = query.data.split("remove:")[1]
    user_id = query.from_user.id

    current = user_wallets.get(user_id, [])
    updated = [entry for entry in current if entry[0] != address]
    user_wallets[user_id] = updated

    wallet_to_user.pop(address.lower(), None)
    nickname_map.pop(address.lower(), None)

    await query.edit_message_text("Wallet removed successfully.")

async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    wallets = user_wallets.get(user_id)

    if not wallets:
        await update.message.reply_text("❗ You have no wallets saved. Use /add to register one.")
        return

    buttons = [
        [InlineKeyboardButton(
            f"{nickname_map.get(addr.lower(), addr)}", callback_data=f"positions:{addr}"
        )]
        for addr, _ in wallets
    ]
    buttons.append([InlineKeyboardButton("← Back", callback_data="back")])
    keyboard = InlineKeyboardMarkup(buttons)

    await update.message.reply_text(
        "Please select the wallet you would like to view the positions of:",
        reply_markup=keyboard
    )

async def handle_positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("positions:"):
        return

    address = query.data.split("positions:")[1]
    user_id = query.from_user.id
    nickname = nickname_map.get(address.lower(), None)

    try:
        perp_data = info.user_state(address)
        spot_data = info.spot_user_state(address)
    except Exception as e:
        await query.edit_message_text(f"⚠️ Error fetching data for {address}: {e}")
        return

    messages = [f"*Wallet:* {nickname or address}"]
    account_value = perp_data.get("marginSummary", {}).get("accountValue", None)
    if account_value:
        messages.append(f"💵 *Perp USDC Account Value:* ${float(account_value):,.2f}")

    perp_positions = perp_data.get("assetPositions", [])
    if not perp_positions:
        messages.append("\n📭 No open perpetual positions found.")
    else:
        messages.append("\n📈 *Perpetual Positions:*")
        for pos in perp_positions:
            p = pos["position"]
            coin = p.get("coin", "Unknown")
            size = float(p.get("szi", "0"))
            side = "LONG" if size > 0 else "SHORT"
            entry = float(p.get("entryPx", "0"))
            liq = p.get("liquidationPx", "N/A")
            unrealized = float(p.get("unrealizedPnl", "0"))
            roe = float(p.get("returnOnEquity", "0"))
            messages.append(
                f"- {coin}: {side} {abs(size)} @ {entry} | PnL: {unrealized} | ROE: {roe:.2%} | Liq: {liq}"
            )

    balances = spot_data.get("balances", [])
    nonzero_spot = [b for b in balances if float(b.get("total", 0)) > 0]
    if not nonzero_spot:
        messages.append("\n📭 No spot balances found.")
    else:
        messages.append("\n💰 *Spot Balances:*")
        for b in nonzero_spot:
            coin = b.get("coin", "Unknown")
            total = b.get("total", "0")
            value = b.get("entryNtl", None)
            if value:
                messages.append(f"- {coin}: {total} (≈ ${float(value):,.2f})")
            else:
                messages.append(f"- {coin}: {total}")

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="back")]])
    await query.edit_message_text("\n".join(messages), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def handle_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("⬅️ Back to main menu.")

async def handle_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lines = update.message.text.strip().splitlines()
    current_wallets = user_wallets.get(user_id, [])

    if len(current_wallets) + len(lines) > MAX_WALLETS_PER_USER:
        await update.message.reply_text(f"❌ You can only track up to {MAX_WALLETS_PER_USER} wallets.")
        return

    new_wallets = []

    for line in lines:
        parts = [part.strip() for part in line.split(",", 1)]
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", parts[0]):
            await update.message.reply_text(f"❌ Invalid wallet address: {parts[0]}")
            continue

        address = parts[0]
        nickname = parts[1] if len(parts) > 1 else None
        if nickname and len(nickname) > 40:
            await update.message.reply_text(f"❌ Nickname too long for {address}.")
            continue

        new_wallets.append((address, nickname))
        wallet_to_user[address.lower()] = user_id
        if nickname:
            nickname_map[address.lower()] = nickname

        if ws_connection and ws_connection.sock and ws_connection.sock.connected:
            for channel in ["orderUpdates", "userEvents", "userFills"]:
                sub_msg = {
                    "method": "subscribe",
                    "subscription": {
                        "type": channel,
                        "user": address
                    }
                }
                ws_connection.send(json.dumps(sub_msg))
                print(f"📡 Subscribed to {channel} for {address}")

    user_wallets[user_id].extend(new_wallets)
    confirmed = [f"{addr} ({name})" if name else addr for addr, name in new_wallets]
    await update.message.reply_text("✅ Wallets saved:\n" + "\n".join(confirmed))

# --- WebSocket Handlers ---

def on_message(ws, message):
    try:
        data = json.loads(message)
        channel = data.get("channel")

        if channel == "userFills":
            handle_user_fills(data)
        elif channel == "orderUpdates":
            handle_order_updates(data)
        elif channel == "userEvents":
            print(f"\n🔔 [userEvents] Event:\n{json.dumps(data, indent=2)}")
        elif channel == "subscriptionResponse":
            print("✅ Subscription acknowledged.")
        else:
            print(f"📨 [Unhandled channel: {channel}]\n{json.dumps(data, indent=2)}")
    except Exception as e:
        print(f"⚠️ Failed to parse WebSocket message: {e}")

def handle_order_updates(data):
    updates = data.get("data", [])
    for update in updates:
        order = update.get("order", {})
        oid = order.get("oid")
        coin = order.get("coin")
        if oid and coin:
            recent_order_coins[oid] = coin
    print(f"\n📦 [orderUpdates]: {json.dumps(data, indent=2)}")

def handle_user_fills(data):
    fills_data = data.get("data", {})
    fills = fills_data.get("fills", [])
    wallet = fills_data.get("user", "").lower()
    is_snapshot = fills_data.get("isSnapshot", False)

    if is_snapshot or wallet not in wallet_to_user:
        return

    user_id = wallet_to_user[wallet]
    nickname = nickname_map.get(wallet)

    for fill in fills:
        side = "Buy" if fill["side"] == "B" else "Sell"
        raw_coin = fill["coin"]
        fee_token = fill["feeToken"]
        price = fill["px"]
        size = fill["sz"]
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(fill["time"] / 1000))

        if raw_coin.startswith("@"):
            spot_id = int(raw_coin[1:])
            resolved_coin = fee_token if side == "Buy" else spot_id_to_base_token_name.get(spot_id, raw_coin)
        else:
            resolved_coin = raw_coin

        wallet_label = f"{nickname or wallet}"
        alert = (
            f"📣 {side} - {resolved_coin} ({wallet_label})\n"
            f"{side} {size} @ {price}\n"
            f"🕒 {timestamp}"
        )

        if bot_app and bot_loop:
            asyncio.run_coroutine_threadsafe(
                bot_app.bot.send_message(chat_id=user_id, text=alert, parse_mode=ParseMode.MARKDOWN),
                bot_loop
            )

def on_open(ws):
    print("✅ WebSocket connection established.")

def run_ws():
    global ws_connection
    ws_connection = websocket.WebSocketApp(
        "wss://api.hyperliquid.xyz/ws",
        on_open=on_open,
        on_message=on_message
    )
    ws_connection.run_forever()

# --- Main ---

def main():
    global bot_app, bot_loop

    ws_thread = threading.Thread(target=run_ws, daemon=True)
    ws_thread.start()

    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    bot_app = ApplicationBuilder().token(TOKEN).build()

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("positions", positions))
    bot_app.add_handler(CommandHandler("add", add_wallet))
    bot_app.add_handler(CommandHandler("show", show_wallets))
    bot_app.add_handler(CommandHandler("remove", remove_wallet))
    bot_app.add_handler(CallbackQueryHandler(handle_remove_callback, pattern="^remove:"))
    bot_app.add_handler(CallbackQueryHandler(handle_positions_callback, pattern="^positions:"))
    bot_app.add_handler(CallbackQueryHandler(handle_back_callback, pattern="^back$"))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wallet))

    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("positions", "View wallet positions"),
        BotCommand("add", "Track a wallet"),
        BotCommand("show", "Show tracked wallets"),
        BotCommand("remove", "Remove a tracked wallet"),
    ]
    bot_loop.run_until_complete(bot_app.bot.set_my_commands(commands))

    print("🤖 Telegram bot is running...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
