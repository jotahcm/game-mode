import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import gamemode as gm  # noqa: E402


def write_session(config_dir, pid, **fields):
    os.makedirs(os.path.join(config_dir, "sessions"), exist_ok=True)
    data = {
        "pid": pid,
        "sessionId": f"sid-{pid}",
        "cwd": "/tmp/x",
        "kind": "interactive",
        "entrypoint": "cli",
        "status": "idle",
        "name": f"name-{pid}",
    }
    data.update(fields)
    with open(os.path.join(config_dir, "sessions", f"{pid}.json"), "w") as f:
        json.dump(data, f)


class TestDiscoverConfigDirs(unittest.TestCase):
    def test_finds_every_claude_dir_with_sessions(self):
        with tempfile.TemporaryDirectory() as home:
            for d in (".claude", ".claude-work", ".claude-science"):
                os.makedirs(os.path.join(home, d, "sessions"))
            os.makedirs(os.path.join(home, ".claude-nosessions"))
            os.makedirs(os.path.join(home, ".other", "sessions"))
            found = gm.discover_config_dirs(home)
            names = sorted(os.path.basename(p) for p in found)
            self.assertEqual(names, [".claude", ".claude-science", ".claude-work"])

    def test_includes_env_config_dir(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as extra:
            os.makedirs(os.path.join(extra, "sessions"))
            found = gm.discover_config_dirs(home, env_dir=extra)
            self.assertIn(extra, found)


class TestLoadSessions(unittest.TestCase):
    def test_only_alive_interactive_sessions(self):
        with tempfile.TemporaryDirectory() as home:
            a = os.path.join(home, ".claude")
            b = os.path.join(home, ".claude-work")
            write_session(a, 1)
            write_session(a, 2)  # dead
            write_session(b, 3, entrypoint="claude-desktop")
            write_session(b, 4, kind="sdk")  # not interactive
            alive = {1, 3, 4}
            sessions = gm.load_sessions([a, b], is_alive=lambda p: p in alive)
            by_pid = {s["pid"]: s for s in sessions}
            self.assertEqual(sorted(by_pid), [1, 3])
            self.assertEqual(by_pid[1]["config_dir"], a)
            self.assertEqual(by_pid[3]["config_dir"], b)

    def test_ignores_corrupt_files(self):
        with tempfile.TemporaryDirectory() as home:
            a = os.path.join(home, ".claude")
            write_session(a, 1)
            with open(os.path.join(a, "sessions", "9.json"), "w") as f:
                f.write("{not json")
            sessions = gm.load_sessions([a], is_alive=lambda p: True)
            self.assertEqual([s["pid"] for s in sessions], [1])


class TestCleanArgs(unittest.TestCase):
    def test_drops_resume_continue_and_prompts(self):
        argv = ["claude", "--resume", "abc", "-c", "fix the bug", "--session-id", "x"]
        self.assertEqual(gm.clean_args(argv), [])

    def test_keeps_safe_flags_with_values(self):
        argv = ["/usr/bin/claude", "--model", "opus", "--dangerously-skip-permissions",
                "-r", "abc", "--permission-mode=plan", "--add-dir", "/a"]
        self.assertEqual(
            gm.clean_args(argv),
            ["--model", "opus", "--dangerously-skip-permissions",
             "--permission-mode=plan", "--add-dir", "/a"],
        )

    def test_drops_unknown_flags(self):
        self.assertEqual(gm.clean_args(["claude", "--print", "-p", "hi"]), [])


class TestResumeCommand(unittest.TestCase):
    def test_default_config_dir_has_no_env(self):
        e = {"sessionId": "s1", "cwd": "/Users/j/my dir", "config_dir": "/Users/j/.claude", "args": []}
        cmd = gm.resume_command(e, home="/Users/j")
        self.assertEqual(cmd, "cd '/Users/j/my dir' && command claude --resume s1")

    def test_other_config_dir_sets_env_and_keeps_args(self):
        e = {"sessionId": "s2", "cwd": "/w", "config_dir": "/Users/j/.claude-work",
             "args": ["--model", "opus"]}
        cmd = gm.resume_command(e, home="/Users/j")
        self.assertEqual(
            cmd,
            "cd /w && CLAUDE_CONFIG_DIR=/Users/j/.claude-work command claude --model opus --resume s2",
        )


class TestBuildPlan(unittest.TestCase):
    def setUp(self):
        self.sessions = [
            {"pid": 10, "sessionId": "a", "cwd": "/a", "config_dir": "/h/.claude",
             "entrypoint": "cli", "status": "idle", "name": "A"},
            {"pid": 11, "sessionId": "b", "cwd": "/b", "config_dir": "/h/.claude",
             "entrypoint": "cli", "status": "busy", "name": "B"},
            {"pid": 12, "sessionId": "c", "cwd": "/c", "config_dir": "/h/.claude-work",
             "entrypoint": "cli", "status": "idle", "name": "C"},
            {"pid": 13, "sessionId": "d", "cwd": "/d", "config_dir": "/h/.claude",
             "entrypoint": "claude-desktop", "status": "idle", "name": "D"},
            {"pid": 14, "sessionId": "e", "cwd": "/e", "config_dir": "/h/.claude",
             "entrypoint": "cli", "status": "idle", "name": "E"},
        ]
        self.ttys = {10: "/dev/ttys001", 11: "/dev/ttys002", 12: "/dev/ttys003", 14: "/dev/ttys009"}
        # iTerm: window 1 has tabs [002, 001], window 2 has [003, 004 (plain shell)]
        self.layout = [["/dev/ttys002", "/dev/ttys001"], ["/dev/ttys003", "/dev/ttys004"]]

    def plan(self, self_pid=None):
        return gm.build_plan(
            self.sessions, self.layout,
            tty_of=lambda p: self.ttys.get(p),
            args_of=lambda p: ["claude"],
            self_pid=self_pid,
        )

    def test_groups_terminal_sessions_by_iterm_window_and_tab_order(self):
        windows = self.plan()["windows"]
        self.assertEqual([[e["sessionId"] for e in w] for w in windows], [["b", "a"], ["c"], ["e"]])

    def test_sessions_outside_iterm_go_to_extra_window(self):
        windows = self.plan()["windows"]
        self.assertEqual(windows[-1][0]["tty"], "/dev/ttys009")

    def test_desktop_sessions_listed_separately(self):
        plan = self.plan()
        self.assertEqual([d["sessionId"] for d in plan["desktop_sessions"]], ["d"])
        all_terminal = [e["sessionId"] for w in plan["windows"] for e in w]
        self.assertNotIn("d", all_terminal)

    def test_marks_self_session(self):
        windows = self.plan(self_pid=12)["windows"]
        flags = {e["sessionId"]: e["is_self"] for w in windows for e in w}
        self.assertTrue(flags["c"])
        self.assertFalse(flags["a"])

    def test_empty_state(self):
        plan = gm.build_plan([], [], tty_of=lambda p: None, args_of=lambda p: [], self_pid=None)
        self.assertEqual(plan["windows"], [])
        self.assertEqual(plan["desktop_sessions"], [])


class TestFindSelfPid(unittest.TestCase):
    def test_walks_parent_chain(self):
        parents = {500: 400, 400: 300, 300: 1}
        self.assertEqual(gm.find_self_pid({300, 999}, start=500, parent_of=parents.get), 300)

    def test_none_when_not_inside_a_session(self):
        parents = {500: 1}
        self.assertIsNone(gm.find_self_pid({300}, start=500, parent_of=parents.get))


class TestRemoveSessions(unittest.TestCase):
    def test_drops_sessions_and_empty_windows(self):
        state = {"windows": [[{"sessionId": "a"}], [{"sessionId": "b"}, {"sessionId": "c"}]]}
        gm.remove_sessions(state, {"a", "c"})
        self.assertEqual(state["windows"], [[{"sessionId": "b"}]])


class TestResumeScript(unittest.TestCase):
    def test_one_window_per_group_and_tabs_after_first(self):
        script = gm.iterm_resume_script([["cmd 1", "cmd 2"], ["say \"hi\""]])
        self.assertEqual(script.count("create window with default profile"), 2)
        self.assertEqual(script.count("create tab with default profile"), 1)
        self.assertIn('write text "say \\"hi\\""', script)

    def test_empty(self):
        self.assertEqual(gm.iterm_resume_script([]), "")


class TestSelectAppsToQuit(unittest.TestCase):
    def apps(self):
        return [
            {"bundle_id": "com.google.Chrome", "name": "Google Chrome", "is_game": False},
            {"bundle_id": "com.apple.finder", "name": "Finder", "is_game": False},
            {"bundle_id": "com.valvesoftware.steam", "name": "Steam", "is_game": False},
            {"bundle_id": "com.example.coolgame", "name": "Cool Game", "is_game": True},
            {"bundle_id": "com.googlecode.iterm2", "name": "iTerm2", "is_game": False},
            {"bundle_id": "com.electron.dockerdesktop", "name": "Docker Desktop", "is_game": False},
            {"bundle_id": "com.tinyspeck.slackmacgap", "name": "Slack", "is_game": False},
        ]

    def test_skips_finder_games_launchers_and_specially_handled_apps(self):
        got = [a["name"] for a in gm.select_apps_to_quit(self.apps(), keep=set())]
        self.assertEqual(got, ["Google Chrome", "Slack"])

    def test_user_keep_list_matches_name_or_bundle_id(self):
        got = [a["name"] for a in gm.select_apps_to_quit(self.apps(), keep={"slack", "com.google.chrome"})]
        self.assertEqual(got, [])


class TestBrowserWindows(unittest.TestCase):
    def test_parse_dump_skips_incognito_and_blank_tabs(self):
        dump = "W normal\nhttps://a\nchrome://newtab/\nW incognito\nhttps://secret\nW normal\nabout:blank\nW normal\nhttps://b\n"
        self.assertEqual(gm.parse_window_dump(dump), [["https://a"], ["https://b"]])

    def test_restore_only_windows_not_already_open(self):
        saved = [["https://a", "https://b"], ["https://c", "https://d", "https://e"]]
        open_urls = {"https://a", "https://b", "https://c"}
        self.assertEqual(gm.windows_to_restore(saved, open_urls), [["https://c", "https://d", "https://e"]])

    def test_restore_all_when_browser_starts_empty(self):
        saved = [["https://a"], ["https://b"]]
        self.assertEqual(gm.windows_to_restore(saved, set()), saved)

    def test_chromium_script_escapes_and_makes_tabs(self):
        script = gm.browser_restore_script("com.google.Chrome", [["https://a?q=\"x\"", "https://b"]])
        self.assertIn('application id "com.google.Chrome"', script)
        self.assertIn('make new window', script)
        self.assertIn('https://a?q=\\"x\\"', script)
        self.assertEqual(script.count("make new tab"), 1)

    def test_safari_script(self):
        script = gm.browser_restore_script("com.apple.Safari", [["https://a", "https://b"]])
        self.assertIn("make new document", script)
        self.assertEqual(script.count("make new tab"), 1)


class TestMergeState(unittest.TestCase):
    def test_merges_without_duplicates(self):
        old = {"windows": [[{"sessionId": "a"}]], "desktop_sessions": [{"sessionId": "d"}],
               "apps": [{"bundle_id": "x"}], "browsers": {"com.google.Chrome": [["https://a"]]},
               "claude_app": True, "docker": {"running": True, "containers": ["db"]}}
        new = {"windows": [[{"sessionId": "a"}, {"sessionId": "b"}]], "desktop_sessions": [],
               "apps": [{"bundle_id": "x"}, {"bundle_id": "y"}],
               "browsers": {"com.google.Chrome": [["https://a"], ["https://g"]]},
               "claude_app": False, "docker": {"running": False, "containers": []}}
        m = gm.merge_state(old, new)
        self.assertEqual([[e["sessionId"] for e in w] for w in m["windows"]], [["a"], ["b"]])
        self.assertEqual([a["bundle_id"] for a in m["apps"]], ["x", "y"])
        self.assertEqual(m["browsers"]["com.google.Chrome"], [["https://a"], ["https://g"]])
        self.assertTrue(m["claude_app"])
        self.assertEqual(m["docker"], {"running": True, "containers": ["db"]})
        self.assertEqual([d["sessionId"] for d in m["desktop_sessions"]], ["d"])


class TestSplitHostingApps(unittest.TestCase):
    def test_app_hosting_a_session_quits_later(self):
        parents = {900: 800, 800: 650, 650: 1, 901: 1}
        apps = [{"name": "Cursor", "pid": 650}, {"name": "Slack", "pid": 653}]
        now, later = gm.split_hosting_apps(apps, {900, 901}, parents.get)
        self.assertEqual([a["name"] for a in now], ["Slack"])
        self.assertEqual([a["name"] for a in later], ["Cursor"])


if __name__ == "__main__":
    unittest.main()
