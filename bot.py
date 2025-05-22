import os
import json
from flask import Flask, make_response, request
from threading import Thread
import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from pytz import timezone

# Define the file path for excluded users
EXCLUDED_USERS_FILE = 'excluded_users.json'

# --- Functions to manage excluded users ---
def load_excluded_users():
    if os.path.exists(EXCLUDED_USERS_FILE):
        with open(EXCLUDED_USERS_FILE, 'r') as f:
            try:
                data = json.load(f)
                return data.get('user_ids', [])
            except json.JSONDecodeError:
                print(f"Error decoding JSON from {EXCLUDED_USERS_FILE}. Starting with empty list.")
                return []
    return []

def save_excluded_users(user_ids):
    with open(EXCLUDED_USERS_FILE, 'w') as f:
        json.dump({'user_ids': user_ids}, f, indent=4)
# ----------------------------------------

# ----- Keep Alive Server -----
app = Flask('')

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ----- Discord Bot Setup -----
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Google Sheets API scope
scope = [
    "https://spreadsheets.google.com/feeds",
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

# Load Google credentials from environment variable
creds_json = json.loads(os.environ['GOOGLE_CREDS'])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)

# Open your Google Sheet (replace with your actual sheet name)
sheet = client.open("Employee Time Log").sheet1

daily_clock_ins = {}

# Load excluded user IDs at bot startup
# This will now be dynamically managed by commands
excluded_user_ids = load_excluded_users()
print(f"Loaded {len(excluded_user_ids)} excluded user(s) from {EXCLUDED_USERS_FILE}")


# ----- Flask Route -----
@app.route('/')
def home():
    response = make_response("I'm alive!")
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# ----- Discord Bot Events -----
@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user.name}')
    print('Ready to track voice channel activity!')
    print(f"Current excluded user IDs: {excluded_user_ids}") # For debugging

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    # Check if the member is in the dynamically managed excluded_user_ids list
    if member.id in excluded_user_ids:
        print(f"Ignoring voice state update for excluded user: {member.name} ({member.id})")
        return

    if before.channel != after.channel and after.channel is not None:
        ph_tz = timezone('Asia/Manila')
        current_date = datetime.now(ph_tz).strftime("%Y-%m-%d")

        if member.name not in daily_clock_ins or daily_clock_ins[member.name] != current_date:
            timestamp = datetime.now(ph_tz).strftime("%Y-%m-%d %H:%M:%S")
            sheet.append_row([member.name, 'Clock In', timestamp])
            daily_clock_ins[member.name] = current_date
            print(f'{member.name} Clock In at {timestamp}')

# ----- Command to Clock Out -----
@bot.command()
async def clockout(ctx):
    # Check if the author is in the dynamically managed excluded_user_ids list
    if ctx.author.id in excluded_user_ids:
        await ctx.send(f'{ctx.author.mention}, you are not eligible for time tracking.')
        return

    ph_tz = timezone('Asia/Manila')
    timestamp = datetime.now(ph_tz).strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([ctx.author.name, 'Clock Out', timestamp])
    await ctx.send(f'{ctx.author.mention} has clocked out at {timestamp}')
    print(f'{ctx.author.name} Clock Out at {timestamp}')

# ----- New Commands for Exclusion Management -----

@bot.command(name='exclude')
@commands.has_permissions(administrator=True) # Only administrators can use this command
async def exclude_user(ctx, member: discord.Member):
    """Excludes a user from time tracking. Usage: !exclude @User or !exclude UserID"""
    global excluded_user_ids # Declare global to modify the list

    if member.id in excluded_user_ids:
        await ctx.send(f'{member.mention} is already in the exclusion list.')
        return

    excluded_user_ids.append(member.id)
    save_excluded_users(excluded_user_ids) # Save changes to file
    await ctx.send(f'{member.mention} has been added to the exclusion list and will no longer be tracked.')
    print(f"Added {member.name} ({member.id}) to exclusion list.")

@exclude_user.error
async def exclude_user_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the necessary permissions (Administrator) to use this command.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Please provide a valid user to exclude (mention them or provide their ID). Example: `!exclude @JohnDoe` or `!exclude 123456789012345678`")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.command(name='unexclude')
@commands.has_permissions(administrator=True)
async def unexclude_user(ctx, member: discord.Member):
    """Removes a user from time tracking exclusion. Usage: !unexclude @User or !unexclude UserID"""
    global excluded_user_ids # Declare global to modify the list

    if member.id not in excluded_user_ids:
        await ctx.send(f'{member.mention} is not in the exclusion list.')
        return

    excluded_user_ids.remove(member.id)
    save_excluded_users(excluded_user_ids) # Save changes to file
    await ctx.send(f'{member.mention} has been removed from the exclusion list and will now be tracked.')
    print(f"Removed {member.name} ({member.id}) from exclusion list.")

@unexclude_user.error
async def unexclude_user_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the necessary permissions (Administrator) to use this command.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Please provide a valid user to unexclude (mention them or provide their ID). Example: `!unexclude @JohnDoe` or `!unexclude 123456789012345678`")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.command(name='listexcluded')
async def list_excluded(ctx):
    """Lists all users currently excluded from time tracking."""
    if not excluded_user_ids:
        await ctx.send("No users are currently excluded from time tracking.")
        return

    excluded_names = []
    for user_id in excluded_user_ids:
        user = bot.get_user(user_id) # Try to fetch the user object
        if user:
            excluded_names.append(f"{user.display_name} ({user_id})")
        else:
            excluded_names.append(f"Unknown User ({user_id})") # Fallback if user not found

    response_message = "Users currently excluded from time tracking:\n" + "\n".join(excluded_names)
    await ctx.send(response_message)


# Start the keep_alive server and bot
if __name__ == '__main__':
    keep_alive()
    bot.run(os.environ['DISCORD_TOKEN'])
