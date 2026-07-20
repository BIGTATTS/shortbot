import os
import sqlite3
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DB_PATH = os.path.join(os.environ.get("DATA_DIR", "."), "shortbot.db")
THRESHOLDS = [10, 20]

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        chat_id INTEGER, ticker TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS up_alerts (
        chat_id INTEGER, ticker TEXT, threshold REAL)""")
    return conn

def get_price(ticker: str):
    data = yf.Ticker(ticker).history(period="1d")
    if data.empty:
        return None
    return round(data['Close'].iloc[-1], 2)

def get_pct_change(ticker: str):
    data = yf.Ticker(ticker).history(period="2d")
    if len(data) < 2:
        return None, None
    prev_close = data['Close'].iloc[-2]
    last_close = data['Close'].iloc[-1]
    pct = round((last_close - prev_close) / prev_close * 100, 2)
    return pct, round(last_close, 2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "VKC Short Bot\n\n"
        "Commands:\n"
        "/watch TICKER - add to watchlist (auto-alerts at +10% and +20%)\n"
        "/unwatch TICKER - remove from watchlist\n"
        "/list - show watchlist with current prices\n"
        "/price TICKER - check a price on demand\n\n"
        "You'll only be alerted on upward moves of 10% or 20%+."
    )

async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /watch TICKER [TICKER2 TICKER3 ...]")
        return
    chat_id = update.effective_chat.id
    conn = get_conn()
    added, skipped = [], []
    for raw in context.args:
        ticker = raw.upper()
        existing = conn.execute(
            "SELECT 1 FROM watchlist WHERE chat_id=? AND ticker=?", (chat_id, ticker)
        ).fetchone()
        if existing:
            skipped.append(ticker)
            continue
        conn.execute("INSERT INTO watchlist VALUES (?, ?)", (chat_id, ticker))
        for t in THRESHOLDS:
            conn.execute("INSERT INTO up_alerts VALUES (?, ?, ?)", (chat_id, ticker, t))
        added.append(ticker)
    conn.commit()
    conn.close()
    lines = []
    if added:
        lines.append(f"Added: {', '.join(added)} (alerts at +{THRESHOLDS[0]}% and +{THRESHOLDS[1]}%)")
    if skipped:
        lines.append(f"Already watching: {', '.join(skipped)}")
    await update.message.reply_text("\n".join(lines))

async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unwatch TICKER")
        return
    ticker = context.args[0].upper()
    chat_id = update.effective_chat.id
    conn = get_conn()
    conn.execute("DELETE FROM watchlist WHERE chat_id=? AND ticker=?", (chat_id, ticker))
    conn.execute("DELETE FROM up_alerts WHERE chat_id=? AND ticker=?", (chat_id, ticker))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Removed {ticker} from your watchlist.")

async def list_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = get_conn()
    rows = conn.execute("SELECT ticker FROM watchlist WHERE chat_id=?", (chat_id,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Your watchlist is empty. Add one with /watch TICKER")
        return
    lines = []
    for (ticker,) in rows:
        p = get_price(ticker)
        lines.append(f"{ticker}: ${p}" if p is not None else f"{ticker}: unavailable")
    await update.message.reply_text("\n".join(lines))

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price TICKER")
        return
    ticker = context.args[0].upper()
    p = get_price(ticker)
    if p is None:
        await update.message.reply_text(f"Couldn't find {ticker}")
        return
    await update.message.reply_text(f"{ticker}: ${p}")

async def check_up_alerts(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    rows = conn.execute("SELECT rowid, chat_id, ticker, threshold FROM up_alerts").fetchall()
    for rowid, chat_id, ticker, threshold in rows:
        pct, current = get_pct_change(ticker)
        if pct is None:
            continue
        if pct >= threshold:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"\U0001F680 {ticker} is up {pct}% today (${current}) \u2014 crossed +{threshold}%"
            )
            conn.execute("DELETE FROM up_alerts WHERE rowid=?", (rowid,))
            conn.commit()
    conn.close()

app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("watch", watch))
app.add_handler(CommandHandler("unwatch", unwatch))
app.add_handler(CommandHandler("list", list_watchlist))
app.add_handler(CommandHandler("price", price))

app.job_queue.run_repeating(check_up_alerts, interval=300, first=10)

app.run_polling()