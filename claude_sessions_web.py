#!/usr/bin/env python3
"""claude-sessions-web: a small web page to manage long-running Claude Code sessions
(systemd user units claude@<name> running `claude` inside tmux; see README.md).

Per session: display name, group, conversation id (.sid), running or not, "keep running"
(systemd enabled = starts at boot), "startup tab" (CLAUDE_WINSTART: opened as a terminal tab by a
client login script), start / restart / stop, archive and restore. Plus a search over
the transcripts (user + assistant text) of every conversation in ~/.claude/projects.

stdlib only; runs as the user unit systemd/claude-sessions-web.service (see README.md):
  python3 claude_sessions_web.py [--host ADDR ...] [--port N] [--name HOSTNAME ...]
                                 [--sessions-dir DIR] [--projects-dir DIR] [--cache-dir DIR]
Binds only the addresses given (default 127.0.0.1; e.g. a VPN address). There is no login: whoever
can reach the port can stop and archive sessions. Changes need a JSON POST with the X-Claude-Sessions header (a page on
another site cannot send that without a CORS preflight, which this server never grants), and the
Host header must name this machine (no DNS rebinding).
"""
import argparse, datetime, json, os, re, shutil, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Defaults; main() replaces them from the command line.
SESSIONS = Path.home() / ".config" / "claude-sessions"      # <name>.env / .sid / .prompt per session
ARCHIVE = SESSIONS / "deleted"                              # archived sessions' settings
PROJECTS = Path.home() / ".claude" / "projects"             # Claude Code transcripts (*.jsonl)
CACHE = Path.home() / ".cache" / "claude-sessions-web"      # search index
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
GROUP_RE = NAME_RE
DISPLAY_RE = re.compile(r'[^"$`\\\n\r]{1,80}')     # the .env is sourced by bash
SYS_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def proj_dir(cwd):
    return PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def read_env(path):
    vals = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r'^([A-Z_][A-Z0-9_]*)="?(.*?)"?$', line)
            if m:
                vals[m.group(1)] = m.group(2)
    except OSError:
        pass
    return vals


def set_env(name, key, value):
    env = SESSIONS / f"{name}.env"
    t = env.read_text(encoding="utf-8")
    line = f'{key}="{value}"' if " " in value else f"{key}={value}"
    if re.search(rf"^{key}=", t, re.M):
        t = re.sub(rf"^{key}=.*$", lambda _: line, t, flags=re.M)
    else:
        t = t.rstrip("\n") + f"\n{line}\n"
    env.write_text(t, encoding="utf-8")


def sh(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def tmux_up():
    rc, out = sh("tmux", "ls", "-F", "#{session_name}")
    return {s[len("claude-"):] for s in out.splitlines() if s.startswith("claude-")} if rc == 0 else set()


def describe(name, env_path, sid_path, up, enabled, archived=None):
    e = read_env(env_path)
    sid = sid_path.read_text(encoding="utf-8").strip() if sid_path.exists() else ""
    cwd = e.get("CLAUDE_CWD") or str(Path.home() / "work" / name)
    tr = proj_dir(cwd) / f"{sid}.jsonl" if sid else None
    last = tr.stat().st_mtime if tr and tr.exists() else None
    meta = INDEX.meta.get(str(tr), {}) if tr else {}
    return {"name": name, "display": e.get("CLAUDE_NAME") or f"linux-{name}",
            "group": e.get("CLAUDE_GROUP") or "general", "sid": sid, "cwd": cwd,
            "bridge": meta.get("bridge", ""), "running": name in up, "keep": enabled,
            "winstart": e.get("CLAUDE_WINSTART", "1") != "0", "chrome": "--chrome" in e.get("CLAUDE_ARGS", ""),
            "last": last, "archived": archived}


def list_sessions():
    up = tmux_up()
    names = sorted(p.stem for p in SESSIONS.glob("*.env"))
    rc, out = sh("systemctl", "--user", "is-enabled", *[f"claude@{n}" for n in names]) if names else (0, "")
    en = out.splitlines()
    live = [describe(n, SESSIONS / f"{n}.env", SESSIONS / f"{n}.sid", up,
                     i < len(en) and en[i].strip() == "enabled") for i, n in enumerate(names)]
    arch = []
    for d in sorted(ARCHIVE.glob("*-*-*"), reverse=True) if ARCHIVE.exists() else []:
        m = re.fullmatch(r"(.+)-(\d{8}-\d{6})", d.name)
        if not m or not d.is_dir():
            continue
        n = m.group(1)
        arch.append(describe(n, d / f"{n}.env", d / f"{n}.sid", set(), False, archived=d.name))
    return {"sessions": live, "archived": arch, "index": INDEX.status(), "host": socket.gethostname()}


# ---------------------------------------------------------------- actions

def act(name, action, value=None):
    if action == "restore":
        d = ARCHIVE / value
        if not re.fullmatch(r"[a-z0-9-]+-\d{8}-\d{6}", value or "") or not d.is_dir():
            return 400, "no such archive"
        if (SESSIONS / f"{name}.env").exists():
            return 409, f"a session named {name} exists already"
        for f in d.iterdir():
            shutil.move(str(f), SESSIONS / f.name)
        d.rmdir()
        return 200, f"restored {name} (stopped; Start it when needed)"
    if not NAME_RE.fullmatch(name) or not (SESSIONS / f"{name}.env").exists():
        return 404, f"no such session: {name}"
    unit = f"claude@{name}"
    if action == "display":
        if not DISPLAY_RE.fullmatch(value or ""):
            return 400, 'display name: 1-80 characters, no " $ ` \\'
        set_env(name, "CLAUDE_NAME", value)
        return 200, "renamed (Claude shows it after the next restart)"
    if action == "group":
        if not GROUP_RE.fullmatch(value or ""):
            return 400, "group: lowercase letters, digits and - only"
        set_env(name, "CLAUDE_GROUP", value)
        return 200, f"group set to {value}"
    if action == "winstart":
        set_env(name, "CLAUDE_WINSTART", "1" if value else "0")
        return 200, "startup tab " + ("on" if value else "off")
    if action in ("start", "restart"):
        rc, out = sh("systemctl", "--user", "restart", unit)   # the oneshot unit stays "active" after /exit
        return (200, f"{name} started") if rc == 0 else (500, out)
    if action == "stop":
        rc, out = sh("systemctl", "--user", "stop", unit)
        return (200, f"{name} stopped") if rc == 0 else (500, out)
    if action == "keep":
        if value:
            rc, out = sh("systemctl", "--user", "enable", unit)
            if rc == 0 and name not in tmux_up():
                rc, out = sh("systemctl", "--user", "restart", unit)
            return (200, f"{name}: keep running (starts at boot)") if rc == 0 else (500, out)
        rc, out = sh("systemctl", "--user", "disable", "--now", unit)
        return (200, f"{name}: stopped, not started at boot") if rc == 0 else (500, out)
    if action == "archive":
        if name == "main":
            return 400, "refusing to archive 'main'"
        sh("systemctl", "--user", "disable", "--now", unit)
        arch = ARCHIVE / f"{name}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        arch.mkdir(parents=True, mode=0o700)
        for f in SESSIONS.glob(f"{name}.*"):
            shutil.move(str(f), arch / f.name)
        return 200, f"{name} archived (conversation kept)"
    return 400, f"unknown action {action}"


# ---------------------------------------------------------------- search index

class Index:
    """Per transcript: the user/assistant text, one line per message ("ts<TAB>role<TAB>text"), in
    CACHE/text/. Transcripts only grow, so each refresh reads just the bytes added since last time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state_file = CACHE / "state.json"
        try:
            self.state = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.meta = {k: v.get("meta", {}) for k, v in self.state.items()}
        self.updated = None
        self.busy = False

    def status(self):
        return {"conversations": len(self.state), "updated": self.updated, "busy": self.busy}

    def cache_of(self, path):
        return CACHE / "text" / path.parent.name / (path.stem + ".txt")

    def refresh(self):
        with self.lock:
            self.busy = True
            try:
                seen = set()
                for p in PROJECTS.glob("*/*.jsonl"):
                    seen.add(str(p))
                    self._update(p)
                for k in [k for k in self.state if k not in seen]:
                    self.state.pop(k)
                    self.meta.pop(k, None)
                CACHE.mkdir(parents=True, exist_ok=True)
                tmp = self.state_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.state))
                tmp.replace(self.state_file)
                self.updated = time.time()
            finally:
                self.busy = False

    def _update(self, p):
        key = str(p)
        st = self.state.get(key, {"offset": 0, "meta": {}})
        try:
            size = p.stat().st_size
        except OSError:
            return
        out = self.cache_of(p)
        if size < st["offset"] or not out.exists():
            st = {"offset": 0, "meta": {}}
        if size == st["offset"]:
            self.state[key] = st
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        meta = st["meta"]
        with open(p, "rb") as f, open(out, "a" if st["offset"] else "w", encoding="utf-8") as w:
            f.seek(st["offset"])
            data = f.read()
            end = data.rfind(b"\n") + 1          # never consume a half-written last line
            for raw in data[:end].splitlines():
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                t = o.get("type")
                if t == "custom-title":
                    meta["title"] = o.get("customTitle", "")
                elif t == "bridge-session":
                    meta["bridge"] = o.get("bridgeSessionId", "")
                elif t in ("user", "assistant") and not o.get("isMeta"):
                    c = (o.get("message") or {}).get("content")
                    parts = [c] if isinstance(c, str) else [
                        b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text"]
                    s = SYS_RE.sub("", " ".join(parts)).strip()
                    if s:
                        s = re.sub(r"\s*\n\s*", " ⏎ ", s).replace("\t", " ")
                        w.write(f"{o.get('timestamp', '')}\t{t}\t{s}\n")
                    meta["last"] = o.get("timestamp", meta.get("last", ""))
        st["offset"] += end
        self.state[key] = st
        self.meta[key] = meta

    def search(self, q, limit=60):
        """Words (all must appear in one message) and "exact phrases", case-insensitive;
        or /regex/ (Python syntax, case-insensitive; /regex/c = case-sensitive)."""
        m = re.fullmatch(r"/(.+)/(c?)", q.strip())
        if m:
            rxs = [re.compile(m.group(1), 0 if m.group(2) else re.I)]   # re.error -> caller
        else:
            rxs = [re.compile(re.escape(t), re.I)
                   for t in re.findall(r'"([^"]+)"|(\S+)', q) for t in t if t]
        if not rxs:
            return []
        if not self.busy:
            self.refresh()
        names = name_by_project()
        hits = []
        for key in list(self.state):
            p = Path(key)
            try:
                f = open(self.cache_of(p), encoding="utf-8")
            except OSError:
                continue
            found = []
            with f:
                for line in f:
                    ts, role, text = (line.rstrip("\n").split("\t", 2) + ["", ""])[:3]
                    first = [r.search(text) for r in rxs]
                    if not all(first):
                        continue
                    i = first[0].start()
                    a, b = max(0, i - 140), i + 220
                    snip = text[a:b]
                    marks = sorted((x.start(), x.end()) for r in rxs for x in r.finditer(snip) if x.end() > x.start())
                    found.append({"ts": ts, "role": role, "text": snip, "marks": marks,
                                  "pre": a > 0, "post": b < len(text)})
            if found:
                mt = self.meta.get(key, {})
                hits.append({"sid": p.stem, "session": names.get(p.parent.name, ""),
                             "folder": p.parent.name, "title": mt.get("title", ""),
                             "last": mt.get("last", ""), "count": len(found), "snippets": found[-3:]})
        hits.sort(key=lambda h: h["last"], reverse=True)
        return hits[:limit]


def name_by_project():
    out = {}
    for env, label in [(e, e.stem) for e in SESSIONS.glob("*.env")] + \
                      [(e, e.stem + " (archived)") for e in ARCHIVE.glob("*/*.env")]:
        cwd = read_env(env).get("CLAUDE_CWD") or str(Path.home() / "work" / env.stem)
        out.setdefault(proj_dir(cwd).name, label)
    return out


INDEX = None      # created in main(), once the paths are known


def indexer():
    while True:
        try:
            INDEX.refresh()
        except Exception as e:  # keep the page alive; the next round retries
            print("index refresh failed:", e, file=sys.stderr)
        time.sleep(300)


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "claude-sessions-web"

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/sessions"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def host_ok(self):
        h = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return h in ALLOWED_HOSTS or h.split(".")[0] in ALLOWED_NAMES

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "bad host"})
        u = urlparse(self.path)
        if u.path == "/":
            return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        if u.path == "/api/sessions":
            return self.send(200, list_sessions())
        if u.path == "/api/search":
            q = parse_qs(u.query).get("q", [""])[0][:200]
            try:
                hits = INDEX.search(q)
            except re.error as e:
                return self.send(400, {"error": f"bad regex: {e}", "index": INDEX.status()})
            return self.send(200, {"hits": hits, "index": INDEX.status()})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-Claude-Sessions") != "1" \
                or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)))
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if urlparse(self.path).path != "/api/action":
            return self.send(404, {"error": "not found"})
        results = []
        for name in body.get("names") or []:
            code, msg = act(str(name), str(body.get("action", "")), body.get("value"))
            results.append({"name": name, "ok": code == 200, "message": msg})
        self.send(200, {"results": results})


ALLOWED_HOSTS = {"localhost", "127.0.0.1"}
ALLOWED_NAMES = {socket.gethostname().lower()}   # + --name; also matches <name>.<domain>
PAGE = (Path(__file__).resolve().parent / "claude_sessions_web.html").read_text(encoding="utf-8")


def main():
    global SESSIONS, ARCHIVE, PROJECTS, CACHE, INDEX
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", action="append", help="address to bind; repeat for several "
                   "(default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8190, help="default 8190")
    p.add_argument("--sessions-dir", type=Path, default=SESSIONS, help="session settings (default ~/.config/claude-sessions)")
    p.add_argument("--projects-dir", type=Path, default=PROJECTS, help="Claude Code transcripts (default ~/.claude/projects)")
    p.add_argument("--cache-dir", type=Path, default=CACHE, help="search index (default ~/.cache/claude-sessions-web)")
    p.add_argument("--name", action="append", default=[], help="extra host name the page answers to "
                   "(e.g. a DNS or VPN name pointing at this machine)")
    a = p.parse_args()
    SESSIONS, PROJECTS, CACHE = a.sessions_dir.expanduser(), a.projects_dir.expanduser(), a.cache_dir.expanduser()
    ARCHIVE = SESSIONS / "deleted"
    INDEX = Index()
    hosts = a.host or ["127.0.0.1"]
    ALLOWED_HOSTS.update(hosts)
    ALLOWED_NAMES.update(a.name)
    threading.Thread(target=indexer, daemon=True).start()
    servers = []
    for h in hosts:
        cls = type("V6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6}) if ":" in h \
            else ThreadingHTTPServer
        servers.append(cls((h, a.port), Handler))
        print(f"claude-sessions-web on http://{'[' + h + ']' if ':' in h else h}:{a.port}/", file=sys.stderr)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
