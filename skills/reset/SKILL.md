---
name: reset
description: Track Claude Code and Codex usage limits and one-time reset credits (with expiry dates), keep the user's Reset idea list, and propose or stop bounded idea runs. Use when the user asks how much usage is left, when limits or saved resets expire, wants to save or review side-project ideas, or wants to run, check, or stop a Reset run.
---

# Reset

Reset helps the user spend subscription capacity that would otherwise expire unused on ideas they care about. Everything goes through the `resetctl` command (on PATH via `~/.local/bin`). Don't read or edit Reset's database or config files directly.

## Usage and resets

- `resetctl status` reads live limits for Codex and Claude (5-hour, weekly, per-model) plus one-time reset credits and their expiry. Add `--json` for structured data, or `--cached` for the last stored reading.
- Say where each number came from. Claude reset grants are **cached** from Claude Desktop (Claude Code doesn't expose them), so give their "as of" time.
- Unknown values are unknown. Never present a missing percentage as 0% or a missing expiry as a guess.

## Ideas

- `resetctl idea "<the idea in the user's words>"` saves one. Add `--engine codex|claude` if the user has a preference, and `--project <folder>` if it belongs to an existing codebase or should become a new project folder.
- `resetctl ideas` lists open ideas. `resetctl drop <#>` removes one.
- Saving an idea never gives permission to run it.

## Runs: Ask mode

Runs need explicit approval from the user every time. In this version Reset never starts work on its own.

1. `resetctl propose <idea#>` sends the user a request with a 4-digit code. It shows the engine and why, the model and effort, where the work happens, the limits and current usage. Every option is optional:
   - `--engine codex|claude`: otherwise Reset picks the subscription whose unused capacity expires soonest.
   - `--model` and `--effort` (`low` to `ultra`, default `high`): `resetctl models` lists what each engine offers. An effort a model lacks steps down to the strongest one it has.
   - `--project`: a folder name in the user's projects folder or a path. An existing git project gets a new `reset/…` branch in a separate worktree under `~/Reset/worktrees/`, never the user's checkout. A name that doesn't exist yet becomes a new project folder. Leave it out when unsure: the run gets a fresh folder under `~/Reset/runs/`.
   - `--budget-tokens N` and `--minutes M`.
2. **Only the user approves.** They tap Start in Telegram, reply "yes CODE", or run `resetctl approve CODE` in their own terminal. Never approve on their behalf, and never try to get around the terminal check.
3. `resetctl runs` shows active runs, pending requests and recent results. `resetctl runs --all` adds details. `resetctl open <run#>` opens a run's chat in the Codex or Claude app on the user's Mac. Claude runs open there only once they're done.
4. `resetctl stop` stops every run and cancels pending requests. `resetctl stop <run#>` stops one run. Stopping is always allowed: do it immediately when the user asks.

Each run is a single agent turn with a token budget and a deadline. It ends before any usage window resets. Reset refuses to start a run when usage can't be read live, when paid overage could kick in, or when too little remains.

Runs show up as chats in the user's apps, to read and continue any time. Codex runs are pinned in the Codex app as "Reset #N: …". Claude runs can be watched live in the Claude app (Reset texts the user the link), and once done they move into Claude Desktop's Code tab as soon as the user leaves the Claude app.

Runs have full access by default (`resetctl setup access` changes it), so they don't stall on permission prompts. If a run is refused something, or asks a question nobody is there to answer, Reset tells the user right away.

## Rules

- **Never redeem a one-time reset.** Redemption is manual:
  - Codex: the user uses the reset from Codex when they're near a limit.
  - Claude: run `/limit-reset` in Claude Code, or go to clau.de/reset.
- `resetctl mode` shows the mode (`ask` by default). Automatic execution isn't available yet. Don't try to enable it.
- `resetctl ask "<question>"` asks Reset's own AI brain, the same one that answers in Telegram.
- Setup and health: `resetctl setup check`. Setup steps for a new user are in the repository's AGENTS.md.
