#!/usr/bin/env python3
"""Nexus bridge: lets local agents and scripts work with the sidebar's bookmarks.

One file, two roles:

* Native messaging host. Chrome starts it with the caller's origin
  (``chrome-extension://<id>/``) as the first argument and talks over
  stdin/stdout. The host opens a private Unix socket so local clients can reach
  the extension, and it writes bookmark snapshots into a Git repository.
* ``nexusctl`` command line. It connects to that socket, sends one request and
  prints the answer. Every change goes through an approval card in the sidebar
  unless the user has allowed it there.

Standard library only; runs on the macOS system Python (3.9).
"""

import argparse
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

VERSION = "1.0.0"
PROTOCOL = 1
HOST_NAME = "com.nexus_ai_browser.bridge"

# Chrome limits host-to-extension messages to 1 MB; stay well below it.
MAX_TO_EXTENSION = 900 * 1024
# Extension-to-host messages may be up to 64 MiB; snapshots are far smaller.
MAX_FROM_EXTENSION = 64 * 1024 * 1024
MAX_CLIENT_REQUEST = 900 * 1024
SNAPSHOT_FILES = ("bookmarks.json", "bookmarks.html", "pinned.json")
SOCKET_NAME = "bridge.sock"
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_APPROVAL_TIMEOUT = 600.0
GIT_TIMEOUT = 60
SYNC_GRACE = 300  # apply, a running sync and a queued follow-up sync (2 x 120 s)

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_OFFLINE, EXIT_REJECTED, EXIT_TIMEOUT = 0, 1, 2, 3, 4, 5
ERROR_EXIT = {"rejected": EXIT_REJECTED, "timeout": EXIT_TIMEOUT, "offline": EXIT_OFFLINE, "sandboxed": EXIT_OFFLINE}


def state_dir():
    override = os.environ.get("NEXUS_BRIDGE_DIR")
    if override:
        return override
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "NexusBridge")
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"), "nexus-bridge")


def config_path():
    override = os.environ.get("NEXUS_BRIDGE_CONFIG")
    if override:
        return override
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "nexus-bridge", "config.json")


def socket_path():
    return os.path.join(state_dir(), SOCKET_NAME)


def load_config():
    defaults = {"repo": None, "push": True, "requireSecretScan": True}
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    if isinstance(data.get("repo"), str) and data["repo"].strip():
        defaults["repo"] = os.path.expanduser(data["repo"].strip())
    for key in ("push", "requireSecretScan"):
        if isinstance(data.get(key), bool):
            defaults[key] = data[key]
    return defaults


def tool_env():
    """Chrome starts hosts with a minimal PATH; add the usual tool folders."""
    env = dict(os.environ)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for extra in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        if extra not in parts:
            parts.append(extra)
    env["PATH"] = os.pathsep.join(parts)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# --------------------------------------------------------------------------
# Native messaging framing


def read_native(stream):
    header = stream.read(4)
    if not header:
        return None
    if len(header) < 4:
        raise EOFError("truncated native message header")
    (length,) = struct.unpack("=I", header)
    if length > MAX_FROM_EXTENSION:
        raise ValueError("native message too large")
    body = stream.read(length)
    if len(body) < length:
        raise EOFError("truncated native message")
    return json.loads(body.decode("utf-8"))


def encode_native(message):
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_TO_EXTENSION:
        raise ValueError("message for the extension exceeds the size limit")
    return struct.pack("=I", len(body)) + body


# --------------------------------------------------------------------------
# Host


class Host:
    def __init__(self, origin, stdin, stdout):
        self.origin = origin
        self.stdin = stdin
        self.stdout = stdout
        self.write_lock = threading.Lock()
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.sync_lock = threading.Lock()
        self.server = None
        self.socket_error = None
        self.stopping = threading.Event()

    # -- extension side ------------------------------------------------------

    def send(self, message):
        frame = encode_native(message)
        with self.write_lock:
            self.stdout.write(frame)
            self.stdout.flush()

    def run(self):
        self.open_socket()
        self.send({
            "type": "hello",
            "version": VERSION,
            "protocol": PROTOCOL,
            "socket": socket_path() if self.server else None,
            "socketError": self.socket_error,
            "repo": load_config()["repo"],
        })
        try:
            while not self.stopping.is_set():
                message = read_native(self.stdin)
                if message is None:
                    break
                self.on_extension_message(message)
        except (EOFError, ValueError, OSError) as exc:
            log("host input closed: %s" % exc)
        finally:
            self.shutdown()

    def on_extension_message(self, message):
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind in ("response", "event"):
            with self.pending_lock:
                waiter = self.pending.get(message.get("id"))
            if waiter is not None:
                waiter.deliver(message)
        elif kind == "sync":
            threading.Thread(target=self.handle_sync, args=(message,), daemon=True).start()

    def handle_sync(self, message):
        reply = {"type": "syncResult", "id": message.get("id")}
        try:
            with self.sync_lock:
                reply.update(sync_snapshot(message.get("files"), message.get("message")))
        except SyncError as exc:
            reply.update({"ok": False, "error": {"code": exc.code, "message": str(exc), "findings": exc.findings}})
        except Exception as exc:  # report, never crash the host
            reply.update({"ok": False, "error": {"code": "sync_failed", "message": str(exc)}})
        try:
            self.send(reply)
        except (OSError, ValueError) as exc:
            log("could not report sync result: %s" % exc)

    # -- client side ---------------------------------------------------------

    def open_socket(self):
        directory = state_dir()
        path = socket_path()
        if len(path.encode("utf-8")) >= 100:
            self.socket_error = "socket path too long: %s" % path
            return
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError as exc:
            self.socket_error = "cannot create %s: %s" % (directory, exc)
            return
        if os.path.exists(path):
            if socket_alive(path):
                # Another browser already serves agents; keep syncing, skip the socket.
                self.socket_error = "another browser is already connected"
                return
            try:
                os.unlink(path)
            except OSError as exc:
                self.socket_error = "cannot remove stale socket: %s" % exc
                return
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            server.bind(path)
        except OSError as exc:
            self.socket_error = "cannot bind socket: %s" % exc
            server.close()
            return
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)
        server.listen(16)
        self.server = server
        threading.Thread(target=self.accept_loop, daemon=True).start()

    def accept_loop(self):
        while not self.stopping.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.serve_client, args=(conn,), daemon=True).start()

    def serve_client(self, conn):
        request_id = "r" + uuid.uuid4().hex[:12]
        waiter = Waiter()
        try:
            conn.settimeout(10)
            raw = read_line(conn, MAX_CLIENT_REQUEST)
            request = json.loads(raw.decode("utf-8"))
            method, params, client, timeout = validate_client_request(request)
            if method == "host.status":
                write_line(conn, {"ok": True, "result": self.status()})
                return
            with self.pending_lock:
                self.pending[request_id] = waiter
            self.send({
                "type": "request",
                "id": request_id,
                "method": method,
                "params": params,
                "client": client,
                "deadline": int((time.time() + timeout) * 1000),
            })
            conn.settimeout(None)
            watcher = threading.Thread(target=watch_disconnect, args=(conn, waiter), daemon=True)
            watcher.start()
            # The extension answers approvals itself at the deadline; the extra
            # time covers applying the plan and the Git sync that follows it.
            deadline = time.time() + timeout + SYNC_GRACE
            while True:
                message = waiter.next(max(0.0, deadline - time.time()))
                if message is None:
                    if waiter.disconnected:
                        self.send({"type": "cancel", "id": request_id})
                        return
                    if waiter.closed:
                        write_line(conn, error_reply("offline", "The browser closed the bridge."))
                        return
                    self.send({"type": "cancel", "id": request_id})
                    write_line(conn, error_reply("timeout", "No answer from the browser in time."))
                    return
                if message.get("type") == "event":
                    write_line(conn, {"event": message.get("event"), "data": message.get("data")})
                    continue
                if message.get("ok"):
                    write_line(conn, {"ok": True, "result": message.get("result")})
                else:
                    error = message.get("error") or {}
                    write_line(conn, error_reply(error.get("code", "failed"), error.get("message", "Request failed."), error.get("details")))
                return
        except ClientError as exc:
            safe_write(conn, error_reply(exc.code, str(exc)))
        except (ValueError, UnicodeDecodeError):
            safe_write(conn, error_reply("bad_request", "Send one JSON request per line."))
        except (OSError, socket.timeout):
            pass
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)
            try:
                conn.close()
            except OSError:
                pass

    def status(self):
        config = load_config()
        return {
            "version": VERSION,
            "protocol": PROTOCOL,
            "origin": self.origin,
            "socket": socket_path(),
            "repo": config["repo"],
            "push": config["push"],
            "pid": os.getpid(),
        }

    def shutdown(self):
        self.stopping.set()
        with self.pending_lock:
            waiters = list(self.pending.values())
        for waiter in waiters:
            waiter.close()
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
            try:
                os.unlink(socket_path())
            except OSError:
                pass


class Waiter:
    """Messages for one client request, plus close and disconnect signals."""

    def __init__(self):
        self.cond = threading.Condition()
        self.queue = []
        self.closed = False
        self.disconnected = False

    def deliver(self, message):
        with self.cond:
            self.queue.append(message)
            self.cond.notify_all()

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def drop(self):
        with self.cond:
            self.disconnected = True
            self.cond.notify_all()

    def next(self, timeout):
        end = time.time() + timeout
        with self.cond:
            while not self.queue and not self.closed and not self.disconnected:
                remaining = end - time.time()
                if remaining <= 0:
                    return None
                self.cond.wait(remaining)
            if self.queue:
                return self.queue.pop(0)
            return None


class ClientError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


def validate_client_request(request):
    if not isinstance(request, dict) or request.get("v") != PROTOCOL:
        raise ClientError("bad_request", "Unsupported request; update nexusctl.")
    method = request.get("method")
    if not isinstance(method, str) or not method or len(method) > 64:
        raise ClientError("bad_request", "Missing method.")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise ClientError("bad_request", "params must be an object.")
    client = request.get("client") or "cli"
    if not isinstance(client, str):
        raise ClientError("bad_request", "client must be a string.")
    client = "".join(ch for ch in client if ch.isalnum() or ch in "-_. ")[:40] or "cli"
    timeout = request.get("timeout", DEFAULT_READ_TIMEOUT)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = DEFAULT_READ_TIMEOUT
    return method, params, client, min(float(timeout), 3600.0)


def watch_disconnect(conn, waiter):
    """A client that closes its connection cancels its request."""
    try:
        while True:
            chunk = conn.recv(1024)
            if not chunk:
                break
    except OSError:
        pass
    waiter.drop()


def read_line(conn, limit):
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise ClientError("too_large", "Request is too large.")
    line = data.split(b"\n", 1)[0]
    if not line:
        raise ClientError("bad_request", "Empty request.")
    return line


def write_line(conn, payload):
    conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def safe_write(conn, payload):
    try:
        write_line(conn, payload)
    except OSError:
        pass


def error_reply(code, message, details=None):
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}


def socket_alive(path):
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def log(text):
    # stdout belongs to Chrome; diagnostics go to stderr (Chrome's log).
    try:
        sys.stderr.write("nexus-bridge: %s\n" % text)
        sys.stderr.flush()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Git sync


class SyncError(Exception):
    def __init__(self, code, message, findings=None):
        Exception.__init__(self, message)
        self.code = code
        self.findings = findings or []


def git(repo, *args, **kwargs):
    try:
        return subprocess.run(
            ["git", "-C", repo] + list(args),
            capture_output=True, text=True, env=tool_env(), timeout=kwargs.get("timeout", GIT_TIMEOUT),
        )
    except FileNotFoundError:
        raise SyncError("git_missing", "Git is not installed (xcode-select --install).")


def clean_commit_message(message):
    """One-line subject (120 chars), then up to 50 body lines (240 chars each)."""
    lines = [" ".join(line.split()) for line in (message if isinstance(message, str) else "").split("\n")]
    lines = [line for line in lines if line]
    subject = lines[0][:120] if lines else "sync: bookmarks"
    body = [line[:240] for line in lines[1:51]]
    return subject + ("\n\n" + "\n".join(body) if body else "")


def sync_snapshot(files, message, config=None):
    """Write the snapshot files into the repository, scan, commit and push."""
    config = config or load_config()
    repo = config["repo"]
    if not repo:
        raise SyncError("no_repo", "No bookmarks repository configured (run install.sh --repo PATH again).")
    # The folder must be a work tree's top level (a linked worktree is fine);
    # a subfolder would commit and push the parent repository.
    top = git(repo, "rev-parse", "--show-toplevel") if os.path.isdir(repo) else None
    if top is None or top.returncode != 0 or os.path.realpath(top.stdout.strip()) != os.path.realpath(repo):
        raise SyncError("no_repo", "Not the top folder of a Git repository: %s" % repo)
    if not isinstance(files, dict) or not files:
        raise SyncError("bad_snapshot", "Snapshot has no files.")
    names = sorted(files)
    for name in names:
        if name not in SNAPSHOT_FILES or not isinstance(files[name], str):
            raise SyncError("bad_snapshot", "Unexpected snapshot file: %s" % name)

    scan_snapshot(files, config["requireSecretScan"], repo)

    for name in names:
        target = os.path.join(repo, name)
        fd, tmp = tempfile.mkstemp(prefix="." + name + ".", dir=repo)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(files[name])
        os.replace(tmp, target)

    status = git(repo, "status", "--porcelain", "--", *names)
    if status.returncode != 0:
        raise SyncError("git_failed", status.stderr.strip() or "git status failed")
    changed = bool(status.stdout.strip())
    if changed:
        added = git(repo, "add", "--", *names)
        if added.returncode != 0:
            raise SyncError("git_failed", added.stderr.strip() or "git add failed")
        commit = git(repo, "commit", "--quiet", "-m", clean_commit_message(message), "--", *names)
        if commit.returncode != 0:
            raise SyncError("git_failed", commit.stderr.strip() or commit.stdout.strip() or "git commit failed")
    result = {"ok": True, "changed": changed, "head": head(repo), "pushed": False}
    if config["push"]:
        result.update(push(repo))
    return result


def push(repo):
    """Push HEAD to origin; also sends commits left behind by an earlier failed push."""
    remote = git(repo, "remote")
    if remote.returncode != 0 or "origin" not in remote.stdout.split():
        return {"pushError": "no origin remote"}
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if not branch:
        return {"pushError": "repository is not on a branch"}
    try:
        ahead = git(repo, "rev-list", "--count", "origin/%s..HEAD" % branch)
        if ahead.returncode == 0 and ahead.stdout.strip() == "0":
            return {"pushed": False, "upToDate": True}
        pushed = git(repo, "push", "--quiet", "origin", "HEAD:refs/heads/%s" % branch)
        if pushed.returncode != 0 and is_behind(pushed.stderr) and tree_clean(repo):
            rebased = git(repo, "pull", "--rebase", "--quiet", "origin", branch)
            if rebased.returncode != 0:
                git(repo, "rebase", "--abort")
                return {"pushError": "remote has other changes and they do not rebase cleanly; resolve in %s" % repo}
            pushed = git(repo, "push", "--quiet", "origin", "HEAD:refs/heads/%s" % branch)
    except subprocess.TimeoutExpired:
        return {"pushError": "push timed out; the commit stays local and goes out with the next sync"}
    if pushed.returncode == 0:
        return {"pushed": True, "head": head(repo)}
    lines = (pushed.stderr.strip() or "push failed").splitlines()
    return {"pushError": lines[-1][:300]}


def is_behind(stderr):
    text = stderr.lower()
    return "fetch first" in text or "non-fast-forward" in text or "rejected" in text


def tree_clean(repo):
    status = git(repo, "status", "--porcelain")
    return status.returncode == 0 and not status.stdout.strip()


def head(repo):
    out = git(repo, "rev-parse", "--short", "HEAD")
    return out.stdout.strip() if out.returncode == 0 else None


def scan_snapshot(files, required, repo=None):
    """Refuse snapshots that contain secret-like values (gitleaks, fail-closed).

    A .gitleaks.toml in the repository is used as the rules file, so known false
    positives can be allowlisted there by regex.
    """
    gitleaks = shutil.which("gitleaks", path=tool_env()["PATH"])
    if not gitleaks:
        if required:
            raise SyncError("scanner_missing", "gitleaks is required for sync (brew install gitleaks).")
        return
    work = tempfile.mkdtemp(prefix="nexus-scan-")
    try:
        for name, text in files.items():
            with open(os.path.join(work, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        report = os.path.join(work, ".report.json")
        command = [gitleaks, "dir", work, "--no-banner", "--redact", "--report-format", "json", "--report-path", report]
        rules = os.path.join(repo, ".gitleaks.toml") if repo else None
        if rules and os.path.isfile(rules):
            command += ["--config", rules]
        proc = subprocess.run(
            command,
            capture_output=True, text=True, env=tool_env(), timeout=GIT_TIMEOUT,
        )
        if proc.returncode == 0:
            return
        findings = []
        try:
            with open(report, "r", encoding="utf-8") as fh:
                for item in json.load(fh) or []:
                    findings.append({
                        "file": os.path.basename(item.get("File", "")),
                        "line": item.get("StartLine"),
                        "rule": item.get("RuleID"),
                    })
        except (OSError, ValueError):
            pass
        if proc.returncode == 1 and findings:
            raise SyncError("secret_detected", "Sync blocked: %d secret-like value(s) found." % len(findings), findings)
        raise SyncError("scanner_failed", "gitleaks failed: %s" % (proc.stderr.strip()[-200:] or proc.returncode))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# Client


class BridgeError(Exception):
    def __init__(self, code, message, details=None):
        Exception.__init__(self, message)
        self.code = code
        self.details = details


def call(method, params=None, timeout=DEFAULT_READ_TIMEOUT, client=None, on_event=None):
    path = socket_path()
    if not os.path.exists(path):
        raise BridgeError("offline", "The sidebar bridge is not running. Open the browser and connect agents in the sidebar's Agent tab.")
    request = {
        "v": PROTOCOL,
        "method": method,
        "params": params or {},
        "client": client or os.environ.get("NEXUS_CLIENT") or "cli",
        "timeout": timeout,
    }
    payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_CLIENT_REQUEST:
        raise BridgeError("too_large", "Request is too large; split the plan into smaller batches.")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(5)
        try:
            conn.connect(path)
        except PermissionError:
            # The socket exists but this process may not open it: an agent sandbox.
            raise BridgeError("sandboxed", "This process may not open %s. Run nexusctl outside the sandbox, or allow the "
                              "socket (Claude Code: sandbox.network.allowUnixSockets)." % path)
        except OSError:
            raise BridgeError("offline", "The sidebar bridge is not answering. Reopen the browser or reconnect in the Agent tab.")
        conn.sendall(payload)
        conn.settimeout(timeout + 15)
        buffer = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                raise BridgeError("timeout", "No answer from the browser in time.")
            if not chunk:
                raise BridgeError("offline", "The bridge closed the connection.")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                reply = json.loads(line.decode("utf-8"))
                if "event" in reply:
                    if on_event:
                        on_event(reply)
                    continue
                if reply.get("ok"):
                    return reply.get("result")
                error = reply.get("error") or {}
                raise BridgeError(error.get("code", "failed"), error.get("message", "Request failed."), error.get("details"))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Command line


def folder_ref(value):
    """Folder references are ids (digits) or slash paths such as 'Work/Projects'."""
    value = value.strip()
    return {"id": value} if value.isdigit() else {"path": value}


def print_tree(nodes, depth=0):
    for node in nodes or []:
        pad = "  " * depth
        if node.get("url"):
            print("%s- %s  [%s]  %s" % (pad, node.get("title") or "(untitled)", node.get("id"), node.get("url")))
        else:
            count = node.get("count")
            suffix = "  (%s)" % count if count is not None else ""
            print("%s+ %s  [%s]%s" % (pad, node.get("title") or "(untitled)", node.get("id"), suffix))
            print_tree(node.get("children"), depth + 1)


def print_items(items):
    for item in items or []:
        when = time.strftime("%Y-%m-%d", time.localtime((item.get("dateAdded") or 0) / 1000.0))
        print("%s  [%s]  %s" % (when, item.get("id"), item.get("title") or "(untitled)"))
        print("            %s" % item.get("url"))
        if item.get("path"):
            print("            in %s" % item.get("path"))


def print_tabs(result):
    for window in result.get("windows", []):
        print("Window %s%s" % (window.get("id"), " (focused)" if window.get("focused") else ""))
        for tab in window.get("tabs", []):
            group = (" {%s}" % tab["group"]) if tab.get("group") else ""
            flags = "*" if tab.get("active") else " "
            print("  %s [%s]%s %s  %s" % (flags, tab.get("id"), group, tab.get("title") or "", tab.get("url") or ""))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="nexusctl",
        description="Work with the Nexus sidebar's bookmarks and tabs. Changes need approval in the sidebar.",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--client", help="name shown on approval cards (default: $NEXUS_CLIENT or 'cli')")
    parser.add_argument("--timeout", type=float, default=DEFAULT_APPROVAL_TIMEOUT,
                        help="seconds to wait for an approval (default 600)")
    parser.add_argument("--version", action="version", version="nexusctl %s" % VERSION)
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("status", help="bridge, permissions and sync state")
    p = sub.add_parser("tree", help="bookmark tree with ids")
    p.add_argument("folder", nargs="?", help="folder id or path (default: everything)")
    p.add_argument("--depth", type=int, default=0, help="levels to show (0 = all)")
    sub.add_parser("folders", help="every folder with its path and size")
    p = sub.add_parser("recent", help="newest bookmarks first")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("search", help="search titles and URLs")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=50)
    sub.add_parser("inbox", help="links waiting in the Inbox")
    sub.add_parser("tabs", help="open windows and tabs (needs tab sharing on)")

    p = sub.add_parser("add", help="save a link (default: the Inbox)")
    p.add_argument("url")
    p.add_argument("--title")
    p.add_argument("--to", help="folder id or path")
    p = sub.add_parser("mkdir", help="create a folder path such as 'Work/Library/AI'")
    p.add_argument("path")
    p = sub.add_parser("move", help="move bookmarks or folders")
    p.add_argument("ids", nargs="+")
    p.add_argument("--to", required=True, help="folder id or path")
    p = sub.add_parser("rename", help="rename a bookmark or folder")
    p.add_argument("id")
    p.add_argument("title")
    p = sub.add_parser("delete", help="delete bookmarks or folders")
    p.add_argument("ids", nargs="+")
    p = sub.add_parser("apply", help="propose a plan file (JSON) as one approval")
    p.add_argument("plan", help="path to the plan, or - for stdin")
    p.add_argument("--dry-run", action="store_true", help="validate and preview without asking")
    p = sub.add_parser("sync", help="commit the bookmarks to the Git repository now")
    p.add_argument("--message", help="commit message")
    return parser


def plan_for(args):
    if args.command == "add":
        op = {"op": "add", "url": args.url}
        if args.title:
            op["title"] = args.title
        if args.to:
            op["to"] = folder_ref(args.to)
        return {"summary": "Save %s" % args.url, "ops": [op]}
    if args.command == "mkdir":
        return {"summary": "Create folder %s" % args.path, "ops": [{"op": "mkdir", "path": args.path}]}
    if args.command == "move":
        return {"summary": "Move %d item(s)" % len(args.ids),
                "ops": [{"op": "move", "id": i, "to": folder_ref(args.to)} for i in args.ids]}
    if args.command == "rename":
        return {"summary": "Rename %s" % args.id, "ops": [{"op": "rename", "id": args.id, "title": args.title}]}
    if args.command == "delete":
        return {"summary": "Delete %d item(s)" % len(args.ids), "ops": [{"op": "delete", "id": i} for i in args.ids]}
    return None


def load_plan(path):
    try:
        text = sys.stdin.read() if path == "-" else open(path, "r", encoding="utf-8").read()
        plan = json.loads(text)
    except (OSError, ValueError) as exc:
        raise BridgeError("bad_plan", "Cannot read plan: %s" % exc)
    if not isinstance(plan, dict) or not isinstance(plan.get("ops"), list):
        raise BridgeError("bad_plan", "A plan is an object with 'summary' and an 'ops' list.")
    return plan


def run_cli(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    def on_event(event):
        if event.get("event") == "awaiting_approval" and not args.json:
            data = event.get("data") or {}
            sys.stderr.write("Waiting for approval in the sidebar: %s\n" % data.get("summary", ""))
            sys.stderr.flush()

    try:
        result, printer = dispatch(args, on_event)
    except BridgeError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc), "details": exc.details}}, ensure_ascii=False))
        else:
            sys.stderr.write("nexusctl: %s\n" % exc)
            if exc.details:
                sys.stderr.write(json.dumps(exc.details, ensure_ascii=False, indent=2) + "\n")
        return ERROR_EXIT.get(exc.code, EXIT_ERROR)
    if args.json:
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
    else:
        printer(result)
    return EXIT_OK


def dispatch(args, on_event):
    client = args.client
    read = lambda method, params=None: call(method, params, DEFAULT_READ_TIMEOUT, client)
    dump = lambda result: print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "status":
        return read("status"), dump
    if args.command == "tree":
        params = {"depth": max(0, args.depth)}
        if args.folder:
            params["folder"] = folder_ref(args.folder)
        return read("bookmarks.tree", params), lambda r: print_tree(r.get("nodes"))
    if args.command == "folders":
        return read("bookmarks.folders"), lambda r: [print("[%s] %s  (%s)" % (f["id"], f["path"], f.get("count", 0))) for f in r.get("folders", [])]
    if args.command == "recent":
        return read("bookmarks.recent", {"limit": args.limit}), lambda r: print_items(r.get("items"))
    if args.command == "search":
        return read("bookmarks.search", {"query": args.query, "limit": args.limit}), lambda r: print_items(r.get("items"))
    if args.command == "inbox":
        return read("inbox.list"), lambda r: (print("Inbox: %s [%s]" % (r.get("path"), r.get("folderId"))), print_items(r.get("items")))
    if args.command == "tabs":
        return read("tabs.list"), print_tabs
    if args.command == "sync":
        params = {"message": args.message} if args.message else {}
        result = call("sync", params, 120, client)
        return result, lambda r: print(describe_sync(r))
    if args.command == "apply":
        plan = load_plan(args.plan)
        if args.dry_run:
            return read("plan.validate", plan), lambda r: print_preview(r)
        result = call("plan.apply", plan, args.timeout, client, on_event)
        return result, print_applied
    plan = plan_for(args)
    result = call("plan.apply", plan, args.timeout, client, on_event)
    return result, print_applied


def print_preview(result):
    print(result.get("summary") or "Plan")
    for line in result.get("preview", []):
        print("  " + line)


def print_applied(result):
    if result.get("autoApproved"):
        print("Applied without asking (allowed in the sidebar).")
    print("Applied %d change(s)." % len(result.get("results", [])))
    for item in result.get("results", []):
        if item.get("id"):
            print("  %s -> [%s]" % (item.get("op"), item.get("id")))
    if result.get("sync"):
        print(describe_sync(result["sync"]))


def describe_sync(result):
    if not result:
        return "Sync: not run."
    if not result.get("ok", True):
        error = result.get("error") or {}
        return "Sync failed: %s" % error.get("message", "unknown error")
    if not result.get("changed"):
        return "Sync: no changes (%s)." % (result.get("head") or "-")
    text = "Synced %s" % result.get("head")
    if result.get("pushed"):
        return text + ", pushed."
    return text + ", not pushed: %s" % result.get("pushError", "push off")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0].startswith("chrome-extension://"):
        stdin = sys.stdin.buffer
        stdout = sys.stdout.buffer
        Host(argv[0], stdin, stdout).run()
        return EXIT_OK
    return run_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
