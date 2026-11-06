"""Runs the dashboard locally with demo data, against the same fake cluster and
fake SSH the tests use. Nothing touches a real cluster or machine.

    .venv/bin/python tests/demo_server.py [port]    # default 8080, admin token: demo

Task templates for the pipeline builder are read from a ci-definitions clone
next to this repo when there is one, otherwise from tests/templates.

The controller loop runs every 5 s, so a started preset is scheduled like in
the real deployment, and a running pipeline finishes after a minute. State is
in memory and starts over on every launch.
"""
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import conftest  # noqa: E402  same guards as the tests: real oc, kubectl, tkn and SSH all refuse

import checks  # noqa: E402
import cluster  # noqa: E402
import control  # noqa: E402
import inform  # noqa: E402
import machines  # noqa: E402
import task_templates  # noqa: E402
import web  # noqa: E402
from fake_oc import FakeCluster  # noqa: E402
from fake_ssh import LINUX_OK, WINDOWS_OK, FakeSSH, private_key  # noqa: E402
from log import logger  # noqa: E402

TOKEN = "demo"
LOOP_SECONDS = 5
RUN_SECONDS = 60


def demo_cluster():
    fake = FakeCluster(os.environ["MACHINE_CM"], os.environ["PIPELINE_CM"])
    for name, status, host in [("linux-x86-1", "busy", "10.0.0.11"), ("linux-arm64-1", "free", "10.0.0.12"),
                               ("windows-11", "free", "10.0.0.13"), ("linux-old", "maintenance", "10.0.0.14")]:
        fake.set_machine(name, status)
        fake.add_secret(name, host=host, username="crcqe", id_rsa=private_key())
    fake.add_pipelinerun("crc-e2e-linux-7xk2p", labels={"machine": "linux-x86-1"}, pending=False)
    fake.set_holds(runs=[("crc-e2e-linux-7xk2p", "linux-x86-1")])
    fake.add_pipeline("crc-e2e", description="CRC end-to-end tests",
                      params=[("crc-version", None), ("bundle", "4.17"), ("tags", ["e2e"], "array")],
                      workspaces=["source", ("cache", True)])
    fake.add_pipeline("crc-installer-windows", description="Builds and tests the Windows installer",
                      params=[("installer-version", None)], workspaces=["source"])
    fake.add("configmap", os.environ["PRESET_CM"], data={})
    fake.data(os.environ["PRESET_CM"])["e2e-linux"] = (
        '{"machine": "linux-arm64-1", "params": {"crc-version": "2.45.0"}, "pipeline": "crc-e2e", '
        '"service_account": "", "task_machine": "", "timeout": "", '
        '"workspaces": {"source": {"type": "volumeClaimTemplate", "value": "1Gi"}}}')
    return fake


def demo_ssh():
    """Per-host answers, so each demo machine shows something different when checked."""
    linux_without_go = {k: v for k, v in LINUX_OK.items() if k != "go version"}
    windows_without_dotnet = {k: v for k, v in WINDOWS_OK.items() if k != "dotnet --version"}
    hosts = {"10.0.0.11": LINUX_OK, "10.0.0.12": linux_without_go, "10.0.0.13": windows_without_dotnet}

    def session(host, user, key):
        ssh = FakeSSH()
        if host in hosts:
            ssh.answers = dict(hosts[host])
        elif host == "10.0.0.14":
            ssh.connect_error = ConnectionRefusedError(61, "Connection refused")
        return ssh.session(host, user, key)  # any other host, e.g. one you add: a healthy Linux machine

    return session


def controller_loop(fake):
    started = {}
    while True:
        try:
            control.monitor_task()
            control.monitor_runs_finish()
            control.monitor_pending_run()
            # stand in for the pipelines: a running run finishes after RUN_SECONDS
            for name, run in list(fake.objects["pipelinerun"].items()):
                if cluster.pipelinerun_status(run) == "Running":
                    if time.time() - started.setdefault(name, time.time()) > RUN_SECONDS:
                        fake.finish_pipelinerun(name)
        except Exception:
            logger.exception("demo controller loop failed")
        time.sleep(LOOP_SECONDS)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    fake = demo_cluster()
    cluster.oc = fake.oc
    checks.Session = demo_ssh()
    machines.KEY_CACHE_DIR = str(conftest.TMP / "keys")
    # MOTD updates would SSH to the machines; log them instead
    inform.set_inform_message = lambda machine, run: logger.info(f"(demo) MOTD on {machine}: in use by {run}")
    inform.clean_inform_message = lambda machine: logger.info(f"(demo) MOTD on {machine} cleared")
    web.API_TOKEN = TOKEN
    local_templates = HERE.parent.parent / "ci-definitions"
    if local_templates.is_dir():
        task_templates.TEMPLATE_DIR = str(local_templates)
    threading.Thread(target=controller_loop, args=(fake,), daemon=True).start()
    print(f"\n  Demo dashboard: http://localhost:{port}   admin token: {TOKEN}\n"
          f"  Task templates from {task_templates.TEMPLATE_DIR}\n"
          "  Fake cluster and fake SSH only, nothing real is touched. Ctrl+C to stop.\n")
    web.app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
