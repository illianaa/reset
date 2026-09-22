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

- `resetctl idea "<the idea in the user's words>"` saves one. Add `--engine codex|claude` if the user has a preference.
- `resetctl ideas` lists open ideas. `resetctl drop <#>` removes one.
- Saving an idea never gives permission to run it.

## Runs: Ask mode

Runs need explicit approval from the user every time. In this version Reset never starts work on its own.

1. `resetctl propose <idea#> [--engine codex|claude] [--budget-tokens N] [--minutes M]` sends the user a request with a 4-digit code. It includes the limits and current usage.
2. **Only the user approves.** They tap Start in Telegram, reply "yes CODE", or run `resetctl approve CODE` in their own terminal. Never approve on their behalf, and never try to get around the terminal check.
3. `resetctl runs` shows active runs, pending requests and recent results. `resetctl runs --all` adds details.
4. `resetctl stop` stops every run and cancels pending requests. `resetctl stop <run#>` stops one run. Stopping is always allowed: do it immediately when the user asks.

Each run is a single agent turn in its own folder under `~/Reset/runs/`, with a token budget and a deadline. It ends before any usage window resets. Reset refuses to start a run when usage can't be read live, when paid overage could kick in, or when too little remains.

## Rules

- **Never redeem a one-time reset.** Redemption is manual:
  - Codex: the user uses the reset from Codex when they're near a limit.
  - Claude: run `/limit-reset` in Claude Code, or go to clau.de/reset.
- `resetctl mode` shows the mode (`ask` by default). Automatic execution isn't available yet. Don't try to enable it.
- `resetctl ask "<question>"` asks Reset's own AI brain, the same one that answers in Telegram.
- Setup and health: `resetctl setup check`. Setup steps for a new user are in the repository's AGENTS.md.
