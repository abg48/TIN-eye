import discord
from discord import app_commands
from discord.ext import commands
from typing import List
from app.bot.db_queries import(
    get_member_total_xp,
    get_all_member_xp,
    get_member_xp_gains,
    get_inactive_members,
    get_members_by_rank,
    get_member_xp_history,
    get_inactive_members_by_rank_and_days,
    get_private_members,
    rename_member
)

RANK_THRESHOLDS = [
    ("Recruit", 0),
    ("Corporal", 20_000_000),
    ("Sergeant", 75_000_000),
    ("Lieutenant", 250_000_000),
    ("Captain", 700_000_000),
    ("General", 1_500_000_000),
]

def paginate_lines(lines: List[str], max_chars=2000) -> List[str]:
    pages = []
    current = []
    current_len = 0
    for line in lines:
        l = line + "\n"
        if current_len + len(l) > max_chars:
            pages.append("".join(current).rstrip())
            current = [l]
            current_len = len(l)
        else:
            current.append(l)
            current_len += len(l)
    if current:
        pages.append("".join(current).rstrip())
    return pages

class InactivePaginationView(discord.ui.View):
    def __init__(self, pages: List[str], author_id: int, title: str, timeout: int = 120,
                 color: discord.Color = discord.Color.red(), footer_note: str = ""):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author_id = author_id
        self.title = title
        self.color = color
        self.footer_note = footer_note
        self.index = 0

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=self.title, description=self.pages[self.index], color=self.color)
        footer = f"Page {self.index + 1}/{len(self.pages)}"
        if self.footer_note:
            footer = f"{footer} | {self.footer_note}"
        embed.set_footer(text=footer)
        return embed
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This pagination session isn't for you", ephemeral=True)
            return False
        return True
    
    @discord.ui.button(label="⬅️ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="➡️ Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="⛔ Close", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(view=None)

    async def on_timeout(self):
        # remove buttons when view times out
        for child in self.children:
            child.disabled = True
        try:
            # edit original message if still present
            await self.message.edit(view=self)
        except Exception:
            pass

    # will be set by caller after sending the initial message
    message: discord.Message

class MembersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="xp", description="Get a member's total xp")
    async def get_xp(self, interaction: discord.Interaction, rsn: str):
        member_data = get_member_total_xp(rsn)

        if not member_data or member_data['total_xp'] is None:
            await interaction.response.send_message(f"Member **{rsn}** not found or has no xp data.")
            return
        
        embed = discord.Embed(
            title=f"{member_data['rsn']}'s Stats",
            color=discord.Color.blue()
        )
        embed.add_field(name="Rank", value=member_data['rank'], inline=True)
        embed.add_field(name="Total XP", value=f"{member_data['total_xp']:,}", inline=True)
        embed.add_field(name="Last Updated", value=member_data['timestamp'], inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Show the clan xp leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        members = get_all_member_xp()

        if not members:
            await interaction.response.send_message("No members with xp data found.")
            return
        
        # Take top 10 members
        top_10 = members[:10]

        embed = discord.Embed(
            title="Clan XP Leaderboard",
            color=discord.Color.gold()
        )

        leaderboard_text = "\n".join(
            [f"{i+1}. {m['rsn']} - {m['total_xp']:,} XP ({m['rank']})" 
             for i, m in enumerate(top_10)]
        )
        embed.description = leaderboard_text

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rankups", description="Show members eligible for a rank-up based on clan XP")
    async def rankups(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            members = get_member_xp_gains()
        except Exception:
            await interaction.followup.send(
                "Could not retrieve member XP data. Please try again later.",
                ephemeral=True,
            )
            return

        rank_indices = {rank.casefold(): index for index, (rank, _) in enumerate(RANK_THRESHOLDS)}
        eligible = []
        for member in members:
            current_rank = member.get("rank")
            if not isinstance(current_rank, str):
                continue

            current_rank_index = rank_indices.get(current_rank.casefold())
            if current_rank_index is None:
                continue

            gained_xp = member["current_xp"] - member["baseline_xp"]
            if gained_xp < 0:
                continue

            earned_rank_index = max(
                index for index, (_, threshold) in enumerate(RANK_THRESHOLDS)
                if gained_xp >= threshold
            )
            if earned_rank_index > current_rank_index:
                eligible.append((member, gained_xp, earned_rank_index, current_rank_index))

        eligible.sort(key=lambda result: (-result[1], result[0]["rsn"].casefold()))
        if not eligible:
            await interaction.followup.send("No members are currently eligible for a rank-up.")
            return

        lines = [
            f"- **{member['rsn']}**: {RANK_THRESHOLDS[current_rank_index][0]} "
            f"-> {RANK_THRESHOLDS[earned_rank_index][0]} ({gained_xp:,} tracked XP)"
            for member, gained_xp, earned_rank_index, current_rank_index in eligible
        ]
        pages = paginate_lines(lines)
        title = f"Rank-up Eligibility ({len(eligible)} members)"
        footer_note = "Earliest recorded public snapshot; older members may be undercounted"

        if len(pages) == 1:
            embed = discord.Embed(title=title, description=pages[0], color=discord.Color.gold())
            embed.set_footer(text=footer_note)
            await interaction.followup.send(embed=embed)
            return

        view = InactivePaginationView(
            pages=pages,
            author_id=interaction.user.id,
            title=title,
            color=discord.Color.gold(),
            footer_note=footer_note,
        )
        view.message = await interaction.followup.send(embed=view._build_embed(), view=view, wait=True)

    @app_commands.command(name="inactive", description="Show inactive members by rank and days")
    async def inactive(self, interaction: discord.Interaction, rank: str, days: int):
        if days < 1:
            await interaction.response.send_message("Days must be at least 1.", ephemeral=True)
            return
        
        inactive_members = get_inactive_members_by_rank_and_days(rank, days)

        if not inactive_members:
            await interaction.response.send_message(f"No **{rank}** members found inactive in {days}+ days.", ephemeral=True)
            return
        
        lines = [f"- {m['rsn']}: {m['current_xp']:,} XP (unchanged since {m['old_timestamp']})" 
                 for m in inactive_members]
        pages = paginate_lines(lines)
        title = f"Inactive {rank}s ({len(inactive_members)} - No XP gain in {days}+ days)"

        if len(pages) == 1:
            embed = discord.Embed(title=title, description=pages[0], color=discord.Color.red())
            await interaction.response.send_message(embed=embed)
            return

        view = InactivePaginationView(pages=pages, author_id=interaction.user.id, title=title)
        await interaction.response.send_message(embed=view._build_embed(), view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="private", description="Show which clan members have private profiles")
    async def private(self, interaction: discord.Interaction):
        private_members = get_private_members()

        if not private_members:
            await interaction.response.send_message("No members with private profiles found.", ephemeral=True)
            return
        
        lines = [f"- {m['rsn']} ({m['rank']})" for m in private_members]
        pages = paginate_lines(lines)
        title = f"Private Profiles: ({len(private_members)} members)"

        if len(pages) == 1:
            embed = discord.Embed(title=title, description=pages[0], color=discord.Color.orange())
            await interaction.response.send_message(embed=embed)
            return
        
        view = InactivePaginationView(pages=pages, author_id=interaction.user.id, title=title)
        await interaction.response.send_message(embed=view._build_embed(), view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="rename", description="Manually link a new RSN to an older player's data")
    async def rename(self, interaction: discord.Interaction, new_rsn: str, old_rsn: str):
        
        success, message = rename_member(old_rsn, new_rsn)
        
        if success:
            embed = discord.Embed(
                title="Member Renamed",
                description=message,
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="Rename Failed",
                description=message,
                color=discord.Color.red()
            )
        
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(MembersCog(bot))