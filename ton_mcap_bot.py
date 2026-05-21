import os
import sys
import sqlite3
import requests
import asyncio
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = "8317423103:AAEcqWbC_I0SuMeLTx46Wql_L8pbxh5jkRk"
CHANNEL_USERNAME = "@TWinXposT"

MCAP_MIN = float(os.getenv("MCAP_MIN", "5000"))
MCAP_MAX = float(os.getenv("MCAP_MAX", "20000"))
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))

# Referral bot trade
STONKS_REF_CODE = "phreak3r044"
STONKS_TRADE_BOT = "stonks_sniper_bot"

DTRADE_REF_CODE = "26UsbJoMqw"
DTRADE_BOT = "dtrade"

DB_NAME = "ton_tokens.db"

DEX_PAIRS = "https://api.dexscreener.com/token-pairs/v1/ton/{}"
GECKO_POOLS = "https://api.geckoterminal.com/api/v2/networks/ton/pools"
GECKO_HEADERS = {"Accept": "application/json;version=20230302"}


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS posted_tokens (
            token_address TEXT PRIMARY KEY,
            symbol TEXT,
            name TEXT,
            market_cap REAL,
            posted_at TEXT,
            message_id INTEGER,
            max_mcap REAL,
            last_updated TEXT
        )
    """)
    # Safely migrate existing databases
    try:
        cur.execute("ALTER TABLE posted_tokens ADD COLUMN message_id INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE posted_tokens ADD COLUMN max_mcap REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE posted_tokens ADD COLUMN last_updated TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def already_posted(token_address, name=None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    if name:
        cur.execute(
            "SELECT token_address FROM posted_tokens WHERE token_address = ? OR LOWER(name) = LOWER(?)",
            (token_address, name)
        )
    else:
        cur.execute(
            "SELECT token_address FROM posted_tokens WHERE token_address = ?",
            (token_address,)
        )
    row = cur.fetchone()
    conn.close()
    return row is not None


def save_posted(token_address, symbol, name, market_cap, message_id=None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO posted_tokens (token_address, symbol, name, market_cap, posted_at, message_id, max_mcap, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        token_address,
        symbol,
        name,
        market_cap,
        datetime.utcnow().isoformat(),
        message_id,
        market_cap,
        datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()


def check_token_security(token_address):
    url = f"https://tonapi.io/v2/jettons/{token_address}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            
            # 1. Whitelisted is safe
            verification = data.get("verification", "none")
            if verification == "whitelist":
                return True
                
            # 2. Blacklisted or marked scam is unsafe
            if verification == "blacklist":
                print(f"Skipping {token_address}: Blacklisted token")
                return False
                
            if data.get("admin", {}).get("is_scam", False):
                print(f"Skipping {token_address}: Admin account is marked as scam")
                return False
                
            # 3. Custom/Modified contract check
            # Standard Jetton master code hashes:
            standard_hashes = {
                "mg+Y3W+/Il7vgWXk5kQX7pMffuoABlNDnntdzcBkTNY=",
                "ci03vlGO4NS2cUB3cn7WByS/N56PFHnCILhT0a888A0="
            }
            code_hash = data.get("code_hash")
            if code_hash not in standard_hashes:
                print(f"Skipping {token_address}: Modified contract ({code_hash}), proceed with caution!")
                return False
                
            return True
            
        elif response.status_code == 404:
            print(f"Skipping {token_address}: Not found on TonAPI")
            return False
            
    except Exception as e:
        print(f"Security check error for {token_address}: {e}")
        return True # Fallback to True to avoid complete blocking on transient network errors


def money(value):
    try:
        return f"${float(value):,.0f}"
    except:
        return "-"


def make_stonks_link(contract):
    return f"https://t.me/stonks_sniper_bot?start=id=phreak3r044={contract}"


def make_dtrade_link(contract):
    return f"https://t.me/dtrade?start=26UsbJoMqw_{contract}"


def get_active_ton_pairs():
    """Fetch active TON pools from GeckoTerminal in the MCAP range,
    then enrich each with DexScreener pair data for chart URL and image.
    Scans up to 10 pages since pools are sorted by volume (not MCAP).
    Falls back to DexScreener marketCap/fdv when GeckoTerminal MCAP is null."""
    result = []
    seen_addresses = set()

    for page in range(1, 11):  # Check up to 10 pages (pools sorted by volume, not MCAP)
        try:
            r = requests.get(
                GECKO_POOLS,
                params={"page": page},
                headers=GECKO_HEADERS,
                timeout=20
            )
            r.raise_for_status()
            pools = r.json().get("data", [])
        except Exception as e:
            print(f"GeckoTerminal page {page} error: {e}")
            break

        if not pools:
            break

        for pool in pools:
            attrs = pool.get("attributes", {})
            rels = pool.get("relationships", {})

            # Extract base token address from relationship id: "ton_<address>"
            base_token_id = (rels.get("base_token", {}).get("data") or {}).get("id", "")
            token_address = base_token_id.replace("ton_", "", 1) if base_token_id.startswith("ton_") else ""

            if not token_address or token_address in seen_addresses:
                continue

            # Try GeckoTerminal MCAP first
            mcap_str = attrs.get("market_cap_usd")
            gecko_mcap = None
            try:
                gecko_mcap = float(mcap_str) if mcap_str else None
            except:
                gecko_mcap = None

            # If GeckoTerminal MCAP is available and clearly out of range, skip early
            if gecko_mcap is not None:
                if gecko_mcap < MCAP_MIN or gecko_mcap > MCAP_MAX:
                    continue

            # Enrich with DexScreener pair data for chart URL, image, txns, and accurate MCAP
            try:
                r2 = requests.get(DEX_PAIRS.format(token_address), timeout=15)
                pairs = r2.json() if r2.status_code == 200 else []
            except:
                pairs = []

            if not pairs:
                continue

            ton_pairs = [p for p in pairs if p.get("chainId") == "ton"]
            if not ton_pairs:
                continue

            best_pair = max(ton_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))

            # Use DexScreener marketCap/fdv as the authoritative MCAP value
            dex_mcap = best_pair.get("marketCap") or best_pair.get("fdv") or 0
            try:
                dex_mcap = float(dex_mcap)
            except:
                dex_mcap = 0

            # Final MCAP check using DexScreener (more reliable)
            if dex_mcap == 0:
                # If DexScreener has no MCAP either, only include if GeckoTerminal confirmed range
                if gecko_mcap is None:
                    continue
            else:
                if dex_mcap < MCAP_MIN or dex_mcap > MCAP_MAX:
                    continue

            seen_addresses.add(token_address)
            result.append(best_pair)

    print(f"Found {len(result)} TON pairs in MCAP range ${MCAP_MIN:,.0f}-${MCAP_MAX:,.0f}")
    return result


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
            InlineKeyboardButton("💬 Marketing", url="https://t.me/Phreak3r044")
        ]
    ])

    return text, keyboard, contract, symbol, name, float(market_cap), image_url


async def update_pnl(context: ContextTypes.DEFAULT_TYPE):
    print("Updating PnL of posted tokens...")
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        # Fetch tokens posted in the last 24 hours that have a message_id
        one_day_ago = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        cur.execute("""
            SELECT token_address, symbol, name, market_cap, message_id, max_mcap
            FROM posted_tokens
            WHERE posted_at >= ? AND message_id IS NOT NULL
        """, (one_day_ago,))
        rows = cur.fetchall()
        conn.close()

        for row in rows:
            token_address, symbol, name, initial_mcap, message_id, max_mcap = row

            pair = get_best_pair(token_address)
            await asyncio.sleep(1) # Add delay to avoid rate limiting

            if not pair:
                continue

            current_mcap = pair.get("marketCap") or pair.get("fdv") or 0
            try:
                current_mcap = float(current_mcap)
            except:
                continue

            if current_mcap <= 0:
                continue

            # Calculate PnL
            pnl_percent = ((current_mcap - initial_mcap) / initial_mcap) * 100

            # Check and update peak/max mcap
            new_max_mcap = max(max_mcap or 0, current_mcap)
            peak_pnl_percent = ((new_max_mcap - initial_mcap) / initial_mcap) * 100

            # Update database
            conn = sqlite3.connect(DB_NAME)
            cur = conn.cursor()
            cur.execute("""
                UPDATE posted_tokens
                SET max_mcap = ?, last_updated = ?
                WHERE token_address = ?
            """, (new_max_mcap, datetime.utcnow().isoformat(), token_address))
            conn.commit()
            conn.close()

            # Rebuild original post text
            text, keyboard, contract, symbol, name, _, image_url = build_post(pair)

            # Formulate PnL block
            pnl_emoji = "📈" if pnl_percent >= 0 else "📉"
            pnl_section = (
                f"📊 Current Mcap: <b>{money(current_mcap)}</b>\n"
                f"{pnl_emoji} <b>PnL: {pnl_percent:+.1f}%</b> (Peak: {peak_pnl_percent:+.1f}%)\n\n"
            )

            # Replace current market cap line with initial market cap line
            text = re.sub(
                r"💰 Market Cap: <b>.*?</b>",
                f"💰 Initial Mcap: <b>{money(initial_mcap)}</b>",
                text
            )

            # Insert PnL block before "DYOR"
            text = text.replace("⚠️ <b>DYOR. Not financial advice.</b>", f"{pnl_section}⚠️ <b>DYOR. Not financial advice.</b>")

            try:
                if image_url:
                    await context.bot.edit_message_caption(
                        chat_id=CHANNEL_USERNAME,
                        message_id=message_id,
                        caption=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=CHANNEL_USERNAME,
                        message_id=message_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                        disable_web_page_preview=False
                    )
                print(f"Updated PnL for {name} (${symbol}): {pnl_percent:+.1f}%")
            except Exception as e:
                print(f"Failed to edit message {message_id} for {name}: {e}")

    except Exception as e:
        print("ERROR updating PnL:", e)


async def scan_and_post(context: ContextTypes.DEFAULT_TYPE):
    print("Scanning TON tokens...")

    try:
        pairs = get_active_ton_pairs()

        for pair in pairs:
            contract = (pair.get("baseToken") or {}).get("address", "")
            name = (pair.get("baseToken") or {}).get("name", "Unknown")
            symbol = (pair.get("baseToken") or {}).get("symbol", "UNKNOWN")

            if not contract:
                continue

            # Skip if already posted (by address or name)
            if already_posted(contract, name):
                continue

            # Check security of token contract
            if not check_token_security(contract):
                continue

            await asyncio.sleep(1)  # Avoid rate limiting on TonAPI

            text, keyboard, contract, symbol, name, market_cap, image_url = build_post(pair)

            msg = None
            if image_url:
                msg = await context.bot.send_photo(
                    chat_id=CHANNEL_USERNAME,
                    photo=image_url,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard
                )
            else:
                msg = await context.bot.send_message(
                    chat_id=CHANNEL_USERNAME,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=False
                )

            message_id = msg.message_id if msg else None
            save_posted(contract, symbol, name, market_cap, message_id)
            print(f"Posted: {name} ${symbol} - {money(market_cap)} (Message ID: {message_id})")

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
    await update_pnl(context)
    await update.message.reply_text("✅ Scan selesai.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM posted_tokens")
    total = cur.fetchone()[0]
    conn.close()

    await update.message.reply_text(f"📊 Total token posted: {total}")


PID_FILE = "/tmp/ton_mcap_bot.pid"


def acquire_lock():
    """Prevent multiple instances using a PID file."""
    if os.path.exists(PID_FILE):
        with open(PID_FILE, "r") as f:
            old_pid = f.read().strip()
        try:
            os.kill(int(old_pid), 0)  # Check if process still alive
            print(f"Bot already running (PID {old_pid}). Killing old instance...")
            os.kill(int(old_pid), 9)
            import time; time.sleep(3)
        except (OSError, ValueError):
            pass  # Process not running, stale PID file
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)


def main():
    init_db()
    acquire_lock()

    try:
        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .connect_timeout(30)
            .read_timeout(30)
            .write_timeout(30)
            .build()
        )

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("scan", scan_cmd))
        app.add_handler(CommandHandler("stats", stats_cmd))

        app.job_queue.run_repeating(
            scan_and_post,
            interval=SCAN_INTERVAL_MINUTES * 60,
            first=10
        )

        # Run PnL updater every 5 minutes
        app.job_queue.run_repeating(
            update_pnl,
            interval=300,
            first=30
        )

        print("TON MCAP bot running...")
        app.run_polling(
            drop_pending_updates=True,  # Clear stale connections on startup
            allowed_updates=["message", "callback_query"]
        )
    finally:
        release_lock()


if __name__ == "__main__":
    main()