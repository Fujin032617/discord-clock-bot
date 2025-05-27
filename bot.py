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
    # Always try to create with the latest schema. If it exists, IF NOT EXISTS will prevent re-creation.
    # The migration logic below handles adding missing columns to existing tables.
    c.execute('''CREATE TABLE IF NOT EXISTS active_shifts (user_id INTEGER PRIMARY KEY, clock_in TEXT, guild_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS last_clockouts (user_id INTEGER PRIMARY KEY, timestamp TEXT)''')

    # --- DATABASE MIGRATION LOGIC ---
    # Check if 'guild_id' column exists in 'active_shifts' table.
    # If not, add it using ALTER TABLE. This preserves existing data.
    try:
        c.execute("SELECT guild_id FROM active_shifts LIMIT 1")
    except sqlite3.OperationalError:
        print("Migrating active_shifts table: Adding 'guild_id' column...")
        c.execute("ALTER TABLE active_shifts ADD COLUMN guild_id INTEGER")
        conn.commit()
        print("Migration complete. Existing active shifts will have NULL for guild_id until re-clocked.")
    # --- END MIGRATION LOGIC ---

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

# MODIFIED: load_active_shifts_db to load guild_id
def load_active_shifts_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, clock_in, guild_id FROM active_shifts')
    rows = c.fetchall()
    conn.close()
    # Store as {user_id: {'clock_in': clock_in_time, 'guild_id': guild_id}}
    return {row['user_id']: {'clock_in': row['clock_in'], 'guild_id': row['guild_id']} for row in rows}

# MODIFIED: save_active_shift_db to accept guild_id
def save_active_shift_db(user_id, clock_in, guild_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO active_shifts (user_id, clock_in, guild_id) VALUES (?, ?, ?)', (user_id, clock_in, guild_id))
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
# MODIFIED: active_shifts now stores dicts
active_shifts = load_active_shifts_db() # Keys are integers, values are {'clock_in': str, 'guild_id': int}
last_clockouts = load_last_clockouts_db() # Keys are integers

ph_tz = timezone('Asia/Manila')

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    auto_clockout_expired_shifts.start()

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
        return False

    # 3. Check the last clock-out time (cooldown)
    cooldown_period = timedelta(minutes=5) # You can adjust this duration

    last_out_str = last_clockouts.get(user_id)
    if last_out_str:
        last_out_time = ph_tz.localize(datetime.strptime(last_out_str, "%Y-%m-%d %H:%M:%S"))
        if (now - last_out_time) < cooldown_period:
            return False

    # If all checks pass, the user can clock in
    return True

# ========== MODIFIED AUTOMATIC CLOCK-IN ONLY ==========
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    user_id = member.id
    guild_id = member.guild.id # Get the guild ID where the event occurred
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
        if can_clock_in(user_id): # The can_clock_in now includes the cooldown check
            # MODIFIED: Store guild_id in active_shifts
            active_shifts[user_id] = {'clock_in': timestamp_str, 'guild_id': guild_id}
            save_active_shift_db(user_id, timestamp_str, guild_id)

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


# ========== MANUAL CLOCK-IN/OUT COMMANDS ==========

@bot.command()
@commands.has_permissions(administrator=True) # THIS LINE RESTRICTS THE COMMAND TO ADMINS
async def clockin(ctx, member: discord.Member = None, *, username: str = None):
    target_user = None
    if member:
        target_user = member
    elif username:
        # Search for member by username
        found_members = [m for m in ctx.guild.members if m.name.lower() == username.lower()]
        if not found_members:
            await ctx.send(f'No user found with username "{username}". Please mention the user or provide exact username.')
            return
        target_user = found_members[0]
    else:
        # If no member or username provided, the admin is clocking themselves in.
        target_user = ctx.author

    user_id = target_user.id
    guild_id = ctx.guild.id
    target_name = target_user.name

    if user_id in excluded_user_ids:
        await ctx.send(f"{target_user.mention} is excluded from time tracking.")
        return

    if not can_clock_in(user_id):
        # Specific message depending on whether it's self or another user
        if target_user == ctx.author:
            await ctx.send(f"{ctx.author.mention}, you cannot clock in at this time. You might already be clocked in, or have clocked out too recently. If you wish to end your current shift, use `!clockout`.")
        else:
            await ctx.send(f"{target_user.mention} cannot be clocked in at this time. They might already be clocked in, or have clocked out too recently.")
        return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        sheet.append_row([target_name, "Clock In", timestamp_str])
    except Exception as e:
        await ctx.send(f"Failed to log {target_name}'s clock-in to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append manual clock-in to Google Sheets for {target_name}: {e}")
        return

    active_shifts[user_id] = {'clock_in': timestamp_str, 'guild_id': guild_id}
    save_active_shift_db(user_id, timestamp_str, guild_id)

    if target_user == ctx.author:
        await ctx.send(f"{ctx.author.mention} clocked in at {timestamp_str}.")
    else:
        await ctx.send(f"{target_user.mention} has been manually clocked in by {ctx.author.mention} at {timestamp_str}.")

@bot.command()
async def clockout(ctx):
    user_id = ctx.author.id
    if user_id in excluded_user_ids:
        await ctx.send(f"{ctx.author.mention}, you are excluded from time tracking.")
        return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    was_active = user_id in active_shifts # Check if they were active BEFORE modification

    # Log clock-out in Google Sheets
    try:
        sheet.append_row([ctx.author.name, "Clock Out", timestamp_str])
    except Exception as e:
        await ctx.send(f"Failed to log your clock-out to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append manual clock-out to Google Sheets for {ctx.author.name}: {e}")
        return

    # Attempt to remove active shift regardless if found, to ensure state consistency
    if was_active: # Only delete if it was present
        del active_shifts[user_id]
        remove_active_shift_db(user_id)

    last_clockouts[user_id] = timestamp_str
    save_last_clockout_db(user_id, timestamp_str)

    # Provide a more flexible response
    if was_active:
        await ctx.send(f"{ctx.author.mention} clocked out at {timestamp_str}.")
    else:
        await ctx.send(f"{ctx.author.mention} recorded a clock-out at {timestamp_str}. (Note: You were not registered as actively clocked in by the bot, but your clock-out has been logged and your status updated.)")


@tasks.loop(minutes=15)
async def auto_clockout_expired_shifts():
    now = datetime.now(ph_tz)
    expired = []

    # Iterate over a copy because we'll be modifying the dictionary
    for uid, shift_info in list(active_shifts.items()):
        # Handle cases where guild_id might be None (from pre-migration entries)
        clock_in_str = shift_info['clock_in']
        guild_id = shift_info.get('guild_id') # Use .get() to safely handle missing 'guild_id' key if somehow present

        clock_in_time = ph_tz.localize(datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S"))

        if now - clock_in_time >= timedelta(hours=14):
            user = bot.get_user(uid)
            name = user.name if user else f"User ID: {uid}"
            timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

            try:
                sheet.append_row([name, "Clock Out (Auto)", timestamp_str])
            except Exception as e:
                print(f"Failed to append auto clock-out to Google Sheets for {name}: {e}")

            last_clockouts[uid] = timestamp_str
            save_last_clockout_db(uid, timestamp_str)

            expired.append(uid)

            if user:
                # If guild_id is known, try to send message to that specific guild
                if guild_id:
                    guild = bot.get_guild(guild_id)
                    if guild:
                        member = guild.get_member(uid)
                        if member:
                            target_channel = guild.system_channel or \
                                             discord.utils.get(guild.text_channels, name='general') or \
                                             (guild.text_channels[0] if guild.text_channels else None)
                            if target_channel:
                                try:
                                    await target_channel.send(f"{user.mention} was automatically clocked out after 14 hours.")
                                except discord.Forbidden:
                                    print(f"Cannot send message to {target_channel.name} in {guild.name}.")
                        else:
                            print(f"User {user.name} (ID: {uid}) not found in guild {guild.name} (ID: {guild_id}) for auto clock-out notification.")
                    else:
                        print(f"Could not find Discord guild for ID {guild_id} for auto clock-out notification for user {name}.")
                else: # Fallback for old entries where guild_id is NULL
                    # Try to find user in any guild the bot is in and send a message.
                    # This is less ideal but better than no message.
                    found_guild_for_message = False
                    for guild_in_bot in bot.guilds:
                        member_in_guild = guild_in_bot.get_member(uid)
                        if member_in_guild:
                            target_channel = guild_in_bot.system_channel or \
                                             discord.utils.get(guild_in_bot.text_channels, name='general') or \
                                             (guild_in_bot.text_channels[0] if guild_in_bot.text_channels else None)
                            if target_channel:
                                try:
                                    await target_channel.send(f"{user.mention} was automatically clocked out after 14 hours. (Guild not identified precisely for this older entry.)")
                                    found_guild_for_message = True
                                    break # Only send to one guild if not specific
                                except discord.Forbidden:
                                    print(f"Cannot send message to {target_channel.name} in {guild_in_bot.name}.")
                    if not found_guild_for_message:
                        print(f"Could not find a guild to send auto clock-out notification for user {name} (ID: {uid}) with missing guild_id.")
            else:
                print(f"Could not find Discord user for ID {uid} for auto clock-out notification.")


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
    excluded_user_ids.add(target_user_id)

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
    excluded_user_ids.remove(target_user_id)

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
            excluded_names.append(f"Unknown User (ID: {uid})")
    await ctx.send("Excluded users:\n" + "\n".join(excluded_names))

@bot.command()
@commands.has_permissions(administrator=True)
async def onduty(ctx):
    """Show list of users currently clocked in (on duty) for the current guild."""
    guild_id = ctx.guild.id # Get the ID of the guild where the command was issued
    on_duty_in_guild = {}

    # Filter active_shifts for the current guild
    for uid, shift_info in active_shifts.items():
        # Only include entries where guild_id matches the current guild, AND guild_id is not NULL/None
        if shift_info.get('guild_id') == guild_id:
            on_duty_in_guild[uid] = shift_info['clock_in']

    if not on_duty_in_guild:
        await ctx.send("No users are currently on duty in this server.")
        return

    msg = "**Currently on duty in this server:**\n"
    for uid, clock_in_str in on_duty_in_guild.items():
        user = ctx.guild.get_member(uid) # Try to get member from *current guild*
        name = user.name if user else f"Unknown User (ID: {uid})"
        msg += f"- {name} (clocked in at {clock_in_str})\n"
    await ctx.send(msg)

@bot.command()
@commands.has_permissions(administrator=True)
async def forceclockout(ctx, member: discord.Member):
    """Force a user to clock out."""
    user_id = member.id
    # We remove the check here for flexibility, similar to !clockout
    # if user_id not in active_shifts:
    #    await ctx.send(f"{member.name} is not currently clocked in.")
    #    return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    was_active = user_id in active_shifts # Check if they were active BEFORE modification

    try:
        sheet.append_row([member.name, "Clock Out (Force)", timestamp_str])
    except Exception as e:
        await ctx.send(f"Failed to log force clock-out to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append force clock-out to Google Sheets for {member.name}: {e}")
        return

    # Attempt to remove active shift regardless if found, to ensure state consistency
    if was_active: # Only delete if it was present
        del active_shifts[user_id]
        remove_active_shift_db(user_id)

    last_clockouts[user_id] = timestamp_str
    save_last_clockout_db(user_id, timestamp_str)

    if was_active:
        await ctx.send(f"{member.mention} has been force clocked out at {timestamp_str}.")
    else:
        await ctx.send(f"{member.mention} has had a force clock-out logged at {timestamp_str}. (Note: They were not registered as actively clocked in by the bot, but their clock-out has been logged and status updated.)")


if __name__ == '__main__':
    keep_alive()
    bot.run(os.environ['DISCORD_TOKEN'])
