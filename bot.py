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
    c.execute('''CREATE TABLE IF NOT EXISTS active_shifts (user_id INTEGER PRIMARY KEY, clock_in TEXT, guild_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS last_clockouts (user_id INTEGER PRIMARY KEY, timestamp TEXT)''')

    # --- DATABASE MIGRATION LOGIC ---
    # This block ensures new columns are added if they don't exist from previous versions.
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

# Initialize the database tables when the script starts
init_db()

def load_excluded_users_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id FROM excluded_users')
    rows = c.fetchall()
    conn.close()
    return {row['user_id'] for row in rows}

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
    c.execute('SELECT user_id, clock_in, guild_id FROM active_shifts')
    rows = c.fetchall()
    conn.close()
    return {row['user_id']: {'clock_in': row['clock_in'], 'guild_id': row['guild_id']} for row in rows}

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
    return {row['user_id']: row['timestamp'] for row in rows}

def save_last_clockout_db(user_id, timestamp):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO last_clockouts (user_id, timestamp) VALUES (?, ?)', (user_id, timestamp))
    conn.commit()
    conn.close()

# Discord Bot Setup
intents = discord.Intents.default()
intents.members = True # Essential for getting member info, including name
intents.message_content = True # Essential for reading command messages
intents.voice_states = True # Crucial for voice channel events for auto clock-in
bot = commands.Bot(command_prefix='!', intents=intents)

# Google Sheets Setup
# Ensure your GOOGLE_CREDS environment variable contains the JSON key
scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds_json = json.loads(os.environ['GOOGLE_CREDS'])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
# Replace "Employee Time Log" with the actual name of your Google Sheet
sheet = client.open("Employee Time Log").sheet1

# Load data into memory from SQLite using the functions
excluded_user_ids = load_excluded_users_db() # This is now a set of excluded user IDs
# active_shifts now stores dicts {user_id: {'clock_in': str, 'guild_id': int}}
active_shifts = load_active_shifts_db()
last_clockouts = load_last_clockouts_db() # Keys are integers, values are timestamp strings

# Set the timezone for accurate timekeeping
ph_tz = timezone('Asia/Manila') # Philippine Timezone

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    # Start the auto-clockout loop when the bot is ready
    auto_clockout_expired_shifts.start()

def can_clock_in(user_id):
    """
    Checks if a user is eligible for an automatic clock-in.
    Rules: Not excluded, no active shift, and not recently clocked out (cooldown).
    """
    now = datetime.now(ph_tz)

    if user_id in excluded_user_ids:
        return False # User is excluded

    if user_id in active_shifts:
        return False # User already has an active shift

    # Cooldown period after last clock-out to prevent immediate re-clocking
    cooldown_period = timedelta(minutes=5) # You can adjust this duration
    last_out_str = last_clockouts.get(user_id)
    if last_out_str:
        # Convert the stored string timestamp back to a timezone-aware datetime object
        last_out_time = ph_tz.localize(datetime.strptime(last_out_str, "%Y-%m-%d %H:%M:%S"))
        if (now - last_out_time) < cooldown_period:
            return False # Still within cooldown period

    return True # All checks pass, user can clock in

# ========== AUTOMATIC CLOCK-IN VIA VOICE CHANNEL EVENTS ==========
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    user_id = member.id
    guild_id = member.guild.id # Get the guild ID where the voice state change occurred
    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Ignore bots to prevent self-triggering or tracking other bots
    if member.bot:
        return

    # Ignore users who are in the excluded list
    if user_id in excluded_user_ids:
        return

    # Auto Clock-in Logic: User joins a voice channel (and wasn't in one before, or moved channels)
    # This triggers a clock-in if they move into *any* channel, assuming they weren't in one before.
    if after.channel and (before.channel != after.channel):
        if can_clock_in(user_id): # Check if the user is eligible for clock-in based on rules
            # Store the clock-in time and the guild ID where they clocked in
            active_shifts[user_id] = {'clock_in': timestamp_str, 'guild_id': guild_id}
            save_active_shift_db(user_id, timestamp_str, guild_id) # Persist to DB

            try:
                # Log the auto clock-in to Google Sheets
                sheet.append_row([member.name, "Clock In", timestamp_str])
                print(f"Logged auto clock-in for {member.name} at {timestamp_str}")
            except Exception as e:
                print(f"Failed to append auto clock-in to Google Sheets for {member.name}: {e}")

            # Send a notification to a relevant channel (e.g., system channel or general)
            channel_to_send = member.guild.system_channel or \
                              (discord.utils.get(member.guild.text_channels, name='general')) or \
                              (member.guild.text_channels[0] if member.guild.text_channels else None)
            if channel_to_send:
                try:
                    await channel_to_send.send(f"{member.mention} has automatically clocked in (joined voice channel).")
                except discord.Forbidden:
                    print(f"Cannot send message to {channel_to_send.name} in {member.guild.name} (Forbidden).")


# ========== MANUAL EMPLOYEE COMMANDS & ADMIN COMMANDS ==========

@bot.command()
@commands.has_permissions(administrator=True) # THIS LINE RESTRICTS THIS COMMAND TO ADMINS ONLY
async def clockin(ctx, member: discord.Member = None, *, username: str = None):
    """
    Admin command to manually clock in a user.
    Usage: !clockin @User or !clockin Username
    If no user specified, attempts to clock in the admin.
    """
    target_user = None
    if member:
        target_user = member
    elif username:
        # Search for member by username (case-insensitive)
        found_members = [m for m in ctx.guild.members if m.name.lower() == username.lower()]
        if not found_members:
            await ctx.send(f'No user found with username "{username}". Please mention the user or provide exact username.')
            return
        target_user = found_members[0] # Take the first match
    else:
        # If no member or username provided, the admin is clocking themselves in.
        target_user = ctx.author

    user_id = target_user.id
    guild_id = ctx.guild.id # The guild where the command was issued
    target_name = target_user.name

    if user_id in excluded_user_ids:
        await ctx.send(f"{target_user.mention} is excluded from time tracking and cannot be clocked in.")
        return

    if not can_clock_in(user_id):
        # Provide specific feedback if they cannot clock in
        if target_user == ctx.author:
            await ctx.send(f"{ctx.author.mention}, you cannot clock in at this time. You might already be clocked in, or have clocked out too recently. If you wish to end your current shift, use `!clockout` (if applicable).")
        else:
            await ctx.send(f"{target_user.mention} cannot be clocked in at this time. They might already be clocked in, or have clocked out too recently.")
        return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Log the manual clock-in to Google Sheets
        sheet.append_row([target_name, "Clock In", timestamp_str])
        print(f"Logged manual clock-in for {target_name} at {timestamp_str}")
    except Exception as e:
        await ctx.send(f"Failed to log {target_name}'s clock-in to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append manual clock-in to Google Sheets for {target_name}: {e}")
        return # Exit if Google Sheet logging fails

    # Update active_shifts and persist to DB
    active_shifts[user_id] = {'clock_in': timestamp_str, 'guild_id': guild_id}
    save_active_shift_db(user_id, timestamp_str, guild_id)

    # Confirm to the user who issued the command
    if target_user == ctx.author:
        await ctx.send(f"{ctx.author.mention} clocked in at {timestamp_str}.")
    else:
        await ctx.send(f"{target_user.mention} has been manually clocked in by {ctx.author.mention} at {timestamp_str}.")

@bot.command()
async def clockout(ctx):
    """
    Employee command to manually clock out.
    Usage: !clockout
    """
    user_id = ctx.author.id
    user_name = ctx.author.name # Get name for logging

    if user_id in excluded_user_ids:
        await ctx.send(f"❌ {ctx.author.mention}, you are excluded from time tracking and cannot clock out.")
        return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Check if the user was considered active by the bot before processing
    was_active = user_id in active_shifts

    try:
        # Log the manual clock-out to Google Sheets
        sheet.append_row([user_name, "Clock Out", timestamp_str])
        print(f"Logged manual clock-out for {user_name} at {timestamp_str}")
    except Exception as e:
        await ctx.send(f"Failed to log your clock-out to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append manual clock-out to Google Sheets for {user_name}: {e}")
        return # Exit if Google Sheet logging fails

    # If they were active, remove their shift from active_shifts and DB
    if was_active:
        del active_shifts[user_id]
        remove_active_shift_db(user_id)

    # Update last_clockouts regardless, as a successful clock-out (manual or auto) updates this
    last_clockouts[user_id] = timestamp_str
    save_last_clockout_db(user_id, timestamp_str)

    # Provide a flexible response based on if they were actively clocked in
    if was_active:
        await ctx.send(f"✅ {ctx.author.mention} clocked out at {timestamp_str}.")
    else:
        await ctx.send(f"⚠️ {ctx.author.mention} recorded a clock-out at {timestamp_str}. (Note: You were not actively registered as clocked in by the bot, but your clock-out has been logged and your status updated.)")


@bot.command()
async def status(ctx):
    """Allows an employee to check their current clock-in/out status."""
    user_id = ctx.author.id

    if user_id in excluded_user_ids:
        await ctx.send(f"❌ {ctx.author.mention}, you are currently **excluded** from time tracking. Please contact an administrator if this is an error.")
        return

    if user_id in active_shifts:
        # User is clocked in
        shift_info = active_shifts[user_id]
        clock_in_str = shift_info['clock_in']

        try:
            # Convert stored string to datetime object and localize it
            clock_in_time = ph_tz.localize(datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S"))
            # Format for better readability
            display_time = clock_in_time.strftime("%I:%M %p on %B %d, %Y")
            await ctx.send(f"🟢 {ctx.author.mention}, you are currently **Clocked In** since {display_time}.")
        except ValueError:
            # Handle cases where the stored datetime string might be malformed
            await ctx.send(f"⚠️ {ctx.author.mention}, your clock-in time data is corrupted. Please contact an administrator.")
            print(f"Error parsing clock_in_time for user {user_id}: {clock_in_str}")
    else:
        # User is clocked out
        last_out_str = last_clockouts.get(user_id)
        if last_out_str:
            # Show their last clock-out time if available
            try:
                last_out_time = ph_tz.localize(datetime.strptime(last_out_str, "%Y-%m-%d %H:%M:%S"))
                display_time = last_out_time.strftime("%I:%M %p on %B %d, %Y")
                await ctx.send(f"🔴 {ctx.author.mention}, you are currently **Clocked Out**. Your last recorded clock-out was at {display_time}.")
            except ValueError:
                 # Handle cases where last clock-out string might be malformed
                 await ctx.send(f"🔴 {ctx.author.mention}, you are currently **Clocked Out**. (Last clock-out time data unavailable or corrupted.)")
                 print(f"Error parsing last_out_time for user {user_id}: {last_out_str}")
        else:
            # No clock-in or clock-out records found for the user at all
            await ctx.send(f"🔴 {ctx.author.mention}, you are currently **Clocked Out**. (No previous clock-in/out records found.)")


# ========== BACKGROUND TASKS ==========

@tasks.loop(minutes=15) # This task runs every 15 minutes
async def auto_clockout_expired_shifts():
    print("Running auto_clockout_expired_shifts task...")
    now = datetime.now(ph_tz)
    expired = [] # List to hold user IDs of shifts to be expired

    # Iterate over a copy of active_shifts because we'll be modifying the original dictionary
    for uid, shift_info in list(active_shifts.items()):
        clock_in_str = shift_info['clock_in']
        guild_id = shift_info.get('guild_id') # Safely get guild_id (can be None from old entries)

        # Convert the stored clock-in string to a timezone-aware datetime object
        try:
            clock_in_time = ph_tz.localize(datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            print(f"Warning: Corrupted clock-in string for user {uid}: '{clock_in_str}'. Skipping auto-clockout for this user.")
            continue # Skip this user if time string is unparseable

        # Check if the shift has exceeded the 14-hour limit
        if now - clock_in_time >= timedelta(hours=14):
            user = bot.get_user(uid) # Get the Discord user object
            name = user.name if user else f"User ID: {uid}"
            timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

            try:
                # Log the auto clock-out to Google Sheets
                sheet.append_row([name, "Clock Out (Auto)", timestamp_str])
                print(f"Logged auto clock-out for {name} at {timestamp_str}")
            except Exception as e:
                print(f"Failed to append auto clock-out to Google Sheets for {name}: {e}")

            # Update last_clockouts and persist (important for cooldown)
            last_clockouts[uid] = timestamp_str
            save_last_clockout_db(uid, timestamp_str)

            expired.append(uid) # Mark this user's shift as expired

            # Attempt to send a notification to the user in a relevant channel
            if user:
                if guild_id: # If we know the specific guild they clocked in from
                    guild = bot.get_guild(guild_id)
                    if guild:
                        member = guild.get_member(uid) # Get guild-specific member object
                        if member:
                            # Try system channel, then 'general', then any available text channel
                            target_channel = guild.system_channel or \
                                             discord.utils.get(guild.text_channels, name='general') or \
                                             (guild.text_channels[0] if guild.text_channels else None)
                            if target_channel:
                                try:
                                    await target_channel.send(f"⚠️ {user.mention} was automatically clocked out after 14 hours. Please remember to `!clockout` manually at the end of your shift.")
                                except discord.Forbidden:
                                    print(f"Cannot send message to {target_channel.name} in {guild.name} (Forbidden).")
                        else:
                            print(f"User {user.name} (ID: {uid}) not found as member in guild {guild.name} (ID: {guild_id}) for auto clock-out notification.")
                    else:
                        print(f"Could not find Discord guild for ID {guild_id} for auto clock-out notification for user {name}.")
                else: # Fallback for older entries where guild_id might be NULL
                    found_guild_for_message = False
                    for guild_in_bot in bot.guilds:
                        member_in_guild = guild_in_bot.get_member(uid)
                        if member_in_guild:
                            target_channel = guild_in_bot.system_channel or \
                                             discord.utils.get(guild_in_bot.text_channels, name='general') or \
                                             (guild_in_bot.text_channels[0] if guild_in_bot.text_channels else None)
                            if target_channel:
                                try:
                                    await target_channel.send(f"⚠️ {user.mention} was automatically clocked out after 14 hours. Please remember to `!clockout` manually at the end of your shift.")
                                    found_guild_for_message = True
                                    break # Only send to one guild if not specific
                                except discord.Forbidden:
                                    print(f"Cannot send message to {target_channel.name} in {guild_in_bot.name} (Forbidden).")
                    if not found_guild_for_message:
                        print(f"Could not find a guild to send auto clock-out notification for user {name} (ID: {uid}) with missing guild_id.")
            else:
                print(f"Could not find Discord user for ID {uid} for auto clock-out notification.")

    # After iterating, remove all expired shifts from active_shifts and the database
    for uid in expired:
        del active_shifts[uid]
        remove_active_shift_db(uid)

# ========== BOT STARTUP ==========
if __name__ == '__main__':
    keep_alive() # Starts the Flask web server in a separate thread
    # Run the Discord bot using your token from environment variables
    bot.run(os.environ['DISCORD_TOKEN'])
