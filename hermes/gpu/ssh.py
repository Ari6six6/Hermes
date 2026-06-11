"""SSH plumbing via the openssh binary (no paramiko — Termux-friendly).

ControlMaster multiplexing keeps per-command round-trips fast on a phone
connection. The tunnel is a background `ssh -N -L` process.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import dataclass

from hermes.config import hermes_home

_SSH_URL_RE = re.compile(r"^ssh://(?:(?P<user>[^@]+)@)?(?P<host>[^:/]+)(?::(?P<port>\d+))?/?$")
_SSH_CMD_RE = re.compile(
    r"ssh\s+(?:.*?-p\s*(?P<port>\d+)\s+)?.*?(?P<user>[A-Za-z0-9_.-]+)@(?P<host>[A-Za-z0-9_.-]+)"
    r"(?:\s+.*?-p\s*(?P<port2>\d+))?"
)


class SSHError(Exception):
    pass


def parse_ssh_string(text: str):
    """Accepts 'ssh://root@host:port' or a pasted 'ssh -p PORT root@host ...'."""
    text = text.strip()
    m = _SSH_URL_RE.match(text)
    if m:
        return (m["user"] or "root", m["host"], int(m["port"] or 22))
    m = _SSH_CMD_RE.search(text)
    if m:
        port = m["port"] or m["port2"] or "22"
        return (m["user"], m["host"], int(port))
    raise SSHError(f"could not parse SSH string: {text!r}")


@dataclass
class SSHEndpoint:
    host: str
    port: int = 22
    user: str = "root"
    remote_workspace: str = "~/hermes-workspace"

    def base_args(self) -> list[str]:
        sockets = hermes_home() / "cm-sockets"
        sockets.mkdir(parents=True, exist_ok=True)
        return [
            "ssh",
            "-p", str(self.port),
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={sockets}/%r@%h-%p",
            "-o", "ControlPersist=600",
            "-o", "ServerAliveInterval=30",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
            f"{self.user}@{self.host}",
        ]

    def run(self, command: str, timeout: int = 120, stdin: str | None = None):
        """Returns (rc, stdout, stderr)."""
        try:
            proc = subprocess.run(
                self.base_args() + [command],
                capture_output=True,
                text=True,
                timeout=timeout,
                input=stdin,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"
        except FileNotFoundError:
            return 127, "", "ssh binary not found — `pkg install openssh` on Termux"

    def check(self) -> bool:
        rc, out, _ = self.run("echo HERMES_OK", timeout=30)
        return rc == 0 and "HERMES_OK" in out

    def write_file(self, path: str, content: str):
        return self.run(f"mkdir -p $(dirname {path}) && cat > {path}", stdin=content)

    # -- tunnel --------------------------------------------------------------
    def tunnel_args(self, local_port: int, remote_port: int) -> list[str]:
        args = self.base_args()
        # A tunnel should be its own connection, not the multiplexed master.
        args[args.index("ControlMaster=auto")] = "ControlMaster=no"
        return args[:1] + [
            "-N",
            "-L", f"{local_port}:127.0.0.1:{remote_port}",
            "-o", "ExitOnForwardFailure=yes",
        ] + args[1:]

    def start_tunnel(self, local_port: int, remote_port: int) -> int:
        proc = subprocess.Popen(
            self.tunnel_args(local_port, remote_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc.pid


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
