"""Small development reloader for the standard-library HTTP server."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid


VERSION = uuid.uuid4().hex
VERSION_PATH = "/api/dev/version"
SOURCE_SUFFIXES = {".py", ".html", ".css", ".js"}
CHILD_ENV = "AI_ORCHESTRATOR_RELOAD_CHILD"
OPEN_BROWSER_ENV = "AI_ORCHESTRATOR_OPEN_BROWSER"


def snapshot(root):
    """Watch source only; database, logs, browser profile and venv are ignored."""
    paths = [path for path in root.iterdir() if path.is_file()]
    for name in ("static", "templates"):
        folder = root / name
        if folder.is_dir():
            paths.extend(path for path in folder.rglob("*") if path.is_file())
    result = {}
    for path in paths:
        if path.suffix in SOURCE_SUFFIXES:
            try:
                result[path] = hashlib.sha256(path.read_bytes()).digest()
            except FileNotFoundError:
                pass  # Some editors replace the file while saving.
    return result


def check_python(files):
    for path in files:
        if path.suffix == ".py":
            compile(path.read_bytes(), str(path), "exec")


def stop_child(child):
    if child.poll() is None:
        try:
            child.stdin.write(b"reload\n")
            child.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=5)
    child.stdin.close()


def supervise(entry, args, stop_event=None):
    root = entry.parent
    stop_event = stop_event or threading.Event()
    current = snapshot(root)
    changed_at = None
    first_start = True
    child = None
    with tempfile.TemporaryDirectory(prefix="orchestrator-reload-") as cache:
        env = os.environ.copy()
        env[CHILD_ENV] = "1"
        # -B prevents writing bytecode; an empty prefix also avoids reading stale
        # timestamp-based caches after rapid edits with the same file size.
        env["PYTHONPYCACHEPREFIX"] = cache
        try:
            while not stop_event.is_set():
                if child is None:
                    env[OPEN_BROWSER_ENV] = "1" if first_start else "0"
                    child = subprocess.Popen(
                        [sys.executable, "-B", "-u", str(entry), *args],
                        cwd=str(root), env=env, stdin=subprocess.PIPE,
                    )
                    first_start = False
                    reported_exit = False
                if child.poll() is not None and not reported_exit:
                    print("[reload] Server da dung; dang cho thay doi ma nguon.", flush=True)
                    reported_exit = True
                if stop_event.wait(0.25):
                    break
                latest = snapshot(root)
                if latest != current:
                    current = latest
                    changed_at = time.monotonic()
                elif changed_at is not None and time.monotonic() - changed_at >= 0.5:
                    changed_at = None
                    try:
                        check_python(current)
                    except (SyntaxError, OSError) as error:
                        print(f"[reload] Chua tai lai: {error}", flush=True)
                        continue
                    print("[reload] Ma nguon da doi, dang khoi dong lai server...", flush=True)
                    stop_child(child)
                    child = None
        except KeyboardInterrupt:
            print("\n[reload] Da dung.", flush=True)
        finally:
            if child is not None:
                stop_child(child)


def launch(entry):
    """Call before importing application modules so startup errors can recover."""
    parser = argparse.ArgumentParser(description="Toa dam - tu tai lai khi sua code")
    parser.add_argument("--no-reload", action="store_true", help="Tat tu dong tai lai")
    parser.add_argument("--no-browser", action="store_true", help="Khong tu mo trinh duyet")
    args = parser.parse_args()
    if not args.no_reload and os.environ.get(CHILD_ENV) != "1":
        supervise(Path(entry).resolve(), sys.argv[1:])
        raise SystemExit(0)


def should_open_browser():
    return "--no-browser" not in sys.argv and os.environ.get(OPEN_BROWSER_ENV, "1") == "1"


def listen_for_shutdown(shutdown):
    if os.environ.get(CHILD_ENV) == "1":
        def listen():
            # EOF also stops the child if the supervisor exits unexpectedly.
            sys.stdin.readline()
            shutdown()
        threading.Thread(target=listen, daemon=True).start()


def inject_browser_reload(page):
    if "--no-reload" in sys.argv:
        return page
    script = """<script>
(() => {
  const version = __VERSION__;
  async function checkVersion() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);
    try {
      const response = await fetch("/api/dev/version", {
        cache: "no-store", signal: controller.signal
      });
      if (response.ok && (await response.json()).version !== version) {
        window.dispatchEvent(new Event("beforedevreload"));
        location.reload();
        return;
      }
    } catch (e) { /* The server may be restarting; retry when it is ready. */ }
    finally { clearTimeout(timeout); }
    setTimeout(checkVersion, 1000);
  }
  setTimeout(checkVersion, 1000);
})();
</script>
""".replace("__VERSION__", json.dumps(VERSION))
    return page.replace("</body>", script + "</body>")
