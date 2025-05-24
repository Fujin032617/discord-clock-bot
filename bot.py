import os
import sqlite3
from flask import Flask, make_response
from threading import Thread
import discord
from discord.ext import commands, tasks
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
from pytz import timezone

# === SQLite DB Setup ===
DB_PATH = '/data/bot_data.db'
os.makedirs('/data', exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS excluded_users (user_id INTEGER PRIMARY KEY)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS active_shifts (
                        user_id INTEGER PRIMARY KEY,
                        date TEXT,
                        timestamp TEXT
                    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS last_clockouts (
                        user_id INTEGER PRIMARY KEY,
                        timestamp TEXT
                    )''')
    conn.commit()
    conn.close()

def add_excluded_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO excluded_users (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def remove_excluded_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM excluded_users WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def get_excluded_users():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM excluded_users')
    result = [row[0] for row in cursor.fetchall()]
    conn.close()
    return result

def save_active_shift(user_id, date, timestamp):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('REPLACE INTO active_shifts (user_id, date, timestamp) VALUES (?, ?, ?)', (user_id, date, timestamp))
    conn.commit()
    conn.close()

def remove_active_shift(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM active_shifts WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def get_active_shifts():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, date, timestamp FROM active_shifts')
    result = {str(row[0]): {'date': row[1], 'timestamp': row[2]} for row in cursor.fetchall()}
    conn.close()
    return result

def get_last_clockouts():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, timestamp FROM last_clockouts')
    result = {str(row[0]): row[1] for row in cursor.fetchall()}
    conn.close()
    return result

def save_last_clockout(user_id, timestamp):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('REPLACE INTO last_clockouts (user_id, timestamp) VALUES (?, ?)', (user_id, timestamp))
    conn.commit()
    conn.close()

# === Flask Setup ===
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

# === Discord Setup ===
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# === Google Sheets Setup ===
scope = [
    "https://spreadsheets.google.com/feeds",
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
creds_json = json.loads(os.environ['GOOGLE_CREDS'])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
sheet = client.open("Employee Time Log").sheet1

init_db()

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    cleanup_old_shifts.start()
    public_clockout_reminder.start()

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or member.id in get_excluded_users():
        return
    if before.channel is not None or after.channel is None:
        return

    ph_tz = timezone('Asia/Manila')
    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    user_id_str = str(member.id)

    last_clockouts = get_last_clockouts()
    if user_id_str in last_clockouts:
        last_out_time = ph_tz.localize(datetime.strptime(last_clockouts[user_id_str], "%Y-%m-%d %H:%M:%S"))
        if (now - last_out_time) < timedelta(minutes=15):
            print(f"{member.name} tried to Clock In too soon after Clock Out.")
            return

    active_shifts = get_active_shifts()
    allow_clock_in = False
    if user_id_str in active_shifts:
        last_clock_in_time = ph_tz.localize(datetime.strptime(active_shifts[user_id_str]['timestamp'], "%Y-%m-%d %H:%M:%S"))
        if (now - last_clock_in_time) >= timedelta(hours=14):
            allow_clock_in = True
    else:
        allow_clock_in = True

    if allow_clock_in:
        sheet.append_row([member.name, 'Clock In', timestamp_str])
        save_active_shift(member.id, now.strftime("%Y-%m-%d"), timestamp_str)
        print(f'{member.name} Clock In at {timestamp_str}')

@bot.command()
async def clockout(ctx):
    if ctx.author.id in get_excluded_users():
        await ctx.send(f'{ctx.author.mention}, you are not eligible for time tracking.')
        return

    ph_tz = timezone('Asia/Manila')
    now = datetime.now(ph_tz)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([ctx.author.name, 'Clock Out', timestamp])
    await ctx.send(f'{ctx.author.mention} has clocked out at {timestamp}')
    print(f'{ctx.author.name} Clock Out at {timestamp}')

    remove_active_shift(ctx.author.id)
    save_last_clockout(ctx.author.id, timestamp)

@bot.command(name='exclude')
@commands.has_permissions(administrator=True)
async def exclude_user(ctx, member: discord.Member):
    add_excluded_user(member.id)
    await ctx.send(f'{member.mention} has been excluded from time tracking.')

@bot.command(name='unexclude')
@commands.has_permissions(administrator=True)
async def unexclude_user(ctx, member: discord.Member):
    remove_excluded_user(member.id)
    await ctx.send(f'{member.mention} has been re-included in time tracking.')

@bot.command(name='listexcluded')
async def list_excluded(ctx):
    excluded_ids = get_excluded_users()
    if not excluded_ids:
        await ctx.send("No users are currently excluded.")
    else:
        excluded_names = []
        for uid in excluded_ids:
            user = bot.get_user(uid)
            excluded_names.append(f"{user.display_name if user else 'Unknown'} ({uid})")
        await ctx.send("Excluded users:\n" + "\n".join(excluded_names))

@tasks.loop(minutes=30)
async def cleanup_old_shifts():
    ph_tz = timezone('Asia/Manila')
    now = datetime.now(ph_tz)
    active_shifts = get_active_shifts()
    expired = []
    for uid, entry in active_shifts.items():
        shift_time = ph_tz.localize(datetime.strptime(entry['timestamp'], "%Y-%m-%d %H:%M:%S"))
        if (now - shift_time) >= timedelta(hours=14):
            expired.append(uid)

    for uid in expired:
        remove_active_shift(int(uid))
    if expired:
        print(f"Cleaned up {len(expired)} expired shift entries.")

@tasks.loop(hours=8)
async def public_clockout_reminder():
    for guild in bot.guilds:
        general_channel = discord.utils.get(guild.text_channels, name='general')
        if general_channel:
            try:
                await general_channel.send("⏰ Friendly reminder: Don't forget to clock out after your shift. Anyone who doesn't clock out will be marked as absent! -HRJEL ")
            except Exception as e:
                print(f"Failed to send reminder in {guild.name}: {e}")
        else:
            print(f"No #general channel found in {guild.name}")

if __name__ == '__main__':
    keep_alive()
    bot.run(os.environ['DISCORD_TOKEN'])
