"""Discord moderation bot with slash commands.

The bot token is intentionally read from the DISCORD_BOT_TOKEN environment
variable. It should never be committed to source control.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import urllib.parse
import urllib.request

import discord
from discord import app_commands
from discord.ext import commands

from .fixture_cards import render_fixture_card

LOGGER = logging.getLogger("league_mod_bot")
DEFAULT_REASON = "No reason provided"
MAX_TIMEOUT_MINUTES = 28 * 24 * 60
MAX_SLOWMODE_SECONDS = 6 * 60 * 60
DEFAULT_APPROVAL_QUORUM = 2
MAX_REQUEST_TITLE_LENGTH = 80
MAX_REQUEST_DESCRIPTION_LENGTH = 1000
MAX_MATCH_TEAM_LENGTH = 80
DEFAULT_PREDICTION_MINUTES = 60
MAX_PREDICTION_MINUTES = 7 * 24 * 60
APPROVAL_DATA_PATH = Path(os.getenv("APPROVAL_DATA_PATH", "data/approval_requests.json"))
HISTORY_DATA_PATH = Path(os.getenv("MODERATION_HISTORY_PATH", "data/moderation_history.json"))
LEAGUE_DATA_PATH = Path(os.getenv("LEAGUE_DATA_PATH", "data/league_state.json"))
BOT_PROFILE_NAME = "PL Assistant"
BOT_AVATAR_PATH = Path(__file__).with_name("assets") / "fel-bot-avatar.png"
DEFAULT_PLAYER_OVR = 40

approval_requests: dict[str, dict[str, object]] = {}
moderation_history: dict[str, list[dict[str, str]]] = {}
league_state: dict[str, object] = {"guilds": {}}
team_logo_cache: dict[str, bytes | None] = {}
prediction_expiration_tasks: dict[str, asyncio.Task[None]] = {}
auction_expiration_tasks: dict[str, asyncio.Task[None]] = {}
scrim_expiration_tasks: dict[str, asyncio.Task[None]] = {}
approval_lock: asyncio.Lock | None = None


def configure_logging() -> None:
    """Configure compact, deployment-friendly logs."""

    level_name = os.getenv("DISCORD_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


class LeagueModBot(commands.Bot):
    """Bot instance that synchronizes its application commands at startup."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        if os.getenv("DISCORD_ENABLE_MEMBERS_INTENT", "").strip().lower() in {"1", "true", "yes"}:
            intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self._profile_sync_attempted = False

    async def setup_hook(self) -> None:
        load_approval_requests()
        load_moderation_history()
        load_league_state()
        self.add_view(ApprovalView())
        self.add_view(PredictionView())
        self.add_view(VerifyView())
        self.add_view(TicketPanelView())
        self.add_view(RulebookView())
        guilds = league_state.get("guilds", {})
        if isinstance(guilds, dict):
            for guild_data in guilds.values():
                scrims = guild_data.get("scrims", []) if isinstance(guild_data, dict) else []
                if not isinstance(scrims, list):
                    continue
                for scrim in scrims:
                    if not isinstance(scrim, dict) or scrim.get("status") != "open" or not scrim.get("message_id"):
                        continue
                    try:
                        self.add_view(
                            ScrimSignupView(str(scrim["id"])),
                            message_id=int(scrim["message_id"]),
                        )
                    except (KeyError, TypeError, ValueError):
                        LOGGER.warning("Skipping scrim with invalid persistent message information")

        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            try:
                guild = discord.Object(id=int(guild_id))
            except ValueError as error:
                raise RuntimeError("DISCORD_GUILD_ID must be a numeric server ID") from error

            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            LOGGER.info("Synced %d slash commands to server %s", len(synced), guild_id)
            return

        synced = await self.tree.sync()
        LOGGER.info(
            "Synced %d global slash commands. Global propagation can take up to an hour.",
            len(synced),
        )

    async def on_ready(self) -> None:
        if self.user is None:
            LOGGER.info("Discord connection ready")
            return
        await self.change_presence(status=discord.Status.online)
        if not self._profile_sync_attempted:
            await self.sync_profile()
        schedule_open_prediction_expirations()
        schedule_open_auction_expirations()
        schedule_open_scrim_expirations()
        LOGGER.info("Bot online as %s (ID %s) in %d server(s)", self.user, self.user.id, len(self.guilds))

    async def on_member_join(self, member: discord.Member) -> None:
        """Apply configured autoroles and send configured welcome messages."""

        data = get_guild_data(member.guild.id)
        config = data["config"]
        role_id = config.get("autorole_id")
        if role_id:
            role = member.guild.get_role(int(role_id))
            if role is not None and role_can_be_managed_for_guild(member.guild, role):
                try:
                    await member.add_roles(role, reason="Configured autorole")
                except discord.HTTPException:
                    LOGGER.warning("Could not apply autorole in guild %s", member.guild.id)

        welcome_channel_id = config.get("welcome_channel_id")
        welcome_message = config.get("welcome_message")
        if welcome_channel_id and welcome_message:
            channel = member.guild.get_channel(int(welcome_channel_id))
            if isinstance(channel, discord.TextChannel):
                content = str(welcome_message).replace("{user}", member.mention).replace(
                    "{server}", member.guild.name
                )
                try:
                    await channel.send(content, allowed_mentions=discord.AllowedMentions(users=True))
                except discord.HTTPException:
                    LOGGER.warning("Could not send welcome message in guild %s", member.guild.id)

    async def sync_profile(self) -> None:
        """Apply the requested bot name and avatar once per process."""

        self._profile_sync_attempted = True
        if self.user is None:
            return
        try:
            avatar = BOT_AVATAR_PATH.read_bytes()
            await self.user.edit(username=BOT_PROFILE_NAME, avatar=avatar)
            LOGGER.info("Bot profile updated to %s", BOT_PROFILE_NAME)
        except (OSError, discord.HTTPException) as error:
            LOGGER.warning("Could not update bot profile: %s", error)


bot = LeagueModBot()


def new_guild_data() -> dict[str, object]:
    """Return the persisted league/configuration shape for one server."""

    return {
        "teams": {},
        "players": {},
        "fixtures": [],
        "scrims": [],
        "auctions": {},
        "config": {},
    }


def get_guild_data(guild_id: int) -> dict[str, object]:
    """Get or initialize a server's league data."""

    guilds = league_state.setdefault("guilds", {})
    if not isinstance(guilds, dict):
        league_state["guilds"] = guilds = {}
    data = guilds.setdefault(str(guild_id), new_guild_data())
    if not isinstance(data, dict):
        data = new_guild_data()
        guilds[str(guild_id)] = data
    for key, default in new_guild_data().items():
        if key not in data or not isinstance(data[key], type(default)):
            data[key] = default
    return data


def load_league_state() -> None:
    """Load league and server configuration data without blocking startup."""

    if not LEAGUE_DATA_PATH.exists():
        return
    try:
        saved = json.loads(LEAGUE_DATA_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict) and isinstance(saved.get("guilds"), dict):
            league_state["guilds"] = saved["guilds"]
        LOGGER.info("Loaded league data for %d server(s)", len(league_state.get("guilds", {})))
    except (OSError, json.JSONDecodeError) as error:
        LOGGER.warning("Could not load league data: %s", error)


def save_league_state() -> None:
    """Persist league and server configuration atomically."""

    LEAGUE_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = LEAGUE_DATA_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(league_state, indent=2, sort_keys=True), encoding="utf-8")
    temporary_path.replace(LEAGUE_DATA_PATH)


def team_key(name: str) -> str:
    """Normalize a team lookup name while preserving its display name in storage."""

    return " ".join(name.casefold().split())


def find_team(guild: discord.Guild, name: str) -> tuple[str, dict[str, object]] | None:
    teams = get_guild_data(guild.id)["teams"]
    if not isinstance(teams, dict):
        return None
    key = team_key(name)
    record = teams.get(key)
    return (key, record) if isinstance(record, dict) else None


LIGUE_1_TEAM_NAMES = (
    "AJ Auxerre",
    "Angers SCO",
    "AS Monaco",
    "Stade Brestois 29",
    "Le Havre AC",
    "RC Lens",
    "LOSC Lille",
    "FC Lorient",
    "Olympique Lyonnais",
    "Olympique de Marseille",
    "FC Metz",
    "FC Nantes",
    "OGC Nice",
    "Paris FC",
    "Paris Saint-Germain",
    "Stade Rennais FC",
    "RC Strasbourg Alsace",
    "Toulouse FC",
)
LIGUE_1_NAME_ALIASES = {
    "auxerre": "AJ Auxerre",
    "angers": "Angers SCO",
    "monaco": "AS Monaco",
    "as monaco": "AS Monaco",
    "brest": "Stade Brestois 29",
    "le havre": "Le Havre AC",
    "lens": "RC Lens",
    "lille": "LOSC Lille",
    "lorient": "FC Lorient",
    "lyon": "Olympique Lyonnais",
    "ol": "Olympique Lyonnais",
    "marseille": "Olympique de Marseille",
    "om": "Olympique de Marseille",
    "metz": "FC Metz",
    "nantes": "FC Nantes",
    "nice": "OGC Nice",
    "paris fc": "Paris FC",
    "psg": "Paris Saint-Germain",
    "paris": "Paris Saint-Germain",
    "rennes": "Stade Rennais FC",
    "strasbourg": "RC Strasbourg Alsace",
    "toulouse": "Toulouse FC",
}


def canonical_ligue_1_name(name: str) -> str | None:
    normalized = team_key(name)
    if normalized in LIGUE_1_NAME_ALIASES:
        return LIGUE_1_NAME_ALIASES[normalized]
    return next((candidate for candidate in LIGUE_1_TEAM_NAMES if team_key(candidate) == normalized), None)


def find_fixture_team(guild: discord.Guild, name: str) -> tuple[str, dict[str, object]] | None:
    """Resolve a stored team or a known Ligue 1 club for fixture commands."""

    found = find_team(guild, name)
    if found is not None:
        return found
    canonical_name = canonical_ligue_1_name(name)
    if canonical_name is None:
        return None
    key = team_key(canonical_name)
    teams = get_guild_data(guild.id)["teams"]
    if not isinstance(teams, dict):
        return None
    for stored_key, record in teams.items():
        if (
            isinstance(record, dict)
            and canonical_ligue_1_name(str(record.get("name", ""))) == canonical_name
        ):
            return str(stored_key), record
    record = teams.setdefault(
        key,
        {
            "name": canonical_name,
            "role_id": "",
            "manager_id": "",
            "channel_id": "",
            "budget": 0,
            "players": [],
        },
    )
    return (key, record) if isinstance(record, dict) else None


async def team_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Offer existing league teams as selectable slash-command choices."""

    if interaction.guild is None:
        return []
    query = current.casefold().strip()
    names = set(LIGUE_1_TEAM_NAMES)
    teams = get_guild_data(interaction.guild.id)["teams"]
    if isinstance(teams, dict):
        for record in teams.values():
            if not isinstance(record, dict) or not record.get("name"):
                continue
            name = str(record.get("name", "")).strip()
            if name and canonical_ligue_1_name(name) is None:
                names.add(name)
    choices: list[app_commands.Choice[str]] = []
    for name in sorted(names):
        aliases = [alias for alias, canonical in LIGUE_1_NAME_ALIASES.items() if canonical == name]
        if name and (not query or query in name.casefold() or any(query in alias for alias in aliases)):
            choices.append(app_commands.Choice(name=name[:100], value=name[:100]))
    return choices[:25]


async def saved_team_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Offer only teams that exist in this server's saved league data."""

    if interaction.guild is None:
        return []
    teams = get_guild_data(interaction.guild.id)["teams"]
    if not isinstance(teams, dict):
        return []
    query = current.casefold().strip()
    choices: list[app_commands.Choice[str]] = []
    saved_names = sorted(
        {
            str(record.get("name", key)).strip()
            for key, record in teams.items()
            if isinstance(record, dict) and str(record.get("name", key)).strip()
        },
        key=str.casefold,
    )
    for name in saved_names:
        if not query or query in name.casefold():
            choices.append(app_commands.Choice(name=name[:100], value=name[:100]))
    return choices[:25]


def currency(value: int | float) -> str:
    return f"${value:,.0f}"


def role_can_be_managed_for_guild(guild: discord.Guild, role: discord.Role) -> bool:
    guild_me = guild.me
    return guild_me is not None and not role.managed and role < guild_me.top_role


async def send_configured_log(
    guild: discord.Guild,
    config_key: str,
    embed: discord.Embed,
) -> None:
    """Best-effort delivery to a configured server log channel."""

    config = get_guild_data(guild.id)["config"]
    if not isinstance(config, dict):
        return
    channel_id = config.get(config_key)
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            LOGGER.warning("Could not send %s log in guild %s", config_key, guild.id)


def auction_for_message(guild: discord.Guild, message_id: int) -> dict[str, object] | None:
    auctions = get_guild_data(guild.id)["auctions"]
    if not isinstance(auctions, dict):
        return None
    for record in auctions.values():
        if isinstance(record, dict) and str(record.get("message_id")) == str(message_id):
            return record
    return None


def ticket_owner_id(channel: discord.TextChannel) -> int | None:
    topic = channel.topic or ""
    match = re.fullmatch(r"ticket-owner:(\d+)", topic.strip())
    return int(match.group(1)) if match else None


def is_ticket_staff(interaction: discord.Interaction, channel: discord.TextChannel) -> bool:
    member = interaction.user
    return (
        isinstance(member, discord.Member)
        and (member.id == ticket_owner_id(channel) or member.guild_permissions.manage_channels)
    )


def get_player_ovr(guild_id: int, player_id: object) -> int:
    players = get_guild_data(guild_id).get("players", {})
    record = players.get(str(player_id)) if isinstance(players, dict) else None
    if not isinstance(record, dict):
        return DEFAULT_PLAYER_OVR
    try:
        return max(1, min(99, int(record.get("ovr", DEFAULT_PLAYER_OVR))))
    except (TypeError, ValueError):
        return DEFAULT_PLAYER_OVR


def format_team_roster(
    guild: discord.Guild,
    team: dict[str, object],
) -> str:
    player_ids = team.get("players", [])
    players = get_guild_data(guild.id)["players"]
    if not isinstance(player_ids, list) or not isinstance(players, dict) or not player_ids:
        return "No players signed."
    lines: list[str] = []
    for player_id in player_ids:
        player = players.get(str(player_id))
        if isinstance(player, dict):
            price = int(player.get("price", 0))
            position = str(player.get("position", "ST"))
            loan = player.get("loan")
        else:
            price = 0
            position = "ST"
            loan = None
        line = (
            f"<@{player_id}> · OVR {get_player_ovr(guild.id, player_id)} · "
            f"{currency(price)} · {position}"
        )
        if isinstance(loan, dict) and loan.get("from_team_name"):
            line += f" · loan from {loan['from_team_name']}"
        lines.append(line)
    return "\n".join(lines) or "No players signed."


def get_transfer_news_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Return the configured transaction-news channel, if it still exists."""

    config = get_guild_data(guild.id)["config"]
    channel_id = config.get("transfer_news_channel_id") if isinstance(config, dict) else None
    try:
        channel = guild.get_channel(int(channel_id)) if channel_id else None
    except (TypeError, ValueError):
        return None
    return channel if isinstance(channel, discord.TextChannel) else None


async def post_transfer_news(
    guild: discord.Guild,
    embed: discord.Embed,
    crest_team_name: str,
) -> bool:
    """Post an official deal card into the configured news channel."""

    channel = get_transfer_news_channel(guild)
    if channel is None:
        return False
    try:
        crest = await fetch_team_logo(crest_team_name)
        if crest:
            file = discord.File(io.BytesIO(crest), filename="team-logo.png")
            embed.set_thumbnail(url="attachment://team-logo.png")
            await channel.send(
                embed=embed,
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except discord.HTTPException as error:
        LOGGER.warning("Could not post transaction news in guild %s: %s", guild.id, error)
        return False
    return True


async def update_player_team_role(
    guild: discord.Guild,
    member: discord.Member,
    from_team: dict[str, object],
    to_team: dict[str, object],
    reason: str,
) -> None:
    """Move the member between configured team roles when possible."""

    old_role_id = from_team.get("role_id")
    new_role_id = to_team.get("role_id")
    try:
        old_role = guild.get_role(int(old_role_id)) if old_role_id else None
        new_role = guild.get_role(int(new_role_id)) if new_role_id else None
    except (TypeError, ValueError):
        LOGGER.warning("Invalid team role configured while moving player %s", member.id)
        return
    if old_role is not None and old_role == new_role:
        return
    try:
        if old_role is not None and old_role in member.roles:
            await member.remove_roles(old_role, reason=reason)
        if new_role is not None and new_role not in member.roles:
            await member.add_roles(new_role, reason=reason)
    except discord.HTTPException as error:
        LOGGER.warning("Could not update team role for player %s: %s", member.id, error)


def build_loan_return_embed(player_id: str, player_name: str, team_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="🚨 OFFICIAL LOAN RETURN",
        description=f"<@{player_id}> has returned to **{team_name}**.",
        color=discord.Color.orange(),
    )
    embed.set_footer(text=player_name)
    return embed


def advance_loans_after_result(
    data: dict[str, object],
    played_team_keys: set[str],
) -> list[dict[str, str]]:
    """Count each posted result for the borrowing team and return expired players."""

    players = data.get("players")
    teams = data.get("teams")
    if not isinstance(players, dict) or not isinstance(teams, dict):
        return []

    returned: list[dict[str, str]] = []
    for player_id, player in players.items():
        if not isinstance(player, dict):
            continue
        loan = player.get("loan")
        current_key = str(player.get("team_key", ""))
        if not isinstance(loan, dict) or current_key not in played_team_keys:
            continue
        try:
            remaining = int(loan.get("matches_remaining", 1)) - 1
        except (TypeError, ValueError):
            remaining = 0
        loan["matches_remaining"] = max(remaining, 0)
        if remaining > 0:
            continue

        source_key = str(loan.get("from_team_key", ""))
        source_team = teams.get(source_key)
        destination_team = teams.get(current_key)
        if not isinstance(source_team, dict) or not isinstance(destination_team, dict):
            LOGGER.warning("Cannot return loaned player %s because a team record is missing", player_id)
            continue
        source_players = source_team.get("players")
        destination_players = destination_team.get("players")
        if not isinstance(source_players, list) or not isinstance(destination_players, list):
            LOGGER.warning("Cannot return loaned player %s because a team roster is invalid", player_id)
            continue

        player_id_string = str(player_id)
        if player_id_string in destination_players:
            destination_players.remove(player_id_string)
        if player_id_string not in source_players:
            source_players.append(player_id_string)
        source_name = str(source_team.get("name", source_key))
        destination_name = str(destination_team.get("name", current_key))
        player["team_key"] = source_key
        player["team_name"] = source_name
        player.pop("loan", None)
        returned.append(
            {
                "player_id": player_id_string,
                "player_name": str(player.get("name", player_id_string)),
                "from_team_name": destination_name,
                "to_team_name": source_name,
                "from_team_key": current_key,
                "to_team_key": source_key,
            }
        )
    return returned


async def publish_loan_returns(
    guild: discord.Guild,
    data: dict[str, object],
    returned: list[dict[str, str]],
) -> None:
    teams = data.get("teams")
    for loan_return in returned:
        if not isinstance(teams, dict):
            continue
        from_team = teams.get(loan_return["from_team_key"])
        to_team = teams.get(loan_return["to_team_key"])
        if isinstance(from_team, dict) and isinstance(to_team, dict):
            try:
                member = guild.get_member(int(loan_return["player_id"]))
                if member is None:
                    member = await guild.fetch_member(int(loan_return["player_id"]))
                await update_player_team_role(
                    guild,
                    member,
                    from_team,
                    to_team,
                    f"Loan completed; returned to {loan_return['to_team_name']}",
                )
            except (TypeError, ValueError, discord.HTTPException):
                LOGGER.info("Could not update team role for returned player %s", loan_return["player_id"])

        embed = build_loan_return_embed(
            loan_return["player_id"],
            loan_return["player_name"],
            loan_return["to_team_name"],
        )
        if not await post_transfer_news(guild, embed, loan_return["to_team_name"]):
            LOGGER.warning("Could not post loan return for player %s", loan_return["player_id"])


def standings_record(data: dict[str, object], key: str) -> dict[str, int]:
    standings = data["standings"] if "standings" in data else None
    if not isinstance(standings, dict):
        standings = {}
        data["standings"] = standings
    record = standings.setdefault(
        key,
        {"played": 0, "wins": 0, "draws": 0, "losses": 0, "goals_for": 0, "goals_against": 0, "points": 0},
    )
    if not isinstance(record, dict):
        record = {"played": 0, "wins": 0, "draws": 0, "losses": 0, "goals_for": 0, "goals_against": 0, "points": 0}
        standings[key] = record
    return record


FIXTURE_REACTION_A = "🔴"
FIXTURE_REACTION_B = "🔵"
TEAM_LOGO_ALIASES = {
    "om": "Olympique de Marseille",
    "marseille": "Olympique de Marseille",
    "as monaco": "AS Monaco",
    "monaco": "AS Monaco",
}
TEAM_LOGO_WIKI_PAGES = {
    "aj auxerre": "AJ_Auxerre",
    "angers sco": "Angers_SCO",
    "as monaco": "AS_Monaco",
    "stade brestois 29": "Stade_Brestois_29",
    "le havre ac": "Le_Havre_AC",
    "rc lens": "RC_Lens",
    "losc lille": "Lille_OSC",
    "fc lorient": "FC_Lorient",
    "olympique lyonnais": "Olympique_Lyonnais",
    "olympique de marseille": "Olympique_de_Marseille",
    "fc metz": "FC_Metz",
    "fc nantes": "FC_Nantes",
    "ogc nice": "OGC_Nice",
    "paris fc": "Paris_FC",
    "paris saint-germain": "Paris_Saint-Germain_F.C.",
    "stade rennais fc": "Stade_Rennais_F.C.",
    "rc strasbourg alsace": "RC_Strasbourg_Alsace",
    "toulouse fc": "Toulouse_FC",
}


async def fetch_team_logo(team_name: str) -> bytes | None:
    """Fetch the matching club crest from Wikimedia and cache it by team name."""

    cache_key = team_key(team_name)
    if cache_key in team_logo_cache:
        return team_logo_cache[cache_key]
    canonical_name = canonical_ligue_1_name(team_name)
    search_name = canonical_name or TEAM_LOGO_ALIASES.get(cache_key, team_name)
    page = TEAM_LOGO_WIKI_PAGES.get(team_key(search_name), search_name.replace(" ", "_"))

    def download() -> bytes | None:
        try:
            page_name = urllib.parse.quote(page, safe="_.'()")
            summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{page_name}"
            request = urllib.request.Request(
                summary_url,
                headers={"User-Agent": "FEL-BOT/1.0 (public fixture crest lookup)"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            thumbnail = payload.get("thumbnail")
            logo_url = thumbnail.get("source") if isinstance(thumbnail, dict) else None
            if not isinstance(logo_url, str) or not logo_url.startswith("https://"):
                return None
            logo_request = urllib.request.Request(
                logo_url,
                headers={"User-Agent": "FEL-BOT/1.0 (public fixture crest lookup)"},
            )
            with urllib.request.urlopen(logo_request, timeout=5) as response:
                image_bytes = response.read(5_000_001)
            if len(image_bytes) > 5_000_000:
                LOGGER.warning("Ignoring oversized team crest image for %s", team_name)
                return None
            return image_bytes
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as error:
            LOGGER.info("No crest found for team %s: %s", team_name, error)
            return None

    logo_data = await asyncio.to_thread(download)
    team_logo_cache[cache_key] = logo_data
    return logo_data


def parse_match_timestamp(value: str | None, fallback: int) -> int | None:
    """Accept a Unix timestamp or an ISO-like UTC date entered in a slash command."""

    raw = (value or "").strip()
    if not raw:
        return fallback
    if raw.isdigit():
        timestamp = int(raw)
        return timestamp if timestamp > 0 else None
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


def fixture_embed(record: dict[str, object]) -> discord.Embed:
    """Build the text wrapper and reaction legend for a fixture card."""

    team_a = str(record.get("team1_name", "Team A"))
    team_b = str(record.get("team2_name", "Team B"))
    status = str(record.get("status", "scheduled"))
    timestamp = int(record.get("timestamp", 0))
    if status == "completed":
        score_label = f"{record.get('score1', 0)}–{record.get('score2', 0)}"
        title = f"Final result · {team_a} vs {team_b}"
        description = f"**{score_label}** · <t:{timestamp}:F>"
        color = discord.Color.dark_grey()
    else:
        title = f"Fixture · {team_a} vs {team_b}"
        description = (
            f"<t:{timestamp}:F> (<t:{timestamp}:R>)\n"
            f"React {FIXTURE_REACTION_A} for **{team_a}** or {FIXTURE_REACTION_B} for **{team_b}**."
        )
        color = discord.Color.blurple()
    return discord.Embed(title=title, description=description, color=color)


async def fixture_card_file(record: dict[str, object]) -> discord.File:
    """Render a fixture/result image as a Discord attachment."""

    completed = str(record.get("status")) == "completed"
    logo_a, logo_b = await asyncio.gather(
        fetch_team_logo(str(record.get("team1_name", "Team A"))),
        fetch_team_logo(str(record.get("team2_name", "Team B"))),
    )
    image = render_fixture_card(
        str(record.get("team1_name", "Team A")),
        str(record.get("team2_name", "Team B")),
        int(record.get("timestamp", 0)),
        int(record["score1"]) if completed and record.get("score1") is not None else None,
        int(record["score2"]) if completed and record.get("score2") is not None else None,
        logo_a,
        logo_b,
    )
    return discord.File(image, filename="fixture-card.png")


async def post_fixture_card(
    channel: discord.TextChannel,
    record: dict[str, object],
    *,
    update_existing: bool = False,
) -> discord.Message:
    """Post or update a fixture card and add the two supporter reactions."""

    file = await fixture_card_file(record)
    embed = fixture_embed(record)
    message: discord.Message | None = None
    if update_existing and record.get("message_id"):
        try:
            message = await channel.fetch_message(int(record["message_id"]))
            await message.edit(embed=embed, attachments=[file])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None
    if message is None:
        message = await channel.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
        record["message_id"] = str(message.id)
    record["channel_id"] = str(channel.id)
    if str(record.get("status", "scheduled")) == "scheduled":
        for emoji in (FIXTURE_REACTION_A, FIXTURE_REACTION_B):
            try:
                await message.add_reaction(emoji)
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Could not add fixture reaction %s to message %s", emoji, message.id)
    else:
        try:
            await message.clear_reactions()
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.info("Could not clear fixture reactions on message %s", message.id)
    return message


def member_can_be_moderated(interaction: discord.Interaction, member: discord.Member) -> bool:
    """Return whether the bot can act on a member under Discord's hierarchy rules."""

    guild_me = interaction.guild.me if interaction.guild else None
    if guild_me is None:
        return False
    return member != guild_me and member < guild_me


def role_can_be_managed(interaction: discord.Interaction, role: discord.Role) -> bool:
    """Return whether the bot can add or remove a role."""

    guild_me = interaction.guild.me if interaction.guild else None
    return guild_me is not None and not role.managed and role < guild_me.top_role


def load_approval_requests() -> None:
    """Load saved requests without preventing the bot from starting."""

    if not APPROVAL_DATA_PATH.exists():
        return
    try:
        saved = json.loads(APPROVAL_DATA_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            approval_requests.update(
                {
                    str(message_id): record
                    for message_id, record in saved.items()
                    if isinstance(record, dict)
                }
            )
        LOGGER.info("Loaded %d saved approval request(s)", len(approval_requests))
    except (OSError, json.JSONDecodeError) as error:
        LOGGER.warning("Could not load approval requests: %s", error)


def save_approval_requests() -> None:
    """Persist request state atomically so button votes survive restarts."""

    APPROVAL_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = APPROVAL_DATA_PATH.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(approval_requests, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary_path.replace(APPROVAL_DATA_PATH)


def load_moderation_history() -> None:
    """Load the moderation audit trail without blocking bot startup."""

    if not HISTORY_DATA_PATH.exists():
        return
    try:
        saved = json.loads(HISTORY_DATA_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            moderation_history.update(
                {
                    str(key): entries
                    for key, entries in saved.items()
                    if isinstance(entries, list)
                    and all(isinstance(entry, dict) for entry in entries)
                }
            )
        LOGGER.info("Loaded moderation history for %d player(s)", len(moderation_history))
    except (OSError, json.JSONDecodeError) as error:
        LOGGER.warning("Could not load moderation history: %s", error)


def save_moderation_history() -> None:
    """Persist moderation records atomically."""

    HISTORY_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = HISTORY_DATA_PATH.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(moderation_history, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary_path.replace(HISTORY_DATA_PATH)


async def record_moderation_action(
    interaction: discord.Interaction,
    target_id: int,
    action: str,
    reason: str,
    details: str = "",
) -> str | None:
    """Append a successful moderation action to the server-scoped history."""

    if interaction.guild is None:
        return None
    key = f"{interaction.guild.id}:{target_id}"
    case_id = str(interaction.id)
    record = {
        "case_id": case_id,
        "action": action,
        "reason": reason,
        "details": details,
        "moderator_id": str(interaction.user.id),
        "at": discord.utils.utcnow().isoformat(),
    }
    async with get_approval_lock():
        moderation_history.setdefault(key, []).append(record)
        save_moderation_history()
    await send_configured_log(
        interaction.guild,
        "punishment_logs_channel_id",
        discord.Embed(
            title=f"Moderation action · {action.title()}",
            description=f"Target: <@{target_id}>\nReason: {reason}",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        ).set_footer(text=f"Moderator: {interaction.user}"),
    )
    return case_id


def approval_quorum() -> int:
    """Read the matching-vote threshold, defaulting to two moderators."""

    raw_value = os.getenv("DISCORD_APPROVAL_QUORUM", str(DEFAULT_APPROVAL_QUORUM)).strip()
    try:
        return max(1, min(20, int(raw_value)))
    except ValueError:
        LOGGER.warning("DISCORD_APPROVAL_QUORUM must be a number; using %d", DEFAULT_APPROVAL_QUORUM)
        return DEFAULT_APPROVAL_QUORUM


def approval_embed(record: dict[str, object]) -> discord.Embed:
    """Build the review message shown to moderators."""

    approve_votes = record.get("approve_votes", [])
    deny_votes = record.get("deny_votes", [])
    status = str(record.get("status", "pending"))
    status_label = {
        "pending": "Pending review",
        "approved": "Approved",
        "denied": "Denied",
    }.get(status, "Pending review")
    embed = discord.Embed(
        title="Permission request",
        description=str(record.get("description", "")),
        color=discord.Color.green() if status == "approved" else discord.Color.red() if status == "denied" else discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Proposed creation", value=str(record.get("title", "")), inline=False)
    embed.add_field(name="Requested by", value=f"<@{record.get('requester_id', '')}>")
    embed.add_field(
        name="Vote",
        value=f"{status_label} · {len(approve_votes)} approve / {len(deny_votes)} deny\n"
        f"Needs {approval_quorum()} matching vote(s) to close.",
        inline=False,
    )
    embed.set_footer(text=f"Request ID: {record.get('request_id', 'unknown')}")
    return embed


def is_approval_reviewer(interaction: discord.Interaction) -> bool:
    """Limit approval votes to moderators and administrators."""

    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    permissions = member.guild_permissions
    return permissions.administrator or permissions.manage_guild or permissions.manage_messages


async def handle_approval_vote(interaction: discord.Interaction, decision: str) -> None:
    """Record a moderator vote and update the approval message."""

    if not is_approval_reviewer(interaction):
        await interaction.response.send_message(
            "Only moderators with server-management or message-management permissions can vote.",
            ephemeral=True,
        )
        return
    if interaction.message is None:
        await interaction.response.send_message("This approval request is no longer available.", ephemeral=True)
        return

    message_id = str(interaction.message.id)
    async with get_approval_lock():
        record = approval_requests.get(message_id)
        if record is None:
            await interaction.response.send_message(
                "This approval request was created before request tracking was enabled.",
                ephemeral=True,
            )
            return

        if record.get("status") != "pending":
            await interaction.response.send_message(
                f"This request is already {record.get('status')}.",
                ephemeral=True,
            )
            return

        voter_id = str(interaction.user.id)
        approve_votes = record.setdefault("approve_votes", [])
        deny_votes = record.setdefault("deny_votes", [])
        if not isinstance(approve_votes, list) or not isinstance(deny_votes, list):
            await interaction.response.send_message("This approval request has invalid vote data.", ephemeral=True)
            return
        if voter_id in approve_votes or voter_id in deny_votes:
            await interaction.response.send_message(
                "Your vote is already recorded for this request.",
                ephemeral=True,
            )
            return

        if decision == "approved":
            approve_votes.append(voter_id)
        else:
            deny_votes.append(voter_id)

        if len(approve_votes) >= approval_quorum():
            record["status"] = "approved"
        elif len(deny_votes) >= approval_quorum():
            record["status"] = "denied"
        save_approval_requests()

        await interaction.response.defer()
        await interaction.message.edit(
            embed=approval_embed(record),
            view=ApprovalView(disabled=record.get("status") != "pending"),
        )
        await interaction.followup.send(
            f"Vote recorded: **{decision}**. "
            f"Current result: **{record.get('status', 'pending')}**.",
            ephemeral=True,
        )


class ApprovalView(discord.ui.View):
    """Persistent buttons for approval messages."""

    def __init__(self, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = disabled

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="league_mod:approval:approve",
    )
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await handle_approval_vote(interaction, "approved")

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        custom_id="league_mod:approval:deny",
    )
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await handle_approval_vote(interaction, "denied")


def prediction_embed(record: dict[str, object]) -> discord.Embed:
    """Build the spectator prediction message."""

    team_a = str(record.get("team_a", "Team A"))
    team_b = str(record.get("team_b", "Team B"))
    votes_a = record.get("votes_a", [])
    votes_b = record.get("votes_b", [])
    status = str(record.get("status", "open"))
    closes_at_raw = str(record.get("closes_at", ""))
    try:
        closes_at = datetime.datetime.fromisoformat(closes_at_raw)
        closes_label = discord.utils.format_dt(closes_at, style="R")
    except ValueError:
        closes_label = "manually"

    if status == "open":
        title = "Match prediction"
        color = discord.Color.blurple()
        status_label = f"Voting open · closes {closes_label}"
    else:
        title = "Match prediction · closed"
        color = discord.Color.dark_grey()
        status_label = "Voting closed"

    embed = discord.Embed(
        title=title,
        description=f"Who will win: **{team_a}** or **{team_b}**?\n{status_label}",
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name=team_a, value=f"{len(votes_a)} vote(s)", inline=True)
    embed.add_field(name=team_b, value=f"{len(votes_b)} vote(s)", inline=True)
    embed.set_footer(text="Each spectator gets one vote and can change it while voting is open.")
    return embed


class PredictionView(discord.ui.View):
    """Persistent buttons for spectator match predictions."""

    def __init__(self, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = disabled

    @discord.ui.button(
        label="Vote Team A",
        style=discord.ButtonStyle.primary,
        custom_id="league_mod:prediction:team_a",
    )
    async def vote_team_a(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await handle_prediction_vote(interaction, "votes_a")

    @discord.ui.button(
        label="Vote Team B",
        style=discord.ButtonStyle.secondary,
        custom_id="league_mod:prediction:team_b",
    )
    async def vote_team_b(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await handle_prediction_vote(interaction, "votes_b")


class VerifyView(discord.ui.View):
    """Persistent verification button for a configured verification channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify",
        style=discord.ButtonStyle.success,
        custom_id="league_mod:verify",
    )
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Verification is only available in a server.", ephemeral=True)
            return
        config = get_guild_data(interaction.guild.id)["config"]
        role_id = config.get("verify_role_id") if isinstance(config, dict) else None
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        if role is None:
            await interaction.response.send_message("Verification is not configured yet.", ephemeral=True)
            return
        if not role_can_be_managed_for_guild(interaction.guild, role):
            await interaction.response.send_message(
                "I cannot assign the configured verification role. Move my bot role above it.",
                ephemeral=True,
            )
            return
        if role in interaction.user.roles:
            await interaction.response.send_message("You are already verified.", ephemeral=True)
            return
        await interaction.user.add_roles(role, reason="Self-service verification")
        await interaction.response.send_message("You are verified.", ephemeral=True)


class RulebookSelect(discord.ui.Select):
    """Persistent menu for choosing one of the two server rulebooks."""

    def __init__(self) -> None:
        super().__init__(
            placeholder="Select a rulebook...",
            min_values=1,
            max_values=1,
            custom_id="league_mod:rules:select",
            options=[
                discord.SelectOption(label="Rulebook 1", value="rulebook_1"),
                discord.SelectOption(label="Rulebook 2", value="rulebook_2"),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Rulebooks are only available inside a server.",
                ephemeral=True,
            )
            return
        config = get_guild_data(interaction.guild.id)["config"]
        books = config.get("rulebooks", {}) if isinstance(config, dict) else {}
        book_id = self.values[0]
        book = books.get(book_id) if isinstance(books, dict) else None
        if not isinstance(book, dict) or not str(book.get("text", "")).strip():
            await interaction.response.send_message(
                f"This rulebook has not been filled in yet. A server manager can set it with `/rulebook-set`.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title=str(book.get("title") or ("Rulebook 1" if book_id == "rulebook_1" else "Rulebook 2")),
            description=str(book.get("text", ""))[:4096],
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RulebookView(discord.ui.View):
    """Persistent rulebook selection menu."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(RulebookSelect())


class TicketPanelView(discord.ui.View):
    """Persistent ticket creation button."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create ticket",
        style=discord.ButtonStyle.primary,
        custom_id="league_mod:ticket:create",
    )
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Tickets are only available in a server.", ephemeral=True)
            return
        setup_channel = interaction.channel
        if not isinstance(setup_channel, discord.TextChannel):
            await interaction.response.send_message("This ticket panel is not in a text channel.", ephemeral=True)
            return
        existing = discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel)
            and ticket_owner_id(channel) == interaction.user.id,
            interaction.guild.text_channels,
        )
        if existing is not None:
            await interaction.response.send_message(
                f"You already have an open ticket: {existing.mention}",
                ephemeral=True,
            )
            return

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message("I cannot identify my server member yet.", ephemeral=True)
            return
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
            ),
        }
        await interaction.response.defer(ephemeral=True)
        ticket = await interaction.guild.create_text_channel(
            f"ticket-{interaction.user.name}"[:100],
            category=setup_channel.category,
            overwrites=overwrites,
            topic=f"ticket-owner:{interaction.user.id}",
            reason="Ticket created from panel",
        )
        await ticket.send(
            f"{interaction.user.mention}, support will be with you shortly.\n"
            "Use `/close` when this ticket is resolved.",
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        await interaction.followup.send(f"Your ticket is ready: {ticket.mention}", ephemeral=True)


class AuctionBidModal(discord.ui.Modal, title="Place an auction bid"):
    bid = discord.ui.TextInput(
        label="Bid amount",
        placeholder="Enter a whole-number bid",
        min_length=1,
        max_length=12,
    )

    def __init__(self, message_id: int) -> None:
        super().__init__()
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This auction is no longer available.", ephemeral=True)
            return
        record = auction_for_message(interaction.guild, self.message_id)
        if record is None or record.get("status") != "open":
            await interaction.response.send_message("This auction is closed.", ephemeral=True)
            return
        try:
            amount = int(str(self.bid.value).replace(",", "").strip())
        except ValueError:
            await interaction.response.send_message("Enter a whole-number bid.", ephemeral=True)
            return
        current_bid = int(record.get("current_bid", record.get("starting_bid", 0)))
        if amount <= current_bid:
            await interaction.response.send_message(
                f"Your bid must be higher than {currency(current_bid)}.",
                ephemeral=True,
            )
            return
        record["current_bid"] = amount
        record["current_bidder_id"] = str(interaction.user.id)
        bids = record.setdefault("bids", [])
        if isinstance(bids, list):
            bids.append({"user_id": str(interaction.user.id), "amount": amount, "at": discord.utils.utcnow().isoformat()})
        save_league_state()
        await interaction.response.send_message(f"Bid accepted at **{currency(amount)}**.", ephemeral=True)
        if isinstance(interaction.channel, discord.TextChannel):
            try:
                message = await interaction.channel.fetch_message(self.message_id)
                await message.edit(embed=auction_embed(record), view=AuctionView())
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.info("Could not update auction bid message %s", self.message_id)


class AuctionView(discord.ui.View):
    """Persistent button for placing auction bids."""

    def __init__(self, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = disabled

    @discord.ui.button(
        label="Place bid",
        style=discord.ButtonStyle.success,
        custom_id="league_mod:auction:bid",
    )
    async def place_bid(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or interaction.message is None:
            await interaction.response.send_message("This auction is no longer available.", ephemeral=True)
            return
        record = auction_for_message(interaction.guild, interaction.message.id)
        if record is None or record.get("status") != "open":
            await interaction.response.send_message("This auction is closed.", ephemeral=True)
            return
        await interaction.response.send_modal(AuctionBidModal(interaction.message.id))


def auction_embed(record: dict[str, object]) -> discord.Embed:
    current_bid = int(record.get("current_bid", record.get("starting_bid", 0)))
    bidder_id = record.get("current_bidder_id")
    status = str(record.get("status", "open"))
    description = (
        f"Player: **{record.get('player_name', 'Unknown player')}**\n"
        f"Current bid: **{currency(current_bid)}**\n"
        f"Status: **{status.title()}**"
    )
    if bidder_id:
        description += f"\nLeading bidder: <@{bidder_id}>"
    if status == "open" and record.get("ends_at"):
        try:
            ends_at = datetime.datetime.fromisoformat(str(record["ends_at"]))
            description += f"\nEnds: {discord.utils.format_dt(ends_at, style='R')}"
        except ValueError:
            pass
    return discord.Embed(
        title="Player auction",
        description=description,
        color=discord.Color.gold() if status == "open" else discord.Color.dark_grey(),
    )


def schedule_open_auction_expirations() -> None:
    """Restore auction timers after a restart."""

    guilds = league_state.get("guilds", {})
    if not isinstance(guilds, dict):
        return
    for guild_id, data in guilds.items():
        if not isinstance(data, dict) or not isinstance(data.get("auctions"), dict):
            continue
        for player_id, record in data["auctions"].items():
            if isinstance(record, dict) and record.get("status") == "open":
                schedule_auction_expiration(str(guild_id), str(player_id), record)


def schedule_auction_expiration(guild_id: str, player_id: str, record: dict[str, object]) -> None:
    key = f"{guild_id}:{player_id}"
    existing_task = auction_expiration_tasks.get(key)
    if existing_task is not None and not existing_task.done():
        return
    try:
        ends_at = datetime.datetime.fromisoformat(str(record["ends_at"]))
    except (KeyError, ValueError):
        return
    auction_expiration_tasks[key] = asyncio.create_task(
        expire_auction(guild_id, player_id, max(0, (ends_at - discord.utils.utcnow()).total_seconds()))
    )


async def expire_auction(guild_id: str, player_id: str, delay: float) -> None:
    await asyncio.sleep(delay)
    data = get_guild_data(int(guild_id))
    auctions = data["auctions"]
    record = auctions.get(player_id) if isinstance(auctions, dict) else None
    if not isinstance(record, dict) or record.get("status") != "open":
        return
    record["status"] = "ended"
    save_league_state()
    guild = bot.get_guild(int(guild_id))
    channel_id = record.get("channel_id")
    message_id = record.get("message_id")
    channel = guild.get_channel(int(channel_id)) if guild and channel_id else None
    if isinstance(channel, discord.TextChannel) and message_id:
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=auction_embed(record), view=AuctionView(disabled=True))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.info("Could not update ended auction %s", player_id)


async def update_auction_message(guild: discord.Guild, record: dict[str, object]) -> None:
    channel_id = record.get("channel_id")
    message_id = record.get("message_id")
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if not isinstance(channel, discord.TextChannel) or not message_id:
        return
    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(
            embed=auction_embed(record),
            view=AuctionView(disabled=record.get("status") != "open"),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        LOGGER.info("Could not update auction message %s", message_id)


def schedule_prediction_expiration(message_id: str, record: dict[str, object]) -> None:
    """Schedule a saved prediction to close at its configured time."""

    existing_task = prediction_expiration_tasks.get(message_id)
    if existing_task is not None and not existing_task.done():
        return
    try:
        closes_at = datetime.datetime.fromisoformat(str(record["closes_at"]))
    except (KeyError, ValueError):
        return
    prediction_expiration_tasks[message_id] = asyncio.create_task(
        expire_prediction(message_id, max(0, (closes_at - discord.utils.utcnow()).total_seconds()))
    )


def schedule_open_prediction_expirations() -> None:
    """Restore timers for open predictions after a restart."""

    for message_id, record in prediction_requests().items():
        if record.get("status") == "open":
            schedule_prediction_expiration(message_id, record)


def prediction_requests() -> dict[str, dict[str, object]]:
    """Return only prediction records from the shared persisted store."""

    return {
        message_id: record
        for message_id, record in approval_requests.items()
        if record.get("kind") == "prediction"
    }


async def expire_prediction(message_id: str, delay: float) -> None:
    """Close a prediction and disable its buttons when its timer expires."""

    await asyncio.sleep(delay)
    async with get_approval_lock():
        record = approval_requests.get(message_id)
        if record is None or record.get("status") != "open":
            return
        record["status"] = "closed"
        save_approval_requests()

    channel_id = record.get("channel_id")
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(embed=prediction_embed(record), view=PredictionView(disabled=True))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        LOGGER.info("Could not update expired prediction message %s", message_id)


async def handle_prediction_vote(interaction: discord.Interaction, vote_field: str) -> None:
    """Record or change a spectator's prediction."""

    if interaction.message is None:
        await interaction.response.send_message("This prediction is no longer available.", ephemeral=True)
        return

    message_id = str(interaction.message.id)
    async with get_approval_lock():
        record = approval_requests.get(message_id)
        if record is None or record.get("kind") != "prediction":
            await interaction.response.send_message(
                "This prediction was created before prediction tracking was enabled.",
                ephemeral=True,
            )
            return

        if record.get("status") != "open":
            await interaction.response.send_message("Voting for this match is closed.", ephemeral=True)
            return

        try:
            closes_at = datetime.datetime.fromisoformat(str(record["closes_at"]))
        except (KeyError, ValueError):
            closes_at = discord.utils.utcnow()
        if discord.utils.utcnow() >= closes_at:
            record["status"] = "closed"
            save_approval_requests()
            await interaction.response.send_message("Voting for this match is closed.", ephemeral=True)
            await interaction.message.edit(embed=prediction_embed(record), view=PredictionView(disabled=True))
            return

        votes_a = record.setdefault("votes_a", [])
        votes_b = record.setdefault("votes_b", [])
        if not isinstance(votes_a, list) or not isinstance(votes_b, list):
            await interaction.response.send_message("This prediction has invalid vote data.", ephemeral=True)
            return

        voter_id = str(interaction.user.id)
        votes_a[:] = [voter for v in votes_a if voter != voter_id]
        votes_b[:] = [voter for voter in votes_b if voter != voter_id]
        selected_votes = votes_a if vote_field == "votes_a" else votes_b
        selected_votes.append(voter_id)
        save_approval_requests()

        chosen_team = str(record.get("team_a" if vote_field == "votes_a" else "team_b", "selected team"))
        await interaction.response.defer()
        await interaction.message.edit(embed=prediction_embed(record), view=PredictionView())
        await interaction.followup.send(f"Your prediction is recorded for **{chosen_team}**.", ephemeral=True)


def get_approval_lock() -> asyncio.Lock:
    """Create the event-loop-local lock lazily after startup."""

    global approval_lock
    if approval_lock is None:
        approval_lock = asyncio.Lock()
    return approval_lock


async def find_approval_channel(interaction: discord.Interaction) -> discord.TextChannel | None:
    """Use a configured approval channel, falling back to the current channel."""

    configured_id = os.getenv("DISCORD_APPROVAL_CHANNEL_ID", "").strip()
    if configured_id:
        try:
            channel_id = int(configured_id)
        except ValueError:
            LOGGER.warning("DISCORD_APPROVAL_CHANNEL_ID must be a numeric channel ID")
            return None
        configured_channel = interaction.guild.get_channel(channel_id)
        if isinstance(configured_channel, discord.TextChannel):
            return configured_channel
        try:
            fetched_channel = await interaction.guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched_channel if isinstance(fetched_channel, discord.TextChannel) else None

    return interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None


def parse_user_id(value: str) -> int | None:
    """Accept either a numeric Discord ID or a user mention."""

    match = re.fullmatch(r"<@!?(\d+)>", value.strip())
    candidate = match.group(1) if match else value.strip()
    return int(candidate) if candidate.isdigit() else None


def history_embeds(
    user_label: str,
    records: list[dict[str, str]],
) -> list[discord.Embed]:
    """Split a complete history into Discord-safe ephemeral embeds."""

    if not records:
        return [
            discord.Embed(
                title=f"Moderation history · {user_label}",
                description="No warnings, timeouts, or kicks are recorded for this player.",
                color=discord.Color.dark_grey(),
            )
        ]

    pages: list[discord.Embed] = []
    newest_first = list(reversed(records))
    for offset in range(0, len(newest_first), 20):
        page_records = newest_first[offset : offset + 20]
        page_number = offset // 20 + 1
        total_pages = (len(newest_first) + 19) // 20
        embed = discord.Embed(
            title=f"Moderation history · {user_label}",
            description=f"{len(records)} total record(s)",
            color=discord.Color.blurple(),
        )
        for record in page_records:
            action = str(record.get("action", "action")).title()
            reason = str(record.get("reason", "No reason provided"))
            details = str(record.get("details", ""))
            try:
                when = datetime.datetime.fromisoformat(str(record.get("at", "")))
                when_label = discord.utils.format_dt(when, style="f")
            except ValueError:
                when_label = "Unknown time"
            value = (
                f"**Case:** `{record.get('case_id', 'unknown')}`\n"
                f"**Moderator:** <@{record.get('moderator_id', 'unknown')}>\n"
            )
            if details:
                value += f"**Details:** {details}\n"
            value += f"**Reason:** {reason}"
            embed.add_field(name=f"{action} · {when_label}", value=value[:1024], inline=False)
        embed.set_footer(text=f"Page {page_number} of {total_pages}")
        pages.append(embed)
    return pages


@bot.tree.command(name="coinflip", description="Flip a coin")
async def coinflip(interaction: discord.Interaction) -> None:
    """Return a random heads or tails result."""

    result = secrets.choice(("Heads", "Tails"))
    await interaction.response.send_message(f"**{result}**")


@bot.tree.command(name="predict", description="Open a voting pool for an upcoming match")
@app_commands.describe(
    team_a="First team or player",
    team_b="Second team or player",
    duration_minutes="How long voting stays open (5 minutes to 7 days)",
)
async def predict_match(
    interaction: discord.Interaction,
    team_a: app_commands.Range[str, 1, MAX_MATCH_TEAM_LENGTH],
    team_b: app_commands.Range[str, 1, MAX_MATCH_TEAM_LENGTH],
    duration_minutes: app_commands.Range[int, 5, MAX_PREDICTION_MINUTES] = DEFAULT_PREDICTION_MINUTES,
) -> None:
    """Open a spectator prediction pool in the current text channel."""

    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "Predictions are only available in text channels.",
            ephemeral=True,
        )
        return
    if team_a.casefold() == team_b.casefold():
        await interaction.response.send_message(
            "Please provide two different teams or players.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    closes_at = discord.utils.utcnow() + datetime.timedelta(minutes=duration_minutes)
    record: dict[str, object] = {
        "kind": "prediction",
        "team_a": team_a,
        "team_b": team_b,
        "channel_id": str(channel.id),
        "status": "open",
        "closes_at": closes_at.isoformat(),
        "votes_a": [],
        "votes_b": [],
    }
    message = await channel.send(
        embed=prediction_embed(record),
        view=PredictionView(),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    record["message_id"] = str(message.id)
    approval_requests[str(message.id)] = record
    try:
        save_approval_requests()
    except OSError:
        await message.delete()
        LOGGER.exception("Could not save prediction message %s", message.id)
        await interaction.followup.send("I couldn't save that prediction. Please try again.", ephemeral=True)
        return

    schedule_prediction_expiration(str(message.id), record)
    await interaction.followup.send(
        f"Prediction opened for **{team_a}** vs **{team_b}**: {message.jump_url}",
        ephemeral=True,
    )


@bot.tree.command(name="history", description="Show a player's warning, timeout, and kick history")
@app_commands.describe(user="The player's Discord user ID or mention")
@app_commands.checks.has_permissions(manage_messages=True)
async def history(interaction: discord.Interaction, user: str) -> None:
    """Show the complete stored moderation history to staff."""

    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return
    user_id = parse_user_id(user)
    if user_id is None:
        await interaction.response.send_message(
            "Enter a numeric Discord user ID or a user mention.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        target = await bot.fetch_user(user_id)
        user_label = f"{target} · {user_id}"
    except (discord.NotFound, discord.HTTPException):
        user_label = str(user_id)

    records = moderation_history.get(f"{interaction.guild.id}:{user_id}", [])
    pages = history_embeds(user_label, records)
    await interaction.followup.send(embeds=pages[:10], ephemeral=True)
    for page in pages[10:]:
        await interaction.followup.send(embed=page, ephemeral=True)


@bot.tree.command(name="case", description="Show a moderation case by case ID")
@app_commands.describe(case_id="The case ID shown in moderation history")
@app_commands.checks.has_permissions(manage_messages=True)
async def case(interaction: discord.Interaction, case_id: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    guild_prefix = f"{interaction.guild.id}:"
    match: dict[str, str] | None = None
    for key, records in moderation_history.items():
        if not key.startswith(guild_prefix):
            continue
        for record in records:
            if record.get("case_id") == case_id.strip():
                match = record
                break
        if match is not None:
            break
    if match is None:
        await interaction.response.send_message("That case was not found.", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"Moderation case · {case_id}",
        description=f"Action: **{str(match.get('action', 'Unknown')).title()}**",
        color=discord.Color.orange(),
        timestamp=datetime.datetime.fromisoformat(match["at"]),
    )
    target_id = next(
        (key.split(":", 1)[1] for key, records in moderation_history.items() if match in records and key.startswith(guild_prefix)),
        "unknown",
    )
    embed.add_field(name="Target", value=f"<@{target_id}>")
    embed.add_field(name="Moderator", value=f"<@{match.get('moderator_id', 'unknown')}>")
    embed.add_field(name="Reason", value=match.get("reason", DEFAULT_REASON), inline=False)
    if match.get("details"):
        embed.add_field(name="Details", value=match["details"], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="team-create", description="Create a league team")
@app_commands.describe(
    name="Team name",
    role="Role assigned to signed players",
    manager="Optional team manager",
    channel="Optional team text channel",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def team_create(
    interaction: discord.Interaction,
    name: app_commands.Range[str, 1, 80],
    role: discord.Role,
    manager: discord.Member | None = None,
    channel: discord.TextChannel | None = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    key = team_key(name)
    data = get_guild_data(interaction.guild.id)
    teams = data["teams"]
    if not isinstance(teams, dict):
        await interaction.response.send_message("League storage is unavailable.", ephemeral=True)
        return
    if key in teams:
        await interaction.response.send_message("A team with that name already exists.", ephemeral=True)
        return
    if not role_can_be_managed_for_guild(interaction.guild, role):
        await interaction.response.send_message(
            "I cannot use that role. It must be unmanaged and below my highest role.",
            ephemeral=True,
        )
        return
    teams[key] = {
        "name": name.strip(),
        "role_id": str(role.id),
        "manager_id": str(manager.id) if manager else "",
        "channel_id": str(channel.id) if channel else "",
        "budget": 0,
        "players": [],
    }
    standings_record(data, key)
    save_league_state()
    await interaction.response.send_message(f"Team **{name.strip()}** created.")


make_group = app_commands.Group(name="make", description="Assign league team roles")


@make_group.command(name="manager", description="Assign a manager to a saved team")
@app_commands.describe(
    team="Saved league team",
    member="Server member to assign as manager",
)
@app_commands.autocomplete(team=saved_team_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def make_manager(
    interaction: discord.Interaction,
    team: str,
    member: discord.Member,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    found = find_team(interaction.guild, team)
    if found is None:
        await interaction.response.send_message("That team does not exist.", ephemeral=True)
        return

    key, record = found
    record["manager_id"] = str(member.id)
    save_league_state()
    team_name = str(record.get("name", key))
    await interaction.response.send_message(
        f"{member.mention} is now manager of **{team_name}**.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(make_group)


@bot.tree.command(name="team-info", description="Show team details")
@app_commands.describe(team="Team name")
async def team_info(interaction: discord.Interaction, team: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    found = find_team(interaction.guild, team)
    if found is None:
        await interaction.response.send_message("That team does not exist.", ephemeral=True)
        return
    key, record = found
    manager_id = record.get("manager_id")
    channel_id = record.get("channel_id")
    embed = discord.Embed(title=str(record.get("name", key)), color=discord.Color.blue())
    embed.add_field(name="Manager", value=f"<@{manager_id}>" if manager_id else "Not assigned")
    embed.add_field(name="Budget", value=currency(int(record.get("budget", 0))))
    embed.add_field(name="Roster", value=format_team_roster(interaction.guild, record), inline=False)
    if channel_id:
        embed.add_field(name="Team channel", value=f"<#{channel_id}>")
    await interaction.response.send_message(embed=embed)


transfer_group = app_commands.Group(name="transfer", description="Manage player transfers and transaction news")


@transfer_group.command(name="news", description="Choose where loan and transfer news is posted")
@app_commands.describe(channel="Text channel for official transaction announcements")
@app_commands.checks.has_permissions(manage_guild=True)
async def transfer_news(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    bot_member = interaction.guild.me
    if bot_member is None:
        await interaction.response.send_message("I cannot verify channel permissions right now.", ephemeral=True)
        return
    permissions = channel.permissions_for(bot_member)
    if not permissions.send_messages or not permissions.embed_links or not permissions.attach_files:
        await interaction.response.send_message(
            "I need permission to send messages, embeds, and logo files in that channel.",
            ephemeral=True,
        )
        return

    config = get_guild_data(interaction.guild.id)["config"]
    if not isinstance(config, dict):
        await interaction.response.send_message("Server configuration is unavailable.", ephemeral=True)
        return
    config["transfer_news_channel_id"] = str(channel.id)
    save_league_state()
    await interaction.response.send_message(
        f"Loan and transfer announcements will be posted in {channel.mention}.",
        ephemeral=True,
    )


@transfer_group.command(name="player", description="Transfer a signed player to another team")
@app_commands.describe(team="Destination team", player="Player to transfer")
@app_commands.autocomplete(team=saved_team_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def transfer_player(
    interaction: discord.Interaction,
    team: str,
    player: discord.Member,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    news_channel = get_transfer_news_channel(interaction.guild)
    if news_channel is None:
        await interaction.response.send_message(
            "Set the transaction channel first with `/transfer news`.",
            ephemeral=True,
        )
        return
    destination = find_team(interaction.guild, team)
    if destination is None:
        await interaction.response.send_message("That destination team does not exist.", ephemeral=True)
        return

    destination_key, destination_record = destination
    data = get_guild_data(interaction.guild.id)
    teams = data.get("teams")
    players = data.get("players")
    player_id = str(player.id)
    player_record = players.get(player_id) if isinstance(players, dict) else None
    if not isinstance(teams, dict) or not isinstance(player_record, dict):
        await interaction.response.send_message("That player is not on a saved team roster.", ephemeral=True)
        return
    if isinstance(player_record.get("loan"), dict):
        await interaction.response.send_message(
            "A loaned player must return to their parent team before being transferred.",
            ephemeral=True,
        )
        return

    source_key = str(player_record.get("team_key", ""))
    source_record = teams.get(source_key)
    source_roster = source_record.get("players") if isinstance(source_record, dict) else None
    destination_roster = destination_record.get("players")
    if (
        not isinstance(source_record, dict)
        or not isinstance(source_roster, list)
        or player_id not in source_roster
        or not isinstance(destination_roster, list)
    ):
        await interaction.response.send_message("That player is not on a saved team roster.", ephemeral=True)
        return
    if source_key == destination_key:
        await interaction.response.send_message("The player is already on that team.", ephemeral=True)
        return
    try:
        transfer_fee = int(player_record.get("market_value", player_record.get("price", 0)))
        source_budget = int(source_record.get("budget", 0))
        destination_budget = int(destination_record.get("budget", 0))
    except (TypeError, ValueError):
        await interaction.response.send_message("The player or team has invalid financial data.", ephemeral=True)
        return
    if transfer_fee < 0:
        await interaction.response.send_message("The saved market value cannot be negative.", ephemeral=True)
        return
    if destination_budget < transfer_fee:
        await interaction.response.send_message(
            f"**{destination_record.get('name', team)}** needs **{currency(transfer_fee - destination_budget)}** "
            "more funds to complete this transfer.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    source_roster.remove(player_id)
    if player_id not in destination_roster:
        destination_roster.append(player_id)
    source_record["budget"] = source_budget + transfer_fee
    destination_record["budget"] = destination_budget - transfer_fee
    destination_name = str(destination_record.get("name", team))
    source_name = str(source_record.get("name", source_key))
    player_record["team_key"] = destination_key
    player_record["team_name"] = destination_name
    player_record["price"] = transfer_fee
    save_league_state()
    await update_player_team_role(
        interaction.guild,
        player,
        source_record,
        destination_record,
        f"Transferred to {destination_name}",
    )

    embed = discord.Embed(
        title="🚨 OFFICIAL COMMUNICATION",
        description=f"DEAL DONE. {player.mention} has moved from **{source_name}** to **{destination_name}**.",
        color=discord.Color.green(),
    )
    embed.add_field(name="Position", value=str(player_record.get("position", "ST")), inline=True)
    embed.add_field(name="Rating", value=f"⭐ {get_player_ovr(interaction.guild.id, player_id)} OVR", inline=True)
    embed.add_field(name="Total Cost", value=f"💰 {currency(transfer_fee)}", inline=False)
    posted = await post_transfer_news(interaction.guild, embed, destination_name)
    if posted:
        response = f"Transfer complete: {player.mention} joined **{destination_name}** for **{currency(transfer_fee)}**."
    else:
        response = (
            f"The transfer was saved, but I could not post to {news_channel.mention}. "
            "Check that I still have permission to post embeds there."
        )
    await interaction.followup.send(
        response,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(transfer_group)


@bot.tree.command(name="loan", description="Loan a player to another team")
@app_commands.describe(
    team="Borrowing team",
    player="Player to loan",
    matches="Number of the borrowing team's results before the loan returns",
)
@app_commands.autocomplete(team=saved_team_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def loan_player(
    interaction: discord.Interaction,
    team: str,
    player: discord.Member,
    matches: app_commands.Range[int, 1, 100] = 1,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    news_channel = get_transfer_news_channel(interaction.guild)
    if news_channel is None:
        await interaction.response.send_message(
            "Set the transaction channel first with `/transfer news`.",
            ephemeral=True,
        )
        return
    destination = find_team(interaction.guild, team)
    if destination is None:
        await interaction.response.send_message("That borrowing team does not exist.", ephemeral=True)
        return
    destination_key, destination_record = destination
    data = get_guild_data(interaction.guild.id)
    teams = data.get("teams")
    players = data.get("players")
    player_id = str(player.id)
    player_record = players.get(player_id) if isinstance(players, dict) else None
    if not isinstance(teams, dict) or not isinstance(player_record, dict):
        await interaction.response.send_message("That player is not on a saved team roster.", ephemeral=True)
        return
    if isinstance(player_record.get("loan"), dict):
        await interaction.response.send_message("That player is already on loan.", ephemeral=True)
        return

    source_key = str(player_record.get("team_key", ""))
    source_record = teams.get(source_key)
    source_roster = source_record.get("players") if isinstance(source_record, dict) else None
    destination_roster = destination_record.get("players")
    if (
        not isinstance(source_record, dict)
        or not isinstance(source_roster, list)
        or player_id not in source_roster
        or not isinstance(destination_roster, list)
    ):
        await interaction.response.send_message("That player is not on a saved team roster.", ephemeral=True)
        return
    if source_key == destination_key:
        await interaction.response.send_message("A player cannot be loaned to their current team.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    source_roster.remove(player_id)
    if player_id not in destination_roster:
        destination_roster.append(player_id)
    source_name = str(source_record.get("name", source_key))
    destination_name = str(destination_record.get("name", team))
    player_record["loan"] = {
        "from_team_key": source_key,
        "from_team_name": source_name,
        "matches_remaining": matches,
    }
    player_record["team_key"] = destination_key
    player_record["team_name"] = destination_name
    save_league_state()
    await update_player_team_role(
        interaction.guild,
        player,
        source_record,
        destination_record,
        f"Loaned to {destination_name}",
    )

    duration = f"{matches} Match" if matches == 1 else f"{matches} Matches"
    embed = discord.Embed(
        title="🚨 OFFICIAL LOAN",
        description=f"{player.mention} joins **{destination_name}** on loan.",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Duration", value=f"⏱️ {duration}", inline=True)
    embed.add_field(name="Position", value=str(player_record.get("position", "ST")), inline=True)
    embed.add_field(name="Rating", value=f"⭐ {get_player_ovr(interaction.guild.id, player_id)} OVR", inline=True)
    posted = await post_transfer_news(interaction.guild, embed, destination_name)
    if posted:
        response = f"{player.mention} joined **{destination_name}** on loan for **{duration}**."
    else:
        response = (
            f"The loan was saved, but I could not post to {news_channel.mention}. "
            "Check that I still have permission to post embeds there."
        )
    await interaction.followup.send(
        response,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="loan-return", description="Return a player to their parent team")
@app_commands.describe(player="Player to return from loan")
@app_commands.checks.has_permissions(manage_guild=True)
async def loan_return(interaction: discord.Interaction, player: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    news_channel = get_transfer_news_channel(interaction.guild)
    if news_channel is None:
        await interaction.response.send_message(
            "Set the transaction channel first with `/transfer news`.",
            ephemeral=True,
        )
        return
    data = get_guild_data(interaction.guild.id)
    teams = data.get("teams")
    players = data.get("players")
    player_id = str(player.id)
    player_record = players.get(player_id) if isinstance(players, dict) else None
    loan = player_record.get("loan") if isinstance(player_record, dict) else None
    if not isinstance(teams, dict) or not isinstance(player_record, dict) or not isinstance(loan, dict):
        await interaction.response.send_message("That player does not have an active loan.", ephemeral=True)
        return

    current_key = str(player_record.get("team_key", ""))
    source_key = str(loan.get("from_team_key", ""))
    current_record = teams.get(current_key)
    source_record = teams.get(source_key)
    current_roster = current_record.get("players") if isinstance(current_record, dict) else None
    source_roster = source_record.get("players") if isinstance(source_record, dict) else None
    if (
        not isinstance(current_record, dict)
        or not isinstance(source_record, dict)
        or not isinstance(current_roster, list)
        or not isinstance(source_roster, list)
    ):
        await interaction.response.send_message("The loan's saved team rosters are unavailable.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    if player_id in current_roster:
        current_roster.remove(player_id)
    if player_id not in source_roster:
        source_roster.append(player_id)
    source_name = str(source_record.get("name", source_key))
    current_name = str(current_record.get("name", current_key))
    player_record["team_key"] = source_key
    player_record["team_name"] = source_name
    player_record.pop("loan", None)
    save_league_state()
    await update_player_team_role(
        interaction.guild,
        player,
        current_record,
        source_record,
        f"Loan returned to {source_name}",
    )

    embed = build_loan_return_embed(player_id, player.display_name, source_name)
    posted = await post_transfer_news(interaction.guild, embed, source_name)
    if posted:
        response = f"{player.mention} returned from **{current_name}** to **{source_name}**."
    else:
        response = (
            f"The return was saved, but I could not post to {news_channel.mention}. "
            "Check that I still have permission to post embeds there."
        )
    await interaction.followup.send(
        response,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="rules", description="Choose and privately view a rulebook")
async def rules(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Rulebooks are only available inside a server.", ephemeral=True)
        return
    embed = discord.Embed(
        title="Continental Elite League Hub",
        description="Select a rulebook below to view its contents.",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=RulebookView())


@bot.tree.command(name="rulebook-set", description="Set the text for one of the two rulebooks")
@app_commands.describe(
    book="Choose which rulebook to update",
    text="Full rulebook text shown to members",
    title="Optional title shown when members open it",
)
@app_commands.choices(
    book=[
        app_commands.Choice(name="Rulebook 1", value="rulebook_1"),
        app_commands.Choice(name="Rulebook 2", value="rulebook_2"),
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
async def rulebook_set(
    interaction: discord.Interaction,
    book: app_commands.Choice[str],
    text: app_commands.Range[str, 1, 4000],
    title: app_commands.Range[str, 1, 80] | None = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    content = text.strip()
    if not content:
        await interaction.response.send_message("Rulebook text cannot be blank.", ephemeral=True)
        return
    config = get_guild_data(interaction.guild.id)["config"]
    if not isinstance(config, dict):
        await interaction.response.send_message("Server configuration is unavailable.", ephemeral=True)
        return
    books = config.get("rulebooks")
    if not isinstance(books, dict):
        books = {}
        config["rulebooks"] = books
    existing = books.get(book.value)
    default_title = "Rulebook 1" if book.value == "rulebook_1" else "Rulebook 2"
    books[book.value] = {
        "title": title.strip() if title and title.strip() else (
            existing.get("title", default_title) if isinstance(existing, dict) else default_title
        ),
        "text": content,
    }
    save_league_state()
    await interaction.response.send_message(
        f"Updated **{books[book.value]['title']}**. Run `/rules` to view it.",
        ephemeral=True,
    )


@bot.tree.command(name="team-budget", description="Set or adjust a team's budget")
@app_commands.describe(team="Team name", amount="Whole-number budget amount", mode="Set, add, or subtract")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Set", value="set"),
        app_commands.Choice(name="Add", value="add"),
        app_commands.Choice(name="Subtract", value="subtract"),
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
async def team_budget(
    interaction: discord.Interaction,
    team: str,
    amount: app_commands.Range[int, 0, 2_000_000_000],
    mode: app_commands.Choice[str],
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    found = find_team(interaction.guild, team)
    if found is None:
        await interaction.response.send_message("That team does not exist.", ephemeral=True)
        return
    key, record = found
    current = int(record.get("budget", 0))
    new_amount = amount if mode.value == "set" else current + amount if mode.value == "add" else current - amount
    if new_amount < 0:
        await interaction.response.send_message("A team's budget cannot become negative.", ephemeral=True)
        return
    record["budget"] = new_amount
    save_league_state()
    await interaction.response.send_message(f"**{record.get('name', key)}** budget: **{currency(new_amount)}**.")


@bot.tree.command(name="sign", description="Sign a player to a team")
@app_commands.describe(player="Player to sign", team="Team name", price="Transfer price")
@app_commands.checks.has_permissions(manage_guild=True)
async def sign_player(
    interaction: discord.Interaction,
    player: discord.Member,
    team: str,
    price: app_commands.Range[int, 0, 2_000_000_000],
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    found = find_team(interaction.guild, team)
    if found is None:
        await interaction.response.send_message("That team does not exist.", ephemeral=True)
        return
    key, team_record = found
    data = get_guild_data(interaction.guild.id)
    players = data["players"]
    if not isinstance(players, dict) or not isinstance(team_record.get("players"), list):
        await interaction.response.send_message("League storage is unavailable.", ephemeral=True)
        return
    player_id = str(player.id)
    existing = players.get(player_id)
    if isinstance(existing, dict) and existing.get("team_key"):
        await interaction.response.send_message(
            f"{player.mention} is already signed to **{existing.get('team_name', 'another team')}**.",
            ephemeral=True,
        )
        return
    budget = int(team_record.get("budget", 0))
    if price > budget:
        await interaction.response.send_message(
            f"That team only has **{currency(budget)}** available.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    team_record["budget"] = budget - price
    team_record["players"].append(player_id)
    players[player_id] = {
        "id": player_id,
        "name": player.display_name,
        "team_key": key,
        "team_name": team_record.get("name", team),
        "price": price,
        "ovr": DEFAULT_PLAYER_OVR,
        "position": "ST",
        "market_value": price,
        "weekly_stats": None,
    }
    role_id = team_record.get("role_id")
    team_role = interaction.guild.get_role(int(role_id)) if role_id else None
    if team_role is not None:
        try:
            await player.add_roles(team_role, reason=f"Signed to {team_record.get('name', team)}")
        except discord.HTTPException:
            LOGGER.warning("Could not assign team role to player %s", player.id)
    save_league_state()
    await interaction.followup.send(
        f"{player.mention} signed to **{team_record.get('name', team)}** for **{currency(price)}** "
        f"(starting OVR **{DEFAULT_PLAYER_OVR}**)."
    )


@bot.tree.command(name="roster", description="Privately show a team's funds, manager, and player ratings")
@app_commands.describe(team="Team name")
@app_commands.autocomplete(team=saved_team_autocomplete)
async def roster(interaction: discord.Interaction, team: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    found = find_team(interaction.guild, team)
    if found is None:
        await interaction.response.send_message("That team does not exist.", ephemeral=True)
        return
    _, record = found
    manager_id = str(record.get("manager_id", ""))
    embed = discord.Embed(
        title=f"{record.get('name', team)} roster",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="Manager",
        value=f"<@{manager_id}>" if manager_id else "Not assigned",
        inline=True,
    )
    embed.add_field(
        name="Funds",
        value=currency(int(record.get("budget", 0))),
        inline=True,
    )
    embed.add_field(
        name="Players · OVR · signing value · position",
        value=format_team_roster(interaction.guild, record)[:1024],
        inline=False,
    )
    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="release", description="Release a player from a team")
@app_commands.describe(
    player="Player to release",
    team="Current team name",
    refund_percent="Percentage of the signing price returned to the team",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def release_player(
    interaction: discord.Interaction,
    player: discord.Member,
    team: str,
    refund_percent: app_commands.Range[int, 0, 100] = 100,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    found = find_team(interaction.guild, team)
    if found is None:
        await interaction.response.send_message("That team does not exist.", ephemeral=True)
        return
    key, team_record = found
    data = get_guild_data(interaction.guild.id)
    players = data["players"]
    player_record = players.get(str(player.id)) if isinstance(players, dict) else None
    roster_ids = team_record.get("players")
    if (
        not isinstance(player_record, dict)
        or player_record.get("team_key") != key
        or not isinstance(roster_ids, list)
        or str(player.id) not in roster_ids
    ):
        await interaction.response.send_message("That player is not signed to this team.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    price = int(player_record.get("price", 0))
    refund = price * refund_percent // 100
    roster_ids.remove(str(player.id))
    player_record["team_key"] = ""
    player_record["team_name"] = ""
    team_record["budget"] = int(team_record.get("budget", 0)) + refund
    role_id = team_record.get("role_id")
    team_role = interaction.guild.get_role(int(role_id)) if role_id else None
    if team_role is not None and team_role in player.roles:
        try:
            await player.remove_roles(team_role, reason=f"Released from {team_record.get('name', team)}")
        except discord.HTTPException:
            LOGGER.warning("Could not remove team role from player %s", player.id)
    save_league_state()
    await interaction.followup.send(
        f"{player.mention} released from **{team_record.get('name', team)}**. "
        f"Refunded **{currency(refund)}** ({refund_percent}%)."
    )


@bot.tree.command(name="table", description="Show the league standings")
async def table(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    data = get_guild_data(interaction.guild.id)
    teams = data["teams"]
    if not isinstance(teams, dict) or not teams:
        await interaction.response.send_message("No teams have been created yet.", ephemeral=True)
        return
    rows: list[tuple[str, dict[str, int], dict[str, object]]] = []
    for key, team_record in teams.items():
        if isinstance(team_record, dict):
            rows.append((key, standings_record(data, key), team_record))
    rows.sort(key=lambda row: (row[1]["points"], row[1]["wins"], row[1]["goals_for"] - row[1]["goals_against"]), reverse=True)
    lines = ["`#  Team                 P  W  D  L  GF GA  Pts`"]
    for index, (_, standing, team_record) in enumerate(rows, 1):
        name = str(team_record.get("name", "Team"))[:19]
        lines.append(
            f"`{index:<2} {name:<19} {standing['played']:>2} {standing['wins']:>2} "
            f"{standing['draws']:>2} {standing['losses']:>2} {standing['goals_for']:>2} "
            f"{standing['goals_against']:>2} {standing['points']:>3}`"
        )
    await interaction.response.send_message(
        embed=discord.Embed(title=f"{interaction.guild.name} league table", description="\n".join(lines))
    )


auction_group = app_commands.Group(name="auction", description="Manage player auctions")


@auction_group.command(name="start", description="Start an auction for a player")
@app_commands.describe(player="Player being auctioned", starting_bid="Opening bid", duration="Duration in minutes")
@app_commands.checks.has_permissions(manage_guild=True)
async def auction_start(
    interaction: discord.Interaction,
    player: discord.Member,
    starting_bid: app_commands.Range[int, 0, 2_000_000_000],
    duration: app_commands.Range[int, 1, 1440],
) -> None:
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Start auctions in a server text channel.", ephemeral=True)
        return
    data = get_guild_data(interaction.guild.id)
    auctions = data["auctions"]
    if not isinstance(auctions, dict):
        await interaction.response.send_message("Auction storage is unavailable.", ephemeral=True)
        return
    player_id = str(player.id)
    existing = auctions.get(player_id)
    if isinstance(existing, dict) and existing.get("status") == "open":
        await interaction.response.send_message("That player already has an active auction.", ephemeral=True)
        return
    ends_at = discord.utils.utcnow() + datetime.timedelta(minutes=duration)
    record: dict[str, object] = {
        "player_id": player_id,
        "player_name": player.display_name,
        "channel_id": str(interaction.channel.id),
        "message_id": "",
        "starting_bid": starting_bid,
        "current_bid": starting_bid,
        "current_bidder_id": "",
        "ends_at": ends_at.isoformat(),
        "status": "open",
        "bids": [],
    }
    await interaction.response.defer(ephemeral=True)
    message = await interaction.channel.send(embed=auction_embed(record), view=AuctionView())
    record["message_id"] = str(message.id)
    auctions[player_id] = record
    try:
        save_league_state()
    except OSError:
        await message.delete()
        LOGGER.exception("Could not save auction for player %s", player.id)
        await interaction.followup.send("I couldn't save that auction.", ephemeral=True)
        return
    schedule_auction_expiration(str(interaction.guild.id), player_id, record)
    await interaction.followup.send(f"Auction started: {message.jump_url}", ephemeral=True)


@auction_group.command(name="cancel", description="Cancel a player's active auction")
@app_commands.describe(player="Player whose auction should be cancelled")
@app_commands.checks.has_permissions(manage_guild=True)
async def auction_cancel(interaction: discord.Interaction, player: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    data = get_guild_data(interaction.guild.id)
    auctions = data["auctions"]
    record = auctions.get(str(player.id)) if isinstance(auctions, dict) else None
    if not isinstance(record, dict) or record.get("status") != "open":
        await interaction.response.send_message("That player has no active auction.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    record["status"] = "cancelled"
    save_league_state()
    await update_auction_message(interaction.guild, record)
    await interaction.followup.send(f"Auction for **{player.display_name}** cancelled.")


@auction_group.command(name="status", description="Show an auction's current status")
@app_commands.describe(player="Player whose auction should be shown")
async def auction_status(interaction: discord.Interaction, player: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    data = get_guild_data(interaction.guild.id)
    auctions = data["auctions"]
    record = auctions.get(str(player.id)) if isinstance(auctions, dict) else None
    if not isinstance(record, dict):
        await interaction.response.send_message("No auction record exists for that player.", ephemeral=True)
        return
    await interaction.response.send_message(embed=auction_embed(record), ephemeral=True)


@auction_group.command(name="end", description="End an active auction")
@app_commands.describe(player="Player whose auction should end")
@app_commands.checks.has_permissions(manage_guild=True)
async def auction_end(interaction: discord.Interaction, player: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    data = get_guild_data(interaction.guild.id)
    auctions = data["auctions"]
    record = auctions.get(str(player.id)) if isinstance(auctions, dict) else None
    if not isinstance(record, dict) or record.get("status") != "open":
        await interaction.response.send_message("That player has no active auction.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    record["status"] = "ended"
    save_league_state()
    await update_auction_message(interaction.guild, record)
    bidder = record.get("current_bidder_id")
    winner = f"<@{bidder}>" if bidder else "no winning bidder"
    await interaction.followup.send(
        f"Auction for **{player.display_name}** ended at **{currency(int(record.get('current_bid', 0)))}** "
        f"with {winner}."
    )


bot.tree.add_command(auction_group)


SCRIM_REQUIRED_PLAYERS = 8
SCRIM_REQUIRED_GOALKEEPERS = 2
SCRIM_SIGNUP_MINUTES = 10


def find_scrim_record(scrim_id: str) -> dict[str, object] | None:
    guilds = league_state.get("guilds", {})
    if not isinstance(guilds, dict):
        return None
    for guild_data in guilds.values():
        if not isinstance(guild_data, dict):
            continue
        scrims = guild_data.get("scrims", [])
        if not isinstance(scrims, list):
            continue
        for record in scrims:
            if isinstance(record, dict) and str(record.get("id")) == scrim_id:
                return record
    return None


def scrim_embed(record: dict[str, object]) -> discord.Embed:
    players = record.get("players", [])
    goalkeepers = record.get("goalkeepers", [])
    players = players if isinstance(players, list) else []
    goalkeepers = goalkeepers if isinstance(goalkeepers, list) else []
    status = str(record.get("status", "open"))
    if status == "concluded":
        embed = discord.Embed(
            title="Scrim Concluded",
            description="The current scrim session has ended. A new one will be hosted soon!",
            color=discord.Color.green(),
        )
    else:
        end_timestamp = int(record.get("ends_at", 0))
        embed = discord.Embed(
            title="Scrim Signups",
            description=(
                f"Choose a role below. Signups close <t:{end_timestamp}:R>.\n"
                f"Need **{SCRIM_REQUIRED_PLAYERS} players** and "
                f"**{SCRIM_REQUIRED_GOALKEEPERS} goalkeepers** to start. "
                "Signups stay open for the full 10 minutes."
            ),
            color=discord.Color.blurple(),
        )
    player_list = "\n".join(f"<@{player_id}>" for player_id in players) or "No signups yet"
    goalkeeper_list = "\n".join(f"<@{player_id}>" for player_id in goalkeepers) or "No signups yet"
    embed.add_field(
        name=f"Players · {len(players)}/{SCRIM_REQUIRED_PLAYERS}",
        value=player_list[:1024],
        inline=True,
    )
    embed.add_field(
        name=f"Goalkeepers · {len(goalkeepers)}/{SCRIM_REQUIRED_GOALKEEPERS}",
        value=goalkeeper_list[:1024],
        inline=True,
    )
    if status == "concluded":
        if record.get("outcome") == "ready":
            lineup = record.get("lineup", {})
            if isinstance(lineup, dict):
                for color in ("blue", "red"):
                    side = lineup.get(color, {})
                    if not isinstance(side, dict):
                        continue
                    keeper_id = side.get("goalkeeper")
                    keeper_ovr = side.get("goalkeeper_ovr", DEFAULT_PLAYER_OVR)
                    player_ovrs = side.get("player_ovrs", {})
                    if not isinstance(player_ovrs, dict):
                        player_ovrs = {}
                    side_players = side.get("players", [])
                    if not isinstance(side_players, list):
                        side_players = []
                    member_lines = [
                        f"<@{player_id}> · OVR {player_ovrs.get(str(player_id), DEFAULT_PLAYER_OVR)}"
                        for player_id in side_players
                    ]
                    value = f"Goalkeeper: <@{keeper_id}> · OVR {keeper_ovr}\n" if keeper_id else ""
                    value += "\n".join(member_lines) or "No players"
                    embed.add_field(name=f"{color.title()} team", value=value[:1024], inline=True)
        else:
            embed.add_field(
                name="Result",
                value="Scrim Has Failed. Not Enough",
                inline=False,
            )
    return embed


def scrim_has_required_roster(record: dict[str, object]) -> bool:
    players = record.get("players", [])
    goalkeepers = record.get("goalkeepers", [])
    return (
        isinstance(players, list)
        and len({str(user_id) for user_id in players}) >= SCRIM_REQUIRED_PLAYERS
        and isinstance(goalkeepers, list)
        and len({str(user_id) for user_id in goalkeepers}) >= SCRIM_REQUIRED_GOALKEEPERS
    )


def balanced_scrim_sides(
    guild: discord.Guild,
    user_ids: list[object],
    *,
    include_goalkeeper: bool = False,
) -> tuple[list[str], list[str]]:
    """Split the highest-rated players greedily, alternating ties across two sides."""

    unique_ids = list(dict.fromkeys(str(user_id) for user_id in user_ids))

    def rating(user_id: str) -> int:
        return get_player_ovr(guild.id, user_id)

    ordered = sorted(unique_ids, key=lambda user_id: (-rating(user_id), user_id))
    blue: list[str] = []
    red: list[str] = []
    blue_strength = 0
    red_strength = 0
    tie_to_blue = True
    for user_id in ordered:
        member_rating = rating(user_id)
        if blue_strength < red_strength:
            blue.append(user_id)
            blue_strength += member_rating
        elif red_strength < blue_strength:
            red.append(user_id)
            red_strength += member_rating
        elif len(blue) < len(red):
            blue.append(user_id)
            blue_strength += member_rating
        elif len(red) < len(blue):
            red.append(user_id)
            red_strength += member_rating
        elif tie_to_blue:
            blue.append(user_id)
            blue_strength += member_rating
            tie_to_blue = False
        else:
            red.append(user_id)
            red_strength += member_rating
            tie_to_blue = True
    if include_goalkeeper and len(unique_ids) >= 2:
        return blue[:1], red[:1]
    return blue[:SCRIM_REQUIRED_PLAYERS // 2], red[:SCRIM_REQUIRED_PLAYERS // 2]


def build_scrim_lineup(guild: discord.Guild, record: dict[str, object]) -> dict[str, object]:
    players = record.get("players", [])
    goalkeepers = record.get("goalkeepers", [])
    players = players if isinstance(players, list) else []
    goalkeepers = goalkeepers if isinstance(goalkeepers, list) else []
    blue_players, red_players = balanced_scrim_sides(guild, players)
    blue_keeper, red_keeper = balanced_scrim_sides(guild, goalkeepers, include_goalkeeper=True)

    def side_record(keeper_ids: list[str], player_ids: list[str]) -> dict[str, object]:
        keeper_id = keeper_ids[0] if keeper_ids else ""
        return {
            "goalkeeper": keeper_id,
            "goalkeeper_ovr": get_player_ovr(guild.id, keeper_id) if keeper_id else DEFAULT_PLAYER_OVR,
            "players": player_ids,
            "player_ovrs": {
                player_id: get_player_ovr(guild.id, player_id)
                for player_id in player_ids
            },
        }

    return {
        "blue": side_record(blue_keeper, blue_players),
        "red": side_record(red_keeper, red_players),
    }


def scrim_display_name(guild: discord.Guild, user_id: str) -> str:
    try:
        member = guild.get_member(int(user_id))
    except ValueError:
        member = None
    if member is not None:
        return member.display_name
    players = get_guild_data(guild.id).get("players", {})
    record = players.get(user_id) if isinstance(players, dict) else None
    if isinstance(record, dict) and record.get("name"):
        return str(record["name"])
    return f"<@{user_id}>"


async def notify_scrim_organizer(
    guild: discord.Guild,
    record: dict[str, object],
) -> None:
    organizer_id = str(record.get("organizer_id", ""))
    try:
        organizer = bot.get_user(int(organizer_id))
        if organizer is None:
            organizer = await bot.fetch_user(int(organizer_id))
        if record.get("outcome") != "ready":
            await organizer.send("Scrim Has Failed. Not Enough")
            return
        lineup = record.get("lineup", {})
        if not isinstance(lineup, dict):
            lineup = {}
        lines = ["Scrim concluded with enough signups. Here are the balanced teams:"]
        for color in ("blue", "red"):
            side = lineup.get(color, {})
            if not isinstance(side, dict):
                continue
            keeper_id = str(side.get("goalkeeper", ""))
            keeper_ovr = side.get("goalkeeper_ovr", DEFAULT_PLAYER_OVR)
            player_ids = side.get("players", [])
            if not isinstance(player_ids, list):
                player_ids = []
            player_ovrs = side.get("player_ovrs", {})
            if not isinstance(player_ovrs, dict):
                player_ovrs = {}
            lines.append(f"\n**{color.title()} team**")
            lines.append(f"Goalkeeper: {scrim_display_name(guild, keeper_id)} · OVR {keeper_ovr}")
            lines.extend(
                f"Player: {scrim_display_name(guild, str(user_id))} · "
                f"OVR {player_ovrs.get(str(user_id), DEFAULT_PLAYER_OVR)}"
                for user_id in player_ids
            )
        await organizer.send(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (ValueError, discord.Forbidden, discord.HTTPException):
        LOGGER.warning("Could not DM organizer for scrim %s", record.get("id"))


async def conclude_scrim_signup(guild_id: str, scrim_id: str) -> str | None:
    record = find_scrim_record(scrim_id)
    if record is None or record.get("status") != "open":
        return None
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        LOGGER.warning("Could not find guild %s to conclude scrim %s", guild_id, scrim_id)
        return None

    if scrim_has_required_roster(record):
        record["outcome"] = "ready"
        record["lineup"] = build_scrim_lineup(guild, record)
    else:
        record["outcome"] = "failed"
        record.pop("lineup", None)
    record["status"] = "concluded"
    record["completed_at"] = int(discord.utils.utcnow().timestamp())
    save_league_state()

    try:
        channel = guild.get_channel(int(record.get("channel_id", "")))
        if isinstance(channel, discord.TextChannel):
            message = await channel.fetch_message(int(record.get("message_id", "")))
            await message.edit(
                embed=scrim_embed(record),
                view=ScrimSignupView(scrim_id, disabled=True),
            )
    except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
        LOGGER.warning("Could not update concluded scrim post %s", scrim_id)

    await notify_scrim_organizer(guild, record)
    return str(record.get("outcome", "failed"))


async def expire_scrim_signup(guild_id: str, scrim_id: str, ends_at: int) -> None:
    try:
        await asyncio.sleep(max(0, ends_at - int(discord.utils.utcnow().timestamp())))
        record = find_scrim_record(scrim_id)
        if record is None or record.get("status") != "open":
            return
        await conclude_scrim_signup(guild_id, scrim_id)
    except asyncio.CancelledError:
        raise
    finally:
        task = scrim_expiration_tasks.get(scrim_id)
        if task is asyncio.current_task():
            scrim_expiration_tasks.pop(scrim_id, None)


def schedule_scrim_expiration(guild_id: str, record: dict[str, object]) -> None:
    scrim_id = str(record.get("id", ""))
    if not scrim_id or record.get("status") != "open":
        return
    current = scrim_expiration_tasks.get(scrim_id)
    if current is not None and not current.done():
        return
    ends_at = int(record.get("ends_at", 0))
    scrim_expiration_tasks[scrim_id] = asyncio.create_task(
        expire_scrim_signup(guild_id, scrim_id, ends_at)
    )


def schedule_open_scrim_expirations() -> None:
    guilds = league_state.get("guilds", {})
    if not isinstance(guilds, dict):
        return
    for guild_id, guild_data in guilds.items():
        if not isinstance(guild_data, dict):
            continue
        scrims = guild_data.get("scrims", [])
        if not isinstance(scrims, list):
            continue
        for record in scrims:
            if isinstance(record, dict):
                schedule_scrim_expiration(str(guild_id), record)


class ScrimSignupView(discord.ui.View):
    """Persistent player/goalkeeper signup controls for one scrim session."""

    def __init__(self, scrim_id: str, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.scrim_id = scrim_id
        for label, role, style in (
            ("Join as Player", "players", discord.ButtonStyle.primary),
            ("Join as Goalkeeper", "goalkeepers", discord.ButtonStyle.success),
        ):
            button = discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"fel:scrim:{scrim_id}:{role}",
                disabled=disabled,
            )

            async def callback(
                interaction: discord.Interaction,
                signup_role: str = role,
            ) -> None:
                await self.register(interaction, signup_role)

            button.callback = callback
            self.add_item(button)

        leave_button = discord.ui.Button(
            label="Leave Scrim",
            style=discord.ButtonStyle.secondary,
            custom_id=f"fel:scrim:{scrim_id}:leave",
            disabled=disabled,
        )

        async def leave_callback(interaction: discord.Interaction) -> None:
            await self.register(interaction, None)

        leave_button.callback = leave_callback
        self.add_item(leave_button)

    async def register(self, interaction: discord.Interaction, role: str | None) -> None:
        record = find_scrim_record(self.scrim_id)
        if record is None or record.get("status") != "open":
            await interaction.response.send_message("This scrim signup is closed.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Join a scrim from its server.", ephemeral=True)
            return
        if getattr(interaction.user, "bot", False):
            await interaction.response.send_message("Bots cannot join scrim signups.", ephemeral=True)
            return

        players = record.setdefault("players", [])
        goalkeepers = record.setdefault("goalkeepers", [])
        if not isinstance(players, list) or not isinstance(goalkeepers, list):
            await interaction.response.send_message("Scrim signup data is unavailable.", ephemeral=True)
            return
        user_id = str(interaction.user.id)
        already_signed_up = any(str(member_id) == user_id for member_id in players + goalkeepers)
        if role is None and not already_signed_up:
            await interaction.response.send_message("You are not signed up for this scrim.", ephemeral=True)
            return
        if role == "players":
            is_already_player = any(str(member_id) == user_id for member_id in players)
            if not is_already_player and len(players) >= SCRIM_REQUIRED_PLAYERS:
                await interaction.response.send_message("All player slots are filled.", ephemeral=True)
                return
        elif role == "goalkeepers":
            is_already_goalkeeper = any(str(member_id) == user_id for member_id in goalkeepers)
            if not is_already_goalkeeper and len(goalkeepers) >= SCRIM_REQUIRED_GOALKEEPERS:
                await interaction.response.send_message("All goalkeeper slots are filled.", ephemeral=True)
                return

        original_players = players.copy()
        original_goalkeepers = goalkeepers.copy()
        players[:] = [member_id for member_id in players if str(member_id) != user_id]
        goalkeepers[:] = [member_id for member_id in goalkeepers if str(member_id) != user_id]
        if role == "players":
            players.append(user_id)
        elif role == "goalkeepers":
            goalkeepers.append(user_id)

        try:
            save_league_state()
        except OSError:
            players[:] = original_players
            goalkeepers[:] = original_goalkeepers
            await interaction.response.send_message("I couldn't save your scrim signup.", ephemeral=True)
            return

        await interaction.response.edit_message(
            embed=scrim_embed(record),
            view=self,
        )


scrim_group = app_commands.Group(name="scrim", description="Manage scrim signups, results, and codes")


@scrim_group.command(name="start", description="Open 10-minute player and goalkeeper signups")
async def scrim_start(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Run this command in a server text channel.", ephemeral=True)
        return
    data = get_guild_data(interaction.guild.id)
    saved_scrims = data["scrims"]
    if not isinstance(saved_scrims, list):
        await interaction.response.send_message("Scrim storage is unavailable.", ephemeral=True)
        return
    if any(isinstance(record, dict) and record.get("status") == "open" for record in saved_scrims):
        await interaction.response.send_message(
            "There is already an open scrim signup in this server.",
            ephemeral=True,
        )
        return
    now = int(discord.utils.utcnow().timestamp())
    record: dict[str, object] = {
        "id": str(interaction.id),
        "guild_id": str(interaction.guild.id),
        "organizer_id": str(interaction.user.id),
        "channel_id": str(interaction.channel.id),
        "message_id": "",
        "players": [],
        "goalkeepers": [],
        "created_at": now,
        "ends_at": now + SCRIM_SIGNUP_MINUTES * 60,
        "status": "open",
    }
    await interaction.response.defer(ephemeral=True)
    message: discord.Message | None = None
    try:
        message = await interaction.channel.send(
            embed=scrim_embed(record),
            view=ScrimSignupView(str(record["id"])),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        record["message_id"] = str(message.id)
        saved_scrims.append(record)
        save_league_state()
    except (OSError, discord.Forbidden, discord.HTTPException):
        if record in saved_scrims:
            saved_scrims.remove(record)
        if message is not None:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        LOGGER.exception("Could not create scrim signup")
        await interaction.followup.send("I couldn't create the scrim signup.", ephemeral=True)
        return
    schedule_scrim_expiration(str(interaction.guild.id), record)
    await interaction.followup.send(
        f"Scrim signups are open for **{SCRIM_SIGNUP_MINUTES} minutes**: {message.jump_url}",
        ephemeral=True,
    )


@scrim_group.command(name="end", description="Conclude the active scrim and send balanced teams to its organizer")
async def scrim_end(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    scrims = get_guild_data(interaction.guild.id)["scrims"]
    if not isinstance(scrims, list):
        await interaction.response.send_message("Scrim storage is unavailable.", ephemeral=True)
        return
    record = next(
        (
            candidate
            for candidate in reversed(scrims)
            if isinstance(candidate, dict) and candidate.get("status") == "open"
        ),
        None,
    )
    if record is None:
        await interaction.response.send_message("There is no active scrim signup to end.", ephemeral=True)
        return
    permissions = getattr(interaction.user, "guild_permissions", None)
    can_manage = bool(permissions and permissions.manage_guild)
    if str(record.get("organizer_id")) != str(interaction.user.id) and not can_manage:
        await interaction.response.send_message(
            "Only the scrim organizer or a server manager can end this signup.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    scrim_id = str(record.get("id", ""))
    outcome = await conclude_scrim_signup(str(interaction.guild.id), scrim_id)
    task = scrim_expiration_tasks.pop(scrim_id, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    if outcome == "ready":
        response = "Scrim concluded. I sent the organizer the balanced blue and red lineups."
    else:
        response = "Scrim concluded, but there were not enough players or goalkeepers."
    await interaction.followup.send(response, ephemeral=True)


@scrim_group.command(name="code", description="Post a scrim code in this channel")
@app_commands.describe(code="The code to share with the server")
async def scrim_code(interaction: discord.Interaction, code: str) -> None:
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Run this command in a server text channel.", ephemeral=True)
        return
    code = code.strip()
    if not code or len(code) > 500:
        await interaction.response.send_message(
            "Enter a scrim code with 1–500 characters.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"Scrim code: `{code}`",
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(scrim_group)


@bot.tree.command(name="fixtures", description="Create or post fixtures with supporter reactions")
@app_commands.describe(
    team1="Optional first team; leave blank to post all scheduled fixtures",
    team2="Optional second team",
    match_date="Optional Unix timestamp or ISO date/time in UTC",
)
@app_commands.autocomplete(team1=team_autocomplete, team2=team_autocomplete)
async def fixtures(
    interaction: discord.Interaction,
    team1: str | None = None,
    team2: str | None = None,
    match_date: str | None = None,
) -> None:
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Run this command in a server text channel.", ephemeral=True)
        return
    data = get_guild_data(interaction.guild.id)
    saved_fixtures = data["fixtures"]
    if not isinstance(saved_fixtures, list):
        await interaction.response.send_message("Fixture storage is unavailable.", ephemeral=True)
        return

    if (team1 is None) != (team2 is None):
        await interaction.response.send_message("Choose both team1 and team2, or leave both blank.", ephemeral=True)
        return

    if team1 is not None and team2 is not None:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "Only league staff can create a fixture with this command.",
                ephemeral=True,
            )
            return
        first = find_fixture_team(interaction.guild, team1)
        second = find_fixture_team(interaction.guild, team2)
        if first is None or second is None:
            await interaction.response.send_message(
                "Choose two stored teams or a recognized Ligue 1 club.",
                ephemeral=True,
            )
            return
        first_key, first_record = first
        second_key, second_record = second
        if first_key == second_key:
            await interaction.response.send_message("A team cannot play itself.", ephemeral=True)
            return
        timestamp = parse_match_timestamp(match_date, int(discord.utils.utcnow().timestamp()))
        if timestamp is None:
            await interaction.response.send_message(
                "Invalid date. Use a Unix timestamp or ISO format such as `2026-08-30T20:45:00Z`.",
                ephemeral=True,
            )
            return
        fixture = {
            "id": str(interaction.id),
            "team1": first_key,
            "team1_name": first_record.get("name", team1),
            "team2": second_key,
            "team2_name": second_record.get("name", team2),
            "timestamp": timestamp,
            "channel_id": str(interaction.channel.id),
            "status": "scheduled",
        }
        saved_fixtures.append(fixture)
        await interaction.response.defer(ephemeral=True)
        try:
            message = await post_fixture_card(interaction.channel, fixture)
            save_league_state()
            await interaction.followup.send(
                f"Fixture posted: **{first_record.get('name', team1)}** vs "
                f"**{second_record.get('name', team2)}** · {message.jump_url}",
                ephemeral=True,
            )
        except (discord.Forbidden, discord.HTTPException):
            save_league_state()
            LOGGER.exception("Could not post fixture card")
            await interaction.followup.send(
                "The fixture was saved, but I could not post its image card.",
                ephemeral=True,
            )
        return

    if match_date:
        await interaction.response.send_message(
            "A date can only be used when both teams are selected.",
            ephemeral=True,
        )
        return
    upcoming = [
        fixture
        for fixture in saved_fixtures
        if isinstance(fixture, dict) and fixture.get("status") == "scheduled"
    ]
    upcoming.sort(key=lambda fixture: int(fixture.get("timestamp", 0)))
    if not upcoming:
        await interaction.response.send_message("There are no upcoming fixtures.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    posted = 0
    for fixture in upcoming:
        try:
            await post_fixture_card(interaction.channel, fixture, update_existing=True)
            posted += 1
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not post fixture card for %s", fixture.get("id"))
    save_league_state()
    await interaction.followup.send(
        f"Posted **{posted}** fixture card(s). React with {FIXTURE_REACTION_A} or {FIXTURE_REACTION_B} to support a team.",
        ephemeral=True,
    )


@bot.tree.command(name="schedule", description="Schedule a match")
@app_commands.describe(team1="Home team", team2="Away team", unix_timestamp="Match time as a Unix timestamp")
@app_commands.autocomplete(team1=team_autocomplete, team2=team_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def schedule_match(
    interaction: discord.Interaction,
    team1: str,
    team2: str,
    unix_timestamp: app_commands.Range[int, 1, 4_102_444_800],
) -> None:
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Schedule matches in a server text channel.", ephemeral=True)
        return
    first = find_fixture_team(interaction.guild, team1)
    second = find_fixture_team(interaction.guild, team2)
    if first is None or second is None:
        await interaction.response.send_message(
            "Choose two stored teams or a recognized Ligue 1 club.",
            ephemeral=True,
        )
        return
    first_key, first_record = first
    second_key, second_record = second
    if first_key == second_key:
        await interaction.response.send_message("A team cannot play itself.", ephemeral=True)
        return
    data = get_guild_data(interaction.guild.id)
    fixtures = data["fixtures"]
    if not isinstance(fixtures, list):
        await interaction.response.send_message("Fixture storage is unavailable.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    fixture = {
        "id": str(interaction.id),
        "team1": first_key,
        "team1_name": first_record.get("name", team1),
        "team2": second_key,
        "team2_name": second_record.get("name", team2),
        "timestamp": unix_timestamp,
        "channel_id": str(interaction.channel.id),
        "status": "scheduled",
    }
    fixtures.append(fixture)
    save_league_state()
    try:
        message = await post_fixture_card(interaction.channel, fixture)
        save_league_state()
        response = (
            f"Fixture scheduled: **{first_record.get('name', team1)}** vs "
            f"**{second_record.get('name', team2)}** · {message.jump_url}"
        )
    except (discord.Forbidden, discord.HTTPException):
        LOGGER.exception("Could not post fixture card")
        response = (
            f"Fixture saved for **{first_record.get('name', team1)}** vs "
            f"**{second_record.get('name', team2)}**, but I could not post its image card."
        )
    await interaction.followup.send(response, ephemeral=True)


@bot.tree.command(name="result", description="Record a match result")
@app_commands.describe(
    team1="First team",
    team2="Second team",
    team1_score="First team's score",
    team2_score="Second team's score",
    match_date="Optional Unix timestamp or ISO date/time in UTC; scheduled date is used by default",
)
@app_commands.autocomplete(team1=team_autocomplete, team2=team_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def result_match(
    interaction: discord.Interaction,
    team1: str,
    team2: str,
    team1_score: app_commands.Range[int, 0, 999],
    team2_score: app_commands.Range[int, 0, 999],
    match_date: str | None = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    first = find_fixture_team(interaction.guild, team1)
    second = find_fixture_team(interaction.guild, team2)
    if first is None or second is None:
        await interaction.response.send_message(
            "Choose two stored teams or a recognized Ligue 1 club.",
            ephemeral=True,
        )
        return
    first_key, first_record = first
    second_key, second_record = second
    if first_key == second_key:
        await interaction.response.send_message("A team cannot play itself.", ephemeral=True)
        return
    data = get_guild_data(interaction.guild.id)
    fixtures = data["fixtures"]
    fixture: dict[str, object] | None = None
    if isinstance(fixtures, list):
        for candidate in reversed(fixtures):
            if (
                isinstance(candidate, dict)
                and candidate.get("status") == "scheduled"
                and candidate.get("team1") == first_key
                and candidate.get("team2") == second_key
            ):
                fixture = candidate
                break
    default_timestamp = (
        int(fixture.get("timestamp", int(discord.utils.utcnow().timestamp())))
        if fixture is not None
        else int(discord.utils.utcnow().timestamp())
    )
    result_timestamp = parse_match_timestamp(match_date, default_timestamp)
    if result_timestamp is None:
        await interaction.response.send_message(
            "Invalid date. Use a Unix timestamp or ISO format such as `2026-08-30T20:45:00Z`.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    if fixture is None:
        fixture = {
            "id": str(interaction.id),
            "team1": first_key,
            "team1_name": first_record.get("name", team1),
            "team2": second_key,
            "team2_name": second_record.get("name", team2),
            "timestamp": result_timestamp,
            "channel_id": str(interaction.channel.id) if interaction.channel else "",
            "status": "completed",
            "score1": team1_score,
            "score2": team2_score,
        }
        if isinstance(fixtures, list):
            fixtures.append(fixture)
    else:
        fixture["timestamp"] = result_timestamp
        fixture["status"] = "completed"
        fixture["score1"] = team1_score
        fixture["score2"] = team2_score
    first_standing = standings_record(data, first_key)
    second_standing = standings_record(data, second_key)
    first_standing["played"] += 1
    second_standing["played"] += 1
    first_standing["goals_for"] += team1_score
    first_standing["goals_against"] += team2_score
    second_standing["goals_for"] += team2_score
    second_standing["goals_against"] += team1_score
    if team1_score > team2_score:
        first_standing["wins"] += 1
        first_standing["points"] += 3
        second_standing["losses"] += 1
    elif team2_score > team1_score:
        second_standing["wins"] += 1
        second_standing["points"] += 3
        first_standing["losses"] += 1
    else:
        first_standing["draws"] += 1
        second_standing["draws"] += 1
        first_standing["points"] += 1
        second_standing["points"] += 1
    returned_loans = advance_loans_after_result(data, {first_key, second_key})
    save_league_state()
    await publish_loan_returns(interaction.guild, data, returned_loans)
    target_channel = interaction.channel
    saved_channel_id = fixture.get("channel_id")
    if saved_channel_id and interaction.guild is not None:
        stored_channel = interaction.guild.get_channel(int(saved_channel_id))
        if isinstance(stored_channel, discord.TextChannel):
            target_channel = stored_channel
    card_url = ""
    if isinstance(target_channel, discord.TextChannel):
        try:
            result_message = await post_fixture_card(target_channel, fixture, update_existing=True)
            card_url = f" {result_message.jump_url}"
            save_league_state()
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not post result card")
    await interaction.followup.send(
        f"Result recorded: **{first_record.get('name', team1)} {team1_score}–{team2_score} "
        f"{second_record.get('name', team2)}**.{card_url}"
        + (f" **{len(returned_loans)}** loan(s) returned automatically." if returned_loans else ""),
        ephemeral=True,
    )


async def set_player_ovr(
    interaction: discord.Interaction,
    player: discord.Member,
    new_ovr: int,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    players = get_guild_data(interaction.guild.id)["players"]
    record = players.get(str(player.id)) if isinstance(players, dict) else None
    if not isinstance(record, dict):
        await interaction.response.send_message("That player is not in the league database.", ephemeral=True)
        return
    old_ovr = record.get("ovr", DEFAULT_PLAYER_OVR)
    record["ovr"] = new_ovr
    save_league_state()
    await interaction.response.send_message(f"{player.mention} OVR changed from **{old_ovr}** to **{new_ovr}**.")


@bot.tree.command(name="ratings-adjust", description="Adjust a player's overall rating")
@app_commands.describe(player="Player to update", new_ovr="New overall rating")
@app_commands.checks.has_permissions(manage_guild=True)
async def ratings_adjust(
    interaction: discord.Interaction,
    player: discord.Member,
    new_ovr: app_commands.Range[int, 1, 99],
) -> None:
    await set_player_ovr(interaction, player, new_ovr)


@bot.tree.command(name="update-ovr", description="Set a league player's overall rating")
@app_commands.describe(player="Player to update", ovr="New overall rating (1–99)")
@app_commands.checks.has_permissions(manage_guild=True)
async def update_ovr(
    interaction: discord.Interaction,
    player: discord.Member,
    ovr: app_commands.Range[int, 1, 99],
) -> None:
    await set_player_ovr(interaction, player, ovr)


@bot.tree.command(name="check-ovr", description="Check your or another server member's overall rating")
@app_commands.describe(player="Player to check; leave blank to check yourself")
async def check_ovr(
    interaction: discord.Interaction,
    player: discord.Member | None = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    target = player or interaction.user
    players = get_guild_data(interaction.guild.id)["players"]
    record = players.get(str(target.id)) if isinstance(players, dict) else None
    ovr = get_player_ovr(interaction.guild.id, target.id)
    team_name = record.get("team_name") if isinstance(record, dict) else None
    team_note = f" on **{team_name}**" if team_name else ""
    starting_note = " (starting rating)" if not isinstance(record, dict) else ""
    await interaction.response.send_message(
        f"{target.mention}'s OVR is **{ovr}**{team_note}{starting_note}.",
        ephemeral=True,
    )


@bot.tree.command(name="market-value", description="Set a player's market value")
@app_commands.describe(player="Player to update", value="Whole-number market value")
@app_commands.checks.has_permissions(manage_guild=True)
async def market_value(
    interaction: discord.Interaction,
    player: discord.Member,
    value: app_commands.Range[int, 0, 2_000_000_000],
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    players = get_guild_data(interaction.guild.id)["players"]
    record = players.get(str(player.id)) if isinstance(players, dict) else None
    if not isinstance(record, dict):
        await interaction.response.send_message("That player is not in the league database.", ephemeral=True)
        return
    record["market_value"] = value
    save_league_state()
    await interaction.response.send_message(f"{player.mention} market value is now **{currency(value)}**.")


@bot.tree.command(name="submit-stats", description="Submit a player's weekly statistics")
@app_commands.describe(
    player="Player name",
    team="Team name",
    goals="Goals this week",
    assists="Assists this week",
    saves="Saves this week",
    won="Did the team win?",
)
@app_commands.checks.has_permissions(administrator=True)
async def submit_stats(
    interaction: discord.Interaction,
    player: app_commands.Range[str, 1, 100],
    team: app_commands.Range[str, 1, 80],
    goals: app_commands.Range[int, 0, 100],
    assists: app_commands.Range[int, 0, 100],
    saves: app_commands.Range[int, 0, 200],
    won: bool,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    found = find_team(interaction.guild, team)
    if found is None:
        await interaction.response.send_message("Team not found.", ephemeral=True)
        return
    team_key_value, team_record = found
    roster_ids = team_record.get("players")
    players = get_guild_data(interaction.guild.id)["players"]
    if not isinstance(roster_ids, list) or not isinstance(players, dict):
        await interaction.response.send_message("That team has no valid roster.", ephemeral=True)
        return
    player_name = player.strip().casefold()
    player_id = next(
        (
            str(candidate_id)
            for candidate_id in roster_ids
            if isinstance(players.get(str(candidate_id)), dict)
            and str(players[str(candidate_id)].get("name", "")).casefold() == player_name
        ),
        None,
    )
    record = players.get(player_id) if player_id else None
    if not isinstance(record, dict):
        await interaction.response.send_message("Player not found on that team.", ephemeral=True)
        return
    if record.get("team_key") != team_key_value:
        await interaction.response.send_message("Player not found on that team.", ephemeral=True)
        return
    record["weekly_stats"] = {"goals": goals, "assists": assists, "saves": saves, "won": won}
    save_league_state()
    await interaction.response.send_message(f"Stats saved for **{record.get('name', player)}**.")


TOTW_SLOTS = ("GK", "CB", "CB", "CM", "CAM", "LW", "RW", "ST")


def totw_score(stats: dict[str, object]) -> int:
    return (
        int(stats.get("goals", 0)) * 2
        + int(stats.get("assists", 0))
        + int(stats.get("saves", 0))
        + (3 if stats.get("won") else 0)
    )


@bot.tree.command(name="totw", description="Show Team of the Week from submitted weekly statistics")
@app_commands.checks.has_permissions(administrator=True)
async def totw(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    players = get_guild_data(interaction.guild.id)["players"]
    if not isinstance(players, dict):
        await interaction.response.send_message("No stats submitted yet. Use `/submit-stats` first.", ephemeral=True)
        return
    used: set[str] = set()
    lines: list[str] = []
    for position in TOTW_SLOTS:
        candidates = [
            (player_id, player)
            for player_id, player in players.items()
            if isinstance(player, dict)
            and player.get("position") == position
            and isinstance(player.get("weekly_stats"), dict)
            and player_id not in used
        ]
        candidates.sort(key=lambda item: totw_score(item[1]["weekly_stats"]), reverse=True)
        if not candidates:
            continue
        player_id, player = candidates[0]
        used.add(player_id)
        stats = player["weekly_stats"]
        lines.append(
            f"**{position}** <@{player_id}> ({player.get('team_name') or 'Free agent'}) — "
            f"{stats.get('goals', 0)}⚽ {stats.get('assists', 0)}🎯 "
            f"{stats.get('saves', 0)}🧤 {'✅' if stats.get('won') else '❌'}"
        )
    if not lines:
        await interaction.response.send_message("No stats submitted yet — use `/submit-stats` first.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=discord.Embed(
            title="Team of the Week (8v8)",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
    )


@bot.tree.command(name="setlogs", description="Set the server's general log channel")
@app_commands.describe(channel="Channel for general server logs")
@app_commands.checks.has_permissions(manage_guild=True)
async def setlogs(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    config = get_guild_data(interaction.guild.id)["config"]
    config["logs_channel_id"] = str(channel.id)
    save_league_state()
    await interaction.response.send_message(f"General logs will use {channel.mention}.")


@bot.tree.command(name="setpunishmentlogs", description="Set the moderation punishment log channel")
@app_commands.describe(channel="Channel for warning, timeout, and kick logs")
@app_commands.checks.has_permissions(manage_guild=True)
async def setpunishmentlogs(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    config = get_guild_data(interaction.guild.id)["config"]
    config["punishment_logs_channel_id"] = str(channel.id)
    save_league_state()
    await interaction.response.send_message(f"Punishment logs will use {channel.mention}.")


@bot.tree.command(name="setwelcome", description="Configure the welcome channel and message")
@app_commands.describe(
    channel="Channel where new-member welcomes are sent",
    message="Welcome text; use {user} and {server} as placeholders",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setwelcome(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: app_commands.Range[str, 1, 1000],
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    config = get_guild_data(interaction.guild.id)["config"]
    config["welcome_channel_id"] = str(channel.id)
    config["welcome_message"] = message
    save_league_state()
    await interaction.response.send_message(
        f"Welcome messages will be sent in {channel.mention}. `{message}` is saved."
    )


@bot.tree.command(name="setverify", description="Configure a verification button")
@app_commands.describe(channel="Channel where the verification panel is posted", role="Role granted on verification")
@app_commands.checks.has_permissions(manage_guild=True)
async def setverify(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    role: discord.Role,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    if not role_can_be_managed_for_guild(interaction.guild, role):
        await interaction.response.send_message(
            "I cannot assign that role. It must be unmanaged and below my highest role.",
            ephemeral=True,
        )
        return
    config = get_guild_data(interaction.guild.id)["config"]
    config["verify_channel_id"] = str(channel.id)
    config["verify_role_id"] = str(role.id)
    save_league_state()
    await channel.send(
        embed=discord.Embed(
            title="Server verification",
            description="Click the button below to receive the verified role.",
            color=discord.Color.green(),
        ),
        view=VerifyView(),
    )
    await interaction.response.send_message(f"Verification panel posted in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="setup-freeagents", description="Configure the free-agent forum")
@app_commands.describe(forum_channel="Forum channel for tryout posts")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_freeagents(interaction: discord.Interaction, forum_channel: discord.ForumChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    config = get_guild_data(interaction.guild.id)["config"]
    config["free_agents_forum_id"] = str(forum_channel.id)
    save_league_state()
    await interaction.response.send_message(f"Free-agent tryouts will be posted in {forum_channel.mention}.")


@bot.tree.command(name="tryout", description="Post a team tryout in the free-agent forum")
@app_commands.describe(
    team="Team hosting the tryout",
    time="Tryout date and time in readable text",
    server_link="Discord server or event link",
    description="Optional tryout details",
)
async def tryout(
    interaction: discord.Interaction,
    team: str,
    time: app_commands.Range[str, 1, 100],
    server_link: app_commands.Range[str, 1, 500],
    description: app_commands.Range[str, 1, 1000] | None = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    found = find_team(interaction.guild, team)
    if found is None:
        await interaction.response.send_message("That team does not exist.", ephemeral=True)
        return
    config = get_guild_data(interaction.guild.id)["config"]
    forum_id = config.get("free_agents_forum_id")
    forum = interaction.guild.get_channel(int(forum_id)) if forum_id else None
    if not isinstance(forum, discord.ForumChannel):
        await interaction.response.send_message("Run `/setup-freeagents` first.", ephemeral=True)
        return
    team_name = str(found[1].get("name", team))
    content = (
        f"**Team:** {team_name}\n"
        f"**Time:** {time}\n"
        f"**Server:** {server_link}\n\n"
        f"{description or 'No additional details provided.'}"
    )
    created = await forum.create_thread(name=f"{team_name} tryouts"[:100], content=content)
    thread = getattr(created, "thread", created)
    await interaction.response.send_message(
        f"Tryout posted for **{team_name}**: {getattr(thread, 'jump_url', forum.mention)}",
        ephemeral=True,
    )


@bot.tree.command(name="owner-request", description="Send a request to the league owner")
@app_commands.describe(description="What you are requesting and why")
async def owner_request(interaction: discord.Interaction, description: app_commands.Range[str, 1, 1500]) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    target_channel = await find_approval_channel(interaction)
    if target_channel is None:
        await interaction.response.send_message(
            "I could not find the configured moderator channel for owner requests.",
            ephemeral=True,
        )
        return
    data = get_guild_data(interaction.guild.id)
    requests = data.setdefault("owner_requests", [])
    request = {
        "requester_id": str(interaction.user.id),
        "description": description,
        "at": discord.utils.utcnow().isoformat(),
        "status": "open",
    }
    if isinstance(requests, list):
        requests.append(request)
    save_league_state()
    await target_channel.send(
        embed=discord.Embed(
            title="Owner request",
            description=description,
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow(),
        ).set_footer(text=f"Requested by {interaction.user}"),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await interaction.response.send_message("Your owner request was sent.", ephemeral=True)


@bot.tree.command(name="post-hosting-guide", description="Post the league hosting guide")
@app_commands.describe(channel="Channel where the guide should be posted")
@app_commands.checks.has_permissions(manage_guild=True)
async def post_hosting_guide(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    guide = discord.Embed(
        title="League hosting guide",
        description=(
            "Use `/schedule` to announce a fixture and `/result` to record the final score.\n"
            "Use `/predict` for spectator voting and `/report` or staff tools for moderation.\n\n"
            "Keep match times in UTC or use Discord timestamps so every member sees the correct local time."
        ),
        color=discord.Color.blue(),
    )
    guide.add_field(
        name="Team owners",
        value="Use `/roster`, `/team-info`, and the league staff commands to keep team information current.",
        inline=False,
    )
    guide.add_field(
        name="Spectators",
        value="Use the prediction buttons and follow the team tryout posts in the free-agent forum.",
        inline=False,
    )
    await channel.send(embed=guide)
    await interaction.response.send_message(f"Hosting guide posted in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="autorole", description="Set the role automatically assigned to new members")
@app_commands.describe(role="Role to assign when a member joins")
@app_commands.checks.has_permissions(manage_guild=True)
async def autorole(interaction: discord.Interaction, role: discord.Role) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    if not role_can_be_managed_for_guild(interaction.guild, role):
        await interaction.response.send_message(
            "I cannot assign that role. It must be unmanaged and below my highest role.",
            ephemeral=True,
        )
        return
    config = get_guild_data(interaction.guild.id)["config"]
    config["autorole_id"] = str(role.id)
    save_league_state()
    await interaction.response.send_message(f"New members will receive **{role.name}**.")


@bot.tree.command(name="serverinfo", description="Show server information")
async def serverinfo(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    guild = interaction.guild
    embed = discord.Embed(title=guild.name, color=discord.Color.blurple())
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>")
    embed.add_field(name="Members", value=str(guild.member_count or len(guild.members)))
    embed.add_field(name="Channels", value=str(len(guild.channels)))
    embed.add_field(name="Roles", value=str(len(guild.roles)))
    embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, style="F"))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="userinfo", description="Show information about a member")
@app_commands.describe(user="Member to inspect; defaults to yourself")
async def userinfo(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
    member = user or interaction.user
    embed = discord.Embed(title=f"User info · {member}", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User ID", value=str(member.id), inline=True)
    embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, style="F") if member.joined_at else "Unknown", inline=True)
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, style="F"), inline=False)
    roles = [role.mention for role in member.roles[1:]]
    embed.add_field(name="Roles", value=", ".join(roles[-20:]) if roles else "None", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ticket-setup", description="Post a ticket creation panel")
@app_commands.describe(channel="Channel where members can create tickets")
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_setup(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    config = get_guild_data(interaction.guild.id)["config"]
    config["ticket_panel_channel_id"] = str(channel.id)
    save_league_state()
    await channel.send(
        embed=discord.Embed(
            title="Support tickets",
            description="Click **Create ticket** to open a private support channel.",
            color=discord.Color.blurple(),
        ),
        view=TicketPanelView(),
    )
    await interaction.response.send_message(f"Ticket panel posted in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="close", description="Close the current support ticket")
async def close_ticket(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or ticket_owner_id(channel) is None:
        await interaction.response.send_message("This command can only be used inside a support ticket.", ephemeral=True)
        return
    if not is_ticket_staff(interaction, channel):
        await interaction.response.send_message("Only the ticket owner or support staff can close this ticket.", ephemeral=True)
        return
    await interaction.response.send_message("Closing this ticket.", ephemeral=True)
    await channel.delete(reason=f"Ticket closed by {interaction.user}")


@bot.tree.command(name="add", description="Add a member to the current support ticket")
@app_commands.describe(user="Member to add")
async def add_ticket_member(interaction: discord.Interaction, user: discord.Member) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or ticket_owner_id(channel) is None:
        await interaction.response.send_message("This command can only be used inside a support ticket.", ephemeral=True)
        return
    if not is_ticket_staff(interaction, channel):
        await interaction.response.send_message("Only the ticket owner or support staff can manage this ticket.", ephemeral=True)
        return
    await channel.set_permissions(
        user,
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        reason=f"Added to ticket by {interaction.user}",
    )
    await interaction.response.send_message(f"{user.mention} was added to this ticket.")


@bot.tree.command(name="remove", description="Remove a member from the current support ticket")
@app_commands.describe(user="Member to remove")
async def remove_ticket_member(interaction: discord.Interaction, user: discord.Member) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or ticket_owner_id(channel) is None:
        await interaction.response.send_message("This command can only be used inside a support ticket.", ephemeral=True)
        return
    if not is_ticket_staff(interaction, channel):
        await interaction.response.send_message("Only the ticket owner or support staff can manage this ticket.", ephemeral=True)
        return
    if user.id == ticket_owner_id(channel):
        await interaction.response.send_message("The ticket owner cannot be removed.", ephemeral=True)
        return
    await channel.set_permissions(user, overwrite=None, reason=f"Removed from ticket by {interaction.user}")
    await interaction.response.send_message(f"{user.mention} was removed from this ticket.")


@bot.tree.command(name="transcript", description="Export the current support ticket transcript")
async def transcript(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or ticket_owner_id(channel) is None:
        await interaction.response.send_message("This command can only be used inside a support ticket.", ephemeral=True)
        return
    if not is_ticket_staff(interaction, channel):
        await interaction.response.send_message("Only the ticket owner or support staff can export this ticket.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    lines = [f"Transcript: #{channel.name}", ""]
    async for message in channel.history(limit=1000, oldest_first=True):
        timestamp = message.created_at.isoformat()
        author = f"{message.author} ({message.author.id})"
        content = message.clean_content or "[attachment/embed]"
        attachments = " ".join(attachment.url for attachment in message.attachments)
        lines.append(f"[{timestamp}] {author}: {content} {attachments}".rstrip())
    payload = "\n".join(lines).encode("utf-8")
    await interaction.followup.send(
        file=discord.File(io.BytesIO(payload), filename=f"{channel.name}-transcript.txt"),
        ephemeral=True,
    )


@bot.tree.command(name="ban", description="Ban a user from the server")
@app_commands.describe(user="The user to ban", reason="Reason for the ban")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str = DEFAULT_REASON,
) -> None:
    if not member_can_be_moderated(interaction, user):
        await interaction.response.send_message(
            "I cannot ban that member because of the Discord role hierarchy.",
            ephemeral=True,
        )
        return
    await user.ban(reason=reason)
    await record_moderation_action(interaction, user.id, "ban", reason)
    await interaction.response.send_message(f"**{user.name}** has been banned. Reason: {reason}")


@bot.tree.command(name="unban", description="Unban a user by their exact Discord User ID")
@app_commands.describe(user_id="The numeric ID of the user to unban", reason="Reason for the unban")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(
    interaction: discord.Interaction,
    user_id: str,
    reason: str = DEFAULT_REASON,
) -> None:
    try:
        numeric_id = int(user_id)
        user = await bot.fetch_user(numeric_id)
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return
        await interaction.guild.unban(user, reason=reason)
    except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
        await interaction.response.send_message(
            "That user ID is invalid, or the user is not banned in this server.",
            ephemeral=True,
        )
        return
    await record_moderation_action(interaction, user.id, "unban", reason)
    await interaction.response.send_message(f"**{user.name}** has been unbanned. Reason: {reason}")


@bot.tree.command(name="kick", description="Kick a user from the server")
@app_commands.describe(user="The user to kick", reason="Reason for the kick")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str = DEFAULT_REASON,
) -> None:
    if not member_can_be_moderated(interaction, user):
        await interaction.response.send_message(
            "I cannot kick that member because of the Discord role hierarchy.",
            ephemeral=True,
        )
        return
    await user.kick(reason=reason)
    await record_moderation_action(interaction, user.id, "kick", reason)
    await interaction.response.send_message(f"**{user.name}** has been kicked. Reason: {reason}")


@bot.tree.command(name="timeout", description="Mute or timeout a member")
@app_commands.describe(
    user="The user to timeout",
    duration="Duration in minutes (up to 28 days)",
    reason="Reason for the timeout",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES],
    reason: str = DEFAULT_REASON,
) -> None:
    if not member_can_be_moderated(interaction, user):
        await interaction.response.send_message(
            "I cannot timeout that member because of the Discord role hierarchy.",
            ephemeral=True,
        )
        return
    await user.timeout(datetime.timedelta(minutes=duration), reason=reason)
    await record_moderation_action(
        interaction,
        user.id,
        "timeout",
        reason,
        details=f"{duration} minute(s)",
    )
    await interaction.response.send_message(
        f"**{user.name}** has been timed out for {duration} minutes. Reason: {reason}"
    )


@bot.tree.command(name="untimeout", description="Remove a timeout from a member")
@app_commands.describe(user="The user to remove timeout from")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, user: discord.Member) -> None:
    if not member_can_be_moderated(interaction, user):
        await interaction.response.send_message(
            "I cannot remove that member's timeout because of the Discord role hierarchy.",
            ephemeral=True,
        )
        return
    await user.timeout(None)
    await record_moderation_action(
        interaction,
        user.id,
        "untimeout",
        "Timeout removed",
    )
    await interaction.response.send_message(f"**{user.name}**'s timeout has been removed.")


@bot.tree.command(name="warn", description="Issue a formal warning to a user")
@app_commands.describe(user="The user to warn", reason="Reason for the warning")
@app_commands.checks.has_permissions(manage_messages=True)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
    guild_name = interaction.guild.name if interaction.guild else "this server"
    try:
        await user.send(f"You have been warned in **{guild_name}**.\n**Reason:** {reason}")
    except discord.HTTPException:
        LOGGER.info("Could not DM warning to user %s; their DMs may be disabled", user.id)
    await record_moderation_action(interaction, user.id, "warning", reason)
    await interaction.response.send_message(f"**{user.name}** has been formally warned. Reason: {reason}")


@bot.tree.command(name="clear", description="Clear channel messages")
@app_commands.describe(
    amount="Number of messages to clear (1 to 100)",
    user="Optional: only delete messages from this user",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100],
    user: discord.Member | None = None,
) -> None:
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "Message cleanup is only available in text channels.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    deleted = await channel.purge(
        limit=amount,
        check=lambda message: user is None or message.author.id == user.id,
    )
    await interaction.followup.send(f"Successfully cleared **{len(deleted)}** messages.")


@bot.tree.command(name="slowmode", description="Set a channel cooldown")
@app_commands.describe(seconds="Cooldown in seconds (0 to turn it off, maximum 6 hours)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(
    interaction: discord.Interaction,
    seconds: app_commands.Range[int, 0, MAX_SLOWMODE_SECONDS],
) -> None:
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "Slowmode is only available in text channels.",
            ephemeral=True,
        )
        return

    await channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        await interaction.response.send_message("Slowmode has been disabled.")
    else:
        await interaction.response.send_message(f"Slowmode is set to **{seconds}** seconds.")


@bot.tree.command(name="lock", description="Lock a text channel")
@app_commands.describe(
    channel="Channel to lock (defaults to the current channel)",
    reason="Reason for locking",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    reason: str = DEFAULT_REASON,
) -> None:
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message(
            "Please run this command in a text channel or provide a text channel.",
            ephemeral=True,
        )
        return

    overwrite = target_channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    await target_channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=reason)
    await interaction.response.send_message(f"**{target_channel.mention}** has been locked. Reason: {reason}")


@bot.tree.command(name="unlock", description="Unlock a text channel")
@app_commands.describe(channel="Channel to unlock (defaults to the current channel)")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
) -> None:
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message(
            "Please run this command in a text channel or provide a text channel.",
            ephemeral=True,
        )
        return

    overwrite = target_channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = True
    await target_channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
    await interaction.response.send_message(f"**{target_channel.mention}** is now unlocked.")


@bot.tree.command(name="lockdown", description="Lock all public server text channels")
@app_commands.checks.has_permissions(administrator=True)
async def lockdown(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    for channel in interaction.guild.text_channels:
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        if overwrite.send_messages is not False:
            overwrite.send_messages = False
            await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
    await interaction.followup.send("Server lockdown completed. Public text channels are now locked.")


@bot.tree.command(name="role", description="Add or remove a server role from a user")
@app_commands.describe(user="The targeted server member", role="The role to toggle")
@app_commands.checks.has_permissions(manage_roles=True)
async def role(
    interaction: discord.Interaction,
    user: discord.Member,
    role: discord.Role,
) -> None:
    if not member_can_be_moderated(interaction, user):
        await interaction.response.send_message(
            "I cannot change roles for that member because of the Discord role hierarchy.",
            ephemeral=True,
        )
        return
    if not role_can_be_managed(interaction, role):
        await interaction.response.send_message(
            "I cannot manage that role because it is managed or above my highest role.",
            ephemeral=True,
        )
        return

    if role in user.roles:
        await user.remove_roles(role)
        await interaction.response.send_message(f"Removed role **{role.name}** from {user.name}.")
    else:
        await user.add_roles(role)
        await interaction.response.send_message(f"Added role **{role.name}** to {user.name}.")


@bot.tree.command(name="request", description="Ask moderators for permission to create something")
@app_commands.describe(
    title="What you want to create",
    description="What it is, why you want it, and any relevant details",
)
async def request_permission(
    interaction: discord.Interaction,
    title: app_commands.Range[str, 1, MAX_REQUEST_TITLE_LENGTH],
    description: app_commands.Range[str, 1, MAX_REQUEST_DESCRIPTION_LENGTH],
) -> None:
    """Post a permission request for moderators to review and vote on."""

    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    approval_channel = await find_approval_channel(interaction)
    if approval_channel is None:
        await interaction.response.send_message(
            "I couldn't find the approval channel. Set DISCORD_APPROVAL_CHANNEL_ID "
            "to a text-channel ID, or run the command in a text channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    request_id = str(interaction.id)
    record: dict[str, object] = {
        "kind": "approval",
        "request_id": request_id,
        "requester_id": str(interaction.user.id),
        "title": title,
        "description": description,
        "channel_id": str(approval_channel.id),
        "status": "pending",
        "approve_votes": [],
        "deny_votes": [],
    }
    message = await approval_channel.send(
        embed=approval_embed(record),
        view=ApprovalView(),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    approval_requests[str(message.id)] = record
    try:
        save_approval_requests()
    except OSError:
        await message.delete()
        LOGGER.exception("Could not save permission request %s", request_id)
        await interaction.followup.send(
            "I couldn't save that request. Please try again.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"Your permission request was sent for review: {message.jump_url}",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    command_name = (
        interaction.command.qualified_name
        if interaction.command is not None
        else "unknown"
    )
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "You do not have permission to use this command."
    elif isinstance(error, app_commands.errors.CommandNotFound):
        LOGGER.warning(
            "Received stale slash command '%s' in guild %s",
            command_name,
            interaction.guild_id,
        )
        message = (
            "Discord sent an outdated command entry. Close and reopen the slash-command menu, "
            "then choose the command again. Global command updates can take up to an hour."
        )
    elif isinstance(error, app_commands.errors.CheckFailure):
        message = "This command can only be used inside a server."
    else:
        original = getattr(error, "original", error)
        LOGGER.error(
            "Unhandled slash command error in /%s (guild %s): %r",
            command_name,
            interaction.guild_id,
            original,
            exc_info=(type(original), original, getattr(original, "__traceback__", None)),
        )
        if isinstance(original, discord.Forbidden):
            message = (
                "I lack a Discord permission needed for this action, or my bot role is below the "
                "role I need to manage. Check the required server/channel permission and role order."
            )
        elif isinstance(original, discord.NotFound):
            message = "I could not find a saved channel, role, or member needed for this command."
        elif isinstance(original, discord.HTTPException):
            message = (
                "Discord rejected this action. The exact error is now recorded in the bot log; "
                "check that log before changing permissions."
            )
        else:
            message = (
                "The command hit an internal error. Its full details are recorded in the bot log "
                "instead of being hidden behind a permissions warning."
            )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.NotFound:
        LOGGER.info("Could not reply to /%s because its interaction expired", command_name)
    except discord.HTTPException:
        LOGGER.exception("Could not send the error response for /%s", command_name)


def main() -> None:
    """Start the bot and fail clearly when deployment is misconfigured."""

    configure_logging()
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not configured. Add it as a Replit Secret before starting the bot."
        )
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()