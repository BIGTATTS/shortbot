import os
import sqlite3
import requests
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DB_PATH = os.path.join(os.environ.get("DATA_DIR", "."), "shortbot.db")
THRESHOLDS = [10, 20]  # percent moves to alert on, upward only

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        chat_id INTEGER, ticker TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS up_alerts (
        chat_id INTEGER, ticker TEXT, threshold REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sec_seen (
        ticker TEXT PRIMARY KEY, accession TEXT)""")
    return conn

_CIK_CACHE = {}

def get_cik(ticker: str):
    global _CIK_CACHE
    if not _CIK_CACHE:
        try:
            resp = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": "shortbot contact@example.com"},
                timeout=10,
            )
            data = resp.json()
            for entry in data.values():
                _CIK_CACHE[entry["ticker"].upper()] = str(entry["cik_str"]).zfill(10)
        except Exception:
            return None
    return _CIK_CACHE.get(ticker.upper())

def get_recent_filings(ticker: str, limit: int = 1):
    cik = get_cik(ticker)
    if not cik:
        return []
    try:
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": "shortbot contact@example.com"},
            timeout=10,
        )
        recent = resp.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        filings = []
        for i in range(min(limit, len(forms))):
            acc_nodash = accessions[i].replace("-", "")
            link = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{docs[i]}"
            filings.append({
                "form": forms[i], "date": dates[i],
                "accession": accessions[i], "link": link,
            })
        return filings
    except Exception:
        return []

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
        "You'll only be alerted on upward moves of 10% or 20%+.\n"
        "SEC filings for anything on your watchlist are sent automatically."
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

async def check_sec_filings(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    tickers = conn.execute("SELECT DISTINCT ticker FROM watchlist").fetchall()
    for (ticker,) in tickers:
        filings = get_recent_filings(ticker, limit=1)
        if not filings:
            continue
        latest = filings[0]
        seen = conn.execute("SELECT accession FROM sec_seen WHERE ticker=?", (ticker,)).fetchone()
        if seen is None:
            conn.execute("INSERT INTO sec_seen VALUES (?, ?)", (ticker, latest["accession"]))
            conn.commit()
            continue
        if seen[0] != latest["accession"]:
            conn.execute(
                "UPDATE sec_seen SET accession=? WHERE ticker=?", (latest["accession"], ticker)
            )
            conn.commit()
            chat_ids = conn.execute(
                "SELECT chat_id FROM watchlist WHERE ticker=?", (ticker,)
            ).fetchall()
            for (chat_id,) in chat_ids:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"New SEC filing: {ticker} filed a {latest['form']} on {latest['date']}\n{latest['link']}"
                )
    conn.close()

app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("watch", watch))
app.add_handler(CommandHandler("unwatch", unwatch))
app.add_handler(CommandHandler("list", list_watchlist))
app.add_handler(CommandHandler("price", price))

app.job_queue.run_repeating(check_up_alerts, interval=300, first=10)
app.job_queue.run_repeating(check_sec_filings, interval=1800, first=20)

app.run_polling()