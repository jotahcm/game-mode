---
name: game-mode
description: Free RAM for gaming by closing every app (Chrome, Safari, Cursor, Slack, ...), every Claude Code session, Claude.app and Docker while remembering exactly where the user was, then restore it all. Use when the user says game mode, gaming, "close everything to get RAM back", or asks to resume / restore / "game mode off" afterwards.
---

# Game mode

One script does everything: `~/.claude/skills/game-mode/scripts/gamemode.py`
(also on PATH as `gamemode`, so the user can run it from any terminal without Claude).

## Turn on: "game mode", "I want to game", "close everything"
1. Run `gamemode on` (no confirmation needed when the user asked for game mode).
2. Reply in one or two lines: what's closing and "run `gamemode off` (or ask me to resume) to restore".
   Keep it short: this session closes itself as soon as the reply ends.

Discovered at run time, every time:
- Every regular Dock app gets a polite quit. Chrome-family and Safari windows/tabs are saved first
  (incognito skipped). An app with a "save changes?" dialog stays open and is reported.
- Kept open: Finder, games (Info.plist category), launchers (Steam, CrossOver, Epic, ...),
  Discord, and anything in `~/.claude/game-mode/config.json` → `{"keep": ["App Name" | "bundle.id"]}`.
- Claude Code sessions in any `~/.claude*` config dir, with their iTerm window/tab position.
  Busy sessions finish their turn first (default 15 min, `--timeout MIN`); still busy = left running.
  Apps hosting a session (e.g. Cursor's terminal) quit only after that session stops.
- iTerm quits once it has no windows left; Claude.app; Docker last, remembering running containers.

Running `gamemode on` again while game mode is on closes whatever was opened since and adds it
to the saved list.

## Turn off: "resume", "game mode off", "restore everything"
Run `gamemode off`. Restores apps, browser windows, Claude sessions (`claude --resume` in the
same folder, config dir, iTerm window grouping and tab order), Claude.app, Docker + containers.
Things opened during the game are respected: running apps aren't relaunched, saved browser
windows already open aren't duplicated, Claude sessions already running are skipped.

`gamemode status` shows the saved state; log: `~/.claude/game-mode/worker.log`.

## Tests
`cd ~/.claude/skills/game-mode && python3 -m unittest discover -s tests`
