# Reset

Stop letting your AI subscription expire unused.

Claude and ChatGPT plans give you usage limits that reset every week, and sometimes a one-time "reset credit" that expires after about 30 days. Most weeks a big chunk goes unused. Reset runs on your computer, watches those limits, and helps you spend the leftover capacity on ideas you actually care about. You talk to it through your own Telegram bot.

- **See what's left:** weekly and 5-hour limits, and one-time resets with their expiry dates, for Codex (ChatGPT plans) and Claude Code.
- **Get a heads-up:** a message when a week is about to reset mostly unused, and 7 days before a one-time reset expires.
- **Keep an idea list:** text `/idea build a tiny CLI that…` whenever inspiration strikes.
- **Put ideas to work:** Reset asks before every run, then runs the idea as a bounded background job (token budget, time limit). `/stop` kills it for real.
  - Several runs can go at once: by default up to 3 on each subscription (`runs.maxRunsPerEngine`). When one subscription is full, a new run goes to the other.
  - It picks the subscription whose unused capacity expires soonest, unless you name one.
  - You can choose the model and effort ("run 3 on codex with sol at xhigh").
  - Work on an existing project happens on its own branch in a separate git worktree. New ideas get their own folder.
  - Runs show up as chats in your apps, and you can pick them up days later like chats you started yourself.
    - Codex runs are pinned in the Codex app.
    - You can watch Claude runs live in the Claude app, on your Mac or your phone. When one finishes, its chat stays open while you're in the Claude app. Once you leave the app, it moves into Claude Desktop's Code tab.
    - **Open in Codex** or **Open in Claude** takes you straight to a run's chat on your Mac.
- **Ask it anything:** "how many one-time resets do I have?", "what did run 3 get done?" Replies come from an AI brain running on your own Claude Code or Codex.
- **Tune it by chatting:** "lower my floor to 3%", "allow 5 runs at once", "make runs sandboxed", "remind me 7 days and 1 day before a reset expires". Reset confirms every change in its own message, with an Undo button.

Reset never sees your AI passwords or tokens. It works through the official `codex` and `claude` tools you're already signed in to.

## Get started

Paste this into Claude Code or Codex:

> Set up Reset from https://github.com/illianaa/reset for me: clone it and follow its AGENTS.md.

Your agent handles setup. You do three things yourself:

1. Create a bot with [@BotFather](https://t.me/BotFather) in Telegram.
2. Paste its token into a private terminal prompt.
3. Tap the link it shows.

<details>
<summary>Setting up by hand</summary>

```sh
git clone https://github.com/illianaa/reset && cd reset
./bin/resetctl setup
```

`resetctl setup` walks through these steps. You can also run them one at a time: `check`, `telegram`, `brain`, `skill`, `service`, `test`.
</details>

Requirements:
- macOS or Linux
- Python 3.9+
- at least one of Codex (signed in with ChatGPT) or Claude Code (signed in with a Claude plan)

There's nothing else to install.

## Talking to Reset

| Send | What happens |
| --- | --- |
| `/status` | Usage, reset times and one-time resets |
| `/idea <text>` | Saves an idea (saving never runs it) |
| `/ideas` | Lists your ideas |
| `run 3` | Reset asks you to approve running idea 3, with Start/Skip buttons |
| `/runs` | What's running, and recent results |
| `/stop` | Stops everything immediately and cancels pending requests (`stop 3` stops only run 3) |
| `/floor 8` | Runs leave at least 8% of every usage limit untouched (`/floor` shows the current share) |
| anything else | Answered by the AI brain |

The commands never depend on AI, so they keep working even when every usage limit is exhausted. If the Claude brain is out of usage, the Codex brain answers, and the other way round.

## Safety

- **Nothing runs without your OK.** Every run needs your tap or code, and approvals can't come from an AI.
- **Bounded runs.** Each run has a token budget and a deadline, ends before any usage window resets, and refuses to start if paid overage could kick in.
- **Your share stays yours.** Runs only start while you have at least 5% left of every limit. While runs are going, Reset rechecks every 5 minutes. If a subscription dips below that share, or paid usage turns on, Reset stops that subscription's runs. To keep a different share, ask your bot, or send `floor 10` with the percentage you want.
- **Full access by default, so runs don't stall.** Runs can run commands and install packages without stopping to ask. If a run is still blocked, or asks a question nobody is there to answer, Reset texts you. Prefer tighter limits? `resetctl setup access --access sandboxed`.
- **Your checkout stays untouched.** Runs on an existing codebase work on a new `reset/…` branch in a separate worktree, for you to review.
- **Real cancellation.** Stopping kills every process the run started and verifies that nothing survived.
- **Redemption stays with you.** Reset tells you how to redeem one-time resets; it doesn't redeem them itself.
- **Your data stays local.** Everything lives in `~/.reset/`.
- **Your bot, your chat.** Only your paired Telegram account can talk to your bot.

## Configuration

You don't need to configure anything by hand: `resetctl setup` saves your settings in `~/.reset/config.json`, which only your user account can read. That includes your Telegram bot token.

Runs appear in the Codex and Claude apps by default. Claude runs use Claude Code's Remote Control for this, so while one is running, anyone signed in to your Claude account can also message it. To keep runs out of the apps, set `"runs": {"showInApps": false}` in that file.

To override a setting with an environment variable (for example the bot token, or the path to `codex`), copy [.env.example](.env.example) to `.env` and uncomment what you need. Git ignores `.env`, and the background service reads it too.

## Status

Early and moving fast.

- **Codex:** limits and reset credits are read live.
- **Claude Code:** limits are read live. Its one-time resets aren't exposed to other tools yet, so Reset reads the copy Claude Desktop caches (macOS) and labels it with its age, or you can add one with `resetctl grant add`.
- **Automatic runs** (spending leftover capacity without asking) are designed but not switched on yet.

Changing the code with an AI agent? [AGENTS.md](AGENTS.md) explains how it fits together and the rules it must keep.
