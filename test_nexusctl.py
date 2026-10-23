"""Tests for the native host and CLI. Temporary homes and repositories only; no browser, no network."""

import io
import json
import os
import random
import shutil
import socket
import stat
import string
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexusctl  # noqa: E402


class Pipe:
    """A blocking byte stream that one thread writes and another reads."""

    def __init__(self):
        read_fd, write_fd = os.pipe()
        self.reader = os.fdopen(read_fd, "rb", buffering=0)
        self.writer = os.fdopen(write_fd, "wb", buffering=0)

    def read_exact(self, size):
        data = b""
        while len(data) < size:
            chunk = self.reader.read(size - len(data))
            if not chunk:
                break
            data += chunk
        return data


class ExactReader:
    def __init__(self, pipe):
        self.pipe = pipe

    def read(self, size):
        return self.pipe.read_exact(size)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nx-")
        self.saved = {k: os.environ.get(k) for k in ("NEXUS_BRIDGE_DIR", "NEXUS_BRIDGE_CONFIG", "NEXUS_CLIENT")}
        os.environ["NEXUS_BRIDGE_DIR"] = os.path.join(self.tmp, "state")
        os.environ["NEXUS_BRIDGE_CONFIG"] = os.path.join(self.tmp, "config.json")
        os.environ.pop("NEXUS_CLIENT", None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, **values):
        with open(os.environ["NEXUS_BRIDGE_CONFIG"], "w") as fh:
            json.dump(values, fh)


class FakeExtension:
    """Plays Chrome's side of the native messaging port."""

    def __init__(self, test, expect_socket=True):
        self.to_host = Pipe()
        self.from_host = Pipe()
        self.host = nexusctl.Host("chrome-extension://test/", ExactReader(self.to_host), self.from_host.writer)
        self.thread = threading.Thread(target=self.host.run, daemon=True)
        self.thread.start()
        self.hello = self.receive()
        test.assertEqual(self.hello["type"], "hello")
        if expect_socket and not self.hello["socket"]:
            self.close()
            test.fail("host has no socket: %s" % self.hello["socketError"])

    def receive(self):
        return nexusctl.read_native(ExactReader(self.from_host))

    def send(self, message):
        self.to_host.writer.write(nexusctl.encode_native(message))

    def close(self):
        if not self.to_host.writer.closed:
            self.to_host.writer.close()
        self.thread.join(5)
        for stream in (self.to_host.reader, self.from_host.reader, self.from_host.writer):
            stream.close()


def call_async(*args, **kwargs):
    box = {}

    def run():
        try:
            box["result"] = nexusctl.call(*args, **kwargs)
        except nexusctl.BridgeError as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


class FramingTest(unittest.TestCase):
    def test_round_trip_keeps_unicode(self):
        frame = nexusctl.encode_native({"title": "📥 Inbox — café"})
        self.assertEqual(nexusctl.read_native(io.BytesIO(frame)), {"title": "📥 Inbox — café"})

    def test_end_of_stream_is_none_and_truncation_fails(self):
        self.assertIsNone(nexusctl.read_native(io.BytesIO(b"")))
        frame = nexusctl.encode_native({"a": 1})
        with self.assertRaises(EOFError):
            nexusctl.read_native(io.BytesIO(frame[:-1]))

    def test_refuses_messages_over_chrome_limit(self):
        with self.assertRaises(ValueError):
            nexusctl.encode_native({"blob": "x" * (nexusctl.MAX_TO_EXTENSION + 1)})


class HostTest(Env):
    def test_socket_is_private_and_status_is_local(self):
        ext = FakeExtension(self)
        try:
            path = nexusctl.socket_path()
            self.assertEqual(ext.hello["socket"], path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
            status = nexusctl.call("host.status")
            self.assertEqual(status["origin"], "chrome-extension://test/")
        finally:
            ext.close()
        self.assertFalse(os.path.exists(nexusctl.socket_path()))

    def test_relays_request_events_and_response(self):
        ext = FakeExtension(self)
        try:
            events = []
            thread, box = call_async("plan.apply", {"ops": []}, 5, "claude", events.append)
            request = ext.receive()
            self.assertEqual(request["type"], "request")
            self.assertEqual(request["method"], "plan.apply")
            self.assertEqual(request["client"], "claude")
            ext.send({"type": "event", "id": request["id"], "event": "awaiting_approval", "data": {"summary": "s"}})
            ext.send({"type": "response", "id": request["id"], "ok": True, "result": {"applied": 2}})
            thread.join(5)
            self.assertEqual(box["result"], {"applied": 2})
            self.assertEqual(events[0]["event"], "awaiting_approval")
        finally:
            ext.close()

    def test_error_response_keeps_code(self):
        ext = FakeExtension(self)
        try:
            thread, box = call_async("plan.apply", {}, 5)
            request = ext.receive()
            ext.send({"type": "response", "id": request["id"], "ok": False,
                      "error": {"code": "rejected", "message": "No thanks"}})
            thread.join(5)
            self.assertEqual(box["error"].code, "rejected")
        finally:
            ext.close()

    def test_client_disconnect_cancels_request(self):
        ext = FakeExtension(self)
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(nexusctl.socket_path())
            conn.sendall(json.dumps({"v": 1, "method": "plan.apply", "params": {}, "timeout": 30}).encode() + b"\n")
            request = ext.receive()
            conn.close()
            cancel = ext.receive()
            self.assertEqual(cancel, {"type": "cancel", "id": request["id"]})
        finally:
            ext.close()

    def test_timeout_cancels_and_reports(self):
        ext = FakeExtension(self)
        try:
            thread, box = call_async("plan.apply", {}, 0.2)
            request = ext.receive()
            self.assertEqual(ext.receive(), {"type": "cancel", "id": request["id"]})
            thread.join(10)
            self.assertEqual(box["error"].code, "timeout")
        finally:
            ext.close()

    def test_browser_closing_fails_waiting_clients(self):
        ext = FakeExtension(self)
        thread, box = call_async("plan.apply", {}, 30)
        ext.receive()
        ext.close()
        thread.join(5)
        self.assertEqual(box["error"].code, "offline")

    def test_rejects_malformed_requests(self):
        ext = FakeExtension(self)
        try:
            for payload in (b"not json\n", b'{"v": 99, "method": "x"}\n', b'{"v": 1, "params": {}}\n'):
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(nexusctl.socket_path())
                conn.sendall(payload)
                reply = json.loads(conn.makefile().readline())
                conn.close()
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"]["code"], "bad_request")
        finally:
            ext.close()

    def test_second_browser_does_not_steal_socket_and_stale_socket_is_replaced(self):
        first = FakeExtension(self)
        try:
            second = FakeExtension(self, expect_socket=False)
            self.assertIsNone(second.hello["socket"])
            self.assertIn("another browser", second.hello["socketError"])
            second.close()
            self.assertTrue(nexusctl.socket_alive(nexusctl.socket_path()))
        finally:
            first.close()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(nexusctl.socket_path())
        stale.close()
        third = FakeExtension(self)
        try:
            self.assertEqual(third.hello["socket"], nexusctl.socket_path())
        finally:
            third.close()

    def test_offline_when_no_browser(self):
        with self.assertRaises(nexusctl.BridgeError) as caught:
            nexusctl.call("status")
        self.assertEqual(caught.exception.code, "offline")


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, check=True, env=env).stdout


class SyncTest(Env):
    def setUp(self):
        Env.setUp(self)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "bookmarks")
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", self.origin], check=True)
        subprocess.run(["git", "init", "--quiet", "-b", "main", self.repo], check=True)
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        git(self.repo, "remote", "add", "origin", self.origin)
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("# Bookmarks\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "--quiet", "-m", "init")
        git(self.repo, "push", "--quiet", "-u", "origin", "main")
        self.config = {"repo": self.repo, "push": True, "requireSecretScan": False}

    def snapshot(self, marker):
        return {"bookmarks.json": json.dumps({"marker": marker}) + "\n", "bookmarks.html": "<DL>%s</DL>\n" % marker}

    def test_commits_pushes_and_skips_unchanged(self):
        result = nexusctl.sync_snapshot(self.snapshot("a"), "sync: +1 (manual)", self.config)
        self.assertTrue(result["changed"])
        self.assertTrue(result["pushed"])
        self.assertEqual(git(self.origin, "log", "-1", "--format=%s", "main").strip(), "sync: +1 (manual)")
        again = nexusctl.sync_snapshot(self.snapshot("a"), "sync", self.config)
        self.assertFalse(again["changed"])
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").strip(), "2")

    def test_commits_only_snapshot_files(self):
        with open(os.path.join(self.repo, "README.md"), "a") as fh:
            fh.write("local edit\n")
        nexusctl.sync_snapshot(self.snapshot("a"), "sync", self.config)
        changed = git(self.repo, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(changed), ["bookmarks.html", "bookmarks.json"])
        self.assertIn("README.md", git(self.repo, "status", "--porcelain"))

    def test_failed_push_keeps_commit_and_retries_next_time(self):
        git(self.repo, "remote", "set-url", "origin", os.path.join(self.tmp, "missing.git"))
        result = nexusctl.sync_snapshot(self.snapshot("a"), "sync", self.config)
        self.assertTrue(result["changed"])
        self.assertFalse(result["pushed"])
        self.assertIn("pushError", result)
        git(self.repo, "remote", "set-url", "origin", self.origin)
        retry = nexusctl.sync_snapshot(self.snapshot("a"), "sync", self.config)
        self.assertFalse(retry["changed"])
        self.assertTrue(retry["pushed"])

    def test_rebases_when_remote_moved(self):
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "--quiet", self.origin, other], check=True)
        with open(os.path.join(other, "NOTES.md"), "w") as fh:
            fh.write("notes\n")
        git(other, "add", "NOTES.md")
        git(other, "commit", "--quiet", "-m", "notes")
        git(other, "push", "--quiet", "origin", "main")
        result = nexusctl.sync_snapshot(self.snapshot("b"), "sync", self.config)
        self.assertTrue(result["pushed"], result)
        self.assertEqual(git(self.origin, "log", "--format=%s", "main").split("\n")[:2], ["sync", "notes"])

    def test_syncs_into_a_linked_worktree(self):
        linked = os.path.join(self.tmp, "linked")
        git(self.repo, "worktree", "add", "--quiet", "-b", "linked", linked)
        result = nexusctl.sync_snapshot(self.snapshot("w"), "sync", dict(self.config, repo=linked, push=False))
        self.assertTrue(result["changed"])

    def test_commit_message_keeps_subject_and_body(self):
        self.assertEqual(nexusctl.clean_commit_message("a  b\n\n c\nd" + "x" * 300), "a b\n\nc\nd" + "x" * 239)
        self.assertEqual(nexusctl.clean_commit_message("s" * 200), "s" * 120)
        self.assertEqual(nexusctl.clean_commit_message(None), "sync: bookmarks")
        result = nexusctl.sync_snapshot(self.snapshot("m"), "agent(a): one and 1 more\n\nagent(a): one\nagent(b): two", self.config)
        self.assertTrue(result["changed"])
        self.assertEqual(git(self.repo, "log", "-1", "--format=%B").strip().split("\n"), ["agent(a): one and 1 more", "", "agent(a): one", "agent(b): two"])

    def test_rejects_a_subfolder_of_a_repository(self):
        sub = os.path.join(self.repo, "nested")
        os.makedirs(sub)
        with self.assertRaises(nexusctl.SyncError) as caught:
            nexusctl.sync_snapshot(self.snapshot("s"), "sync", dict(self.config, repo=sub))
        self.assertEqual(caught.exception.code, "no_repo")

    def test_rejects_unknown_files_and_missing_repo(self):
        with self.assertRaises(nexusctl.SyncError) as caught:
            nexusctl.sync_snapshot({"../escape": "x"}, "sync", self.config)
        self.assertEqual(caught.exception.code, "bad_snapshot")
        with self.assertRaises(nexusctl.SyncError) as caught:
            nexusctl.sync_snapshot(self.snapshot("a"), "sync", dict(self.config, repo=None))
        self.assertEqual(caught.exception.code, "no_repo")

    def test_scanner_is_required_by_default(self):
        original = nexusctl.shutil.which
        nexusctl.shutil.which = lambda name, path=None: None if name == "gitleaks" else original(name, path=path)
        try:
            with self.assertRaises(nexusctl.SyncError) as caught:
                nexusctl.sync_snapshot(self.snapshot("a"), "sync", dict(self.config, requireSecretScan=True))
            self.assertEqual(caught.exception.code, "scanner_missing")
        finally:
            nexusctl.shutil.which = original

    @unittest.skipUnless(shutil.which("gitleaks", path=nexusctl.tool_env()["PATH"]), "gitleaks not installed")
    def test_secret_in_snapshot_blocks_sync(self):
        token = "ghp_" + "".join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(36))
        files = {"bookmarks.json": json.dumps({"url": "https://example.com/?access_token=%s" % token}, indent=2) + "\n"}
        with self.assertRaises(nexusctl.SyncError) as caught:
            nexusctl.sync_snapshot(files, "sync", dict(self.config, requireSecretScan=True))
        self.assertEqual(caught.exception.code, "secret_detected")
        self.assertEqual(caught.exception.findings[0]["file"], "bookmarks.json")
        self.assertNotIn(token, json.dumps(caught.exception.findings))
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").strip(), "1")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "bookmarks.json")))

    @unittest.skipUnless(shutil.which("gitleaks", path=nexusctl.tool_env()["PATH"]), "gitleaks not installed")
    def test_repo_gitleaks_config_allowlists_false_positives(self):
        token = "ghp_" + "".join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(36))
        with open(os.path.join(self.repo, ".gitleaks.toml"), "w") as fh:
            fh.write('[extend]\nuseDefault = true\n\n[allowlist]\nregexes = [\'\'\'%s\'\'\']\n' % token)
        files = {"bookmarks.json": json.dumps({"url": "https://example.com/?access_token=%s" % token}, indent=2) + "\n"}
        result = nexusctl.sync_snapshot(files, "sync", dict(self.config, requireSecretScan=True))
        self.assertTrue(result["changed"])

    def test_host_sync_message_reports_result(self):
        self.write_config(**self.config)
        ext = FakeExtension(self)
        try:
            ext.send({"type": "sync", "id": "s1", "files": self.snapshot("c"), "message": "sync: +1 (daily)"})
            reply = ext.receive()
            self.assertEqual(reply["type"], "syncResult")
            self.assertEqual(reply["id"], "s1")
            self.assertTrue(reply["ok"], reply)
            self.assertTrue(reply["pushed"])
        finally:
            ext.close()


class CliTest(Env):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = nexusctl.run_cli(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_offline_exit_code_and_json_error(self):
        code, out, _ = self.run_cli("--json", "tree")
        self.assertEqual(code, nexusctl.EXIT_OFFLINE)
        self.assertEqual(json.loads(out)["error"]["code"], "offline")

    def test_sandbox_denial_is_named(self):
        os.makedirs(os.environ["NEXUS_BRIDGE_DIR"])
        open(nexusctl.socket_path(), "w").close()
        original = socket.socket.connect

        def denied(self, address):
            raise PermissionError(1, "Operation not permitted")
        socket.socket.connect = denied
        try:
            code, out, _ = self.run_cli("--json", "status")
        finally:
            socket.socket.connect = original
        self.assertEqual(code, nexusctl.EXIT_OFFLINE)
        self.assertEqual(json.loads(out)["error"]["code"], "sandboxed")

    def test_write_commands_become_plans(self):
        parser = nexusctl.build_parser()
        plan = nexusctl.plan_for(parser.parse_args(["move", "12", "13", "--to", "Bookmarks bar/Work"]))
        self.assertEqual(plan["ops"], [
            {"op": "move", "id": "12", "to": {"path": "Bookmarks bar/Work"}},
            {"op": "move", "id": "13", "to": {"path": "Bookmarks bar/Work"}},
        ])
        plan = nexusctl.plan_for(parser.parse_args(["add", "https://a.example", "--to", "42"]))
        self.assertEqual(plan["ops"], [{"op": "add", "url": "https://a.example", "to": {"id": "42"}}])

    def test_cli_round_trip_through_host(self):
        ext = FakeExtension(self)
        try:
            def answer():
                request = ext.receive()
                ext.send({"type": "response", "id": request["id"], "ok": True,
                          "result": {"results": [{"op": "delete"}], "sync": {"ok": True, "changed": True, "head": "abc", "pushed": True}}})
            threading.Thread(target=answer, daemon=True).start()
            code, out, _ = self.run_cli("--client", "claude", "delete", "77")
            self.assertEqual(code, 0)
            self.assertIn("Applied 1 change(s).", out)
            self.assertIn("Synced abc, pushed.", out)
        finally:
            ext.close()

    def test_bad_plan_file(self):
        path = os.path.join(self.tmp, "plan.json")
        with open(path, "w") as fh:
            fh.write("[]")
        code, _, err = self.run_cli("apply", path)
        self.assertEqual(code, nexusctl.EXIT_ERROR)
        self.assertIn("ops", err)


class InstallTest(Env):
    def test_registers_host_for_installed_browsers_and_uninstalls(self):
        home = os.path.join(self.tmp, "home")
        support = os.path.join(home, "Library", "Application Support") if sys.platform == "darwin" else os.path.join(home, ".config")
        browser = os.path.join(support, "Chromium" if sys.platform == "darwin" else "chromium")
        os.makedirs(browser)
        repo = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "--quiet", repo], check=True)
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install.sh")
        env = dict(os.environ, NEXUS_INSTALL_HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"),
                   XDG_STATE_HOME=os.path.join(home, ".local", "state"))
        ext_id = "a" * 32
        subprocess.run([script, "--repo", repo, "--no-push", ext_id], check=True, capture_output=True, env=env)
        manifest_path = os.path.join(browser, "NativeMessagingHosts", nexusctl.HOST_NAME + ".json")
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["name"], nexusctl.HOST_NAME)
        self.assertEqual(manifest["allowed_origins"], ["chrome-extension://%s/" % ext_id])
        self.assertTrue(os.access(manifest["path"], os.X_OK))
        with open(os.path.join(home, ".config", "nexus-bridge", "config.json")) as fh:
            config = json.load(fh)
        self.assertEqual(config, {"repo": os.path.realpath(repo), "push": False, "requireSecretScan": True})
        # Re-running without flags keeps earlier choices.
        subprocess.run([script, ext_id], check=True, capture_output=True, env=env)
        with open(os.path.join(home, ".config", "nexus-bridge", "config.json")) as fh:
            self.assertFalse(json.load(fh)["push"])
        bad = subprocess.run([script, "not-an-id"], capture_output=True, env=env)
        self.assertEqual(bad.returncode, 2)
        subprocess.run([script, "--uninstall"], check=True, capture_output=True, env=env)
        self.assertFalse(os.path.exists(manifest_path))


class EntryPointTest(Env):
    def test_main_runs_as_host_for_chrome_origin(self):
        """Chrome starts the host with the origin argument and closes stdin on disconnect."""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nexusctl.py")
        proc = subprocess.Popen([sys.executable, script, "chrome-extension://abc/"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        hello = nexusctl.read_native(proc.stdout)
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["protocol"], nexusctl.PROTOCOL)
        proc.stdin.close()
        self.assertEqual(proc.wait(5), 0)
        proc.stdout.close()
        proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
