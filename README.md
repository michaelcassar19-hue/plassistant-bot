# League Moderation Bot

This project runs a Discord bot with slash commands for common server moderation tasks.

## Required Discord setup

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Invite the bot with the `bot` and `applications.commands` scopes.
3. Grant the bot the moderation permissions required by the commands you want to use.
4. Store the bot token as the `DISCORD_BOT_TOKEN` Replit Secret.

The bot role must be above members it moderates and roles it manages. Discord's own hierarchy rules still apply even when a moderator has a slash-command permission.

## Configuration

- `DISCORD_BOT_TOKEN` — required secret containing the bot token.
- `DISCORD_GUILD_ID` — optional server ID. When set, commands sync to that server immediately. If omitted, commands sync globally, which can take up to an hour to appear.
- `DISCORD_APPROVAL_CHANNEL_ID` — optional text-channel ID where `/request` posts are reviewed. Without it, requests are posted in the channel where the command is used.
- `DISCORD_APPROVAL_QUORUM` — optional number of matching moderator votes needed to close a request, defaulting to `2`.
- `LEAGUE_DATA_PATH` — optional path for persistent team, auction, fixture, ticket, and server configuration data.
- `DISCORD_LOG_LEVEL` — optional Python logging level, defaulting to `INFO`.
- To activate `/autorole` and `/setwelcome`, enable Discord's **Server Members Intent** in the Developer Portal and set `DISCORD_ENABLE_MEMBERS_INTENT=true`. The bot keeps this privileged intent off by default so it can start without that portal setting.

## Commands

- **League:** `/team-create`, `/make manager`, `/team-info`, `/team-budget`, `/sign`, `/roster`, `/release`, `/table` (`/sign` starts new players at 40 OVR; `/make manager` lets server managers assign a member to a saved team; `/roster` privately shows a selected saved team's manager, funds, and player OVRs)
- **Transfers and loans:** `/transfer news` configures the automatic announcement channel; `/transfer player` transfers a signed player at their saved market value; `/loan` temporarily moves a player to another roster; `/loan-return` returns them early. Loan durations count results recorded for the borrowing team and return automatically at zero.
- **Rulebooks:** `/rules` posts a two-book picker; server managers fill each book with `/rulebook-set` by selecting Rulebook 1 or 2, entering its text, and optionally setting its display title.
- **Auctions:** `/auction start`, `/auction cancel`, `/auction status`, `/auction end` (members bid with the button on an auction post)
- **Fixtures:** `/schedule`, `/fixtures`, `/result`
- **Scrims:** `/scrim start` opens 10-minute player/goalkeeper signups (8 players, 2 goalkeepers); `/scrim end` concludes early; `/scrim code` posts a code to the channel.
- **Ratings:** `/update-ovr`, `/check-ovr`, `/ratings-adjust`, `/market-value`, `/submit-stats`, `/totw`
- **Community:** `/predict`, `/coinflip`, `/setup-freeagents`, `/tryout`, `/owner-request`
- **Server setup:** `/setlogs`, `/setpunishmentlogs`, `/setwelcome`, `/setverify`, `/ticket-setup`, `/post-hosting-guide`, `/autorole`
- **Tickets:** `/close`, `/add`, `/remove`, `/transcript` (used inside a ticket channel)
- **Information:** `/serverinfo`, `/userinfo`
- **Moderation:** `/ban`, `/unban`, `/kick`, `/timeout`, `/untimeout`, `/warn`, `/clear`, `/slowmode`, `/lock`, `/unlock`, `/lockdown`, `/case`, `/history`, `/role`
- `/request` — submit a creation request for moderator approval. Moderators vote with the buttons on the approval post.

Discord bots cannot create true multi-user Group DMs, so `/request` uses a grouped approval post in a moderator channel instead. Keep the configured approval channel private to the moderation team.

Fixture commands offer autocomplete for saved teams and the Ligue 1 club list. Use `/fixtures team1 team2` to post a fixture card (optionally include `match_date`), or leave both teams blank to repost all scheduled cards. Use `/result team1 team2 team1_score team2_score` to replace a fixture card with the scored result card. Dates accept a Unix timestamp or ISO UTC time.

Fixture and result cards fetch Ligue 1 crests from Wikimedia and retain initials as a fallback when a club image is unavailable. Scrim signups are saved with the league state and resume after a bot restart. When ended, the bot closes the signup, posts a `Scrim Concluded` card, and DMs the organizer balanced blue/red lineups with each player's OVR, or `Scrim Has Failed. Not Enough`. `/check-ovr` privately shows your or another member's rating.

Run locally or in a deployment with:

```bash
uv run main.py
```