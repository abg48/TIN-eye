import os
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from app.external.sheets import (
    log_item,
    undo_item,
    item_cache,
    get_scoreboard_scores,
    log_signup,
    DuplicateSignupError,
    user_has_signup,
)

async def item_name_autocomplete(interaction: discord.Interaction, current: str):
    suggestions = [item for item in item_cache.get_items() if current.lower() in item.lower()]
    return [app_commands.Choice(name=item, value=item) for item in suggestions[:25]]

def get_team_number(member: discord.Member):
    for role in member.roles:
        if role.name.startswith("Team ") and role.name[5:].isdigit():
            return role.name
    return None

class UndoDropView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Invalidate", style=discord.ButtonStyle.danger, custom_id="undo_drop")
    async def undo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        content = interaction.message.content
        
        try:
            submitter_id = int(content.split("<@")[1].split(">")[0])
        except (IndexError, ValueError):
            submitter_id = None

        admin_role = discord.utils.get(interaction.guild.roles, name="Admin+")
        is_admin = admin_role in interaction.user.roles
        is_submitter = interaction.user.id == submitter_id

        if not is_admin and not is_submitter:
            await interaction.response.send_message("You can not undo another players drop.", ephemeral=True)
            return

        try:
            drop_id = content.split("Drop ID: ")[1].split("\n")[0]
        except IndexError:
            await interaction.response.send_message("Could not read drop ID from message.", ephemeral=True)
            return
        
        success = False
        try:
            success = undo_item(drop_id)
        except Exception as e:
            await interaction.response.send_message(f"Error undoing drop: {e}", ephemeral=True)
            return
        
        button.disabled = True

        if success:
            archive_message = interaction.message
            invalidation_note = f"\n-# Invalidated by {interaction.user.mention}"
            updated_content = f"~~{content}~~\n-# Drop `{drop_id}` has been removed.{invalidation_note}"

            try:
                await interaction.response.edit_message(
                    content=updated_content,
                    view=self
                )
            except Exception as edit_error:
                await interaction.followup.send(f"Failed to update the invalidation message: {edit_error}", ephemeral=True)
                return

            try:
                await archive_message.edit(content=updated_content)
            except Exception:
                pass
        else:
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(f"Drop '{drop_id}' could not be undone (not found).", ephemeral=True)

class EventsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        try:
            item_cache.sync_items()
        except Exception as e:
            print(f"[EventsCog] Warning: failed to sync item cache on startup: {e}")

    @app_commands.command(name="logdrop", description="Log a players drop to the spreadsheet")
    @app_commands.autocomplete(item_name=item_name_autocomplete)
    async def logdrop(self, interaction: discord.Interaction, item_name: str, proof: discord.Attachment):
        if item_name not in item_cache.get_items():
            await interaction.response.send_message(f"**{item_name}** is not a valid drop. Use the autocomplete suggestions or ping admin+ if you believe this is an error.", ephemeral=True)
            return
        
        if not proof.content_type or not proof.content_type.startswith("image/"):
            await interaction.response.send_message("Proof must be an image file.", ephemeral=True)
            return
        
        team = get_team_number(interaction.user)
        if team is None:
            await interaction.response.send_message("You are not assigned to a team.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        user = interaction.user.display_name
        timestamp = datetime.utcnow().isoformat()
        
        try:
            archive_channel_id = int(os.environ["PROOF_ARCHIVE_CHANNEL_ID"])
            archive_channel = self.bot.get_channel(archive_channel_id)
            archive_message = await archive_channel.send(
                f"Drop proof | {item_name} | {interaction.user.mention} | {timestamp}",
                file=await proof.to_file()
            )
            
            drop_id = log_item(item_name, user, timestamp, team, archive_message.jump_url)

            await archive_message.edit(
                content=f"Drop proof | {item_name} | {interaction.user.mention} | {timestamp}\nDrop ID: {drop_id}",
                view=UndoDropView()
            )

            await interaction.followup.send(
                f"Logged **{item_name}** for {interaction.user.mention}. Drop ID: {drop_id}",
                view=UndoDropView(),
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"Failed to log event, please try again or ping admin+", ephemeral=True)
    
    @app_commands.command(name="eventdbsync", description="Sync the event item database with the spreadsheet")
    async def eventdbsync(self, interaction: discord.Interaction):
        try:
            item_cache.sync_items()
            await interaction.response.send_message("Event item database synced successfully.")
        except Exception as e:
            await interaction.response.send_message("Failed to sync event item database", ephemeral=True)

    @app_commands.command(name="scoreboard", description="Display the current event scoreboard")
    async def scoreboard(self, interaction: discord.Interaction):
        try:
            scores = get_scoreboard_scores()
            
            embed = discord.Embed(
                title="Event Scoreboard",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            
            for team_num in range(1, 6):
                score = scores.get(team_num, "0")
                embed.add_field(name=f"Team {team_num}", value=str(score), inline=False)
            
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"Failed to fetch scoreboard: {e}", ephemeral=True)

    @app_commands.command(name="signup", description="Sign up for the latest event")
    @app_commands.describe(
        in_game_name="Your RuneScape in-game name",
        daily_playtime="Your average daily playtime",
    )
    @app_commands.choices(
        daily_playtime=[
            app_commands.Choice(name=label, value=label)
            for label in ("< 1 hour", "1-3 hours", "3-5 hours", "> 5 hours")
        ]
    )
    async def signup(
        self,
        interaction: discord.Interaction,
        in_game_name: str,
        daily_playtime: str,
    ):
        in_game_name = in_game_name.strip()
        if not in_game_name:
            await interaction.response.send_message(
                "In-game name is required.",
                ephemeral=True,
            )
            return

        if user_has_signup(interaction.user.name):
            await interaction.response.send_message(
                "You are already signed up for this event.",
                ephemeral=True,
            )
            return

        try:
            log_signup(
                discord_username=interaction.user.name,
                in_game_name=in_game_name,
                daily_playtime=daily_playtime,
            )
            await interaction.response.send_message(
                "Signup submitted successfully.",
                ephemeral=True,
            )
        except DuplicateSignupError as e:
            await interaction.response.send_message(
                str(e),
                ephemeral=True,
            )
        except Exception as e:
            await interaction.response.send_message(
                f"Failed to submit signup: {e}",
                ephemeral=True,
            )

async def setup(bot):
    await bot.add_cog(EventsCog(bot))