"""Shared test setup. Nothing here talks to a real cluster or machine:

* every test runs against FakeCluster, swapped in for cluster.oc, and
  outside a test cluster.oc raises;
* running oc, kubectl or tkn raises, and KUBECONFIG points at an empty file
  in case a call slips past that;
* paramiko can't open connections, and machine checks run against FakeSSH;
* log.py writes to a temp dir, because /var/log/app only exists in the image;
* Task templates are read from tests/templates, not from GitHub.
"""
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="concurrency-control-tests-"))

# modules read these at import time
(TMP / "kubeconfig").write_text("")
os.environ["KUBECONFIG"] = str(TMP / "kubeconfig")
os.environ.pop("API_TOKEN", None)
os.environ.update({
    "NAMESPACE": "ci",
    "MACHINE_CM": "machine-status",
    "PIPELINE_CM": "pipeline-wait",
    "PRESET_CM": "pipeline-presets",
    # Task templates come from these files, never from GitHub
    "TEMPLATE_DIR": str(ROOT / "tests" / "templates"),
    "TEMPLATE_REPO": "https://github.com/crc-org/ci-definitions",
    "LABEL_MACHINE_PR": "machine",
    "LAEBL_MACHINE_TASK": "task-machine",
    "LABEL_TASK": "build-installer",
})


def load_log_module():
    # log.py creates /var/log/app when imported; load it with the folder moved
    path = ROOT / "log.py"
    source = path.read_text()
    folder_line = 'LOG_FOLDER = "/var/log/app/"'
    assert folder_line in source, "log.py no longer sets LOG_FOLDER like this, update conftest.py"
    source = source.replace(folder_line, f"LOG_FOLDER = {str(TMP / 'log') + os.sep!r}")
    module = types.ModuleType("log")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    sys.modules["log"] = module
    return module


log = load_log_module()

import checks  # noqa: E402
import cluster  # noqa: E402
import inform  # noqa: E402
import machines  # noqa: E402
import paramiko  # noqa: E402
import task_templates  # noqa: E402
import web  # noqa: E402
from fake_oc import FakeCluster  # noqa: E402
from fake_ssh import FakeSSH  # noqa: E402

TOKEN = "s3cret-token"

# The guards stay for the whole session rather than per test, because the UI
# tests' server thread can still be answering a request while a test tears down.
REAL_OC = cluster.oc
REAL_RUN = subprocess.run
SSH_ATTEMPTS = []


def refuse_oc(*args, **kwargs):
    raise AssertionError(f"oc {' '.join(args)} was called without the fake cluster")


def guarded_run(args, *rest, **kwargs):
    program = args[0] if isinstance(args, (list, tuple)) else str(args).split()[0]
    if os.path.basename(str(program)) in ("oc", "kubectl", "tkn"):
        raise AssertionError(f"a test tried to run the real {program}: {args}")
    return REAL_RUN(args, *rest, **kwargs)


def refuse_ssh(self, hostname, *args, **kwargs):
    SSH_ATTEMPTS.append(hostname)
    raise OSError("SSH is disabled in tests")


def refuse_http(url):
    raise AssertionError(f"a test tried to fetch {url}")


cluster.oc = refuse_oc
subprocess.run = guarded_run
paramiko.SSHClient.connect = refuse_ssh
task_templates.http_get = refuse_http


def pytest_addoption(parser):
    parser.addoption("--browser", action="append", choices=["chrome", "chromium", "firefox", "webkit"],
                     help="browser for the UI tests, repeatable (default: the installed Google Chrome)")
    parser.addoption("--headed", action="store_true", help="show the browser while the UI tests run")


@pytest.fixture(autouse=True)
def no_ssh():
    SSH_ATTEMPTS.clear()
    yield
    assert not SSH_ATTEMPTS, f"a test tried to SSH to {SSH_ATTEMPTS}"


@pytest.fixture
def real_oc():
    """cluster.oc as written, for testing it with subprocess.run replaced."""
    return REAL_OC


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    fake_cluster = FakeCluster(os.environ["MACHINE_CM"], os.environ["PIPELINE_CM"])
    monkeypatch.setattr(cluster, "oc", fake_cluster.oc)
    return fake_cluster


@pytest.fixture(autouse=True)
def fresh_templates():
    # the template catalog is cached for minutes; every test starts without it
    task_templates._cache.update(at=0.0, templates=None)


@pytest.fixture(autouse=True)
def log_file():
    # the dashboard shows info.log; every test starts with an empty one
    path = Path(log.INFO_LOG_FILE)
    path.write_text("")
    return path


@pytest.fixture
def api_token(monkeypatch):
    monkeypatch.setattr(web, "API_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def fake_ssh(monkeypatch, tmp_path):
    """Lets machine checks run: SSH sessions answer from fake_ssh.answers instead of a real machine."""
    ssh = FakeSSH()
    monkeypatch.setattr(checks, "Session", ssh.session)
    # secrets read from the cluster are written out as key files; keep them in the test's temp dir
    monkeypatch.setattr(machines, "KEY_CACHE_DIR", str(tmp_path / "keys"))
    return ssh


@pytest.fixture
def motd(monkeypatch):
    """Lets control.py run against the fake cluster: MOTD updates are recorded instead of sent."""
    calls = []
    monkeypatch.setattr(inform, "set_inform_message", lambda machine, run: calls.append(("set", machine, run)))
    monkeypatch.setattr(inform, "clean_inform_message", lambda machine: calls.append(("clear", machine)))
    return calls
