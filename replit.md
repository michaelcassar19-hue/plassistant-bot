# League Moderation Bot

A deployable Discord moderation bot with slash commands for managing members, channels, messages, and roles.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `uv run main.py` — run the Discord moderation bot
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required secret: `DISCORD_BOT_TOKEN` — Discord application bot token
- Optional env: `DISCORD_GUILD_ID` — sync slash commands to one server immediately; omit for global sync
- Optional env: `DISCORD_APPROVAL_CHANNEL_ID` — channel where `/request` approval posts are sent
- Optional env: `DISCORD_APPROVAL_QUORUM` — matching moderator votes needed to approve or deny a request (default `2`)
- Optional env: `LEAGUE_DATA_PATH` — persistent JSON path for league, auction, fixture, ticket, and server settings
- Optional env: `DISCORD_ENABLE_MEMBERS_INTENT=true` — opt in to the privileged members intent after enabling it in Discord for autorole and welcome events

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `discord_bot/bot.py` — bot setup, slash commands, league management, auctions, tickets, moderation history, predictions, and error handling
- `discord_bot/README.md` — Discord invitation and approval workflow setup
- `main.py` — deployment entry point
- `pyproject.toml` / `uv.lock` — Python runtime dependencies

## Architecture decisions

- The bot uses Discord application commands, so moderation permissions are enforced with `app_commands.checks`.
- The bot token is read only from `DISCORD_BOT_TOKEN`; no token is stored in the repository.
- `DISCORD_GUILD_ID` is optional: server-scoped sync is fast for development, while global sync supports a multi-server deployment.

## Product

League moderators can manage teams, rosters, budgets, fixtures, auctions, ratings, free-agent tryouts, tickets, and server configuration alongside the moderation commands. The bot persists league data and moderation history in JSON files under `data/` by default.

## User preferences

No additional preferences recorded.

## Gotchas

- The Discord application must be invited with the moderation permissions required by the commands and must have its bot role above members and roles it manages.
- Discord's global slash-command sync can take up to an hour; set `DISCORD_GUILD_ID` for immediate server-scoped command availability.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
