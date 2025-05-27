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

# --- 1. Configuration & Setup ---

# Setup persistent directory for hosting platforms like Render or fallback to current directory
data_dir = os.getenv('RENDER_DATA_DIR', '.')
os.makedirs(data_dir, exist_ok=True) # Ensure the directory exists
db_path = os.path.join(data_dir, 'bot_data.db') # Path to your SQLite database file

# Flask app to keep the bot alive (for web hosting services like Render)
app = Flask('')

def run_flask_app():
    """Runs the Flask web server."""
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    """Starts the Flask app in a separate thread."""
    t = Thread(target=run_flask_app)
    t.start()

@app.route('/')
def home():
    """Simple 'I'm alive!' endpoint for health checks."""
    response = make_response("I'm alive!")
    # Security headers (good practice)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# --- 2. SQLite Database Functions ---

def get_db_connection():
    """Establishes a connection to the SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row # Allows accessing columns by name
    return conn

def init_db():
    """Initializes database tables if they don't exist, and handles schema migrations."""
    conn = get_db_connection()
    c = conn.cursor()

    # Create tables if they don't exist
    c.execute('''CREATE TABLE IF NOT EXISTS excluded_users (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_shifts (user_id INTEGER PRIMARY KEY, clock_in TEXT, guild_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS last_clockouts (user_id INTEGER PRIMARY KEY, timestamp TEXT)''')

    # --- Database Migration Logic ---
    # This block ensures 'guild_id' column exists in 'active_shifts' table for new features.
    # It adds the column without losing existing data if the table was created before this column was added.
    try:
        c.execute("SELECT guild_id FROM active_shifts LIMIT 1")
    except sqlite3.OperationalError:
        print("Migrating active_shifts table: Adding 'guild_id' column...")
        c.execute("ALTER TABLE active_shifts ADD COLUMN guild_id INTEGER")
        conn.commit()
        print("Migration complete. Existing active shifts will have NULL for guild_id until re-clocked.")
    # --- End Migration Logic ---

    conn.commit()
    conn.close()

# Initialize the database tables when the script starts
init_db()

# Functions for interacting with excluded_users table
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

# Functions for interacting with active_shifts table
def load_active_shifts_db():
    conn = get_db_connection()
    c = conn.cursor()
    # Select guild_id along with user_id and clock_in
    c.execute('SELECT user_id, clock_in, guild_id FROM active_shifts')
    rows = c.fetchall()
    conn.close()
    # Store as {user_id: {'clock_in': clock_in_time_str, 'guild_id': guild_id}}
    return {row['user_id']: {'clock_in': row['clock_in'], 'guild_id': row['guild_id']} for row in rows}

def save_active_shift_db(user_id, clock_in, guild_id):
    conn = get_db_connection()
    c = conn.cursor()
    # Insert or replace the shift, including guild_id
    c.execute('INSERT OR REPLACE INTO active_shifts (user_id, clock_in, guild_id) VALUES (?, ?, ?)', (user_id, clock_in, guild_id))
    conn.commit()
    conn.close()

def remove_active_shift_db(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM active_shifts WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

# Functions for interacting with last_clockouts table
def load_last_clockouts_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, timestamp FROM last_clockouts')
    rows = c.fetchall()
    conn.close()
    return {row['user_id']: row['timestamp'] for row in rows} # Keys are integers, values are timestamp strings

def save_last_clockout_db(user_id, timestamp):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO last_clockouts (user_id, timestamp) VALUES (?, ?)', (user_id, timestamp))
    conn.commit()
    conn.close()

# --- 3. Discord Bot Setup ---

# Define intents (permissions your bot needs)
intents = discord.Intents.default()
intents.members = True          # Required to get member info (names, IDs)
intents.message_content = True  # Required to read command messages
intents.voice_states = True     # Required for tracking voice channel activity (auto clock-in)

# Initialize the bot with a command prefix and intents
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 4. Google Sheets Setup ---

# Define the scope for Google Sheets API access
scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

# Load Google service account credentials from an environment variable
# This environment variable (e.g., GOOGLE_CREDS) should contain the JSON key file content as a string.
creds_json = json.loads(os.environ['GOOGLE_CREDS'])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)

# Open your specific Google Sheet by name
# IMPORTANT: Replace "Employee Time Log" with the exact name of your Google Sheet.
# Also, ensure the service account email has "Editor" access to this Google Sheet.
sheet = client.open("Employee Time Log").sheet1 # Assuming you want to interact with the first sheet

# --- 5. In-Memory Data Storage (Loaded from DB) ---

# Load initial data into memory from SQLite database when the bot starts
excluded_user_ids = load_excluded_users_db() # Set of user IDs who are excluded
# Dictionary storing active shifts: {user_id: {'clock_in': 'timestamp_str', 'guild_id': guild_id}}
active_shifts = load_active_shifts_db()
last_clockouts = load_last_clockouts_db() # Dictionary storing last clock-out times: {user_id: 'timestamp_str'}

# Define the timezone for all time-related operations (e.g., Philippines)
ph_tz = timezone('Asia/Manila')

# --- 6. Bot Events (on_ready, on_voice_state_update) ---

@bot.event
async def on_ready():
    """Called when the bot successfully connects to Discord."""
    print(f'Bot is online as {bot.user.name} (ID: {bot.user.id})')
    # Start the background task for auto-clocking out expired shifts
    auto_clockout_expired_shifts.start()

def can_clock_in(user_id):
    """
    Determines if a user is eligible for an automatic clock-in.
    Rules: Not in excluded list, no active shift, and not recently clocked out (cooldown period).
    """
    now = datetime.now(ph_tz)

    if user_id in excluded_user_ids:
        return False # User is explicitly excluded from time tracking

    if user_id in active_shifts:
        return False # User already has an active shift recorded

    # Cooldown period: Prevent immediate re-clocking after a clock-out
    cooldown_period = timedelta(minutes=5) # You can adjust this duration (e.g., 5 minutes)
    last_out_str = last_clockouts.get(user_id) # Get the last clock-out timestamp string

    if last_out_str: # If there's a record of a last clock-out
        try:
            # Convert the stored string timestamp back to a timezone-aware datetime object
            last_out_time = ph_tz.localize(datetime.strptime(last_out_str, "%Y-%m-%d %H:%M:%S"))
            if (now - last_out_time) < cooldown_period:
                return False # Still within the cooldown period
        except ValueError:
            # Log an error if the stored timestamp string is corrupted, but don't block clock-in
            print(f"Warning: Corrupted last_clockout timestamp for user {user_id}: '{last_out_str}'. Allowing clock-in.")
            pass # Continue to allow clock-in if timestamp is unparseable

    return True # All checks pass, the user is eligible to clock in

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """
    Handles automatic clock-in/out based on voice channel activity.
    Triggers a clock-in when a user joins or moves into a voice channel.
    """
    user_id = member.id
    guild_id = member.guild.id # Get the ID of the guild where the voice state change occurred
    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    if member.bot:
        return # Ignore actions by other bots

    if user_id in excluded_user_ids:
        return # Ignore actions by users in the excluded list

    # Auto Clock-in Logic: When a user joins *any* voice channel (or moves between channels)
    # This prevents clocking in multiple times if they are already in a voice channel.
    if after.channel and (before.channel != after.channel):
        if can_clock_in(user_id): # Check if the user is eligible to clock in based on rules
            # Record the clock-in time and the guild ID
            active_shifts[user_id] = {'clock_in': timestamp_str, 'guild_id': guild_id}
            save_active_shift_db(user_id, timestamp_str, guild_id) # Persist to SQLite DB

            try:
                # Log the auto clock-in to Google Sheets
                sheet.append_row([member.name, "Clock In", timestamp_str])
                print(f"Logged auto clock-in for {member.name} at {timestamp_str}")
            except Exception as e:
                print(f"Failed to append auto clock-in to Google Sheets for {member.name}: {e}")

            # Send a notification message to a visible channel in the guild
            channel_to_send = member.guild.system_channel or \
                              discord.utils.get(member.guild.text_channels, name='general') or \
                              (member.guild.text_channels[0] if member.guild.text_channels else None)
            if channel_to_send:
                try:
                    await channel_to_send.send(f"✅ {member.mention} has automatically clocked in (joined voice channel).")
                except discord.Forbidden:
                    print(f"Cannot send message to {channel_to_send.name} in {member.guild.name} (Forbidden: Bot lacks permissions).")

# --- 7. Discord Bot Commands ---

# ========== Admin Commands (Require Administrator Permissions) ==========

@bot.command()
@commands.has_permissions(administrator=True) # Only users with administrator role can use this
async def clockin(ctx, member: discord.Member = None, *, username: str = None):
    """
    Admin command: Manually clocks in a specified user or the admin themselves.
    Usage: !clockin @User | !clockin Username | !clockin
    """
    target_user = None
    if member: # If a Discord member is mentioned
        target_user = member
    elif username: # If a username string is provided
        # Search for member by username (case-insensitive) in the current guild
        found_members = [m for m in ctx.guild.members if m.name.lower() == username.lower()]
        if not found_members:
            await ctx.send(f'❌ No user found with username "{username}". Please mention the user or provide exact username.')
            return
        target_user = found_members[0] # Take the first match
    else: # If no user is specified, assume the command issuer (admin)
        target_user = ctx.author

    user_id = target_user.id
    guild_id = ctx.guild.id # The guild where the command was issued
    target_name = target_user.name # Display name for logging

    if user_id in excluded_user_ids:
        await ctx.send(f"❌ {target_user.mention} is excluded from time tracking and cannot be clocked in.")
        return

    if not can_clock_in(user_id):
        # Provide specific feedback if they cannot clock in due to existing shift or cooldown
        if target_user == ctx.author:
            await ctx.send(f"⚠️ {ctx.author.mention}, you cannot clock in at this time. You might already be clocked in, or have clocked out too recently. If you wish to end your current shift, use `!clockout` (if applicable).")
        else:
            await ctx.send(f"⚠️ {target_user.mention} cannot be clocked in at this time. They might already be clocked in, or have clocked out too recently.")
        return

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Log the manual clock-in to Google Sheets
        sheet.append_row([target_name, "Clock In", timestamp_str])
        print(f"Logged manual clock-in for {target_name} at {timestamp_str}")
    except Exception as e:
        await ctx.send(f"❌ Failed to log {target_name}'s clock-in to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append manual clock-in to Google Sheets for {target_name}: {e}")
        return # Stop execution if Google Sheet logging fails

    # Store the active shift in memory and persist to DB
    active_shifts[user_id] = {'clock_in': timestamp_str, 'guild_id': guild_id}
    save_active_shift_db(user_id, timestamp_str, guild_id)

    # Confirm the action to the user who issued the command
    if target_user == ctx.author:
        await ctx.send(f"✅ {ctx.author.mention} clocked in at {timestamp_str}.")
    else:
        await ctx.send(f"✅ {target_user.mention} has been manually clocked in by {ctx.author.mention} at {timestamp_str}.")

@bot.command()
@commands.has_permissions(administrator=True)
async def exclude(ctx, member: discord.Member = None, *, username: str = None):
    """
    Admin command: Excludes a user from time tracking.
    Usage: !exclude @User | !exclude Username
    """
    target_user_id = None
    target_user_name = None

    if member is not None:
        target_user_id = member.id
        target_user_name = member.name
    elif username:
        found_members = [m for m in ctx.guild.members if m.name.lower() == username.lower()]
        if not found_members:
            await ctx.send(f'❌ No user found with username "{username}". Please mention the user or provide exact username.')
            return
        target_user_id = found_members[0].id
        target_user_name = found_members[0].name
    else:
        await ctx.send("❌ Please mention a user or provide a username to exclude.")
        return

    if target_user_id in excluded_user_ids:
        await ctx.send(f"⚠️ {target_user_name} is already excluded.")
        return

    save_excluded_user_db(target_user_id) # Persist to DB
    excluded_user_ids.add(target_user_id) # Add to in-memory set

    # If the excluded user currently has an active shift, end it
    if target_user_id in active_shifts:
        del active_shifts[target_user_id]
        remove_active_shift_db(target_user_id) # Remove from DB

    await ctx.send(f"✅ {target_user_name} has been excluded from time tracking.")

@bot.command()
@commands.has_permissions(administrator=True)
async def include(ctx, member: discord.Member = None, *, username: str = None):
    """
    Admin command: Includes a user back into time tracking.
    Usage: !include @User | !include Username
    """
    target_user_id = None
    target_user_name = None

    if member is not None:
        target_user_id = member.id
        target_user_name = member.name
    elif username:
        found_members = [m for m in ctx.guild.members if m.name.lower() == username.lower()]
        if not found_members:
            await ctx.send(f'❌ No user found with username "{username}". Please mention the user or provide exact username.')
            return
        target_user_id = found_members[0].id
        target_user_name = found_members[0].name
    else:
        await ctx.send("❌ Please mention a user or provide a username to include.")
        return

    if target_user_id not in excluded_user_ids:
        await ctx.send(f"⚠️ {target_user_name} is not currently excluded.")
        return

    remove_excluded_user_db(target_user_id) # Remove from DB
    excluded_user_ids.remove(target_user_id) # Remove from in-memory set

    await ctx.send(f"✅ {target_user_name} has been included back in time tracking.")

@bot.command()
@commands.has_permissions(administrator=True)
async def listexcluded(ctx):
    """
    Admin command: Lists all users currently excluded from time tracking.
    Usage: !listexcluded
    """
    if not excluded_user_ids:
        await ctx.send("ℹ️ No users are currently excluded from time tracking.")
        return

    excluded_names = []
    for uid in excluded_user_ids:
        user = bot.get_user(uid) # Get the Discord user object
        if user:
            excluded_names.append(user.name)
        else:
            excluded_names.append(f"Unknown User (ID: {uid})") # Fallback if user object not found

    await ctx.send("🚫 **Excluded users:**\n" + "\n".join(excluded_names))

@bot.command()
@commands.has_permissions(administrator=True)
async def onduty(ctx):
    """
    Admin command: Shows a list of users currently clocked in (on duty) for the current guild.
    Includes User IDs for debugging purposes.
    Usage: !onduty
    """
    guild_id = ctx.guild.id # Get the ID of the guild where the command was issued
    on_duty_in_guild = {} # Dictionary to store active shifts relevant to this guild

    # Filter active_shifts to only include entries from the current guild
    for uid, shift_info in active_shifts.items():
        # Only include if guild_id matches AND guild_id is not NULL/None (for older entries)
        if shift_info.get('guild_id') == guild_id and shift_info.get('guild_id') is not None:
            on_duty_in_guild[uid] = shift_info['clock_in']

    if not on_duty_in_guild:
        await ctx.send("ℹ️ No users are currently on duty in this server.")
        return

    msg = "✅ **Currently on duty in this server:**\n"
    for uid, clock_in_str in on_duty_in_guild.items():
        user = ctx.guild.get_member(uid) # Try to get member from *current guild*
        name = user.display_name if user else f"Unknown User (ID: {uid})" # Use display_name, fallback if not found
        # IMPORTANT: This line now includes the `uid` for debugging.
        msg += f"- {name} (ID: `{uid}`) clocked in at {clock_in_str}\n"
    await ctx.send(msg)

@bot.command()
@commands.has_permissions(administrator=True)
async def forceclockout(ctx, member: discord.Member):
    """
    Admin command: Forces a specified user to clock out.
    Usage: !forceclockout @User
    """
    user_id = member.id
    user_name = member.name # Display name for logging

    now = datetime.now(ph_tz)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Check if the user was considered active by the bot before processing
    was_active = user_id in active_shifts

    try:
        # Log the force clock-out to Google Sheets
        sheet.append_row([user_name, "Clock Out (Force)", timestamp_str])
        print(f"Logged force clock-out for {user_name} at {timestamp_str}")
    except Exception as e:
        await ctx.send(f"❌ Failed to log force clock-out to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append force clock-out to Google Sheets for {user_name}: {e}")
        return # Stop execution if Google Sheet logging fails

    # If they were active, remove their shift from active_shifts and DB
    if was_active:
        del active_shifts[user_id]
        remove_active_shift_db(user_id)

    # Update last_clockouts regardless (important for cooldown on subsequent clock-ins)
    last_clockouts[user_id] = timestamp_str
    save_last_clockout_db(user_id, timestamp_str)

    # Confirm the action to the admin and indicate previous status
    if was_active:
        await ctx.send(f"✅ {member.mention} has been force clocked out at {timestamp_str}.")
    else:
        await ctx.send(f"⚠️ {member.mention} has had a force clock-out logged at {timestamp_str}. (Note: They were not actively registered as clocked in by the bot, but their clock-out has been logged and status updated.)")

# ========== Employee Commands (Accessible by all users) ==========

@bot.command()
async def clockout(ctx):
    """
    Employee command: Manually clocks out the user who sent the command.
    Usage: !clockout
    """
    user_id = ctx.author.id
    user_name = ctx.author.name # Display name for logging

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
        await ctx.send(f"❌ Failed to log your clock-out to Google Sheets. Please contact an admin. Error: {e}")
        print(f"Failed to append manual clock-out to Google Sheets for {user_name}: {e}")
        return # Stop execution if Google Sheet logging fails

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
    """
    Employee command: Allows a user to check their current clock-in/out status.
    Usage: !status
    """
    user_id = ctx.author.id

    if user_id in excluded_user_ids:
        await ctx.send(f"❌ {ctx.author.mention}, you are currently **excluded** from time tracking. Please contact an administrator if this is an error.")
        return

    if user_id in active_shifts:
        # User is clocked in
        shift_info = active_shifts[user_id]
        clock_in_str = shift_info['clock_in']

        try:
            # Convert stored string to a timezone-aware datetime object
            clock_in_time = ph_tz.localize(datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S"))
            # Format the datetime for a user-friendly display
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
                 # Handle cases where the last clock-out string might be malformed
                 await ctx.send(f"🔴 {ctx.author.mention}, you are currently **Clocked Out**. (Last clock-out time data unavailable or corrupted.)")
                 print(f"Error parsing last_out_time for user {user_id}: {last_out_str}")
        else:
            # No clock-in or clock-out records found for the user at all
            await ctx.send(f"🔴 {ctx.author.mention}, you are currently **Clocked Out**. (No previous clock-in/out records found.)")

@bot.command()
async def myid(ctx):
    """
    Employee command: Shows your Discord User ID.
    Useful for debugging issues where your bot status might not match.
    Usage: !myid
    """
    await ctx.send(f"🤖 Your Discord User ID is: `{ctx.author.id}`")


# --- 8. Background Task: Auto Clock-out Expired Shifts ---

@tasks.loop(minutes=15) # This task runs every 15 minutes
async def auto_clockout_expired_shifts():
    """
    Automatically clocks out users whose shifts have exceeded a maximum duration (e.g., 14 hours).
    Prevents shifts from running indefinitely if a manual clock-out is missed.
    """
    print(f"Running auto_clockout_expired_shifts task at {datetime.now(ph_tz).strftime('%Y-%m-%d %H:%M:%S')}...")
    now = datetime.now(ph_tz)
    expired = [] # List to hold user IDs of shifts that need to be expired

    # Iterate over a copy of active_shifts because we'll be modifying the original dictionary
    for uid, shift_info in list(active_shifts.items()):
        clock_in_str = shift_info['clock_in']
        guild_id = shift_info.get('guild_id') # Safely get guild_id (can be None from old entries)

        try:
            # Convert the stored clock-in string to a timezone-aware datetime object
            clock_in_time = ph_tz.localize(datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            print(f"Warning: Corrupted clock-in string for user {uid}: '{clock_in_str}'. Skipping auto-clockout for this user.")
            continue # Skip this user if the time string is unparseable

        # Check if the shift duration has exceeded 14 hours
        if now - clock_in_time >= timedelta(hours=14):
            user = bot.get_user(uid) # Get the Discord user object
            name = user.name if user else f"User ID: {uid}" # Fallback name if user object not found
            timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

            try:
                # Log the auto clock-out event to Google Sheets
                sheet.append_row([name, "Clock Out (Auto)", timestamp_str])
                print(f"Logged auto clock-out for {name} at {timestamp_str}")
            except Exception as e:
                print(f"Failed to append auto clock-out to Google Sheets for {name}: {e}")

            # Update last_clockouts for cooldown purposes and persist
            last_clockouts[uid] = timestamp_str
            save_last_clockout_db(uid, timestamp_str)

            expired.append(uid) # Add user ID to the list of expired shifts

            # Attempt to send a notification message to the user in a relevant channel
            if user:
                if guild_id: # If we know the specific guild they clocked in from (preferred)
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
                                    print(f"Cannot send message to {target_channel.name} in {guild.name} (Forbidden: Bot lacks permissions).")
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
                                    print(f"Cannot send message to {target_channel.name} in {guild_in_bot.name} (Forbidden: Bot lacks permissions).")
                    if not found_guild_for_message:
                        print(f"Could not find a guild to send auto clock-out notification for user {name} (ID: {uid}) with missing guild_id.")
            else:
                print(f"Could not find Discord user object for ID {uid} for auto clock-out notification.")

    # After iterating through all shifts, remove the expired ones from memory and DB
    for uid in expired:
        del active_shifts[uid]
        remove_active_shift_db(uid)

# --- 9. Bot Startup ---

if __name__ == '__main__':
    keep_alive() # Start the Flask web server in a separate thread
    # Run the Discord bot using your token from environment variables
    # The DISCORD_TOKEN environment variable MUST be set.
    bot.run(os.environ['DISCORD_TOKEN'])
