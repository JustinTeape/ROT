import discord
from discord import app_commands
from dotenv import load_dotenv
import os
import asyncpg
import aiosqlite  # For database
import datetime   # For time tracking
import random  # Add this with your other imports
from discord import ui

# --- 1. SETUP ---
load_dotenv()
token = os.getenv('DISCORD_TOKEN')

# --- 2. ECONOMY & DATABASE GLOBALS ---
DB_NAME = "user_data.db"  # A neutral name for the database
CURRENCY_NAME = "GB"
SECONDS_PER_CURRENCY = 60 # 60 seconds = 1 Coin

# This dictionary still stores active sessions: {user_id: join_time}
active_sessions = {}

# --- 3. DATABASE HELPER FUNCTIONS (MODIFIED FOR POSTGRESQL) ---


# This will hold our connection pool
db_pool = None

async def init_database_pool():
    """Initializes the PostgreSQL connection pool and creates the table."""
    global db_pool
    
    # Render provides the database URL in an environment variable
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        print("DATABASE_URL not set. Bot cannot connect to database.")
        return
        
    try:
        db_pool = await asyncpg.create_pool(database_url)
        
        # Create the table if it doesn't exist
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id BIGINT PRIMARY KEY,
                    total_seconds BIGINT DEFAULT 0,
                    balance BIGINT DEFAULT 0
                )
            """)
        print("Database pool initialized and table checked.")
        
    except Exception as e:
        print(f"Error initializing database pool: {e}")


async def record_vc_session(user_id: int, seconds_to_add: int, currency_to_add: int):
    """Updates both time and currency in the database after a VC session."""
    if not db_pool: return
    async with db_pool.acquire() as conn:
        # PostgreSQL uses a different "UPSERT" syntax
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

# --- END OF DATABASE FUNCTIONS ---
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
# --- BLACKJACK HELPER FUNCTIONS ---

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
            
    # Adjust for Aces
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

# --- 4. YOUR CUSTOM CLIENT CLASS (MODIFIED) ---
class aclient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True
        
        super().__init__(intents=intents)
        self.synced = False

    async def on_ready(self):
        await self.wait_until_ready()
        
        # --- ADDED: Initialize the database pool FIRST ---
        await init_database_pool()
        
        if not self.synced:
            await tree.sync()
            self.synced = True
        
        print(f"We have logged in as {self.user}.")

        # Scan VCs on startup
        print("Checking for users in VC on startup...")
        now = datetime.datetime.now()
        for guild in self.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if not member.bot:
                        active_sessions[member.id] = now
                        print(f"Found {member.name} in {vc.name}. Starting timer.")

    # --- MODIFIED: This event now updates BOTH time and currency ---
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Tracks joins/leaves, updating time and awarding currency."""
        if member.bot:
            return

        now = datetime.datetime.now()

        # --- Scenario 1: User LEAVES or SWITCHES ---
        if before.channel is not None and before.channel != after.channel:
            if member.id in active_sessions:
                join_time = active_sessions.pop(member.id)
                duration_seconds = int((now - join_time).total_seconds())
                
                # Convert duration to currency
                currency_earned = int(duration_seconds / SECONDS_PER_CURRENCY)
                
                if duration_seconds > 0:
                    # Use the new function to save both values
                    await record_vc_session(member.id, duration_seconds, currency_earned)
                    print(f"User {member.name} left. Added {duration_seconds}s and {currency_earned} {CURRENCY_NAME}.")

        # --- Scenario 2: User JOINS or SWITCHES ---
        if after.channel is not None and after.channel != before.channel:
            active_sessions[member.id] = now
            print(f"User {member.name} joined. Starting timer.")


client = aclient()
tree = app_commands.CommandTree(client)

# --- 5. YOUR COMMANDS ---

# --- RE-ADDED: The /voicetime command ---
@tree.command(name="voicetime", description="Check your total time spent in voice channels.")
@app_commands.describe(user="The user to check (optional, defaults to you)")
async def voicetime(interaction: discord.Interaction, user: discord.Member = None):
    
    if user is None:
        user = interaction.user
        
    if user.bot:
        await interaction.response.send_message("Bots don't have voice time!", ephemeral=True)
        return

    # Get saved time from the database
    total_seconds_saved = await get_total_time(user.id)
    
    # Check for an active session right now
    total_seconds_current_session = 0
    if user.id in active_sessions:
        join_time = active_sessions[user.id]
        total_seconds_current_session = (datetime.datetime.now() - join_time).total_seconds()

    total_time = total_seconds_saved + int(total_seconds_current_session)
    
    readable_time = format_duration(total_time)
    
    await interaction.response.send_message(f"**{user.display_name}** has spent a total of:\n`{readable_time}` in voice channels.")

# --- KEPT: The /balance command (NOW WITH REAL-TIME UPDATE) ---
@tree.command(name="balance", description="Check your total currency balance.")
@app_commands.describe(user="The user to check (optional, defaults to you)")
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    
    if user is None:
        user = interaction.user
        
    if user.bot:
        await interaction.response.send_message("Bots don't have currency!", ephemeral=True)
        return

    # 1. Get saved balance from the database
    user_balance_saved = await get_balance(user.id)
    
    # 2. Check for an active session right now
    pending_currency = 0
    if user.id in active_sessions:
        join_time = active_sessions[user.id]
        current_session_seconds = (datetime.datetime.now() - join_time).total_seconds()
        
        # Calculate currency earned *in this session*
        pending_currency = int(current_session_seconds / SECONDS_PER_CURRENCY)

    # 3. Add saved + pending for the real-time total
    total_balance = user_balance_saved + pending_currency
    
    await interaction.response.send_message(f"**{user.display_name}** has **{total_balance} {CURRENCY_NAME}**.")

# --- KEPT: The /give command (Admin only) ---
@tree.command(name="give", description="[Admin] Give currency to a user.")
@app_commands.describe(user="The user to give currency to", amount="The amount to give")
@app_commands.checks.has_permissions(administrator=True)
async def give(interaction: discord.Interaction, user: discord.Member, amount: int):
    
    if user.bot:
        await interaction.response.send_message("You can't give currency to a bot.", ephemeral=True)
        return
        
    if amount <= 0:
        await interaction.response.send_message("Amount must be a positive number.", ephemeral=True)
        return
    
    await update_balance(user.id, amount)
    await interaction.response.send_message(f"Successfully gave **{amount} {CURRENCY_NAME}** to **{user.display_name}**.", ephemeral=True)

# --- KEPT: The /take command (Admin only) ---
@tree.command(name="take", description="[Admin] Take currency from a user.")
@app_commands.describe(user="The user to take currency from", amount="The amount to take")
@app_commands.checks.has_permissions(administrator=True)
async def take(interaction: discord.Interaction, user: discord.Member, amount: int):
    
    if amount <= 0:
        await interaction.response.send_message("Amount must be a positive number.", ephemeral=True)
        return
    
    await update_balance(user.id, -amount)
    await interaction.response.send_message(f"Successfully took **{amount} {CURRENCY_NAME}** from **{user.display_name}**.", ephemeral=True)

# --- KEPT: Error handler for permission-locked commands ---
@give.error
@take.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
    else:
        print(f"Error in admin command: {error}")
        await interaction.response.send_message("An error occurred.", ephemeral=True)
# --- ADDED: The /leaderboard-time command ---
# --- REVISED: The /leaderboard-time command (NOW LIVE) ---
# Replace your old /leaderboard-time command with this one.

@tree.command(name="leaderboard-time", description="Shows the global leaderboard for voice time.")
async def leaderboard_time(interaction: discord.Interaction):
    await interaction.response.defer() # This may take a moment
    
    # 1. Get saved data from the database
    # This is a dictionary: {user_id: saved_seconds}
    leaderboard_data = await get_all_time_data()
    
    # 2. Get "live" data from active_sessions
    now = datetime.datetime.now()
    for user_id, join_time in active_sessions.items():
        # Calculate time for this user's current session
        current_session_seconds = (now - join_time).total_seconds()
        
        # Get their saved time (or 0 if they're not in the DB yet)
        saved_time = leaderboard_data.get(user_id, 0)
        
        # Add live + saved time and update their total
        leaderboard_data[user_id] = saved_time + int(current_session_seconds)
        
    # 3. Sort the combined data
    # This gives: [(user_id, total_seconds), (user_id, total_seconds), ...]
    sorted_leaderboard = sorted(leaderboard_data.items(), key=lambda item: item[1], reverse=True)
    
    # 4. Get the top 10
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
        # Fetch username (same logic as before)
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
        
        # Format the time
        formatted_time = format_duration(total_seconds)
        description_lines.append(f"**{i+1}.** {username} - `{formatted_time}`")

    embed.description = "\n".join(description_lines)
    await interaction.followup.send(embed=embed)


# --- REVISED: The /leaderboard-currency command (NOW LIVE) ---
# Replace your old /leaderboard-currency command with this one.

@tree.command(name="leaderboard-currency", description="Shows the global leaderboard for currency.")
async def leaderboard_currency(interaction: discord.Interaction):
    await interaction.response.defer() # This may take a moment
    
    # 1. Get saved data from the database
    # This is a dictionary: {user_id: saved_balance}
    leaderboard_data = await get_all_currency_data()
    
    # 2. Get "live" data from active_sessions
    now = datetime.datetime.now()
    for user_id, join_time in active_sessions.items():
        # Calculate currency for this user's current session
        current_session_seconds = (now - join_time).total_seconds()
        pending_currency = int(current_session_seconds / SECONDS_PER_CURRENCY)
        
        # Get their saved balance (or 0)
        saved_balance = leaderboard_data.get(user_id, 0)
        
        # Add live + saved balance and update their total
        leaderboard_data[user_id] = saved_balance + pending_currency
        
    # 3. Sort the combined data
    # This gives: [(user_id, total_balance), (user_id, total_balance), ...]
    sorted_leaderboard = sorted(leaderboard_data.items(), key=lambda item: item[1], reverse=True)
    
    # 4. Get the top 10
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
        # Fetch username (same logic as before)
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

# --- ADDED: The /blackjack command ---
@tree.command(name="blackjack", description="Play a game of Blackjack for currency.")
@app_commands.describe(amount="The amount of currency you want to bet")
async def blackjack(interaction: discord.Interaction, amount: int):
    
    user_id = interaction.user.id
    
    # --- 1. Check for invalid inputs ---
    if amount <= 0:
        await interaction.response.send_message("You must bet a positive amount.", ephemeral=True)
        return
        
    # --- 2. Check the user's balance ---
    current_balance = await get_balance(user_id)
    
    if amount > current_balance:
        await interaction.response.send_message(
            f"You don't have enough {CURRENCY_NAME} to make that bet.\n"
            f"Your current balance is: **{current_balance} {CURRENCY_NAME}**", 
            ephemeral=True
        )
        return
        
    # --- 3. Start the game ---
    
    # Defer response so we can send a followup
    await interaction.response.defer()
    
    # Create the view and start the game
    game_view = BlackjackView(interaction, amount)
    await game_view.start_game()

# --- ADDED: The /coinflip command ---
@tree.command(name="coinflip", description="Gamble your currency on a 50/50 coin flip.")
@app_commands.describe(amount="The amount of currency you want to bet")
async def coinflip(interaction: discord.Interaction, amount: int):
    
    user_id = interaction.user.id
    
    # --- 1. Check for invalid inputs ---
    if amount <= 0:
        await interaction.response.send_message("You must bet a positive amount.", ephemeral=True)
        return
        
    # --- 2. Check the user's balance ---
    current_balance = await get_balance(user_id)
    
    if amount > current_balance:
        await interaction.response.send_message(
            f"You don't have enough {CURRENCY_NAME} to make that bet.\n"
            f"Your current balance is: **{current_balance} {CURRENCY_NAME}**", 
            ephemeral=True
        )
        return
        
    # --- 3. Perform the coin flip ---
    # Defer the response so we have time to think
    await interaction.response.defer()
    
    # 50/50 chance
    is_win = random.choice([True, False]) 
    
    if is_win:
        # --- 4. User wins ---
        new_balance = current_balance + amount
        await update_balance(user_id, amount) # Add the winnings
        
        await interaction.followup.send(
            f"**You won!**\n\n"
            f"You won **{amount} {CURRENCY_NAME}**.\n"
            f"Your new balance is **{new_balance} {CURRENCY_NAME}**."
        )
    
    else:
        # --- 5. User loses ---
        new_balance = current_balance - amount
        await update_balance(user_id, -amount) # Subtract the loss
        
        await interaction.followup.send(
            f"**You lost!**\n\n"
            f"You lost **{amount} {CURRENCY_NAME}**.\n"
            f"Your new balance is **{new_balance} {CURRENCY_NAME}**."
        )
# --- BLACKJACK GAME VIEW ---

class BlackjackView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, bet_amount: int):
        super().__init__(timeout=180.0)  # 3-minute timeout
        self.interaction = interaction
        self.player = interaction.user
        self.bet_amount = bet_amount
        self.game_over = False

        # Game state
        self.deck = create_deck()
        self.player_hand = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand = [self.deck.pop(), self.deck.pop()]
    
    async def start_game(self):
        """Sends the initial game message."""
        # Check for immediate Blackjack
        player_score = calculate_hand_value(self.player_hand)
        
        if player_score == 21:
            # Player has Blackjack!
            await self.end_game("win", "Blackjack! You win!")
        else:
            # Send the initial game embed
            embed = self.create_game_embed("Make your move!", "")
            await self.interaction.followup.send(embed=embed, view=self)

    def create_game_embed(self, title: str, status: str, reveal_dealer=False, final_color: discord.Color = None):
        """Creates the embed for the game state."""
        player_score = calculate_hand_value(self.player_hand)
        
        # --- NEW COLOR LOGIC ---
        if final_color:
            # Game is over, use the color from end_game (red or green)
            embed_color = final_color
        else:
            # Game is ongoing, use yellow
            embed_color = discord.Color.gold()
        # --- END NEW LOGIC ---
        
        if reveal_dealer:
            dealer_hand_str = format_hand(self.dealer_hand)
            dealer_score = calculate_hand_value(self.dealer_hand)
        else:
            dealer_hand_str = format_dealer_hand_hidden(self.dealer_hand)
            dealer_score = calculate_hand_value([self.dealer_hand[0]]) # Only show value of first card

        embed = discord.Embed(
            title=f"Blackjack Game: {title}",
            description=f"**Bet:** {self.bet_amount} {CURRENCY_NAME}\n{status}",
            color=embed_color # Use the new logic
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
        
        new_balance = await get_balance(self.player.id) # Get current balance for display
        status_message = ""
        
        # --- NEW COLOR LOGIC ---
        final_game_color = discord.Color.gold() # Default to yellow for a push
        
        if result == "win":
            await update_balance(self.player.id, self.bet_amount)
            new_balance += self.bet_amount
            status_message = f"You won {self.bet_amount} {CURRENCY_NAME}!\nNew Balance: **{new_balance}**"
            final_game_color = discord.Color.green() # WIN = GREEN
            
        elif result == "lose":
            await update_balance(self.player.id, -self.bet_amount)
            new_balance -= self.bet_amount
            status_message = f"You lost {self.bet_amount} {CURRENCY_NAME}!\nNew Balance: **{new_balance}**"
            final_game_color = discord.Color.red() # LOSE = RED
            
        elif result == "push":
            status_message = f"It's a push! Bet returned.\nBalance: **{new_balance}**"
            # final_game_color is already gold
        # --- END NEW LOGIC ---
            
        final_embed = self.create_game_embed(message, status_message, reveal_dealer=True, final_color=final_game_color)
        
        # Check if interaction was already responded to
        try:
            await self.interaction.edit_original_response(embed=final_embed, view=self)
        except discord.errors.InteractionResponded:
            await self.interaction.followup.send(embed=final_embed, view=self)
        except Exception as e:
            print(f"Error editing message: {e}")

        self.stop() # Stop the view from listening
        
    async def on_timeout(self):
        if not self.game_over:
            await self.end_game("lose", "Game timed out! You forfeit your bet.")

    # --- BUTTONS ---
    
    @ui.button(label="Hit", style=discord.ButtonStyle.green)
    async def hit(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("This is not your game!", ephemeral=True)
            return

        # Player hits
        self.player_hand.append(self.deck.pop())
        player_score = calculate_hand_value(self.player_hand)
        
        if player_score > 21:
            # Player busts
            await self.end_game("lose", "Bust! You lost.")
        elif player_score == 21:
            # Auto-stand on 21
            await interaction.response.defer() # Acknowledge click
            await self.dealer_turn()
        else:
            # Update embed
            embed = self.create_game_embed("Hit or Stand?", "")
            await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Stand", style=discord.ButtonStyle.red)
    async def stand(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("This is not your game!", ephemeral=True)
            return
            
        await interaction.response.defer() # Acknowledge click
        await self.dealer_turn()

    async def dealer_turn(self):
        """The dealer's logic after the player stands."""
        dealer_score = calculate_hand_value(self.dealer_hand)
        
        # Dealer must hit until they have at least 17
        while dealer_score < 17:
            self.dealer_hand.append(self.deck.pop())
            dealer_score = calculate_hand_value(self.dealer_hand)
            
        player_score = calculate_hand_value(self.player_hand)
        
        # Determine winner
        if dealer_score > 21:
            await self.end_game("win", "Dealer busts! You win!")
        elif dealer_score > player_score:
            await self.end_game("lose", f"Dealer wins with {dealer_score}!")
        elif player_score > dealer_score:
            await self.end_game("win", f"You win with {player_score}!")
        else:
            await self.end_game("push", "It's a push!")
# --- 6. RUN THE BOT ---
client.run(token)