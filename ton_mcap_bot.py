import os
import sqlite3
import requests
from datetime import datetime
from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = "8317423103:AAFo0xO2vqVQnv0_wU6xyfQiVRh3_9vdx5w"
CHANNEL_USERNAME = "@TWinXposT"

MCAP_MIN = float(os.getenv("MCAP_MIN", "10000"))
MCAP_MAX = float(os.getenv("MCAP_MAX", "20000"))
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))

# Referral bot trade
STONKS_REF_CODE = "phreak3r044"
STONKS_TRADE_BOT = "stonks_sniper_bot"

DTRADE_REF_CODE = "26UsbJoMqw"
DTRADE_BOT = "dtrade"

DB_NAME = "ton_tokens.db"

DEX_LATEST = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_PAIRS = "https://api.dexscreener.com/token-pairs/v1/ton/{}"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS posted_tokens (
            token_address TEXT PRIMARY KEY,
            symbol TEXT,
            name TEXT,
            market_cap REAL,
            posted_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def already_posted(token_address):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT token_address FROM posted_tokens WHERE token_address = ?",
        (token_address,)
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def save_posted(token_address, symbol, name, market_cap):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO posted_tokens
        VALUES (?, ?, ?, ?, ?)
    """, (
        token_address,
        symbol,
        name,
        market_cap,
        datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()


def money(value):
    try:
        return f"${float(value):,.0f}"
    except:
        return "-"


def make_stonks_link(contract):
    return f"https://t.me/{STONKS_TRADE_BOT}?start={STONKS_REF_CODE}_{contract}"


def make_dtrade_link(contract):
    return f"https://t.me/{DTRADE_BOT}?start={DTRADE_REF_CODE}"


def get_latest_ton_tokens():
    response = requests.get(DEX_LATEST, timeout=20)
    response.raise_for_status()
    data = response.json()

    return [
        item for item in data
        if item.get("chainId") == "ton" and item.get("tokenAddress")
    ]


def get_best_pair(token_address):
    response = requests.get(DEX_PAIRS.format(token_address), timeout=20)
    response.raise_for_status()

    pairs = response.json()

    if not pairs:
        return None

    ton_pairs = [
        p for p in pairs
        if p.get("chainId") == "ton"
    ]

    if not ton_pairs:
        return None

    return max(
        ton_pairs,
        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0)
    )


def is_valid_pair(pair):
    market_cap = pair.get("marketCap") or pair.get("fdv") or 0

    try:
        market_cap = float(market_cap)
    except:
        return False

    return MCAP_MIN <= market_cap <= MCAP_MAX


def build_post(pair):
    base = pair.get("baseToken") or {}

    name = base.get("name", "Unknown")
    symbol = base.get("symbol", "UNKNOWN")
    contract = base.get("address", "")

    market_cap = pair.get("marketCap") or pair.get("fdv") or 0
    price = pair.get("priceUsd", "-")
    liquidity = (pair.get("liquidity") or {}).get("usd", 0)
    volume_24h = (pair.get("volume") or {}).get("h24", 0)

    buys = ((pair.get("txns") or {}).get("h24") or {}).get("buys", 0)
    sells = ((pair.get("txns") or {}).get("h24") or {}).get("sells", 0)

    change_1h = (pair.get("priceChange") or {}).get("h1", 0)
    change_24h = (pair.get("priceChange") or {}).get("h24", 0)

    chart_url = pair.get("url")
    stonks_trade_url = make_stonks_link(contract)
    dtrade_url = make_dtrade_link(contract)

    info = pair.get("info") or {}
    image_url = info.get("imageUrl")

    text = f"""
🚀 <b>TwinXposT GEMS</b>

💎 <b>{name}</b> ${symbol}
🌐 Network: <b>TON</b>

💰 Market Cap: <b>{money(market_cap)}</b>
💵 Price: <code>${price}</code>
💧 Liquidity: <b>{money(liquidity)}</b>
📊 Volume 24H: <b>{money(volume_24h)}</b>

📈 1H: <b>{change_1h}%</b>
📈 24H: <b>{change_24h}%</b>

🟢 Buys 24H: <b>{buys}</b>
🔴 Sells 24H: <b>{sells}</b>

📄 Contract:
<code>{contract}</code>

⚠️ <b>DYOR. Not financial advice.</b>
""".strip()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Stonks Trade", url=stonks_trade_url),
            InlineKeyboardButton("⚡ DTrade", url=dtrade_url)
        ],
        [
            InlineKeyboardButton("📊 Chart", url=chart_url)
        ],
        [
            InlineKeyboardButton("💬 Channel", url="https://t.me/TWinXposT")
        ]
    ])

    return text, keyboard, contract, symbol, name, float(market_cap), image_url


async def scan_and_post(context: ContextTypes.DEFAULT_TYPE):
    print("Scanning TON tokens...")

    try:
        tokens = get_latest_ton_tokens()

        for token in tokens:
            token_address = token.get("tokenAddress")

            if already_posted(token_address):
                continue

            pair = get_best_pair(token_address)

            if not pair:
                continue

            if not is_valid_pair(pair):
                continue

            text, keyboard, contract, symbol, name, market_cap, image_url = build_post(pair)

            if image_url:
                await context.bot.send_photo(
                    chat_id=CHANNEL_USERNAME,
                    photo=image_url,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard
                )
            else:
                await context.bot.send_message(
                    chat_id=CHANNEL_USERNAME,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=False
                )

            save_posted(contract, symbol, name, market_cap)
            print(f"Posted: {name} ${symbol} - {money(market_cap)}")

    except Exception as e:
        print("ERROR:", e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ TON MCAP Bot aktif.\n\n"
        f"MCAP Range: {money(MCAP_MIN)} - {money(MCAP_MAX)}\n"
        f"Scan tiap: {SCAN_INTERVAL_MINUTES} menit\n\n"
        "Command:\n"
        "/scan - scan manual\n"
        "/stats - total token posted"
    )


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scan manual dimulai...")
    await scan_and_post(context)
    await update.message.reply_text("✅ Scan selesai.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM posted_tokens")
    total = cur.fetchone()[0]
    conn.close()

    await update.message.reply_text(f"📊 Total token posted: {total}")


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))

    app.job_queue.run_repeating(
        scan_and_post,
        interval=SCAN_INTERVAL_MINUTES * 60,
        first=10
    )

    print("TON MCAP bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()