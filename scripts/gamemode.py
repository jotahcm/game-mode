#!/usr/bin/env python3
"""Game mode: close every app, Claude Code session and Docker to free RAM, then restore.

  gamemode on [--dry-run] [--timeout MIN]   save state, close everything
  gamemode off                              restore everything that was closed
  gamemode status                           show saved state

Everything is discovered at run time: every regular (Dock) app, browser
windows/tabs, any ~/.claude* config dir with a sessions/ folder, the iTerm
window/tab layout, Claude.app, Docker and its running containers.
Kept open: Finder, games, game launchers, Discord, plus anything listed in
~/.claude/game-mode/config.json as {"keep": ["App Name" or "bundle.id", ...]}.
Busy Claude sessions finish their turn first; still busy at the timeout = left alone.
Running `on` again while on closes whatever was opened since and adds it to the list.
"""
import argparse
import glob
import json
import os
import plistlib
import shlex
import signal
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".claude", "game-mode")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PENDING_FILE = os.path.join(STATE_DIR, "pending.json")
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")
LOG_FILE = os.path.join(STATE_DIR, "worker.log")

CLAUDE_APP_ID = "com.anthropic.claudefordesktop"
ITERM_ID = "com.googlecode.iterm2"
DOCKER_ID = "com.electron.dockerdesktop"
SAFARI_ID = "com.apple.Safari"
CHROMIUM_IDS = {"com.google.Chrome", "com.brave.Browser", "com.microsoft.edgemac",
                "org.chromium.Chromium", "com.vivaldi.Vivaldi"}
BROWSER_IDS = CHROMIUM_IDS | {SAFARI_ID}
# Never quit by the generic pass: Finder, plus apps handled by their own steps.
SPECIAL_IDS = {"com.apple.finder", ITERM_ID, DOCKER_ID, CLAUDE_APP_ID}
# Games and launchers stay open (games are also detected via their Info.plist category).
KEEP_IDS = {"com.valvesoftware.steam", "com.epicgames.EpicGamesLauncher", "net.battle.app",
            "net.battle.bootstrapper", "com.gog.galaxy", "com.codeweavers.CrossOver",
            "com.isaacmarovitz.Whisky", "com.heroicgameslauncher.hgl", "com.nvidia.gfnpc.mall",
            "com.hnc.Discord", "com.mojang.minecraftlauncher", "org.prismlauncher.PrismLauncher",
            "com.riotgames.RiotGames.RiotClient", "com.ea.app", "com.ubisoft.uplay"}
BLANK_PREFIXES = ("chrome://newtab", "chrome://new-tab-page", "about:blank", "favorites://",
                  "edge://newtab", "brave://newtab")

# Flags worth carrying over to the resumed session. Everything else (prompts,
# --print, --resume, --continue, ...) is dropped so a resume never re-sends work.
KEEP_BOOL = {"--dangerously-skip-permissions", "--verbose", "--chrome", "--no-chrome", "--ide", "--debug"}
KEEP_VALUE = {"--model", "--permission-mode", "--add-dir", "--agent", "--settings",
              "--mcp-config", "--plugin-dir", "--fallback-model"}


# ---------- pure logic (unit tested) ----------

def empty_state():
    return {"windows": [], "desktop_sessions": [], "apps": [], "browsers": {},
            "claude_app": False, "docker": {"running": False, "containers": []}, "left_running": []}


def discover_config_dirs(home, env_dir=None):
    dirs = [d.rstrip("/") for d in glob.glob(os.path.join(home, ".claude*/"))
            if os.path.isdir(os.path.join(d, "sessions"))]
    if env_dir and os.path.isdir(os.path.join(env_dir, "sessions")) and env_dir not in dirs:
        dirs.append(env_dir)
    return sorted(dirs)


def load_sessions(config_dirs, is_alive):
    out = []
    for d in config_dirs:
        for f in glob.glob(os.path.join(d, "sessions", "*.json")):
            try:
                with open(f) as fh:
                    j = json.load(fh)
                pid = int(j["pid"])
            except (ValueError, KeyError, OSError, TypeError):
                continue
            if j.get("kind") != "interactive" or not is_alive(pid):
                continue
            j["pid"] = pid
            j["config_dir"] = d
            out.append(j)
    return out


def clean_args(argv):
    out, i, args = [], 0, argv[1:]
    while i < len(args):
        a = args[i]
        name = a.split("=", 1)[0]
        if a in KEEP_BOOL:
            out.append(a)
        elif name in KEEP_VALUE and "=" in a:
            out.append(a)
        elif a in KEEP_VALUE and i + 1 < len(args):
            out += [a, args[i + 1]]
            i += 1
        elif a in ("--resume", "-r", "--session-id", "--name", "-n") and i + 1 < len(args) \
                and not args[i + 1].startswith("-"):
            i += 1  # skip its value
        i += 1
    return out


def resume_command(entry, home=HOME):
    parts = ["cd", shlex.quote(entry["cwd"]), "&&"]
    if os.path.normpath(entry["config_dir"]) != os.path.join(home, ".claude"):
        parts.append("CLAUDE_CONFIG_DIR=" + shlex.quote(entry["config_dir"]))
    parts += ["command", "claude"] + [shlex.quote(a) for a in entry.get("args", [])]
    parts += ["--resume", shlex.quote(entry["sessionId"])]
    return " ".join(parts)


def build_plan(sessions, layout, tty_of, args_of, self_pid):
    terminal, desktop = [], []
    for s in sessions:
        base = {k: s.get(k) for k in ("pid", "sessionId", "cwd", "config_dir", "name", "entrypoint")}
        if s.get("entrypoint") == "claude-desktop":
            desktop.append(base)
            continue
        base.update(tty=tty_of(s["pid"]), args=clean_args(args_of(s["pid"]) or ["claude"]),
                    is_self=s["pid"] == self_pid)
        terminal.append(base)

    by_tty = {e["tty"]: e for e in terminal if e["tty"]}
    windows, placed = [], set()
    for win in layout:
        group = [by_tty[t] for t in win if t in by_tty]
        if group:
            windows.append(group)
            placed.update(e["pid"] for e in group)
    leftovers = [e for e in terminal if e["pid"] not in placed]
    if leftovers:
        windows.append(leftovers)
    return {"windows": windows, "desktop_sessions": desktop}


def find_self_pid(session_pids, start, parent_of):
    pid, seen = start, set()
    while pid and pid > 1 and pid not in seen:
        if pid in session_pids:
            return pid
        seen.add(pid)
        pid = parent_of(pid)
    return None


def remove_sessions(state, session_ids):
    state["windows"] = [[e for e in w if e["sessionId"] not in session_ids] for w in state["windows"]]
    state["windows"] = [w for w in state["windows"] if w]


def select_apps_to_quit(apps, keep):
    keep = {k.lower() for k in keep}
    return [a for a in apps
            if a["bundle_id"] not in SPECIAL_IDS and a["bundle_id"] not in KEEP_IDS
            and not a.get("is_game")
            and a["bundle_id"].lower() not in keep and a["name"].lower() not in keep]


def split_hosting_apps(apps, session_pids, parent_of):
    """Apps that host a Claude session (e.g. Cursor's terminal) must quit after the session stops."""
    hosts = set()
    for pid in session_pids:
        p, seen = parent_of(pid), set()
        while p and p > 1 and p not in seen:
            hosts.add(p)
            seen.add(p)
            p = parent_of(p)
    now = [a for a in apps if a.get("pid") not in hosts]
    later = [a for a in apps if a.get("pid") in hosts]
    return now, later


def _blank(url):
    return not url or url == "missing value" or url.startswith(BLANK_PREFIXES)


def parse_window_dump(text):
    """'W <mode>' lines start a window, other lines are tab URLs. Drops incognito + blank tabs."""
    windows, cur = [], None
    for line in text.splitlines():
        if line.startswith("W "):
            cur = None if line[2:].strip() == "incognito" else []
            if cur is not None:
                windows.append(cur)
        elif cur is not None and not _blank(line.strip()):
            cur.append(line.strip())
    return [w for w in windows if w]


def windows_to_restore(saved, open_urls):
    """A saved window counts as already open when at least half its tabs are open."""
    return [w for w in saved if sum(u in open_urls for u in w) * 2 < len(w)]


def _as(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def browser_restore_script(bundle_id, windows):
    if not windows:
        return ""
    lines = [f'tell application id "{bundle_id}"']
    for w in windows:
        if bundle_id == SAFARI_ID:
            lines += [f"make new document with properties {{URL:{_as(w[0])}}}", "tell front window"]
        else:
            lines += ["set w to make new window", f"set URL of active tab of w to {_as(w[0])}", "tell w"]
        lines += [f"make new tab with properties {{URL:{_as(u)}}}" for u in w[1:]]
        lines.append("end tell")
    lines.append("end tell")
    return "\n".join(lines)


def iterm_resume_script(window_cmds):
    if not window_cmds:
        return ""
    lines = ['tell application "iTerm2"', "activate"]
    for cmds in window_cmds:
        lines += ["set w to (create window with default profile)",
                  f"tell current session of w to write text {_as(cmds[0])}"]
        for c in cmds[1:]:
            lines += ["tell w to set t to (create tab with default profile)",
                      f"tell current session of t to write text {_as(c)}"]
    lines.append("end tell")
    return "\n".join(lines)


def merge_state(old, new):
    m = dict(old)
    seen = {e["sessionId"] for w in old.get("windows", []) for e in w}
    m["windows"] = [list(w) for w in old.get("windows", [])]
    for w in new.get("windows", []):
        fresh = [e for e in w if e["sessionId"] not in seen]
        seen.update(e["sessionId"] for e in fresh)
        if fresh:
            m["windows"].append(fresh)
    ds = {d["sessionId"] for d in old.get("desktop_sessions", [])}
    m["desktop_sessions"] = old.get("desktop_sessions", []) + \
        [d for d in new.get("desktop_sessions", []) if d["sessionId"] not in ds]
    ids = {a["bundle_id"] for a in old.get("apps", [])}
    m["apps"] = old.get("apps", []) + [a for a in new.get("apps", []) if a["bundle_id"] not in ids]
    m["browsers"] = {k: list(v) for k, v in old.get("browsers", {}).items()}
    for bid, wins in new.get("browsers", {}).items():
        have = m["browsers"].setdefault(bid, [])
        have += [w for w in wins if w not in have]
    m["claude_app"] = bool(old.get("claude_app") or new.get("claude_app"))
    od, nd = old.get("docker", {}), new.get("docker", {})
    containers = list(od.get("containers", []))
    containers += [c for c in nd.get("containers", []) if c not in containers]
    m["docker"] = {"running": bool(od.get("running") or nd.get("running")), "containers": containers}
    m["left_running"] = sorted(set(old.get("left_running", [])) | set(new.get("left_running", [])))
    return m


# ---------- system helpers ----------

def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 1, ""


def osa(script, timeout=30):
    return sh(["osascript", "-e", script], timeout)


def is_alive(pid):
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def ps_field(pid, field):
    rc, out = sh(["ps", "-ww", "-o", f"{field}=", "-p", str(pid)])
    return out if rc == 0 else ""


def tty_of(pid):
    t = ps_field(pid, "tty")
    return None if not t or t.startswith("?") else "/dev/" + t


def args_of(pid):
    try:
        return shlex.split(ps_field(pid, "args"))
    except ValueError:
        return ps_field(pid, "args").split()


def parent_of(pid):
    p = ps_field(pid, "ppid").strip()
    return int(p) if p.isdigit() else None


def app_running(bundle_id):
    return bundle_id in {a["bundle_id"] for a in running_apps()}


def _is_game(app_path):
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            return "games" in str(plistlib.load(f).get("LSApplicationCategoryType", ""))
    except Exception:
        return False


def running_apps():
    """Regular (Dock) apps: bundle_id, name, path, pid, is_game."""
    js = ('ObjC.import("AppKit"); var a=$.NSWorkspace.sharedWorkspace.runningApplications, o=[];'
          'for (var i=0;i<a.count;i++){var x=a.objectAtIndex(i);'
          ' if (x.activationPolicy==0 && x.bundleIdentifier.js) o.push([ObjC.unwrap(x.bundleIdentifier),'
          ' ObjC.unwrap(x.localizedName), ObjC.unwrap(x.bundleURL.path), x.processIdentifier].join("\\t"));}'
          ' o.join("\\n")')
    apps = []
    for line in sh(["osascript", "-l", "JavaScript", "-e", js])[1].splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            bid, name, path, pid = parts
            apps.append({"bundle_id": bid, "name": name, "path": path,
                         "pid": int(pid) if pid.isdigit() else None, "is_game": _is_game(path)})
    return apps


def load_keep():
    try:
        with open(CONFIG_FILE) as f:
            return set(json.load(f).get("keep", []))
    except (OSError, ValueError):
        return set()


def iterm_layout():
    if not app_running(ITERM_ID):
        return []
    script = '''tell application "iTerm2"
set out to ""
repeat with w in windows
set out to out & "W" & linefeed
repeat with t in tabs of w
repeat with s in sessions of t
set out to out & (tty of s) & linefeed
end repeat
end repeat
end repeat
return out
end tell'''
    layout = []
    for line in osa(script)[1].splitlines():
        if line == "W":
            layout.append([])
        elif line and layout:
            layout[-1].append(line)
    return layout


def iterm_window_count():
    if not app_running(ITERM_ID):
        return 0
    out = osa('tell application "iTerm2" to count windows')[1]
    return int(out) if out.isdigit() else 0


def capture_browser(bundle_id):
    """Open windows -> list of URL lists. Only call while the browser is running."""
    if bundle_id == SAFARI_ID:
        body = ('repeat with w in windows\ntry\nset tl to tabs of w\nset out to out & "W normal" & linefeed\n'
                'repeat with t in tl\nset u to URL of t\nif u is missing value then set u to ""\n'
                'set out to out & u & linefeed\nend repeat\nend try\nend repeat')
    else:
        body = ('repeat with w in windows\nset out to out & "W " & (mode of w) & linefeed\n'
                'repeat with t in tabs of w\nset out to out & (URL of t) & linefeed\nend repeat\nend repeat')
    script = f'tell application id "{bundle_id}"\nset out to ""\n{body}\nreturn out\nend tell'
    return parse_window_dump(osa(script)[1])


def wait_browser_ready(bundle_id, max_wait=25):
    """Wait until the browser's window count settles (it may be restoring its own session)."""
    last, stable, start = -1, 0, time.time()
    while time.time() - start < max_wait:
        out = osa(f'tell application id "{bundle_id}" to count windows')[1]
        n = int(out) if out.isdigit() else 0
        stable = stable + 1 if n == last else 0
        last = n
        if (n > 0 and stable >= 3) or (n == 0 and time.time() - start > 10):
            return
        time.sleep(1)


def close_blank_chromium_windows(bundle_id):
    osa(f'''tell application id "{bundle_id}"
repeat with i from (count windows) to 1 by -1
set w to window i
set blank to true
repeat with t in tabs of w
set u to URL of t
if not (u starts with "chrome://newtab" or u starts with "chrome://new-tab-page" or u starts with "about:blank") then set blank to false
end repeat
if blank then close w
end repeat
end tell''')


def quit_apps(apps, timeout=30):
    """Polite quit in parallel; an app showing a 'save changes?' dialog simply stays open."""
    if not apps:
        return [], []
    procs = [subprocess.Popen(["osascript", "-e", f"with timeout of {timeout} seconds",
                               "-e", f'tell application id "{a["bundle_id"]}" to quit',
                               "-e", "end timeout"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for a in apps]
    ids = {a["bundle_id"] for a in apps}
    deadline = time.time() + timeout + 5
    while time.time() < deadline and ids & {a["bundle_id"] for a in running_apps()}:
        time.sleep(1)
    for p in procs:
        if p.poll() is None:
            p.kill()
    still = {a["bundle_id"] for a in running_apps()}
    return [a for a in apps if a["bundle_id"] not in still], [a for a in apps if a["bundle_id"] in still]


def docker_up():
    return sh(["docker", "info", "--format", "{{.ServerVersion}}"], 15)[0] == 0


def notify(msg):
    osa(f'display notification {_as(msg)} with title "Game mode"')


def log(msg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(time.strftime("%H:%M:%S ") + msg + "\n")


def _write_json(path, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(state):
    _write_json(STATE_FILE, state)


def load_state():
    return _read_json(STATE_FILE)


def session_status(entry):
    """Live status from the session's own file: 'busy', 'idle' or 'gone'."""
    if not is_alive(entry["pid"]):
        return "gone"
    data = _read_json(os.path.join(entry["config_dir"], "sessions", f"{entry['pid']}.json"))
    return (data or {}).get("status", "idle")


def stop_pid(pid, grace=10):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.time() + grace
        while time.time() < deadline and is_alive(pid):
            time.sleep(0.3)
        if not is_alive(pid):
            return


def close_iterm_ttys(ttys):
    if not ttys:
        return
    targets = "{" + ", ".join(_as(t) for t in ttys) + "}"
    osa(f'''tell application "iTerm2"
set targets to {targets}
repeat {len(ttys) + 2} times
set found to missing value
repeat with w in windows
repeat with t in tabs of w
repeat with s in sessions of t
if targets contains (tty of s) then
set found to s
exit repeat
end if
end repeat
if found is not missing value then exit repeat
end repeat
if found is not missing value then exit repeat
end repeat
if found is missing value then exit repeat
close found
delay 0.2
end repeat
end tell''', timeout=60)


# ---------- commands ----------

def snapshot():
    dirs = discover_config_dirs(HOME, os.environ.get("CLAUDE_CONFIG_DIR"))
    sessions = load_sessions(dirs, is_alive)
    pids = {s["pid"] for s in sessions}
    self_pid = find_self_pid(pids, os.getpid(), parent_of)
    batch = build_plan(sessions, iterm_layout(), tty_of, args_of, self_pid)
    apps = running_apps()
    running = {a["bundle_id"] for a in apps}
    now, later = split_hosting_apps(select_apps_to_quit(apps, load_keep()), pids, parent_of)
    docker = docker_up()
    containers = [c for c in sh(["docker", "ps", "--format", "{{.Names}}"])[1].splitlines() if c] if docker else []
    batch.update(apps_now=now, apps_later=later, claude_app=CLAUDE_APP_ID in running,
                 docker={"running": docker, "containers": containers},
                 kept=[a["name"] for a in apps if a not in now and a not in later
                       and a["bundle_id"] not in SPECIAL_IDS - {"com.apple.finder"}])
    return batch


def describe(batch):
    lines = []
    for i, w in enumerate(batch["windows"], 1):
        lines.append(f"Claude sessions, iTerm window {i}:")
        for e in w:
            me = "  (this session, closes after its reply)" if e.get("is_self") else ""
            lines.append(f"  - {e['name']}  [{e['cwd']}]{me}")
    apps = batch.get("apps_now", []) + batch.get("apps_later", [])
    if apps:
        lines.append("Apps: " + ", ".join(a["name"] for a in apps)
                     + ("  (browser windows/tabs saved)" if any(a["bundle_id"] in BROWSER_IDS for a in apps) else ""))
    if batch.get("claude_app"):
        names = ", ".join(d["name"] for d in batch["desktop_sessions"]) or "no Code sessions"
        lines.append(f"Claude.app ({names})")
    d = batch["docker"]
    if d["running"]:
        lines.append(f"Docker + {len(d['containers'])} running containers: {', '.join(d['containers']) or 'none'}")
    if batch.get("kept"):
        lines.append("Kept open: " + ", ".join(batch["kept"]))
    return "\n".join(lines) or "Nothing to close."


def cmd_on(args):
    existing = load_state()
    if existing and existing.get("phase") == "pausing" and is_alive(existing.get("worker_pid")):
        print("Game mode is still closing things (see `gamemode status`). Try again in a minute.")
        return 1
    batch = snapshot()
    batch["timeout_min"] = args.timeout
    if args.dry_run:
        print("Dry run, would close:\n" + describe(batch))
        return 0
    adding = bool(existing and existing.get("phase") in ("on", "pausing"))
    state = existing if adding else dict(empty_state(), created=time.strftime("%Y-%m-%d %H:%M:%S"))
    _write_json(PENDING_FILE, batch)
    with open(LOG_FILE, "a") as logf:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_worker"],
                             stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
    state.update(phase="pausing", worker_pid=p.pid)
    save_state(state)
    print(("Game mode already on, adding what's open now:\n" if adding else "Game mode starting:\n")
          + describe(batch))
    print(f"\nBusy Claude sessions finish their turn first (up to {args.timeout:g} min). "
          "A notification appears when done. Restore with: gamemode off")
    return 0


def cmd_worker():
    batch = _read_json(PENDING_FILE)
    if not batch:
        return 1
    result = empty_state()
    batch["result"] = result
    log("---- game mode on")

    def checkpoint():
        _write_json(PENDING_FILE, batch)

    # 1. Apps go right away; browser tabs are captured just before quitting.
    for a in batch["apps_now"] + batch["apps_later"]:
        if a["bundle_id"] in BROWSER_IDS and app_running(a["bundle_id"]):
            result["browsers"][a["bundle_id"]] = capture_browser(a["bundle_id"])
    checkpoint()
    quit_ok, kept = quit_apps(batch["apps_now"])
    result["apps"] += quit_ok
    checkpoint()
    log(f"quit apps: {[a['name'] for a in quit_ok]}; refused: {[a['name'] for a in kept]}")

    # 2. Let busy Claude sessions finish their turn (the invoking one goes idle after its reply).
    terminal = [e for w in batch["windows"] for e in w]
    pending = terminal + batch["desktop_sessions"]
    deadline = time.time() + batch.get("timeout_min", 15) * 60
    while time.time() < deadline:
        pending = [e for e in pending if session_status(e) == "busy"]
        if not pending:
            break
        time.sleep(3)
    still_busy = {e["sessionId"] for e in pending}
    time.sleep(2)  # let the last reply finish rendering

    # 3. Stop idle terminal sessions and close their tabs.
    ttys = []
    for e in terminal:
        if e["sessionId"] not in still_busy:
            stop_pid(e["pid"])
            log(f"stopped {e['name']} ({e['sessionId']})")
            if e.get("tty"):
                ttys.append(e["tty"])
    close_iterm_ttys(ttys)
    result["windows"] = [list(w) for w in batch["windows"]]
    remove_sessions(result, still_busy)
    result["left_running"] = sorted(still_busy)
    checkpoint()

    # 4. Apps that hosted a session (e.g. Cursor's terminal), and iTerm once it is empty.
    later_ok, later_kept = quit_apps(batch["apps_later"])
    result["apps"] += later_ok
    kept += later_kept
    if app_running(ITERM_ID) and iterm_window_count() == 0:
        ok, _ = quit_apps([{"bundle_id": ITERM_ID, "name": "iTerm2"}])
        result["apps"] += ok

    # 5. Claude.app, unless one of its sessions is mid-turn.
    desktop_busy = any(d["sessionId"] in still_busy for d in batch["desktop_sessions"])
    if batch["claude_app"] and not desktop_busy:
        osa(f'tell application id "{CLAUDE_APP_ID}" to quit')
        result["claude_app"] = True
        result["desktop_sessions"] = batch["desktop_sessions"]
        log("quit Claude.app")

    # 6. Docker last: busy sessions may have been using it.
    if batch["docker"]["running"]:
        if sh(["docker", "desktop", "stop"], 120)[0] != 0:
            osa('tell application "Docker" to quit', timeout=60)
        result["docker"] = batch["docker"]
        log("stopped Docker")

    state = merge_state(load_state() or empty_state(), result)
    state.pop("worker_pid", None)
    state.update(phase="on", updated=time.strftime("%Y-%m-%d %H:%M:%S"))
    save_state(state)
    os.remove(PENDING_FILE)

    msg = "Everything closed. Have fun!"
    left = [a["name"] for a in kept] + [f"{len(still_busy)} busy Claude session(s)"] * bool(still_busy)
    if left:
        msg = "Closed everything except: " + ", ".join(left)
    notify(msg)
    log(msg)
    return 0


def cmd_off(_args):
    state = load_state()
    if not state or state.get("phase") not in ("pausing", "on"):
        print("Game mode is not on, nothing to restore.")
        return 0
    if is_alive(state.get("worker_pid")):
        os.kill(state["worker_pid"], signal.SIGTERM)
    pending = _read_json(PENDING_FILE)
    if pending and pending.get("result"):  # stopped mid-way: restore what it had closed so far
        state = merge_state(state, pending["result"])
    if pending:
        os.remove(PENDING_FILE)

    # Docker takes longest, so start it first.
    d = state["docker"]
    if d["running"] and not docker_up():
        subprocess.Popen(["docker", "desktop", "start"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    dirs = discover_config_dirs(HOME, os.environ.get("CLAUDE_CONFIG_DIR"))
    live_sessions = {s["sessionId"] for s in load_sessions(dirs, is_alive)}
    window_cmds = [c for c in ([resume_command(e) for e in w if e["sessionId"] not in live_sessions]
                               for w in state["windows"]) if c]

    # Apps: anything already running (opened during the game) is left as is.
    running = {a["bundle_id"] for a in running_apps()}
    launched = []
    for a in state["apps"]:
        bid = a["bundle_id"]
        if bid in running or (bid == ITERM_ID and window_cmds):
            continue
        sh(["open", "-b", bid])
        launched.append(bid)
    if state.get("claude_app") and CLAUDE_APP_ID not in running:
        sh(["open", "-b", CLAUDE_APP_ID])
        launched.append(CLAUDE_APP_ID)
    print(f"Reopened {len(launched)} app(s).")

    # Browsers: reopen only saved windows that aren't open already.
    for bid, saved in state.get("browsers", {}).items():
        if not saved:
            continue
        if bid not in running and bid not in launched:
            sh(["open", "-b", bid])
            launched.append(bid)
        wait_browser_ready(bid)
        open_urls = {u for w in capture_browser(bid) for u in w}
        todo = windows_to_restore(saved, open_urls)
        if todo:
            osa(browser_restore_script(bid, todo), timeout=180)
            if bid in launched and bid in CHROMIUM_IDS:
                close_blank_chromium_windows(bid)
        print(f"{bid}: reopened {len(todo)} of {len(saved)} saved window(s), the rest were already open.")

    if window_cmds:
        osa(iterm_resume_script(window_cmds), timeout=120)
    print(f"Resumed {sum(map(len, window_cmds))} Claude session(s) in {len(window_cmds)} iTerm window(s).")

    if d["running"]:
        for _ in range(90):
            if docker_up():
                break
            time.sleep(2)
        if docker_up():
            live = set(sh(["docker", "ps", "--format", "{{.Names}}"])[1].splitlines())
            todo = [c for c in d["containers"] if c not in live]
            if todo:
                sh(["docker", "start"] + todo, 180)
            print(f"Docker is up; restarted {len(todo)} container(s).")
        else:
            print("Docker did not come up within 3 minutes, start it manually.")

    state.update(phase="resumed", resumed=time.strftime("%Y-%m-%d %H:%M:%S"))
    state.pop("worker_pid", None)
    save_state(state)
    return 0


def cmd_status(_args):
    state = load_state()
    if not state:
        print("No saved game-mode state.")
        return 0
    print(f"Phase: {state['phase']} (since {state.get('created')})")
    n = sum(map(len, state.get("windows", [])))
    print(f"Saved: {n} Claude session(s), apps: {', '.join(a['name'] for a in state.get('apps', [])) or 'none'}")
    for bid, wins in state.get("browsers", {}).items():
        print(f"  {bid}: {len(wins)} window(s), {sum(map(len, wins))} tab(s)")
    if state.get("claude_app"):
        print("  Claude.app")
    if state.get("docker", {}).get("running"):
        print(f"  Docker + {len(state['docker']['containers'])} container(s)")
    if state.get("left_running"):
        print(f"Left running (were busy): {', '.join(state['left_running'])}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    on = sub.add_parser("on")
    on.add_argument("--dry-run", action="store_true")
    on.add_argument("--timeout", type=float, default=15, help="minutes to wait for busy sessions")
    sub.add_parser("off")
    sub.add_parser("status")
    sub.add_parser("_worker")
    a = ap.parse_args()
    if a.cmd == "_worker":
        return cmd_worker()
    return {"on": cmd_on, "off": cmd_off, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
