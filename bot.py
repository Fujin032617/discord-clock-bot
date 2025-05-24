import os
import json
import sqlite3
from flask import Flask, make_response
from threading import Thread
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from pytz import timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Use Render's persistent disk directory or fallback to current directory
data_dir = os.getenv('RENDER_DATA_DIR', '.')
os.makedirs(data_dir, exist_ok=True)
db_path = os.path.join(data_dir, 'bot_data.db')

# Flask App Setup
app = Flask('')

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

@app.route('/')
def home():
    response = make_response("I'm alive!")
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# ========== SQLite Setup ==========

def get_db_connection():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS excluded_users (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_shifts (user_id INTEGER PRIMARY KEY, date TEXT, timestamp TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS last_clockouts (user_id INTEGER PRIMARY KEY, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

# ========== Data Access ==========

def load_excluded_users():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id FROM excluded_users')
    rows = c.fetchall()
    conn.close()
    return [row['user_id'] for row in rows]

def save_excluded_user(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO excluded_users (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def remove_excluded_user(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM excluded_users WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def load_active_shifts():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, date, timestamp FROM active_shifts')
    rows = c.fetchall()
    conn.close()
    return {str(row['user_id']): {'date': row['date'], 'timestamp': row['timestamp']} for row in rows}

def save_active_shift(user_id, date, timestamp):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO active_shifts (user_id, date, timestamp) VALUES (?, ?, ?)', (user_id, date, timestamp))
    conn.commit()
    conn.close()

def remove_active_shift(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM active_shifts WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def load_last_clockouts():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, timestamp FROM last_clockouts')
    rows = c.fetchall()
    conn.close()
    return {str(row['user_id']): row['timestamp'] for row in rows}

def save_last_clockout(user_id, timestamp):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO last_clockouts (user_id, timestamp) VALUES (?, ?)', (user_id, timestamp))
    conn.commit()
    conn.close()

# ========== Helper function to check clock-in from DB ==========

ph_tz = timezone('Asia/Manila')

def user_has_recent_clockin(user_id):
    """Return True if user has clocked in less than 14 hours ago (prevents new clock-in)."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT timestamp FROM active_shifts WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        try:
            clock_in_time = ph_tz.localize(datetime.strptime(row['timestamp'], "%Y-%m-%d %H:%M:%S"))
            now = datetime.now(ph_tz)
            if now - clock_in_time < timedelta(hours=14):
                return True
        except Exception as e:
            print(f"Error parsing clock-in time: {e}")
    return False

# ========== Discord Setup ==========

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Google Sheets Setup
scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds_json = json.loads(os.environ['GOOGLE_CREDS'])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
sheet = client.open("Employee Time Log").sheet1

excluded_user_ids = load_excluded_users()
active_shifts = load_active_shifts()
last_clockouts = load_last_clockouts()

print("Excluded users loaded:", excluded_user_ids)
print("Active shifts loaded:", active_shifts)
print("Last clockouts loaded:", last_clockouts)

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    auto_clockout_expired_shifts.start()

@bot.command()
async def clockin(ctx):
    user_id = ctx.author.id
    user_id_str = str(user_id)

    if user_id in excluded_user_ids:
        await ctx.send(f'{ctx.author.mention}, you are not eligible for time tracking.')
        return

    # Check DB if user has recent clock-in
    if user_has_recent_clockin(user_id):
        await ctx.send(f"{ctx.author.mention}, you already clocked in less than 14 hours ago. Please clock out before clocking in again.")
        return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([ctx.author.name, 'Clock In', timestamp_str])

    # Update in-memory and DB active shifts
    active_shifts[user_id_str] = {'date': now.strftime("%Y-%m-%d"), 'timestamp': timestamp_str}
    save_active_shift(user_id, now.strftime("%Y-%m-%d"), timestamp_str)

    await ctx.send(f'{ctx.author.mention} has clocked in at {timestamp_str}')

@bot.command()
async def clockout(ctx):
    user_id = ctx.author.id
    user_id_str = str(user_id)

    if user_id in excluded_user_ids:
        await ctx.send(f'{ctx.author.mention}, you are not eligible for time tracking.')
        return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([ctx.author.name, 'Clock Out', timestamp_str])
    await ctx.send(f'{ctx.author.mention} has clocked out at {timestamp_str}')

    # Remove active shift from memory and DB
    if user_id_str in active_shifts:
        del active_shifts[user_id_str]
        remove_active_shift(user_id)

    # Save last clockout timestamp
    last_clockouts[user_id_str] = timestamp_str
    save_last_clockout(user_id, timestamp_str)

@bot.command()
async def exclude(ctx, user: discord.User):
    user_id = user.id
    if user_id in excluded_user_ids:
        await ctx.send(f"{user.name} is already excluded from time tracking.")
        return
    excluded_user_ids.append(user_id)
    save_excluded_user(user_id)
    # Also remove active shift if any
    if str(user_id) in active_shifts:
        del active_shifts[str(user_id)]
        remove_active_shift(user_id)
    await ctx.send(f"{user.name} has been excluded from time tracking.")

@bot.command()
async def unexclude(ctx, user: discord.User):
    user_id = user.id
    if user_id not in excluded_user_ids:
        await ctx.send(f"{user.name} is not excluded from time tracking.")
        return
    excluded_user_ids.remove(user_id)
    remove_excluded_user(user_id)
    await ctx.send(f"{user.name} is no longer excluded from time tracking.")

@bot.command()
async def listexcluded(ctx):
    if not excluded_user_ids:
        await ctx.send("No users are currently excluded.")
        return
    mentions = []
    for user_id in excluded_user_ids:
        user = bot.get_user(user_id)
        if user:
            mentions.append(user.mention)
        else:
            mentions.append(f"<@{user_id}>")
    await ctx.send("Excluded users:\n" + "\n".join(mentions))

@tasks.loop(minutes=15)
async def auto_clockout_expired_shifts():
    now = datetime.now(ph_tz)
    expired = []

    for uid, entry in active_shifts.items():
        try:
            shift_time = ph_tz.localize(datetime.strptime(entry['timestamp'], "%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            print(f"Error parsing shift_time for user {uid}: {e}")
            continue

        if (now - shift_time) >= timedelta(hours=14):
            user_obj = bot.get_user(int(uid))
            user_name = user_obj.name if user_obj else str(uid)
            timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
            sheet.append_row([user_name, 'Clock Out', timestamp_str])
            save_last_clockout(int(uid), timestamp_str)
            expired.append(uid)
            await user_obj.send(f"You have been automatically clocked out after 14 hours.") if user_obj else None

    for uid in expired:
        if uid in active_shifts:
            del active_shifts[uid]
        remove_active_shift(int(uid))

if __name__ == '__main__':
    keep_alive()
    bot.run(os.environ['DISCORD_TOKEN'])
