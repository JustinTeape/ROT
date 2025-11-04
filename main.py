import discord
from discord import app_commands
from dotenv import load_dotenv
import os
import asyncpg
import datetime
import random
from flask import Flask
from threading import Thread
from discord import ui
from discord.ext import tasks
import asyncio

load_dotenv()
token = os.getenv('DISCORD_TOKEN')

DB_NAME = "user_data.db"
CURRENCY_NAME = "GB"
SECONDS_PER_CURRENCY = 60

HORSE_DEFINITIONS = {
    "Red": "🟥🐎",
    "Blue": "🟦🐎",
    "Green": "🟩🐎",
    "Yellow": "🟨🐎",
    "Purple": "🟪🐎"
}

HORSE_COLORS = list(HORSE_DEFINITIONS.keys())

RACE_TRACK_LENGTH = 20
RACE_PAYOUT_MULTIPLIER = 4
RACE_LOCKOUT_MINUTES = [0, 1, 2, 30, 31, 32]

REDS = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
BLACKS = [2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35]

active_sessions = {}

db_pool = None

async def init_database_pool():
    """Initializes the PostgreSQL connection pool and creates all tables."""
    global db_pool
    
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        print("DATABASE_URL not set. Bot cannot connect to database.")
        return
        
    try:
        db_pool = await asyncpg.create_pool(database_url)
        
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id BIGINT PRIMARY KEY,
                    total_seconds BIGINT DEFAULT 0,
                    balance BIGINT DEFAULT 0
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_configs (
                    guild_id BIGINT PRIMARY KEY,
                    race_channel_id BIGINT
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS horse_bets (
                    user_id BIGINT,
                    guild_id BIGINT,
                    bet_amount BIGINT,
                    horse_color TEXT,
                    PRIMARY KEY (user_id, guild_id)
                )
            """)
            
        print("Database pool initialized and all tables checked.")
        
    except Exception as e:
        print(f"Error initializing database pool: {e}")

async def set_race_channel(guild_id: int, channel_id: int):
    """Sets or updates the racing channel for a guild."""
    if not db_pool: return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_configs (guild_id, race_channel_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET
                race_channel_id = $2
        """, guild_id, channel_id)

async def remove_race_channel(guild_id: int):
    """Disables horse racing for a guild."""
    if not db_pool: return
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM guild_configs WHERE guild_id = $1", guild_id)

async def get_all_race_configs():
    """Gets all guild_id, channel_id pairs that have racing enabled."""
    if not db_pool: return []
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT guild_id, race_channel_id FROM guild_configs")

async def get_guild_race_config(guild_id: int):
    """Checks if a single guild has racing enabled."""
    if not db_pool: return None
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT race_channel_id FROM guild_configs WHERE guild_id = $1", guild_id)

async def place_bet(user_id: int, guild_id: int, amount: int, color: str):
    """Places or updates a user's bet for the next race."""
    if not db_pool: return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO horse_bets (user_id, guild_id, bet_amount, horse_color)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, guild_id) DO UPDATE SET
                bet_amount = $3,
                horse_color = $4
        """, user_id, guild_id, amount, color)

async def get_bets_for_guild(guild_id: int):
    """Gets all bets for a specific guild's race."""
    if not db_pool: return []
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, bet_amount, horse_color FROM horse_bets WHERE guild_id = $1", guild_id)

async def clear_bets_for_guild(guild_id: int):
    """Deletes all bets for a guild after a race."""
    if not db_pool: return
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM horse_bets WHERE guild_id = $1", guild_id)

async def get_user_bet_for_guild(user_id: int, guild_id: int):
    """Gets a single user's current bet for a specific guild."""
    if not db_pool: return None
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT bet_amount, horse_color FROM horse_bets WHERE user_id = $1 AND guild_id = $2", user_id, guild_id)

async def record_vc_session(user_id: int, seconds_to_add: int, currency_to_add: int):
    """Updates both time and currency in the database after a VC session."""
    if not db_pool: return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_stats (user_id, total_seconds, balance)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET
                total_seconds = user_stats.total_seconds + $2,
                balance = user_stats.balance + $3
        """, user_id, seconds_to_add, currency_to_add)

async def update_balance(user_id: int, amount: int):
    """Simple function to give/take currency (for admin commands)."""
    if not db_pool: return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_stats (user_id, balance)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET
                balance = user_stats.balance + $2
        """, user_id, amount)

async def get_balance(user_id: int) -> int:
    """Gets the total balance for a user."""
    if not db_pool: return 0
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM user_stats WHERE user_id = $1", user_id)
        return row['balance'] if row else 0

async def get_total_time(user_id: int) -> int:
    """Gets the total voice time in seconds for a user."""
    if not db_pool: return 0
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT total_seconds FROM user_stats WHERE user_id = $1", user_id)
        return row['total_seconds'] if row else 0

async def get_all_time_data():
    """Gets all users and their total_seconds from the database."""
    if not db_pool: return {}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, total_seconds FROM user_stats")
        return {row['user_id']: row['total_seconds'] for row in rows}

async def get_all_currency_data():
    """Gets all users and their balance from the database."""
    if not db_pool: return {}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, balance FROM user_stats")
        return {row['user_id']: row['balance'] for row in rows}

def format_duration(total_seconds: int) -> str:
    """Converts seconds into a readable string."""
    if total_seconds == 0:
        return "0 seconds"
        
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    
    parts = []
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds > 0:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
        
    return ", ".join(parts)

def create_deck():
    """Creates a standard 52-card deck and shuffles it."""
    ranks = [2, 3, 4, 5, 6, 7, 8, 9, 10, 'J', 'Q', 'K', 'A']
    suits = ['♥', '♦', '♣', '♠']
    deck = [{'rank': rank, 'suit': suit} for rank in ranks for suit in suits]
    random.shuffle(deck)
    return deck

def calculate_hand_value(hand):
    """Calculates the value of a hand, handling Aces correctly."""
    value = 0
    ace_count = 0
    
    for card in hand:
        rank = card['rank']
        if rank in ['J', 'Q', 'K']:
            value += 10
        elif rank == 'A':
            value += 11
            ace_count += 1
        else:
            value += int(rank)
            
    while value > 21 and ace_count > 0:
        value -= 10
        ace_count -= 1
        
    return value

def format_hand(hand):
    """Returns a string representation of a hand."""
    return "  ".join([f"**`{card['rank']}{card['suit']}`**" for card in hand])

def format_dealer_hand_hidden(hand):
    """Returns a string for the dealer's hand with one card hidden."""
    if not hand:
        return ""
    return f"**`{hand[0]['rank']}{hand[0]['suit']}`** **`[ ? ]`**"

@tasks.loop(minutes=1.0)
async def start_race_loop():
    """Checks every minute if it's time to start a race."""
    now = datetime.datetime.now(datetime.timezone.utc)

    if now.minute == 0 or now.minute == 30:
        print(f"Race time! ({now.hour}:{now.minute:02d}) Running global races.")
        await run_global_races()

async def run_global_races():
    """Fetches all configured guilds and starts a race in each one."""
    configs = await get_all_race_configs()
    
    tasks_to_run = []
    for (guild_id, channel_id) in configs:
        tasks_to_run.append(run_race_in_channel(guild_id, channel_id))
    
    await asyncio.gather(*tasks_to_run)
    print("All races finished.")

async def run_race_in_channel(guild_id: int, channel_id: int):
    """Runs a single animated race in a specific channel."""
    
    channel = client.get_channel(channel_id)
    if not channel:
        print(f"Error: Channel {channel_id} for Guild {guild_id} not found. Skipping race.")
        return

    horse_positions = {color: 0 for color in HORSE_COLORS}
    
    def get_race_embed(title: str):
        embed = discord.Embed(title=title, color=discord.Color.blue())
        track_str = ""
        for color in HORSE_COLORS:
            pos = horse_positions[color]
            track_str += "🏁" + ("." * (RACE_TRACK_LENGTH - pos)) + HORSE_DEFINITIONS[color] + ("." * pos) + "\n"
        embed.description = track_str
        embed.set_footer(text="The race is underway!")
        return embed

    try:
        msg = await channel.send(embed=get_race_embed("The race is about to begin!"))
        await asyncio.sleep(3)
    except discord.Forbidden:
        print(f"Error: Cannot send message in channel {channel_id} (Guild {guild_id}). Disabling for guild.")
        await remove_race_channel(guild_id)
        return
    except Exception as e:
        print(f"Error sending race start message: {e}")
        return

    winner = None
    while winner is None:
        await asyncio.sleep(2.0)
        
        for color in HORSE_COLORS:
            move = random.choice([1, 1, 2, 2, 3])
            horse_positions[color] += move
            
            if horse_positions[color] >= RACE_TRACK_LENGTH:
                winner = color
                break
        
        await msg.edit(embed=get_race_embed("The race is in progress!"))

    final_embed = get_race_embed(f"🎉 The race is over! Winner: {HORSE_DEFINITIONS[winner]} {winner} Horse! 🎉")
    final_embed.color = discord.Color.green()
    final_embed.set_footer(text="Processing bets...")
    await msg.edit(embed=final_embed)
    await asyncio.sleep(2)

    bets = await get_bets_for_guild(guild_id)
    
    if not bets:
        await channel.send("No bets were placed for this race.")
        return

    results_description = f"**Winner:** {HORSE_DEFINITIONS[winner]} **{winner} Horse**\n\n**Results:**\n"
    
    user_ids_to_fetch = {bet['user_id'] for bet in bets}
    
    user_map = {}
    for user_id in user_ids_to_fetch:
        try:
            user = await client.fetch_user(user_id)
            user_map[user_id] = user.display_name
        except:
            user_map[user_id] = "<Unknown User>"

    for bet in bets:
        user_id = bet['user_id']
        bet_amount = bet['bet_amount']
        horse_color = bet['horse_color']
        username = user_map[user_id]
        
        if horse_color == winner:
            winnings = bet_amount * RACE_PAYOUT_MULTIPLIER
            total_payout = winnings + bet_amount
            
            await update_balance(user_id, total_payout)
            results_description += f"**{username}** won **{winnings} {CURRENCY_NAME}**! (Total Payout: {total_payout})\n"
        else:
            results_description += f"❌ **{username}** lost **{bet_amount} {CURRENCY_NAME}**.\n"
            
    results_embed = discord.Embed(title="Race Payouts", description=results_description, color=discord.Color.gold())
    await channel.send(embed=results_embed)
    
    await clear_bets_for_guild(guild_id)

class aclient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True
        
        super().__init__(intents=intents)
        self.synced = False

    async def on_ready(self):
        await self.wait_until_ready()
        
        await init_database_pool()
        
        if not self.synced:
            await tree.sync()
            self.synced = True
        
        print(f"We have logged in as {self.user}.")

        print("Checking for users in VC on startup...")
        now = datetime.datetime.now(datetime.timezone.utc) 
        
        for guild in self.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if not member.bot:
                        active_sessions[member.id] = now
                        print(f"Found {member.name} in {vc.name}. Starting timer.")
        start_race_loop.start()

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Tracks joins/leaves, updating time and awarding currency."""
        if member.bot:
            return

        now = datetime.datetime.now(datetime.timezone.utc)

        if before.channel is not None and before.channel != after.channel:
            if member.id in active_sessions:
                join_time = active_sessions.pop(member.id)
                
                if join_time.tzinfo is None:
                     join_time = join_time.replace(tzinfo=datetime.timezone.utc)
                     
                duration_seconds = int((now - join_time).total_seconds())
                
                currency_earned = int(duration_seconds / SECS_PER_CURRENCY)
                
                if duration_seconds > 0:
                    await record_vc_session(member.id, duration_seconds, currency_earned)
                    print(f"User {member.name} left. Added {duration_seconds}s and {currency_earned} {CURRENCY_NAME}.")

        if after.channel is not None and after.channel != before.channel:
            active_sessions[member.id] = now
            print(f"User {member.name} joined. Starting timer.")


client = aclient()
tree = app_commands.CommandTree(client)

@tree.command(name="voicetime", description="Check your total time spent in voice channels.")
@app_commands.describe(user="The user to check (optional, defaults to you)")
async def voicetime(interaction: discord.Interaction, user: discord.Member = None):
    
    await interaction.response.defer()
    
    if user is None:
        user = interaction.user
        
    if user.bot:
        await interaction.followup.send("Bots don't have voice time!", ephemeral=True)
        return

    total_seconds_saved = await get_total_time(user.id)
    total_seconds_current_session = 0
    
    now = datetime.datetime.now(datetime.timezone.utc)
    if user.id in active_sessions:
        join_time = active_sessions[user.id]
        if join_time.tzinfo is None:
             join_time = join_time.replace(tzinfo=datetime.timezone.utc)
             
        current_session_seconds = (now - join_time).total_seconds()
        if current_session_seconds > 0:
            total_seconds_current_session = current_session_seconds

    total_time = total_seconds_saved + int(total_seconds_current_session)
    readable_time = format_duration(total_time)
    await interaction.followup.send(f"**{user.display_name}** has spent a total of:\n`{readable_time}` in voice channels.")

@tree.command(name="balance", description="Check your total currency balance.")
@app_commands.describe(user="The user to check (optional, defaults to you)")
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    
    await interaction.response.defer()

    if user is None:
        user = interaction.user
        
    if user.bot:
        await interaction.followup.send("Bots don't have currency!", ephemeral=True)
        return

    user_balance_saved = await get_balance(user.id)
    pending_currency = 0

    now = datetime.datetime.now(datetime.timezone.utc)
    if user.id in active_sessions:
        join_time = active_sessions[user.id]
        if join_time.tzinfo is None:
             join_time = join_time.replace(tzinfo=datetime.timezone.utc)
             
        current_session_seconds = (now - join_time).total_seconds()
        if current_session_seconds > 0:
            pending_currency = int(current_session_seconds / SECS_PER_CURRENCY)

    total_balance = user_balance_saved + pending_currency
    await interaction.followup.send(f"**{user.display_name}** has **{total_balance} {CURRENCY_NAME}**.")


async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
    else:
        print(f"Error in admin command: {error}")
        await interaction.response.send_message("An error occurred.", ephemeral=True)

@tree.command(name="leaderboard-time", description="Shows the global leaderboard for voice time.")
async def leaderboard_time(interaction: discord.Interaction):
    await interaction.response.defer()
    
    leaderboard_data = await get_all_time_data()
    
    now = datetime.datetime.now(datetime.timezone.utc)
    for user_id, join_time in active_sessions.items():
        current_session_seconds = (now - join_time).total_seconds()
        
        saved_time = leaderboard_data.get(user_id, 0)
        
        leaderboard_data[user_id] = saved_time + int(current_session_seconds)

    sorted_leaderboard = sorted(leaderboard_data.items(), key=lambda item: item[1], reverse=True)
    
    top_10 = sorted_leaderboard[:10]
    
    if not top_10:
        await interaction.followup.send("The leaderboard is empty! Go spend time in a VC.")
        return
        
    embed = discord.Embed(
        title="Global Voice Time Leaderboard ⌛",
        description="Top 10 users across all servers! (Updates live)",
        color=discord.Color.blue()
    )
    
    description_lines = []
    client_obj = interaction.client 
    
    for i, (user_id, total_seconds) in enumerate(top_10):

        user = client_obj.get_user(user_id)
        if user is None:
            try:
                user = await client_obj.fetch_user(user_id)
                username = user.display_name
            except discord.NotFound:
                username = "<Unknown User>"
            except Exception as e:
                print(f"Error fetching user {user_id}: {e}")
                username = "<Error>"
        else:
            username = user.display_name
        
        formatted_time = format_duration(total_seconds)
        description_lines.append(f"**{i+1}.** {username} - `{formatted_time}`")

    embed.description = "\n".join(description_lines)
    await interaction.followup.send(embed=embed)

@tree.command(name="leaderboard-currency", description="Shows the global leaderboard for currency.")
async def leaderboard_currency(interaction: discord.Interaction):
    await interaction.response.defer()

    leaderboard_data = await get_all_currency_data()
    
    now = datetime.datetime.now(datetime.timezone.utc)
    for user_id, join_time in active_sessions.items():
        current_session_seconds = (now - join_time).total_seconds()
        pending_currency = int(current_session_seconds / SECONDS_PER_CURRENCY)
        
        saved_balance = leaderboard_data.get(user_id, 0)
        
        leaderboard_data[user_id] = saved_balance + pending_currency

    sorted_leaderboard = sorted(leaderboard_data.items(), key=lambda item: item[1], reverse=True)
    
    top_10 = sorted_leaderboard[:10]
    
    if not top_10:
        await interaction.followup.send("The leaderboard is empty! Go earn some currency.")
        return
        
    embed = discord.Embed(
        title=f"Global {CURRENCY_NAME} Leaderboard 💰",
        description="Top 10 users across all servers! (Updates live)",
        color=discord.Color.gold()
    )
    
    description_lines = []
    client_obj = interaction.client 
    
    for i, (user_id, balance) in enumerate(top_10):
        user = client_obj.get_user(user_id)
        if user is None:
            try:
                user = await client_obj.fetch_user(user_id)
                username = user.display_name
            except discord.NotFound:
                username = "<Unknown User>"
            except Exception as e:
                print(f"Error fetching user {user_id}: {e}")
                username = "<Error>"
        else:
            username = user.display_name
        
        description_lines.append(f"**{i+1}.** {username} - **{balance}** {CURRENCY_NAME}")

    embed.description = "\n".join(description_lines)
    await interaction.followup.send(embed=embed)

@tree.command(name="blackjack", description="Play a game of Blackjack for currency.")
@app_commands.describe(amount="The amount of currency you want to bet")
async def blackjack(interaction: discord.Interaction, amount: int):
    await interaction.response.defer()
    user_id = interaction.user.id
    
    if amount <= 0:
        await interaction.response.send_message("You must bet a positive amount.", ephemeral=True)
        return
        
    now = datetime.datetime.now(datetime.timezone.utc)
    saved_balance = await get_balance(user_id)
    pending_currency = 0
    if user_id in active_sessions:
        join_time = active_sessions[user_id]
        if join_time.tzinfo is None:
             join_time = join_time.replace(tzinfo=datetime.timezone.utc)
        current_session_seconds = (now - join_time).total_seconds()
        if current_session_seconds > 0:
            pending_currency = int(current_session_seconds / SECS_PER_CURRENCY)
        
    current_balance = saved_balance + pending_currency
    
    if amount > current_balance:
        await interaction.response.send_message(
            f"You don't have enough {CURRENCY_NAME} to make that bet.\n"
            f"Your current balance is: **{current_balance} {CURRENCY_NAME}**", 
            ephemeral=True
        )
        return
        
    await update_balance(user_id, -amount)
    current_balance -= amount
   
    game_view = BlackjackView(interaction, amount, current_balance) 
    await game_view.start_game()

@tree.command(name="pay", description="Give currency to another user.")
@app_commands.describe(user="The user you want to give currency to", amount="The amount to give")
async def donate(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer()

    donator_id = interaction.user.id
    recipient_id = user.id
    

    if amount <= 0:
        await interaction.response.send_message("You must donate a positive amount.", ephemeral=True)
        return
        
    if donator_id == recipient_id:
        await interaction.response.send_message("You cannot donate to yourself.", ephemeral=True)
        return
        
    if user.bot:
        await interaction.response.send_message(f"You cannot donate to a bot.", ephemeral=True)
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    saved_balance = await get_balance(donator_id)
    pending_currency = 0
    if donator_id in active_sessions:
        join_time = active_sessions[donator_id]
        if join_time.tzinfo is None:
             join_time = join_time.replace(tzinfo=datetime.timezone.utc)
        current_session_seconds = (now - join_time).total_seconds()
        if current_session_seconds > 0:
            pending_currency = int(current_session_seconds / SECS_PER_CURRENCY)
            
    donator_balance = saved_balance + pending_currency
    
    if amount > donator_balance:
        await interaction.response.send_message(
            f"You don't have enough {CURRENCY_NAME} to donate that much.\n"
            f"Your current balance is: **{donator_balance} {CURRENCY_NAME}**", 
            ephemeral=True
        )
        return

    try:
        await interaction.response.defer()

        await update_balance(donator_id, -amount)
        
        await update_balance(recipient_id, amount)
        
        await interaction.followup.send(
            f"**Transaction Successful!**\n\n"
            f"**{interaction.user.display_name}** gave **{amount} {CURRENCY_NAME}** to **{user.display_name}**."
        )
        
    except Exception as e:
        print(f"Error during /donate transaction: {e}")
        await interaction.followfup.send("An error occurred during the transaction. Please try again.", ephemeral=True)

@tree.command(name="roulette", description="Bet your currency on a game of roulette.")
@app_commands.describe(
    amount="The amount of currency you want to bet",
    bet="Your choice: Red/Black (1:1), Even/Odd (1:1), or Green (35:1)"
)
@app_commands.choices(bet=[
    app_commands.Choice(name="🔴 Red", value="Red"),
    app_commands.Choice(name="⚫ Black", value="Black"),
    app_commands.Choice(name="🔵 Even", value="Even"),
    app_commands.Choice(name="🟣 Odd", value="Odd"),
    app_commands.Choice(name="🟢 Green (0)", value="Green"),
])
async def roulette(interaction: discord.Interaction, amount: app_commands.Range[int, 1], bet: str):
    
    user_id = interaction.user.id
    await interaction.response.defer()

    now = datetime.datetime.now(datetime.timezone.utc)
    saved_balance = await get_balance(user_id)
    pending_currency = 0
    if user_id in active_sessions:
        join_time = active_sessions[user_id]
        if join_time.tzinfo is None:
             join_time = join_time.replace(tzinfo=datetime.timezone.utc)
        current_session_seconds = (now - join_time).total_seconds()
        if current_session_seconds > 0:
            pending_currency = int(current_session_seconds / SECS_PER_CURRENCY)
            
    current_balance = saved_balance + pending_currency
    
    if amount > current_balance:
        await interaction.followup.send(
            f"You don't have enough {CURRENCY_NAME} to make that bet.\n"
            f"Your current balance is: **{current_balance} {CURRENCY_NAME}**", 
            ephemeral=True
        )
        return

    await update_balance(user_id, -amount)
    current_balance -= amount

    embed = discord.Embed(
        title="Roulette Spin",
        description=f"You bet **{amount} {CURRENCY_NAME}** on **{bet}**...\n\nSpinning... 🔴",
        color=discord.Color.gold()
    )

    msg = await interaction.followup.send(embed=embed)
    
    spin_frames = [
        "⚫", "🔴", "⚫", "🟢", "🔴", "⚫", "🔴", "⚫",
        "🔴", "⚫", "🔴", "⚫", "🟢", "🔴", "⚫"
    ]
    
    for frame in spin_frames:
        await asyncio.sleep(0.5)
        embed.description = f"You bet **{amount} {CURRENCY_NAME}** on **{bet}**...\n\nSpinning... {frame}"
        await msg.edit(embed=embed)
    
    await asyncio.sleep(1)
    embed.description = f"You bet **{amount} {CURRENCY_NAME}** on **{bet}**...\n\n"
    await msg.edit(embed=embed)
    await asyncio.sleep(1.5)

    spin_result = random.randint(0, 36)
    
    spin_color = "Green"
    if spin_result in REDS:
        spin_color = "Red"
    elif spin_result in BLACKS:
        spin_color = "Black"
        
    spin_parity = "None"
    if spin_result != 0:
        spin_parity = "Even" if spin_result % 2 == 0 else "Odd"

    is_win = False
    payout_multiplier = 0

    if bet == spin_color:
        is_win = True
        payout_multiplier = 1
    elif bet == spin_parity:
        is_win = True
        payout_multiplier = 1
    
    if bet == "Green" and spin_color == "Green":
        is_win = True
        payout_multiplier = 35

    embed.add_field(
        name="The wheel landed on...",
        value=f"**{spin_result} {spin_color}**",
        inline=False
    )

    if is_win:
        winnings = amount * payout_multiplier
        total_payout = winnings + amount

        await update_balance(user_id, total_payout)
        new_balance = current_balance + total_payout
        
        embed.color = discord.Color.green()
        embed.add_field(
            name="Your results!",
            value=f"You won **{winnings} {CURRENCY_NAME}**.\nYour new balance is **{new_balance} {CURRENCY_NAME}**.",
            inline=False
        )
    else:
        new_balance = current_balance
        
        embed.color = discord.Color.red()
        embed.add_field(
            name="Your results!",
            value=f"You lost **{amount} {CURRENCY_NAME}**.\nYour new balance is **{new_balance} {CURRENCY_NAME}**.",
            inline=False
        )
        
    await msg.edit(embed=embed)

@tree.command(name="setup-horserace", description="[Admin] Enables and sets the channel for horse racing.")
@app_commands.describe(channel="The channel where races will be posted.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_horserace(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    try:
        temp_msg = await channel.send("Checking permissions...")
        await temp_msg.delete()
        
        await set_race_channel(interaction.guild_id, channel.id)
        await interaction.followup.send(
            f"Horse racing is now **ENABLED**.\n"
            f"Races will be posted in {channel.mention} every 30 minutes (at :00 and :30)."
        )
    except discord.Forbidden:
        await interaction.followup.send("Error: I don't have permission to send messages in that channel.")
    except Exception as e:
        await interaction.followup.send(f"An error occurred: {e}")

@tree.command(name="disable-horserace", description="[Admin] Disables horse racing for this server.")
@app_commands.checks.has_permissions(administrator=True)
async def disable_horserace(interaction: discord.Interaction):
    await remove_race_channel(interaction.guild_id)
    await interaction.response.send_message(
        "Horse racing is now **DISABLED**. I will no longer post races.", 
        ephemeral=True
    )


@tree.command(name="bet-horse", description="Place or update your bet for the next horse race.")
@app_commands.describe(
    amount="The amount of currency you want to bet",
    color="The color of the horse you're betting on"
)
@app_commands.choices(color=[
    app_commands.Choice(name="🟥 Red", value="Red"),
    app_commands.Choice(name="🟦 Blue", value="Blue"),
    app_commands.Choice(name="🟩 Green", value="Green"),
    app_commands.Choice(name="🟨 Yellow", value="Yellow"),
    app_commands.Choice(name="🟪 Purple", value="Purple")
])

async def bet_horse(interaction: discord.Interaction, amount: app_commands.Range[int, 1], color: str):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    guild_id = interaction.guild_id

    def get_next_race_timestamp(now_time):
        if now_time.minute < 30:
            next_race_dt = now_time.replace(minute=30, second=0, microsecond=0)
        else:
            next_race_dt = (now_time + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        timestamp = int(next_race_dt.timestamp())
        return f"<t:{timestamp}:R>"

    config = await get_guild_race_config(guild_id)
    if not config:
        await interaction.followup.send("Horse racing is not set up in this server. An admin must use `/setup-horserace`.")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    if now.minute in RACE_LOCKOUT_MINUTES:
        relative_time_str = get_next_race_timestamp(now)
        await interaction.followup.send(
            f"Sorry, bets are **LOCKED** for the current race.\n"
            f"You can bet on the next race, which starts {relative_time_str}."
        )
        return

    saved_balance = await get_balance(user_id)
    pending_currency = 0
    if user_id in active_sessions:
        join_time = active_sessions[user_id]
        if join_time.tzinfo is None:
             join_time = join_time.replace(tzinfo=datetime.timezone.utc)
        current_session_seconds = (now - join_time).total_seconds()
        if current_session_seconds > 0:
            pending_currency = int(current_session_seconds / SECS_PER_CURRENCY)
    
    current_balance = saved_balance + pending_currency
    
    old_bet = await get_user_bet_for_guild(user_id, guild_id)
    old_bet_amount = old_bet['bet_amount'] if old_bet else 0
    cost_to_change = amount - old_bet_amount
    
    if cost_to_change > current_balance:
        await interaction.followup.send(
            f"You don't have enough {CURRENCY_NAME} to make that bet.\n"
            f"Your current balance is: **{current_balance} {CURRENCY_NAME}**.\n"
            f"You need **{cost_to_change} {CURRENCY_NAME}** more to change your bet from {old_bet_amount} to {amount}."
        )
        return

    await update_balance(user_id, -cost_to_change)
    await place_bet(user_id, guild_id, amount, color)
    
    relative_time_str = get_next_race_timestamp(now) 
    
    if old_bet_amount > 0:
        await interaction.followup.send(
            f"Your bet for *this server* has been **updated**!\n"
            f"Your balance was adjusted by **{cost_to_change} {CURRENCY_NAME}**.\n"
            f"You are now betting **{amount} {CURRENCY_NAME}** on the **{HORSE_DEFINITIONS[color]} {color} Horse** for the race {relative_time_str}."
        )
    else:
        await interaction.followup.send(
            f"Your bet for *this server* has been placed!\n"
            f"**{amount} {CURRENCY_NAME}** has been deducted from your balance.\n"
            f"You are betting on the **{HORSE_DEFINITIONS[color]} {color} Horse** for the race {relative_time_str}."
        )


@tree.command(name="coinflip", description="Gamble your currency on a 50/50 coin flip.")
@app_commands.describe(amount="The amount of currency you want to bet")
async def coinflip(interaction: discord.Interaction, amount: int):
    user_id = interaction.user.id
    
    if amount <= 0:
        await interaction.response.send_message("You must bet a positive amount.", ephemeral=True)
        return
        
    saved_balance = await get_balance(user_id)
    pending_currency = 0
    if user_id in active_sessions:
        join_time = active_sessions[user_id]
        current_session_seconds = (datetime.datetime.now() - join_time).total_seconds()
        pending_currency = int(current_session_seconds / SECS_PER_CURRENCY)
        
    current_balance = saved_balance + pending_currency
    
    if amount > current_balance:
        await interaction.response.send_message(
            f"You don't have enough {CURRENCY_NAME} to make that bet.\n"
            f"Your current balance is: **{current_balance} {CURRENCY_NAME}**", 
            ephemeral=True
        )
        return
        
    await update_balance(user_id, -amount)
    current_balance -= amount

    if not interaction.response.is_done():
        await interaction.response.defer()
    
    is_win = random.choice([True, False]) 
    
    if is_win:
        payout = amount * 2
        await update_balance(user_id, payout) 
        new_balance = current_balance + payout
        
        await interaction.followup.send(
            f"**It's Heads! You won!**\n\n"
            f"You won **{amount} {CURRENCY_NAME}** (Total Payout: {payout}).\n"
            f"Your new balance is **{new_balance} {CURRENCY_NAME}**."
        )
    else:
        new_balance = current_balance
        await interaction.followup.send(
            f"**It's Tails! You lost!**\n\n"
            f"You lost **{amount} {CURRENCY_NAME}**.\n"
            f"Your new balance is **{new_balance} {CURRENCY_NAME}**."
        )

class BlackjackView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, bet_amount: int, start_balance: int):
        super().__init__(timeout=180.0)
        self.interaction = interaction
        self.player = interaction.user
        self.bet_amount = bet_amount
        self.current_balance = start_balance
        self.game_over = False

        self.deck = create_deck()
        self.player_hand = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand = [self.deck.pop(), self.deck.pop()]
    
    async def start_game(self):
        """Sends the initial game message."""
        player_score = calculate_hand_value(self.player_hand)
        
        if player_score == 21:
            await self.end_game("win", "Blackjack! You win!")
        else:
            embed = self.create_game_embed("Make your move!", "")
            await self.interaction.followup.send(embed=embed, view=self)

    def create_game_embed(self, title: str, status: str, reveal_dealer=False, final_color: discord.Color = None):
        """Creates the embed for the game state."""
        player_score = calculate_hand_value(self.player_hand)
        
        if final_color:
            embed_color = final_color
        else:
            embed_color = discord.Color.gold()
        
        if reveal_dealer:
            dealer_hand_str = format_hand(self.dealer_hand)
            dealer_score = calculate_hand_value(self.dealer_hand)
        else:
            dealer_hand_str = format_dealer_hand_hidden(self.dealer_hand)
            dealer_score = calculate_hand_value([self.dealer_hand[0]])

        embed = discord.Embed(
            title=f"Blackjack Game: {title}",
            description=f"**Bet:** {self.bet_amount} {CURRENCY_NAME}\n{status}",
            color=embed_color
        )
        embed.add_field(
            name=f"Your Hand ({player_score})",
            value=format_hand(self.player_hand),
            inline=False
        )
        embed.add_field(
            name=f"Dealer's Hand ({dealer_score})",
            value=dealer_hand_str,
            inline=False
        )
        embed.set_footer(text=f"{self.player.display_name}'s game")
        return embed

    async def disable_buttons(self):
        """Disables all buttons in the view."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        self.game_over = True

    async def end_game(self, result: str, message: str):
        """Handles the end of the game, updates balance, and edits message."""
        await self.disable_buttons()
        
        status_message = ""
        final_game_color = discord.Color.gold() 
        
        if result == "win":
            payout = self.bet_amount * 2
            await update_balance(self.player.id, payout)
            new_balance = self.current_balance + payout 
            
            status_message = f"You won {self.bet_amount} {CURRENCY_NAME}!\nNew Balance: **{new_balance}**"
            final_game_color = discord.Color.green()
            
        elif result == "lose":
            new_balance = self.current_balance
            status_message = f"You lost {self.bet_amount} {CURRENCY_NAME}!\nNew Balance: **{new_balance}**"
            final_game_color = discord.Color.red()
            
        elif result == "push":
            await update_balance(self.player.id, self.bet_amount)
            new_balance = self.current_balance + self.bet_amount
            status_message = f"It's a push! Bet returned.\nBalance: **{new_balance}**"
        
        final_embed = self.create_game_embed(message, status_message, reveal_dealer=True, final_color=final_game_color)
        
        try:
            await self.interaction.edit_original_response(embed=final_embed, view=self)
        except discord.errors.InteractionResponded:
            await self.interaction.followup.send(embed=final_embed, view=self)
        except Exception as e:
            print(f"Error editing message: {e}")

        self.stop()
        
    async def on_timeout(self):
        if not self.game_over:
            await self.end_game("lose", "Game timed out! You forfeit your bet.")

    
    @ui.button(label="Hit", style=discord.ButtonStyle.green)
    async def hit(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("This is not your game!", ephemeral=True)
            return

        self.player_hand.append(self.deck.pop())
        player_score = calculate_hand_value(self.player_hand)
        
        if player_score > 21:
            await self.end_game("lose", "Bust! You lost.")
        elif player_score == 21:
            await interaction.response.defer()
            await self.dealer_turn()
        else:
            embed = self.create_game_embed("Hit or Stand?", "")
            await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Stand", style=discord.ButtonStyle.red)
    async def stand(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("This is not your game!", ephemeral=True)
            return
            
        await interaction.response.defer()
        await self.dealer_turn()

    async def dealer_turn(self):
        """The dealer's logic after the player stands."""
        dealer_score = calculate_hand_value(self.dealer_hand)
        
        while dealer_score < 17:
            self.dealer_hand.append(self.deck.pop())
            dealer_score = calculate_hand_value(self.dealer_hand)
            
        player_score = calculate_hand_value(self.player_hand)
        
        if dealer_score > 21:
            await self.end_game("win", "Dealer busts! You win!")
        elif dealer_score > player_score:
            await self.end_game("lose", f"Dealer wins with {dealer_score}!")
        elif player_score > dealer_score:
            await self.end_game("win", f"You win with {player_score}!")
        else:
            await self.end_game("push", "It's a push!")

app = Flask(__name__)

@app.route('/')
def home():
    return "I am alive and running!"

def run_web_server():
    app.run(host='0.0.0.0', port=10000)

Thread(target=run_web_server).start()

print("Starting bot and web server...")
client.run(token)