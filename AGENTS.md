# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex and others) working in this repository. There are two jobs: **setting Reset up for a user**, and **changing Reset's code**.

Reset helps people use AI subscription capacity that would otherwise expire unused. It runs on the user's computer, talks to them through their own Telegram bot, tracks usage limits and one-time resets for their Codex and Claude Code subscriptions, keeps their idea list, and runs approved ideas as bounded background jobs.

## Set up Reset for a user

If the code isn't on the machine yet, clone it somewhere permanent. The background service and the skill link to this folder, so don't use a temporary directory:

```sh
git clone https://github.com/illianaa/reset.git ~/reset && cd ~/reset
```

Run these from the repository root, in order. Every step is safe to re-run. After each step, report the result to the user in a sentence.

Ground rules:

- **Never ask the user to paste a bot token, API key or password into this chat.** The Telegram step asks for the token privately in the user's own terminal.
- Never approve a run request, and never redeem a usage reset, on the user's behalf.
- Don't try to work around macOS/Linux permission prompts or sign-ins. Ask the user to do them.
- If a step fails, stop and explain what the output says.

1. **Check prerequisites:** `./bin/resetctl setup check`
   - Needs Python 3.9+ on macOS or Linux, and at least one of Codex (signed in with ChatGPT) or Claude Code (signed in with a Claude plan).
   - If neither is signed in, ask the user to sign in themselves: `codex login`, or run `claude` and then `/login`.
   - The "Projects" line is the folder Reset found for the user's code (`~/Projects`, `~/code`, `~/Developer`…). Runs can also use any other folder in their home folder by path.

2. **Link the command and skill:** `./bin/resetctl setup skill`
   - This puts `resetctl` in `~/.local/bin` and links the Reset skill into Claude Code and Codex.
   - If `~/.local/bin` isn't on the user's PATH, use `./bin/resetctl` for the remaining steps.

3. **Connect Telegram. The user does this part.** Tell them:
   > In Telegram, message @BotFather, send `/newbot`, and pick a name and a username ending in "bot". Then run `resetctl setup telegram` in your own terminal and paste the token when it asks (the input is hidden). It shows a link: open it on your phone and tap Start.

   Wait until they confirm. Then check that `resetctl setup check` shows Telegram as paired.

4. **Choose the AI brain:** `resetctl setup brain --use auto`
   - The brain answers free-form messages and summarizes runs that were cut short.
   - `auto` uses the signed-in Claude Code, then Codex. `--use none` means commands only.
   - Show that it works: `resetctl ask "how many one-time resets do I have?"`

5. **Give runs full access (recommended):** `resetctl setup access --access full`
   - Tell the user what this means: approved runs can run commands, install packages and use the network without stopping to ask. Codex runs get no sandbox, and Claude Code runs use bypass-permissions mode.
   - Every run still needs their OK, has a token budget and a deadline, and can be stopped at once. Work on an existing codebase happens on a new branch in its own git worktree, never in their checkout.
   - If they prefer, use `--access sandboxed`: runs can change files in their own folder, and anything else (the network, installs, git commits) is refused. Runs never wait for an answer either way; Reset texts the user when a run is refused something.
   - If Claude runs fail because bypass-permissions mode is turned off (some organizations disable it), switch to sandboxed.
   - Runs show up as chats in the user's apps, to read and continue later like any chat they started.
     - Codex threads are named "Reset #N: …" and pinned in the Codex app.
     - Claude runs turn on Claude Code's Remote Control, so the user can watch them live in the Claude app and on their phone.
     - When a Claude run is done, its live chat stays open while the user is in the Claude app. Once they leave the app, Reset hands it to Claude Desktop, the same way `/desktop` does (macOS only).
     - Remote Control needs a Claude plan login. On Team and Enterprise plans, an admin has to allow it.
     - If it's unavailable, runs still work, and Reset says so.

6. **Start the background service:** `resetctl setup service`
   - This installs a LaunchAgent on macOS or a systemd user service on Linux.
   - On Linux, mention `loginctl enable-linger $USER` if they want it running while logged out.

7. **Send a test message:** `resetctl setup test`
   - The user should get a Telegram message.
   - Suggest they reply `/status`, or ask "which ideas do you have?".

Setup is done when every line of `resetctl setup check` shows `ok`.

## Using Reset from an agent session

The Reset skill (`skills/reset/SKILL.md`) explains the day-to-day commands. The important ones:

- `resetctl status`
- `resetctl idea "…"`
- `resetctl ideas`
- `resetctl propose <idea#> [--engine] [--model] [--effort] [--project]`
- `resetctl models`
- `resetctl runs`
- `resetctl open <run#>`
- `resetctl stop`
- `resetctl ask "…"`

## Working on the code

Python 3.9+, standard library only (no dependencies), macOS and Linux.

| Path | What it does |
| --- | --- |
| `resetagent/cli.py` | `resetctl` commands, including the setup steps |
| `resetagent/daemon.py` | Background service: poll Telegram, run commands, supervise runs, monitor usage, deliver messages. Also the LaunchAgent/systemd install |
| `resetagent/commands.py` | Handles exact commands (`/stop`, approvals, `/idea`…) without any model; free text goes to the brain |
| `resetagent/brain.py` | Optional AI brain: Claude Code / Codex CLIs with Reset's tools, fallback order, provider-neutral conversation |
| `resetagent/tools.py`, `mcp.py` | The brain's tools, served over MCP (stdio) |
| `resetagent/runs.py`, `worker.py`, `proctree.py` | Run requests (engine routing, room on each subscription), one supervised worker per run (several run at once), the usage floor while runs work, enforced and verified cancellation |
| `resetagent/models.py`, `workspace.py` | Live model and effort lists per engine; where a run works (scratch folder, new project, git worktree) and the brief the agent gets |
| `resetagent/apps.py` | Runs as chats in the desktop apps: pinned Codex threads, live Claude runs over Remote Control, finished Claude runs handed to Claude Desktop, and the links that open a run's chat |
| `resetagent/settings.py` | The settings users tune by chatting with the brain (a fixed list, each value checked), announced with an Undo button |
| `resetagent/providers/` | Reading usage: Codex app-server; Claude Code `get_usage`; Claude Desktop's cached reset grants |
| `resetagent/monitor.py`, `notify.py`, `channels/` | Notification rules, durable outbox, Telegram (plus local and iMessage) delivery |
| `resetagent/status.py`, `ideas.py`, `db.py`, `config.py` | Usage snapshots, the idea list, SQLite state in `~/.reset/`, settings |

Invariants. Keep these true in every change:

1. **Anything that must work at 0% usage never calls a model.** That covers saving ideas, stop, approvals, alerts and status. Only free-form chat and run summaries use a brain, and they fall back to commands.
2. **Reset never handles AI credentials.** It drives the official `codex` and `claude` CLIs, and it doesn't read tokens from keychains or auth files.
3. **Only the user approves runs**, with a code, a Telegram button, or `resetctl approve` in a real terminal. Brains get `tools.py` and nothing more: no approve or redeem tools.
   - The settings a brain can change are a fixed list in `settings.py`, and each value is checked.
   - Reset announces every change a brain makes in its own message, with an Undo button, so no setting changes without the user seeing it.
4. **Reset redemption is manual.** The Codex client blocks the redeem method (`FORBIDDEN` in `providers/codex.py`).
5. **Stopping is enforced, not requested.** The supervisor kills every process a run was seen to spawn, identified by pid and start time, and verifies that none survived. After a restart, runs default to stopped.
6. **The conversation is provider-neutral.** It's stored as plain text, and brain calls are stateless, so any model can answer the next message.
7. **Unknown stays unknown.** Missing percentages or expiries are `None`, never 0. Cached data never authorizes a run.

Settings: `~/.reset/config.json`, written by `resetctl setup`, and by `settings.py` and the `floor` command when the user tunes Reset by chatting. Optional environment overrides are listed in `.env.example` and registered in `config.ENV_VARS`. Read them only through `config.env()`, which rejects unregistered names. Never commit secrets or personal data: `.env`, `~/.reset` and local notes are git-ignored.

**Adding a subscription provider** (e.g. another coding agent with plan limits):

- Add `providers/<name>.py` whose status reader returns normalized windows (see `providers/common.window`) and any one-time resets.
- Wire it into `status.collect`.
- Add an engine class in `worker.py` if it can run work headlessly.
- Add a brain backend in `brain.py` (`name`, `provider`, `bin`, `ask(prompt, timeout)` using Reset's MCP tools) if its CLI can answer with tools.
