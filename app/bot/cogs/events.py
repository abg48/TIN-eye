import os
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from app.external.sheets import (
    log_item,
    undo_item,
    item_cache,
    get_scoreboard_scores,
    log_signup,
    DuplicateSignupError,
    user_has_signup,
    get_signup_names,
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

            public_log_message_id = None
            try:
                public_log_message_id = int(content.split("Drop Log Message ID: ")[1].split("\n")[0])
            except (IndexError, ValueError):
                public_log_message_id = None

            try:
                await interaction.response.edit_message(content=updated_content, view=self)
            except Exception as edit_error:
                await interaction.followup.send(f"Failed to update the invalidation message: {edit_error}", ephemeral=True)
                return

            try:
                await archive_message.edit(content=updated_content)
            except Exception:
                pass

            if public_log_message_id is not None:
                try:
                    bot_client = interaction.client
                    drop_log_channel_id = int(os.environ["DROP_LOG_CHANNEL_ID"])
                    drop_log_channel = bot_client.get_channel(drop_log_channel_id)
                    if drop_log_channel is None:
                        drop_log_channel = await bot_client.fetch_channel(drop_log_channel_id)
                    public_log_message = await drop_log_channel.fetch_message(public_log_message_id)
                    public_updated_content = (
                        f"~~{public_log_message.content}~~\n"
                        f"-# Drop `{drop_id}` has been invalidated."
                    )
                    await public_log_message.edit(content=public_updated_content)
                except Exception:
                    pass
        else:
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(f"Drop '{drop_id}' could not be undone (not found).", ephemeral=True)


class RosterView(discord.ui.View):
    PAGE_SIZE = 50

    def __init__(self, names: list[str]):
        super().__init__(timeout=300)
        self.names = names
        self.page = 0
        self.message: discord.Message | None = None
        self.page_count = max(1, -(-len(names) // self.PAGE_SIZE))
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.page_count - 1

    def render(self) -> str:
        if not self.names:
            return "No one has signed up yet."
        start = self.page * self.PAGE_SIZE
        chunk = self.names[start:start + self.PAGE_SIZE]
        width = max(len("In-Game Name"), *(len(n) for n in chunk))
        lines = [f"{'#':>3} | {'In-Game Name':<{width}}", f"{'-' * 3}-+-{'-' * width}"]
        lines += [f"{i:>3} | {n:<{width}}" for i, n in enumerate(chunk, start + 1)]
        body = "\n".join(lines)
        return f"```\n{body}\n```Page {self.page + 1}/{self.page_count}"

    async def _turn(self, interaction: discord.Interaction, delta: int):
        self.page += delta
        self._sync_buttons()
        await interaction.response.edit_message(content=self.render(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn(interaction, 1)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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
        drop_id = None

        try:
            team_number = int(team.split()[1])
            score_before = Decimal(
                str(get_scoreboard_scores()[team_number]).replace(",", "").strip() or "0"
            )

            archive_channel_id = int(os.environ["PROOF_ARCHIVE_CHANNEL_ID"])
            drop_log_channel_id = int(os.environ["DROP_LOG_CHANNEL_ID"])
            archive_channel = self.bot.get_channel(archive_channel_id)
            if archive_channel is None:
                archive_channel = await self.bot.fetch_channel(archive_channel_id)
            drop_log_channel = self.bot.get_channel(drop_log_channel_id)
            if drop_log_channel is None:
                drop_log_channel = await self.bot.fetch_channel(drop_log_channel_id)

            archive_message = await archive_channel.send(
                f"Drop proof | {item_name} | {interaction.user.mention} | {timestamp}",
                file=await proof.to_file()
            )

            drop_id = log_item(item_name, user, timestamp, team, archive_message.jump_url)
            score_after = Decimal(
                str(get_scoreboard_scores()[team_number]).replace(",", "").strip() or "0"
            )
            points_earned = score_after - score_before

            await archive_message.edit(
                content=f"Drop proof | {item_name} | {interaction.user.mention} | {timestamp}\nDrop ID: {drop_id}",
                view=UndoDropView()
            )

            try:
                public_log_message = await drop_log_channel.send(
                    f"{interaction.user.mention} logged **{item_name}** drop for **{team}**, "
                    f"earning them **{format(points_earned.normalize(), 'f')} points**!"
                )
                await archive_message.edit(
                    content=(
                        f"Drop proof | {item_name} | {interaction.user.mention} | {timestamp}\n"
                        f"Drop ID: {drop_id}\n"
                        f"Drop Log Message ID: {public_log_message.id}"
                    ),
                    view=UndoDropView(),
                )
            except Exception:
                await interaction.followup.send(
                    f"Logged **{item_name}** for {interaction.user.mention}. Drop ID: {drop_id}. "
                    "The public drop log could not be updated; please notify admin+.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                f"Logged **{item_name}** for {interaction.user.mention}. Drop ID: {drop_id}",
                view=UndoDropView(),
                ephemeral=True
            )
        except InvalidOperation:
            if drop_id is None:
                message = "Could not read the team score, so the drop was not logged. Please notify admin+."
            else:
                message = f"Drop {drop_id} was logged, but its score change could not be calculated. Please notify admin+."
            await interaction.followup.send(message, ephemeral=True)
        except Exception as e:
            if drop_id is None:
                message = "Failed to log event, please try again or ping admin+"
            else:
                message = f"Drop {drop_id} was logged, but a follow-up step failed. Please notify admin+."
            await interaction.followup.send(message, ephemeral=True)

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
        in_game_name: app_commands.Range[str, 1, 15],
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

    @app_commands.command(name="event_roster", description="Show who is registered for the event")
    async def event_roster(self, interaction: discord.Interaction):
        try:
            names = get_signup_names()
        except Exception as e:
            await interaction.response.send_message(
                f"Failed to fetch roster: {e}",
                ephemeral=True,
            )
            return

        view = RosterView(names)
        await interaction.response.send_message(view.render(), view=view)
        view.message = await interaction.original_response()


async def setup(bot):
    await bot.add_cog(EventsCog(bot))
