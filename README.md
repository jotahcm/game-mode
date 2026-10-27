# game-mode

Free up RAM on macOS before gaming: one command closes your apps, browser tabs, Claude Code sessions, Claude.app and Docker, and a second command brings everything back where it was.

```
gamemode on      # save everything, then close it
gamemode off     # restore it all
```

## What it does

**`gamemode on`**
- Quits every regular (Dock) app. Chrome-family browsers and Safari have their windows and tabs saved first (incognito windows are skipped).
- Stops every Claude Code session in any `~/.claude*` config dir and remembers its folder, flags and iTerm window/tab position. Sessions in the middle of a turn finish it first (up to 15 min by default).
- Quits Claude.app and stops Docker Desktop, remembering which containers were running.
- Leaves open: Finder, games (detected from the app's Info.plist), game launchers (Steam, CrossOver, Epic, Battle.net, GOG, ...) and Discord.
- Apps showing a "save changes?" dialog stay open and are reported in a notification.

**`gamemode off`**
- Reopens apps and browser windows, starts Docker and its containers again, and resumes each Claude session (`claude --resume`) in the same iTerm window grouping and tab order.
- Anything you opened during the game is left alone: running apps aren't relaunched, open browser windows aren't duplicated, live sessions aren't resumed twice.

## Install with an AI agent

Paste this to Claude Code (or any coding agent with shell access):

> Install game-mode from https://github.com/jotahcm/game-mode by following the "Instructions for AI agents" section of its README.

## Requirements

- macOS
- Python 3 (the one from Xcode Command Line Tools is fine; no extra packages)
- Optional: [iTerm2](https://iterm2.com) for restoring Claude sessions into windows and tabs, [Claude Code](https://claude.com/claude-code), Docker Desktop

## Install

```sh
git clone https://github.com/jotahcm/game-mode.git ~/.claude/skills/game-mode
mkdir -p ~/.local/bin
ln -s ~/.claude/skills/game-mode/scripts/gamemode.py ~/.local/bin/gamemode
chmod +x ~/.claude/skills/game-mode/scripts/gamemode.py
```

Make sure `~/.local/bin` is on your `PATH` (e.g. add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc`).

Cloning into `~/.claude/skills/game-mode` also installs it as a Claude Code skill, so you can just tell Claude "game mode" or "restore everything".

**Permissions:** the first run asks for macOS Automation access (System Settings → Privacy & Security → Automation) so the terminal running it can control each app, browser and iTerm. Allow them, otherwise those apps can't be saved or quit.

## Usage

```sh
gamemode on --dry-run      # show what would be closed, change nothing
gamemode on                # close everything
gamemode on --timeout 5    # wait at most 5 min for busy Claude sessions
gamemode status            # show what's saved
gamemode off               # restore
```

Running `gamemode on` again while game mode is already on closes whatever was opened since and adds it to the saved list.

If you run it from inside a Claude Code session, that session is closed too once its reply finishes, and comes back with `gamemode off`.

## Keep extra apps open

Create `~/.claude/game-mode/config.json` with app names or bundle IDs:

```json
{"keep": ["Spotify", "com.apple.Music"]}
```

## Files

| Path | What |
| --- | --- |
| `~/.claude/game-mode/state.json` | what was closed and how to restore it |
| `~/.claude/game-mode/config.json` | your keep list (optional) |
| `~/.claude/game-mode/worker.log` | log of each run |

## Tests

```sh
cd ~/.claude/skills/game-mode && python3 -m unittest discover -s tests
```

## Instructions for AI agents

Follow these steps exactly to install game-mode for the user. Stop and ask the user whenever a step says so.

**Never run `gamemode on` (without `--dry-run`) while installing.** It quits the user's apps and stops every Claude Code session, including yours. Only the user decides when to turn it on.

1. **Check the platform.** `uname -s` must print `Darwin`; otherwise stop and tell the user game-mode only works on macOS. Check `python3 --version` works (3.8+). If it's missing, tell the user to run `xcode-select --install` and stop.
2. **Get the code.**
   - If `~/.claude/skills/game-mode` doesn't exist: `git clone https://github.com/jotahcm/game-mode.git ~/.claude/skills/game-mode`
   - If it exists and is a clone of this repo (`git -C ~/.claude/skills/game-mode remote get-url origin` points at `jotahcm/game-mode`): run `git -C ~/.claude/skills/game-mode pull --ff-only`.
   - If it exists but is anything else: stop and ask the user. Don't overwrite it.
3. **Run the tests:** `cd ~/.claude/skills/game-mode && python3 -m unittest discover -s tests`. All must pass. If any fail, stop and show the user the output.
4. **Put `gamemode` on the PATH.**
   ```sh
   chmod +x ~/.claude/skills/game-mode/scripts/gamemode.py
   mkdir -p ~/.local/bin
   ```
   - If `~/.local/bin/gamemode` doesn't exist: `ln -s ~/.claude/skills/game-mode/scripts/gamemode.py ~/.local/bin/gamemode`
   - If it's already a symlink to that script, leave it. If it's anything else, stop and ask the user.
5. **Check `PATH`.** If `~/.local/bin` isn't on the user's `PATH`, append `export PATH="$HOME/.local/bin:$PATH"` to their shell rc file (`~/.zshrc` for zsh, the default on macOS; `~/.bash_profile` for bash), only if that line isn't already there. Tell the user to open a new terminal or `source` the file.
6. **Verify.** Run `~/.local/bin/gamemode status` (should print "No saved game-mode state." or the saved state), then `~/.local/bin/gamemode on --dry-run` and show the user its output: that's what game mode would close. The dry run changes nothing, but macOS may show Automation permission prompts; tell the user to click Allow.
7. **Tell the user how to use it,** in a few lines:
   - `gamemode on` closes everything, `gamemode off` restores it, `gamemode status` shows what's saved.
   - In Claude Code they can just say "game mode" or "restore everything" (the skill is now in `~/.claude/skills/game-mode`; a new session may be needed to pick it up).
   - Extra apps to keep open go in `~/.claude/game-mode/config.json` as `{"keep": ["App Name"]}`.

**To update later:** `git -C ~/.claude/skills/game-mode pull --ff-only`, then rerun the tests.

**To uninstall:** remove `~/.local/bin/gamemode`, `~/.claude/skills/game-mode` and `~/.claude/game-mode` (ask the user first if game mode is currently on: `gamemode status` shows `Phase: on`, and deleting the state loses what it would restore).
