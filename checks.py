import datetime
import socket
import paramiko
import inform
import machines
from log import logger

# What a machine needs (see "Requirements on the machine" in the README).
# Each command runs over SSH and must exit 0; the first line it prints, e.g.
# the version, is shown next to the result. Commands only read, never change.
COMMON = [
    ("git", "git --version"),
    ("make", "make --version | head -n 1"),
    ("go", "go version"),
]
LINUX = [
    # the controller writes the MOTD with sudo tee
    ("passwordless sudo", "sudo -n true && echo ok"),
]
WINDOWS = [
    ("dotnet", "dotnet --version"),
]

CONNECT_TIMEOUT = 10
COMMAND_TIMEOUT = 20
DETAIL_MAX = 200


class Session:
    """One SSH connection to a machine; tests replace this class."""

    def __init__(self, host: str, user: str, key: paramiko.PKey):
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(host, username=user, pkey=key, timeout=CONNECT_TIMEOUT,
                         banner_timeout=CONNECT_TIMEOUT, auth_timeout=CONNECT_TIMEOUT)

    def run(self, command: str):
        _, stdout, stderr = self.ssh.exec_command(command, timeout=COMMAND_TIMEOUT)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        return stdout.channel.recv_exit_status(), out, err

    def close(self):
        self.ssh.close()


def first_line(text: str):
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:DETAIL_MAX]
    return ""


def result(name: str, ok: bool, detail: str):
    return {"name": name, "ok": ok, "detail": detail}


def run_check(session, name: str, command: str):
    try:
        code, out, err = session.run(command)
    except socket.timeout:
        return result(name, False, f"no answer within {COMMAND_TIMEOUT} s")
    except Exception as e:
        return result(name, False, f"couldn't run: {e}")
    if code == 0:
        return result(name, True, first_line(out) or first_line(err) or "ok")
    # 127 is "command not found" in bash; cmd.exe says "is not recognized"
    if code == 127 or "not recognized" in err:
        return result(name, False, "not found")
    return result(name, False, first_line(err) or first_line(out) or f"exit code {code}")


def check_shell(session, windows: bool):
    # the controller picks the MOTD commands from the name, so the host must match it
    name = "Git Bash" if windows else "Linux shell"
    try:
        code, out, err = session.run("uname -s")
    except Exception as e:
        return result(name, False, f"couldn't run uname: {e}")
    system = first_line(out)
    git_bash = system.startswith(("MINGW", "MSYS"))
    if code != 0:
        if windows:
            return result(name, False, "uname failed, Git Bash must be the default SSH shell")
        return result(name, False, first_line(err) or f"uname exit code {code}")
    if windows and not git_bash:
        return result(name, False, f"the name says Windows but the host runs {system or 'something else'}")
    if not windows and git_bash:
        return result(name, False, "the host runs Windows, but the name doesn't contain 'windows'")
    if not windows and system != "Linux":
        return result(name, False, f"the host runs {system or 'an unknown system'}, the controller expects Linux")
    return result(name, True, system)


def connect_error(e: Exception, host: str, user: str):
    if isinstance(e, paramiko.AuthenticationException):
        return f"{host} refused the key for user {user}"
    if isinstance(e, socket.timeout):
        return f"no answer from {host}:22 within {CONNECT_TIMEOUT} s"
    return f"can't connect to {host}:22: {e}"


def check_machine(name: str):
    """Logs in to the machine and reports, item by item, what it has and what's missing."""
    if name not in machines.list_machines():
        raise machines.MachineError(f"machine {name} not found", 404)
    windows = "windows" in name
    results = []

    def report():
        problems = sum(not r["ok"] for r in results)
        logger.info(f"checked machine {name}: {problems} problem(s)")
        return {
            "machine": name,
            "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "ok": problems == 0,
            "results": results,
        }

    try:
        host, user, keypath = inform.get_machine_secret(name)
    except Exception as e:
        results.append(result("credentials", False, f"can't read the machine's secret: {e}"))
        return report()
    results.append(result("credentials", True, f"{user}@{host}"))

    try:
        key = inform.load_key(keypath)
    except Exception as e:
        results.append(result("SSH key", False, f"the private key can't be read: {e}"))
        return report()
    results.append(result("SSH key", True, key.get_name()))

    try:
        session = Session(host, user, key)
    except Exception as e:
        results.append(result("SSH login", False, connect_error(e, host, user)))
        return report()
    results.append(result("SSH login", True, "connected"))

    try:
        results.append(check_shell(session, windows))
        for item, command in COMMON + (WINDOWS if windows else LINUX):
            results.append(run_check(session, item, command))
    finally:
        session.close()
    return report()
