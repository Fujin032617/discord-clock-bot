import os
import sqlite3
from flask import Flask, make_response
from threading import Thread
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from pytz import timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

# Setup persistent directory for Render or fallback
data_dir = os.getenv('RENDER_DATA_DIR', '.')
os.makedirs(data_dir, exist_ok=True)
db_path = os.path.join(data_dir, 'bot_data.db')

# Flask app to keep bot alive (for hosting platforms like Render)
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

# SQLite database helper functions
def get_db_connection():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS excluded_users (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_shifts (user_id INTEGER PRIMARY KEY, clock_in TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS last_clockouts (user_id INTEGER PRIMARY KEY, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

def load_excluded_users_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id FROM excluded_users')
    rows = c.fetchall()
    conn.close()
    return {row['user_id'] for row in rows} # Return a set for faster lookups

def save_excluded_user_db(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO excluded_users (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def remove_excluded_user_db(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM excluded_users WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def load_active_shifts_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, clock_in FROM active_shifts')
    rows = c.fetchall()
    conn.close()
    return {row['user_id']: row['clock_in'] for row in rows} # Keys are integers

def save_active_shift_db(user_id, clock_in):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO active_shifts (user_id, clock_in) VALUES (?, ?)', (user_id, clock_in))
    conn.commit()
    conn.close()

def remove_active_shift_db(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM active_shifts WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def load_last_clockouts_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, timestamp FROM last_clockouts')
    rows = c.fetchall()
    conn.close()
    return {row['user_id']: row['timestamp'] for row in rows} # Keys are integers

def save_last_clockout_db(user_id, timestamp):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO last_clockouts (user_id, timestamp) VALUES (?, ?)', (user_id, timestamp))
    conn.commit()
    conn.close()

# Discord Bot Setup
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True # Crucial for voice channel events
bot = commands.Bot(command_prefix='!', intents=intents)

# Google Sheets Setup
scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds_json = json.loads(os.environ['GOOGLE_CREDS'])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
sheet = client.open("Employee Time Log").sheet1

# Load data into memory using corrected functions
excluded_user_ids = load_excluded_users_db() # This is now a set
active_shifts = load_active_shifts_db() # Keys are integers
last_clockouts = load_last_clockouts_db() # Keys are integers

ph_tz = timezone('Asia/Manila')

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    auto_clockout_expired_shifts.start()

# ========= MODIFIED can_clock_in FUNCTION ==========
def can_clock_in(user_id):
    """
    Check if user can clock in:
    1. Not excluded.
    2. No active shift.
    3. Last clock-out (if any) was beyond a defined cooldown period.
    """
    now = datetime.now(ph_tz)

    # 1. If user is excluded, never allow clock-in
    if user_id in excluded_user_ids:
        return False

    # 2. Check if user currently has an active shift
    if user_id in active_shifts:
        # If they are already in active_shifts, they cannot clock in again.
        # This covers cases where they were clocked in by voice or manually.
        return False

    # 3. Check the last clock-out time (cooldown)
    # This prevents immediate re-clock-ins after a shift officially ended.
    # Set a cooldown, e.g., 5 minutes. Adjust this to your preference.
    cooldown_period = timedelta(minutes=5)

    last_out_str = last_clockouts.get(user_id)
    if last_out_str:
        last_out_time = ph_tz.localize(datetime.strptime(last_out_str, "%Y-%m-%d %H:%M:%S"))
        # If the time since last clock-out is less than the cooldown period, prevent new clock-in
        if (now - last_out_time) < cooldown_period:
            return False

    # If all checks pass, the user can clock in
    return True

# ========== MODIFIED AUTOMATIC CLOCK-IN ONLY ==========
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    user_id = member.id
    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Ignore bots
    if member.bot:
        return

    # Ignore excluded users
    if user_id in excluded_user_ids:
        return

    # Auto Clock-in: User joins a voice channel (and wasn't in one before, or moved channels)
    if after.channel and (before.channel != after.channel):
        # Only clock in if they aren't already actively clocked in AND pass can_clock_in checks
        if can_clock_in(user_id): # The can_clock_in now includes the cooldown check
            # Perform clock-in
            active_shifts[user_id] = timestamp_str # Use int key
            save_active_shift_db(user_id, timestamp_str)
            
            try:
                sheet.append_row([member.name, "Clock In", timestamp_str])
            except Exception as e:
                print(f"Failed to append auto clock-in to Google Sheets for {member.name}: {e}")

            channel_to_send = member.guild.system_channel or \
                              (member.guild.text_channels[0] if member.guild.text_channels else None)
            if channel_to_send:
                try:
                    await channel_to_send.send(f"{member.mention} has automatically clocked in (joined voice channel).")
                except discord.Forbidden:
                    print(f"Cannot send message to {channel_to_send.name} in {member.guild.name}.")
        # else:
            # Optionally, you could send a message here if auto-clock-in was prevented by cooldown
            # print(f"DEBUG: Auto clock-in prevented for {member.name} due to cooldown or active shift.")
    
    # REMOVED: Auto Clock-out logic from voice channel departure.
    # Users now MUST use !clockout or be auto-clocked out by the 14-hour task or !forceclockout.


# ========== MANUAL CLOCK-IN/OUT COMMANDS ==========

@bot.command()
async def clockin(ctx):
    user_id = ctx.author.id
    if user_id in excluded_user_ids:
        await ctx.send(f"{ctx.author.mention}, you are excluded from time tracking.")
        return

    # Check if they can clock in using the central can_clock_in logic
    if not can_clock_in(user_id):
        await ctx.send(f"{ctx.author.mention}, you cannot clock in at this time. You might already be clocked in, or have clocked out too recently. If you wish to end your current shift, use `!clockout`.")
        return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    
    # Log clock-in in Google Sheets
    try:
        sheet.append_row([ctx.author.name, "Clock In", timestamp_str])
    except Exception as e:
        await ctx.send(f"Failed to log your clock-in to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append manual clock-in to Google Sheets for {ctx.author.name}: {e}")
        return # Prevent incomplete state if sheet fails

    # Save active shift in SQLite and memory
    active_shifts[user_id] = timestamp_str
    save_active_shift_db(user_id, timestamp_str)

    await ctx.send(f"{ctx.author.mention} clocked in at {timestamp_str}")

@bot.command()
async def clockout(ctx):
    user_id = ctx.author.id
    if user_id in excluded_user_ids:
        await ctx.send(f"{ctx.author.mention}, you are excluded from time tracking.")
        return

    if user_id not in active_shifts:
        await ctx.send(f"{ctx.author.mention}, you are not currently clocked in.")
        return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Log clock-out in Google Sheets
    try:
        sheet.append_row([ctx.author.name, "Clock Out", timestamp_str])
    except Exception as e:
        await ctx.send(f"Failed to log your clock-out to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append manual clock-out to Google Sheets for {ctx.author.name}: {e}")
        return # Prevent incomplete state if sheet fails

    # Remove active shift and save last clockout time
    del active_shifts[user_id]
    remove_active_shift_db(user_id)

    last_clockouts[user_id] = timestamp_str
    save_last_clockout_db(user_id, timestamp_str)

    await ctx.send(f"{ctx.author.mention} clocked out at {timestamp_str}")


@tasks.loop(minutes=15)
async def auto_clockout_expired_shifts():
    now = datetime.now(ph_tz)
    expired = []

    # Iterate over a copy of active_shifts in case it's modified during iteration
    for uid, clock_in_str in list(active_shifts.items()): # uid will be int here
        clock_in_time = ph_tz.localize(datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S"))
        
        # This is where the 14-hour rule is applied for auto-clock-out
        if now - clock_in_time >= timedelta(hours=14): # Check if 14 hours have passed since clock-in
            user = bot.get_user(uid) # uid is already an int
            name = user.name if user else f"User ID: {uid}"
            timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
            
            # Log clock-out automatically
            try:
                sheet.append_row([name, "Clock Out (Auto)", timestamp_str])
            except Exception as e:
                print(f"Failed to append auto clock-out to Google Sheets for {name}: {e}")

            # Save last clockout time
            last_clockouts[uid] = timestamp_str
            save_last_clockout_db(uid, timestamp_str)
            
            expired.append(uid) # Add uid (int) to expired list
            
            # Notify the user or a designated channel about auto clock-out
            if user:
                # Attempt to find a suitable channel to send the notification
                for guild in bot.guilds:
                    member = guild.get_member(uid)
                    if member:
                        target_channel = guild.system_channel or \
                                         discord.utils.get(guild.text_channels, name='general') or \
                                         (guild.text_channels[0] if guild.text_channels else None)
                        if target_channel:
                            try:
                                await target_channel.send(f"{user.mention} was automatically clocked out after 14 hours.")
                            except discord.Forbidden:
                                print(f"Cannot send message to {target_channel.name} in {guild.guild.name}.")
                            break
            else:
                print(f"Could not find Discord user for ID {uid} for auto clock-out notification.")

    # Remove expired shifts from memory and DB
    for uid in expired:
        del active_shifts[uid]
        remove_active_shift_db(uid)

@bot.command()
@commands.has_permissions(administrator=True)
async def exclude(ctx, member: discord.Member = None, *, username: str = None):
    target_user_id = None
    target_user_name = None

    if member is not None:
        target_user_id = member.id
        target_user_name = member.name
    elif username:
        found_members = [m for m in ctx.guild.members if m.name.lower() == username.lower()]
        if not found_members:
            await ctx.send(f'No user found with username "{username}". Please mention the user or provide exact username.')
            return
        target_user_id = found_members[0].id
        target_user_name = found_members[0].name
    else:
        await ctx.send("Please mention a user or provide a username to exclude.")
        return

    if target_user_id in excluded_user_ids:
        await ctx.send(f"{target_user_name} is already excluded.")
        return
    
    save_excluded_user_db(target_user_id)
    excluded_user_ids.add(target_user_id) # Add to the in-memory set
    
    # Also remove active shift if any
    if target_user_id in active_shifts:
        del active_shifts[target_user_id]
        remove_active_shift_db(target_user_id)
    
    await ctx.send(f"{target_user_name} has been excluded from time tracking.")

@bot.command()
@commands.has_permissions(administrator=True)
async def include(ctx, member: discord.Member = None, *, username: str = None):
    target_user_id = None
    target_user_name = None

    if member is not None:
        target_user_id = member.id
        target_user_name = member.name
    elif username:
        found_members = [m for m in ctx.guild.members if m.name.lower() == username.lower()]
        if not found_members:
            await ctx.send(f'No user found with username "{username}". Please mention the user or provide exact username.')
            return
        target_user_id = found_members[0].id
        target_user_name = found_members[0].name
    else:
        await ctx.send("Please mention a user or provide a username to include.")
        return

    if target_user_id not in excluded_user_ids:
        await ctx.send(f"{target_user_name} is not excluded.")
        return
    
    remove_excluded_user_db(target_user_id)
    excluded_user_ids.remove(target_user_id) # Remove from the in-memory set
    
    await ctx.send(f"{target_user_name} has been included back in time tracking.")

@bot.command()
@commands.has_permissions(administrator=True)
async def listexcluded(ctx):
    if not excluded_user_ids:
        await ctx.send("No users are excluded.")
        return

    excluded_names = []
    for uid in excluded_user_ids:
        user = bot.get_user(uid)
        if user:
            excluded_names.append(user.name)
        else:
            excluded_names.append(f"Unknown User (ID: {uid})") # More informative
    await ctx.send("Excluded users:\n" + "\n".join(excluded_names))

@bot.command()
@commands.has_permissions(administrator=True)
async def onduty(ctx):
    """Show list of users currently clocked in (on duty)."""
    if not active_shifts:
        await ctx.send("No users are currently on duty.")
        return

    msg = "**Currently on duty:**\n"
    for uid, clock_in_str in active_shifts.items():
        user = bot.get_user(uid)
        name = user.name if user else f"Unknown User (ID: {uid})"
        msg += f"- {name} (clocked in at {clock_in_str})\n"
    await ctx.send(msg)

@bot.command()
@commands.has_permissions(administrator=True)
async def forceclockout(ctx, member: discord.Member):
    """Force a user to clock out."""
    user_id = member.id
    if user_id not in active_shifts:
        await ctx.send(f"{member.name} is not currently clocked in.")
        return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Log clock-out in Google Sheets
    try:
        sheet.append_row([member.name, "Clock Out (Force)", timestamp_str])
    except Exception as e:
        await ctx.send(f"Failed to log force clock-out to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append force clock-out to Google Sheets for {member.name}: {e}")
        return

    # Remove active shift and save last clockout time
    del active_shifts[user_id]
    remove_active_shift_db(user_id)
    
    last_clockouts[user_id] = timestamp_str
    save_last_clockout_db(user_id, timestamp_str)

    await ctx.send(f"{member.mention} has been force clocked out at {timestamp_str}.")


if __name__ == '__main__':
    keep_alive()
    bot.run(os.environ['DISCORD_TOKEN'])
