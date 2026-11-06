"""The machine check: checks.py and POST /api/machines/<name>/check.

SSH is replaced by FakeSSH (fake_ssh.py) and the cluster by FakeCluster, so
these tests never reach a real machine.
"""
import datetime
import socket

import paramiko
import pytest

import checks
import inform
import web
from fake_ssh import WINDOWS_OK, private_key

HOST, USER = "10.0.0.5", "crcqe"


@pytest.fixture
def client():
    return web.app.test_client()


def add_machine(fake, name="linux-1", status="free", key=None, **secret):
    fake.set_machine(name, status)
    values = {"host": HOST, "username": USER, "id_rsa": private_key() if key is None else key}
    values.update(secret)
    fake.add_secret(name, **values)


def results(report):
    return [(r["name"], r["ok"], r["detail"]) for r in report["results"]]


def result(report, name):
    found = [(ok, detail) for item, ok, detail in results(report) if item == name]
    assert found, f"no result named {name!r} in {results(report)}"
    return found[0]


# ---------------- a healthy machine ----------------

def test_linux_machine_with_everything_in_place(fake, fake_ssh):
    add_machine(fake)
    report = checks.check_machine("linux-1")
    assert report["ok"] is True
    assert results(report) == [
        ("credentials", True, f"{USER}@{HOST}"),
        ("SSH key", True, "ssh-ed25519"),
        ("SSH login", True, "connected"),
        ("Linux shell", True, "Linux"),
        ("git", True, "git version 2.45.1"),
        ("make", True, "GNU Make 4.4.1"),
        ("go", True, "go version go1.23.2 linux/amd64"),
        ("passwordless sudo", True, "ok"),
    ]
    assert fake_ssh.logins == [(HOST, USER, "ssh-ed25519")]
    assert fake_ssh.closed == 1


def test_windows_machine_with_everything_in_place(fake, fake_ssh):
    add_machine(fake, "windows-11")
    fake_ssh.answers = dict(WINDOWS_OK)
    report = checks.check_machine("windows-11")
    assert report["ok"] is True
    assert [item for item, _, _ in results(report)] == [
        "credentials", "SSH key", "SSH login", "Git Bash", "git", "make", "go", "dotnet"]
    assert result(report, "Git Bash") == (True, "MINGW64_NT-10.0-22631")
    assert result(report, "dotnet") == (True, "8.0.403")
    assert "sudo -n true && echo ok" not in fake_ssh.commands


def test_report_says_when_and_which_machine(fake, fake_ssh):
    add_machine(fake)
    report = checks.check_machine("linux-1")
    assert report["machine"] == "linux-1"
    checked_at = datetime.datetime.fromisoformat(report["checked_at"])
    assert checked_at.tzinfo is not None
    assert abs(datetime.datetime.now(datetime.timezone.utc) - checked_at) < datetime.timedelta(minutes=1)


@pytest.mark.parametrize("kind,name", [("rsa", "ssh-rsa"), ("ecdsa", "ecdsa-sha2-nistp256"), ("ed25519", "ssh-ed25519")])
def test_every_key_type_ssh_keygen_makes_is_accepted(fake, fake_ssh, kind, name):
    add_machine(fake, key=private_key(kind))
    report = checks.check_machine("linux-1")
    assert result(report, "SSH key") == (True, name)
    assert fake_ssh.logins == [(HOST, USER, name)]


def test_check_only_reads(fake, fake_ssh):
    # nothing that installs, writes or deletes; sudo -n only asks whether a password is needed
    add_machine(fake)
    checks.check_machine("linux-1")
    for command in fake_ssh.commands:
        assert not any(word in command for word in (">", "rm ", "install", "tee", "apt", "dnf", "yum")), command


# ---------------- what's missing or broken inside the machine ----------------

def test_missing_tool_is_reported(fake, fake_ssh):
    add_machine(fake)
    del fake_ssh.answers["go version"]
    report = checks.check_machine("linux-1")
    assert report["ok"] is False
    assert result(report, "go") == (False, "not found")
    assert result(report, "make") == (True, "GNU Make 4.4.1")  # the other checks still run


def test_tool_missing_under_cmd_is_reported(fake, fake_ssh):
    add_machine(fake, "windows-11")
    fake_ssh.answers = dict(WINDOWS_OK)
    fake_ssh.answers["dotnet --version"] = (1, "", "'dotnet' is not recognized as an internal or external command,\n")
    assert result(checks.check_machine("windows-11"), "dotnet") == (False, "not found")


def test_failing_command_shows_its_error(fake, fake_ssh):
    add_machine(fake)
    fake_ssh.answers["sudo -n true && echo ok"] = (1, "", "sudo: a password is required\n")
    assert result(checks.check_machine("linux-1"), "passwordless sudo") == (False, "sudo: a password is required")


def test_failing_command_without_output_shows_the_exit_code(fake, fake_ssh):
    add_machine(fake)
    fake_ssh.answers["git --version"] = (2, "", "")
    assert result(checks.check_machine("linux-1"), "git") == (False, "exit code 2")


def test_long_output_is_cut(fake, fake_ssh):
    add_machine(fake)
    fake_ssh.answers["git --version"] = (0, "\n\n" + "x" * 500 + "\nsecond line\n", "")
    ok, detail = result(checks.check_machine("linux-1"), "git")
    assert ok and detail == "x" * checks.DETAIL_MAX


def test_command_that_hangs_is_reported_and_the_rest_still_runs(fake, fake_ssh):
    add_machine(fake)
    fake_ssh.answers["make --version | head -n 1"] = socket.timeout()
    report = checks.check_machine("linux-1")
    assert result(report, "make") == (False, f"no answer within {checks.COMMAND_TIMEOUT} s")
    assert result(report, "go")[0] is True
    assert fake_ssh.closed == 1


@pytest.mark.parametrize("name,uname,expected", [
    ("windows-11", (0, "Linux\n", ""), ("Git Bash", "the name says Windows but the host runs Linux")),
    ("windows-11", (1, "", "'uname' is not recognized as an internal or external command,\n"),
     ("Git Bash", "uname failed, Git Bash must be the default SSH shell")),
    ("linux-1", (0, "MINGW64_NT-10.0-22631\n", ""),
     ("Linux shell", "the host runs Windows, but the name doesn't contain 'windows'")),
    ("linux-1", (0, "Darwin\n", ""), ("Linux shell", "the host runs Darwin, the controller expects Linux")),
])
def test_host_that_does_not_match_its_name(fake, fake_ssh, name, uname, expected):
    # the controller picks the MOTD commands from the name, so a mismatch breaks the MOTD
    add_machine(fake, name)
    fake_ssh.answers["uname -s"] = uname
    item, detail = expected
    assert result(checks.check_machine(name), item) == (False, detail)


# ---------------- can't get in ----------------

def test_missing_secret_stops_before_ssh(fake, fake_ssh):
    fake.set_machine("linux-1", "free")
    report = checks.check_machine("linux-1")
    assert report["ok"] is False
    [(item, ok, detail)] = results(report)
    assert (item, ok) == ("credentials", False)
    assert "secret linux-1 not found" in detail
    assert fake_ssh.logins == []


@pytest.mark.parametrize("key", ["not a key\n", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n"
                                                "-----END OPENSSH PRIVATE KEY-----\n"])
def test_unreadable_key_stops_before_ssh(fake, fake_ssh, key):
    add_machine(fake, key=key)
    report = checks.check_machine("linux-1")
    ok, detail = result(report, "SSH key")
    assert not ok and detail.startswith("the private key can't be read: ")
    assert [item for item, _, _ in results(report)] == ["credentials", "SSH key"]
    assert fake_ssh.logins == []


@pytest.mark.parametrize("error,expected", [
    (paramiko.AuthenticationException("Authentication failed."), f"{HOST} refused the key for user {USER}"),
    (socket.timeout(), f"no answer from {HOST}:22 within {checks.CONNECT_TIMEOUT} s"),
    (ConnectionRefusedError(61, "Connection refused"), f"can't connect to {HOST}:22: [Errno 61] Connection refused"),
    (socket.gaierror(8, "nodename nor servname provided, or not known"),
     f"can't connect to {HOST}:22: [Errno 8] nodename nor servname provided, or not known"),
])
def test_failed_login_says_why(fake, fake_ssh, error, expected):
    add_machine(fake)
    fake_ssh.connect_error = error
    report = checks.check_machine("linux-1")
    assert results(report)[-1] == ("SSH login", False, expected)
    assert fake_ssh.commands == []


def test_real_session_uses_the_key_and_timeouts(monkeypatch):
    # checks.Session itself, with paramiko's connect captured instead of opening a connection
    calls = []
    monkeypatch.setattr(paramiko.SSHClient, "connect", lambda self, host, **kwargs: calls.append((host, kwargs)))
    key = object()
    checks.Session(HOST, USER, key)
    assert calls == [(HOST, {"username": USER, "pkey": key, "timeout": checks.CONNECT_TIMEOUT,
                             "banner_timeout": checks.CONNECT_TIMEOUT, "auth_timeout": checks.CONNECT_TIMEOUT})]


def test_motd_uses_the_same_key_loader(tmp_path):
    path = tmp_path / "id_ed25519"
    path.write_text(private_key("ed25519"))
    assert inform.load_key(str(path)).get_name() == "ssh-ed25519"


# ---------------- over HTTP ----------------

def test_check_over_http(client, fake, fake_ssh, api_token):
    add_machine(fake)
    resp = client.post("/api/machines/linux-1/check", headers={"Authorization": f"Bearer {api_token}"})
    assert resp.status_code == 200
    assert resp.json["machine"] == "linux-1"
    assert resp.json["ok"] is True
    assert resp.json["results"][0] == {"name": "credentials", "ok": True, "detail": f"{USER}@{HOST}"}


def test_check_unknown_machine_is_404(client, fake, fake_ssh, api_token):
    resp = client.post("/api/machines/nope/check", headers={"Authorization": f"Bearer {api_token}"})
    assert resp.status_code == 404
    assert resp.json == {"error": "machine nope not found"}
    assert fake_ssh.logins == []


def test_check_is_not_a_get(client, fake, api_token):
    add_machine(fake)
    assert client.get("/api/machines/linux-1/check", headers={"Authorization": f"Bearer {api_token}"}).status_code == 405


def test_check_is_logged(client, fake, fake_ssh, api_token, log_file):
    add_machine(fake)
    del fake_ssh.answers["go version"]
    client.post("/api/machines/linux-1/check", headers={"Authorization": f"Bearer {api_token}"})
    assert "checked machine linux-1: 1 problem(s)" in log_file.read_text()
