"""Allowlisted native command runner and BSD/GNU flag adapter."""
import logging
import platform
import shutil
import subprocess
import threading
import time

log = logging.getLogger("barefile.cli")

# Every command the app may run, with every option it may pass.
ALLOWED = {
    "ls": {"-lhnA", "--"},
    "touch": {"--"},
    "mkdir": {"-v", "-pv", "-p", "-m", "--"},
    "cp": {"-RPpv", "--"},
    "mv": {"-vn", "-vnT", "--"},
    "ln": {"-v", "-vT", "--"},
    "rm": {"-v", "-rv", "-rf", "--"},
    "find": {"-mindepth", "-maxdepth", "-iname", "-exec", "-f", "--printf"},
    "sort": {"-t", "-k1,1n", "-k1,1nr", "-k2,2n", "-k2,2nr", "-k3,3f", "-k3,3fr"},
    "stat": {"-f", "--printf", "--"},
    "du": {"-sh", "--"},
    "file": {"--mime-type", "-b", "--"},
    "head": {"-c", "--"},
    "dd": set(),
}


class CommandError(Exception):
    """A native command failed. str() is safe to show to a logged-in user."""


def text(raw):
    """Decode command output, keeping undecodable filename bytes round-trippable."""
    return raw.decode("utf-8", "surrogateescape")


class CLI:
    def __init__(self, cfg):
        self.env = {"PATH": cfg.cli_path, "LC_ALL": "C"}
        self.timeout, self.op_timeout = cfg.cmd_timeout, cfg.op_timeout
        self.bins = {name: shutil.which(name, path=cfg.cli_path) for name in ALLOWED}
        missing = [name for name, path in self.bins.items() if not path]
        if missing:
            raise SystemExit(f"Missing native commands: {', '.join(missing)}")
        self.system = platform.system()
        # Probe rather than trust uname: macOS can have GNU coreutils configured.
        self.gnu = subprocess.run([self.bins["stat"], "--version"], capture_output=True,
                                  env=self.env).returncode == 0
        self.hide = [(cfg.tmp + "/", "[tmp]/"), (cfg.tmp, "[tmp]"),
                     (cfg.trash + "/", "[trash]/"), (cfg.trash, "[trash]"),
                     (cfg.data_dir, "[app]"),
                     (cfg.storage_root + "/", "/"), (cfg.storage_root, "/")]

    # BSD/GNU differences. -T stops GNU moving *into* a directory that appeared at dst.
    @property
    def mv_flags(self):
        return "-vnT" if self.gnu else "-vn"

    @property
    def ln_flags(self):
        return "-vT" if self.gnu else "-v"

    def stat_args(self, mark):
        """Format: <mark>size|mtime|type|path. The random mark frames records,
        so a filename containing a newline cannot forge one."""
        if self.gnu:
            return ["--printf", mark + "%s|%Y|%F|%n\n"]
        return ["-f", mark + "%z|%m|%HT|%N"]

    def clean(self, output, replace=()):
        """Readable command output with internal paths removed."""
        result = output.decode("utf-8", "replace").strip()
        for old, new in list(replace) + self.hide:
            result = result.replace(old, new)
        return result

    def argv(self, name, args):
        if name not in ALLOWED:
            raise CommandError(f"Command not allowed: {name}")
        options = True
        for arg in args:
            if arg == "--" and "--" in ALLOWED[name]:
                options = False
            elif options and arg.startswith("-") and arg not in ALLOWED[name]:
                raise CommandError(f"Option not allowed for {name}")
            elif not options and not arg.startswith("/"):
                raise CommandError(f"Unsafe argument for {name}")
        return [self.bins[name], *args]

    def run(self, name, *args, input=None, timeout=None, check=True):
        argv = self.argv(name, args)
        start = time.monotonic()
        try:
            proc = subprocess.run(argv, input=input, capture_output=True, env=self.env,
                                  timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            log.warning("timeout argv=%r", argv)
            raise CommandError(f"{name} timed out") from None
        log.info("argv=%r rc=%s ms=%d stdout=%dB stderr=%r", argv, proc.returncode,
                 (time.monotonic() - start) * 1000, len(proc.stdout), proc.stderr[-500:])
        if check and proc.returncode:
            raise CommandError(self.clean(proc.stderr).split("\n")[-1] or f"{name} failed")
        return proc

    def stream(self, name, *args, replace=()):
        """Run a long command, yielding each line of merged stdout/stderr."""
        proc = self.popen(name, *args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        timer = threading.Timer(self.op_timeout, proc.kill)
        timer.start()
        last = ""
        try:
            for raw in proc.stdout:
                last = self.clean(raw, replace)
                yield last
            proc.wait()
        finally:  # also runs when the client disconnects mid-stream
            timer.cancel()
            self.finish(proc)
        if proc.returncode:
            raise CommandError(last or f"{name} failed")

    def popen(self, name, *args, **kwargs):
        """Start a long-lived process. The caller must call finish() on it."""
        proc = subprocess.Popen(self.argv(name, args), env=self.env, **kwargs)
        proc.started = time.monotonic()
        return proc

    def finish(self, proc):
        """Stop the process if still running, release its pipes and log the result."""
        proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
        log.info("argv=%r rc=%s ms=%d", proc.args, proc.returncode,
                 (time.monotonic() - proc.started) * 1000)
