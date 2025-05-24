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

# Flask App Setup for keep-alive
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

# ===== SQLite Setup =====

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

# ===== Data Access Functions =====

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

# ===== Discord Bot Setup =====

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

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    auto_clockout_expired_shifts.start()

# --- Clock-in via voice state update ---
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or member.id in excluded_user_ids:
        return
    # We only log clock-in on voice channel join (not on leave)
    if before.channel is not None or after.channel is None:
        return

    ph_tz = timezone('Asia/Manila')
    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    user_id_str = str(member.id)

    # Check last clockout: if recent (less than 15 min), ignore
    if user_id_str in last_clockouts:
        last_out_time = ph_tz.localize(datetime.strptime(last_clockouts[user_id_str], "%Y-%m-%d %H:%M:%S"))
        if (now - last_out_time) < timedelta(minutes=15):
            return

    # Prevent clock-in if last clock-in was less than 14 hours ago
    previous_entry = active_shifts.get(user_id_str)
    if previous_entry:
        last_clock_in_time = ph_tz.localize(datetime.strptime(previous_entry['timestamp'], "%Y-%m-%d %H:%M:%S"))
        if (now - last_clock_in_time) < timedelta(hours=14):
            # Deny clock-in because 14 hours not passed yet
            return

    # Allow clock-in
    sheet.append_row([member.name, 'Clock In', timestamp_str])
    active_shifts[user_id_str] = {'date': now.strftime("%Y-%m-%d"), 'timestamp': timestamp_str}
    save_active_shift(member.id, now.strftime("%Y-%m-%d"), timestamp_str)

@bot.command()
async def clockout(ctx):
    if ctx.author.id in excluded_user_ids:
        await ctx.send(f'{ctx.author.mention}, you are not eligible for time tracking.')
        return

    user_id_str = str(ctx.author.id)
    ph_tz = timezone('Asia/Manila')
    now = datetime.now(ph_tz)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    # Prevent double clock-out within 1 minute
    last_out_str = last_clockouts.get(user_id_str)
    if last_out_str:
        last_out_time = ph_tz.localize(datetime.strptime(last_out_str, "%Y-%m-%d %H:%M:%S"))
        if (now - last_out_time) < timedelta(minutes=1):
            await ctx.send(f'{ctx.author.mention}, you already clocked out recently.')
            return

    sheet.append_row([ctx.author.name, 'Clock Out', timestamp])
    await ctx.send(f'{ctx.author.mention} has clocked out at {timestamp}')

    if user_id_str in active_shifts:
        del active_shifts[user_id_str]
        remove_active_shift(ctx.author.id)
    last_clockouts[user_id_str] = timestamp
    save_last_clockout(ctx.author.id, timestamp)

# Auto clock-out after 14 hours
@tasks.loop(minutes=15)
async def auto_clockout_expired_shifts():
    ph_tz = timezone('Asia/Manila')
    now = datetime.now(ph_tz)
    expired = []

    for uid, entry in list(active_shifts.items()):
        shift_time = ph_tz.localize(datetime.strptime(entry['timestamp'], "%Y-%m-%d %H:%M:%S"))
        if (now - shift_time) >= timedelta(hours=14):
            # Check last clockout to avoid double clock out
            last_out_str = last_clockouts.get(uid)
            if last_out_str:
                last_out_time = ph_tz.localize(datetime.strptime(last_out_str, "%Y-%m-%d %H:%M:%S"))
                if (now - last_out_time) < timedelta(minutes=1):  # Already clocked out recently
                    expired.append(uid)
                    continue

            user = bot.get_user(int(uid))
            user_name = user.name if user else str(uid)
            timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
            sheet.append_row([user_name, 'Clock Out', timestamp_str])
            save_last_clockout(int(uid), timestamp_str)

            expired.append(uid)

    for uid in expired:
        if uid in active_shifts:
            del active_shifts[uid]
            remove_active_shift(int(uid))

# ===== Commands to manage excluded users =====

@bot.command()
async def exclude(ctx, user: discord.User):
    if user.id in excluded_user_ids:
        await ctx.send(f'{user.name} is already excluded.')
        return
    excluded_user_ids.append(user.id)
    save_excluded_user(user.id)
    await ctx.send(f'{user.name} has been excluded from time tracking.')

@bot.command()
async def include(ctx, user: discord.User):
    if user.id not in excluded_user_ids:
        await ctx.send(f'{user.name} is not excluded.')
        return
    excluded_user_ids.remove(user.id)
    remove_excluded_user(user.id)
    await ctx.send(f'{user.name} has been included back into time tracking.')

# Start keep alive server and run bot

keep_alive()
bot.run(os.getenv('DISCORD_TOKEN'))
