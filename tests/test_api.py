"""The dashboard page and the machine API over HTTP (web.py, machines.py).

Tests marked xfail(strict=True) pin down known bugs: they fail today and
start "passing" once the bug is fixed, which strict mode reports as an error
so the marker gets removed.
"""
import json
import re
import subprocess

import pytest

import control
import log
import web

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
MISSING = object()


@pytest.fixture
def client():
    return web.app.test_client()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def machine(**fields):
    body = {"name": "linux-arm64-1", "host": "10.0.0.5", "username": "crcqe", "private_key": KEY}
    body.update(fields)
    return {k: v for k, v in body.items() if v is not MISSING}


def page_data(html):
    """The values the dashboard script starts from, parsed back out of the page."""
    script = html.split("<script>")[1].split("</script>")[0]
    return {name: json.loads(re.search(rf"(?:const|let) {name} = (.*);", script).group(1))
            for name in ("API_ENABLED", "LABEL_TASK", "machines", "runs")}


# ---------------- dashboard page ----------------

def test_dashboard_embeds_machines_runs_and_settings(client, fake):
    fake.set_machine("linux-1", "busy")
    fake.set_machine("mac-1", "free")
    fake.set_holds(runs=[("run-a", "linux-1")], tasks=[("run-a", "mac-1")])
    resp = client.get("/")
    assert resp.status_code == 200
    assert page_data(resp.get_data(as_text=True)) == {
        "API_ENABLED": False,
        "LABEL_TASK": "build-installer",
        "machines": {"linux-1": "busy", "mac-1": "free"},
        "runs": [{"pipelinerun": "run-a", "machine": "linux-1", "until": "run"},
                 {"pipelinerun": "run-a", "machine": "mac-1", "until": "task"}],
    }


def test_dashboard_knows_when_the_api_is_enabled(client, api_token):
    html = client.get("/").get_data(as_text=True)
    assert page_data(html)["API_ENABLED"] is True
    assert api_token not in html


def test_dashboard_shows_the_log_escaped(client, log_file):
    log_file.write_text('pipelinerun <img src=x onerror="alert(1)"> started\n')
    html = client.get("/").get_data(as_text=True)
    assert "<img" not in html
    assert "pipelinerun &lt;img src=x" in html


def test_dashboard_data_cannot_close_the_script_tag(client, fake):
    payload = "</script><script>alert(1)</script>"
    fake.set_machine("linux-1", payload)
    fake.set_holds(runs=[(payload, "linux-1")])
    html = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)" not in html
    data = page_data(html)
    assert data["machines"] == {"linux-1": payload}
    assert data["runs"][0]["pipelinerun"] == payload


def test_dashboard_without_task_holds(client, fake):
    fake.set_holds(runs=[("run-a", "linux-1")])
    del fake.data(fake.pipeline_cm)["wait_finish_task"]
    resp = client.get("/")
    assert resp.status_code == 200
    assert page_data(resp.get_data(as_text=True))["runs"] == [
        {"pipelinerun": "run-a", "machine": "linux-1", "until": "run"}]


def test_dashboard_without_pipeline_configmap(client, fake):
    del fake.objects["configmap"][fake.pipeline_cm]
    resp = client.get("/")
    assert resp.status_code == 200
    assert page_data(resp.get_data(as_text=True))["runs"] == []


# ---------------- log ----------------

def test_log_returns_the_last_100_lines(client, log_file):
    log_file.write_text("".join(f"line {i}\n" for i in range(1, 151)))
    lines = client.get("/log").json["log"].splitlines()
    assert len(lines) == 100
    assert (lines[0], lines[-1]) == ("line 51", "line 150")


def test_log_without_a_log_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(log, "INFO_LOG_FILE", str(tmp_path / "missing.log"))
    assert client.get("/log").json == {"log": "Log file not found."}


def test_controller_log_lines_reach_the_dashboard(client):
    log.logger.info("pipelinerun run-a waits for machine linux-1")
    assert "pipelinerun run-a waits for machine linux-1" in client.get("/log").json["log"]


# ---------------- reading ----------------

def test_list_machines_needs_no_token(client, fake, api_token):
    fake.set_machine("linux-1", "free")
    fake.set_machine("win-1", "maintenance")
    resp = client.get("/api/machines")
    assert resp.status_code == 200
    assert resp.json == {"linux-1": "free", "win-1": "maintenance"}


def test_list_runs(client, fake):
    fake.set_holds(runs=[("run-a", "linux-1"), ("run-b", "mac-1")], tasks=[("run-a", "win-1")])
    assert client.get("/api/runs").json == [
        {"pipelinerun": "run-a", "machine": "linux-1", "until": "run"},
        {"pipelinerun": "run-b", "machine": "mac-1", "until": "run"},
        {"pipelinerun": "run-a", "machine": "win-1", "until": "task"},
    ]


def test_list_runs_when_nothing_holds_a_machine(client):
    assert client.get("/api/runs").json == []


@pytest.mark.xfail(strict=True, reason="an unreadable MACHINE_CM is answered with 200 {}, so the dashboard says "
                                       "'No machines registered yet.' instead of flagging the error")
def test_unreadable_machine_list_is_an_error_not_an_empty_list(client, fake):
    fake.set_machine("linux-1", "free")
    fake.fail("get", "configmap", fake.machine_cm, stderr="Unable to connect to the server: dial tcp: i/o timeout")
    assert client.get("/api/machines").status_code >= 500


# ---------------- authentication ----------------

WRITES = [("GET", "/api/token-check"), ("POST", "/api/machines"), ("DELETE", "/api/machines/linux-1"),
          ("POST", "/api/machines/linux-1/check"), ("PUT", "/api/presets/p"), ("DELETE", "/api/presets/p"),
          ("POST", "/api/presets/p/start"), ("PUT", "/api/pipelines/p"), ("DELETE", "/api/pipelines/p")]


@pytest.mark.parametrize("method,path", WRITES)
@pytest.mark.parametrize("token", [None, ""])
def test_writes_are_off_without_api_token(client, fake, monkeypatch, method, path, token):
    monkeypatch.setattr(web, "API_TOKEN", token)
    fake.set_machine("linux-1", "free")
    resp = client.open(path, method=method, json=machine(), headers=bearer("anything"))
    assert resp.status_code == 403
    assert "API_TOKEN" in resp.json["error"]
    assert fake.machines() == {"linux-1": "free"}


@pytest.mark.parametrize("method,path", WRITES)
@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer wrong", "Bearer {token}x", "Bearer x{token}",
                                    "{token}", "Basic {token}", "Token {token}"])
def test_writes_need_the_right_token(client, fake, api_token, method, path, header):
    fake.set_machine("linux-1", "free")
    headers = {} if header is None else {"Authorization": header.format(token=api_token)}
    resp = client.open(path, method=method, json=machine(), headers=headers)
    assert resp.status_code == 401
    assert resp.json == {"error": "unauthorized"}
    assert fake.machines() == {"linux-1": "free"}


def test_token_check_accepts_the_token(client, api_token):
    resp = client.get("/api/token-check", headers=bearer(api_token))
    assert resp.status_code == 204


@pytest.mark.xfail(strict=True, reason="hmac.compare_digest raises TypeError for non-ASCII str, so this is a 500")
def test_non_ascii_token_is_refused_not_a_server_error(client, api_token):
    resp = client.get("/api/token-check", headers={"Authorization": "Bearer café"})
    assert resp.status_code == 401


# ---------------- adding machines ----------------

def test_add_machine(client, fake, api_token):
    resp = client.post("/api/machines", json=machine(), headers=bearer(api_token))
    assert resp.status_code == 201
    assert resp.json == {"name": "linux-arm64-1", "status": "free"}
    assert fake.machines() == {"linux-arm64-1": "free"}
    assert fake.secret("linux-arm64-1") == {
        "labels": {"concurrency-control/machine": "true"},
        "data": {"host": "10.0.0.5", "username": "crcqe", "id_rsa": KEY + "\n"},
    }


def test_add_machine_keeps_a_single_trailing_newline(client, fake, api_token):
    client.post("/api/machines", json=machine(private_key=KEY + "\n"), headers=bearer(api_token))
    assert fake.secret("linux-arm64-1")["data"]["id_rsa"] == KEY + "\n"


def test_add_machine_passes_the_key_on_stdin_only(client, fake, api_token):
    client.post("/api/machines", json=machine(), headers=bearer(api_token))
    assert not [args for args, _ in fake.calls if any("PRIVATE KEY" in arg for arg in args)]
    assert [stdin for _, stdin in fake.calls if stdin and "PRIVATE KEY" in stdin]


def test_add_machine_is_logged(client, api_token):
    client.post("/api/machines", json=machine(), headers=bearer(api_token))
    assert "machine linux-arm64-1 (crcqe@10.0.0.5) added" in client.get("/log").json["log"]


@pytest.mark.parametrize("name", ["a", "0", "linux-arm64-1", "1-2-3", "a" * 63])
def test_add_machine_accepts_dns_labels(client, fake, api_token, name):
    resp = client.post("/api/machines", json=machine(name=name), headers=bearer(api_token))
    assert resp.status_code == 201
    assert fake.machines() == {name: "free"}


@pytest.mark.parametrize("name", [MISSING, None, "", "Linux-1", "linux_1", "linux.1", "-linux", "linux-",
                                  "linux 1", "a" * 64, "línux", "../etc"])
def test_add_machine_rejects_invalid_names(client, fake, api_token, name):
    resp = client.post("/api/machines", json=machine(name=name), headers=bearer(api_token))
    assert resp.status_code == 400
    assert "invalid machine name" in resp.json["error"]
    assert fake.machines() == {} and fake.objects["secret"] == {}


@pytest.mark.parametrize("field", ["host", "username", "private_key"])
@pytest.mark.parametrize("value", [MISSING, None, ""])
def test_add_machine_requires_every_field(client, fake, api_token, field, value):
    resp = client.post("/api/machines", json=machine(**{field: value}), headers=bearer(api_token))
    assert resp.status_code == 400
    assert resp.json == {"error": "host, username and private_key are required"}
    assert fake.machines() == {} and fake.objects["secret"] == {}


def test_add_machine_without_json(client, fake, api_token):
    resp = client.post("/api/machines", data={"name": "linux-1"}, headers=bearer(api_token))
    assert resp.status_code == 400
    assert fake.machines() == {}


@pytest.mark.xfail(strict=True, reason="body.get() on a JSON array/string/number raises AttributeError, a 500")
@pytest.mark.parametrize("body", [["linux-1"], "linux-1", 42, True])
def test_add_machine_rejects_json_that_is_not_an_object(client, api_token, body):
    resp = client.post("/api/machines", json=body, headers=bearer(api_token))
    assert resp.status_code == 400


@pytest.mark.xfail(strict=True, reason="fields aren't type-checked: non-strings crash or are refused by the API server, a 500")
@pytest.mark.parametrize("field,value", [("name", 123), ("name", ["linux-1"]), ("host", {"ip": "10.0.0.5"}),
                                         ("username", 5), ("private_key", 123), ("private_key", True)])
def test_add_machine_rejects_fields_that_are_not_strings(client, api_token, field, value):
    resp = client.post("/api/machines", json=machine(**{field: value}), headers=bearer(api_token))
    assert resp.status_code == 400


@pytest.mark.xfail(strict=True, reason="NAME_RE ends in $, which also matches before a trailing newline; "
                                       "the API server then refuses the secret name, a 500")
def test_add_machine_rejects_a_name_with_trailing_newline(client, api_token):
    resp = client.post("/api/machines", json=machine(name="linux-1\n"), headers=bearer(api_token))
    assert resp.status_code == 400


@pytest.mark.xfail(strict=True, reason="whitespace-only values pass the required check and register a machine "
                                       "the controller can't reach")
@pytest.mark.parametrize("field", ["host", "username", "private_key"])
def test_add_machine_rejects_blank_fields(client, fake, api_token, field):
    resp = client.post("/api/machines", json=machine(**{field: "   "}), headers=bearer(api_token))
    assert resp.status_code == 400


def test_add_existing_machine_is_409(client, fake, api_token):
    fake.set_machine("linux-arm64-1", "busy")
    resp = client.post("/api/machines", json=machine(), headers=bearer(api_token))
    assert resp.status_code == 409
    assert resp.json == {"error": "machine linux-arm64-1 already exists"}
    assert fake.machines() == {"linux-arm64-1": "busy"}
    assert fake.objects["secret"] == {}


def test_add_machine_leaves_an_existing_secret_alone(client, fake, api_token):
    # e.g. the mounted secret of a machine that was taken out of MACHINE_CM
    fake.add_secret("linux-arm64-1", host="old-host", username="old", id_rsa="old-key")
    resp = client.post("/api/machines", json=machine(), headers=bearer(api_token))
    assert resp.status_code in (409, 500)
    assert "already exists" in resp.json["error"]
    assert fake.secret("linux-arm64-1")["data"]["host"] == "old-host"
    assert fake.machines() == {}


def test_add_machine_reports_a_failed_secret(client, fake, api_token):
    fake.fail("create", stderr='Error from server (Forbidden): secrets is forbidden: User '
                               '"system:serviceaccount:ci:controller" cannot create resource "secrets"')
    resp = client.post("/api/machines", json=machine(), headers=bearer(api_token))
    assert resp.status_code == 500
    assert "cannot create resource" in resp.json["error"]
    assert fake.machines() == {}


def test_add_machine_rolls_back_the_secret_if_the_configmap_fails(client, fake, api_token):
    fake.fail("set", "data", "configmap")
    resp = client.post("/api/machines", json=machine(), headers=bearer(api_token))
    assert resp.status_code == 500
    assert fake.secret("linux-arm64-1") is None
    assert fake.machines() == {}


# ---------------- removing machines ----------------

def test_remove_machine_added_through_the_api(client, fake, api_token):
    client.post("/api/machines", json=machine(), headers=bearer(api_token))
    resp = client.delete("/api/machines/linux-arm64-1", headers=bearer(api_token))
    assert resp.status_code == 204
    assert fake.machines() == {}
    assert fake.secret("linux-arm64-1") is None


def test_remove_machine_keeps_a_secret_it_did_not_create(client, fake, api_token):
    fake.set_machine("linux-1", "free")
    fake.add_secret("linux-1", host="10.0.0.6", username="crcqe", id_rsa=KEY)  # by hand, mounted
    resp = client.delete("/api/machines/linux-1", headers=bearer(api_token))
    assert resp.status_code == 204
    assert fake.machines() == {}
    assert fake.secret("linux-1") is not None


@pytest.mark.parametrize("status", ["free", "maintenance", "broken"])
def test_remove_machine_that_is_not_busy(client, fake, api_token, status):
    fake.set_machine("linux-1", status)
    assert client.delete("/api/machines/linux-1", headers=bearer(api_token)).status_code == 204
    assert fake.machines() == {}


def test_remove_busy_machine_is_409(client, fake, api_token):
    fake.set_machine("linux-1", "busy")
    fake.add_secret("linux-1", labels={"concurrency-control/machine": "true"}, host="h", username="u", id_rsa=KEY)
    resp = client.delete("/api/machines/linux-1", headers=bearer(api_token))
    assert resp.status_code == 409
    assert resp.json == {"error": "machine linux-1 is busy, retry when it is free"}
    assert fake.machines() == {"linux-1": "busy"}
    assert fake.secret("linux-1") is not None


def test_remove_unknown_machine_is_404(client, api_token):
    resp = client.delete("/api/machines/linux-1", headers=bearer(api_token))
    assert resp.status_code == 404
    assert resp.json == {"error": "machine linux-1 not found"}


@pytest.mark.parametrize("name", ["Linux-1", "linux_1", "-linux"])
def test_remove_invalid_name_is_400(client, fake, api_token, name):
    fake.set_machine(name, "free")
    resp = client.delete(f"/api/machines/{name}", headers=bearer(api_token))
    assert resp.status_code == 400
    assert fake.machines() == {name: "free"}


def test_remove_machine_reports_a_failed_configmap_update(client, fake, api_token):
    fake.set_machine("linux-1", "free")
    fake.add_secret("linux-1", labels={"concurrency-control/machine": "true"}, host="h", username="u", id_rsa=KEY)
    fake.fail("set", "data", "configmap")
    resp = client.delete("/api/machines/linux-1", headers=bearer(api_token))
    assert resp.status_code == 500
    assert fake.machines() == {"linux-1": "free"}
    assert fake.secret("linux-1") is not None


# ---------------- with the controller ----------------

def test_dashboard_follows_a_run_through_the_controller(client, fake, api_token, motd):
    fake.set_machine("linux-1", "free")
    fake.set_machine("win-1", "free")
    fake.add_pipelinerun("run-a", labels={"machine": "linux-1", "task-machine": "win-1"})

    control.monitor_pending_run()
    assert client.get("/api/machines").json == {"linux-1": "busy", "win-1": "busy"}
    assert client.get("/api/runs").json == [
        {"pipelinerun": "run-a", "machine": "linux-1", "until": "run"},
        {"pipelinerun": "run-a", "machine": "win-1", "until": "task"},
    ]
    assert client.delete("/api/machines/linux-1", headers=bearer(api_token)).status_code == 409

    fake.add_taskrun("run-a-build", "run-a", "build-installer", succeeded="True")
    control.monitor_task()
    assert client.get("/api/machines").json == {"linux-1": "busy", "win-1": "free"}
    assert client.get("/api/runs").json == [{"pipelinerun": "run-a", "machine": "linux-1", "until": "run"}]

    fake.finish_pipelinerun("run-a")
    control.monitor_runs_finish()
    assert client.get("/api/machines").json == {"linux-1": "free", "win-1": "free"}
    assert client.get("/api/runs").json == []
    assert motd == [("set", "linux-1", "run-a"), ("set", "win-1", "run-a"), ("clear", "win-1"), ("clear", "linux-1")]


@pytest.mark.xfail(strict=True, reason="remove only refuses status 'busy'; a drained machine (the README says to set "
                                       "it to maintenance) that a run still holds is removed, and the controller "
                                       "writes it back as free when the run finishes")
def test_removed_machine_stays_removed_when_its_run_finishes(client, fake, api_token, motd):
    fake.set_machine("linux-1", "maintenance")
    fake.add_pipelinerun("run-a", labels={"machine": "linux-1"}, pending=False)
    fake.set_holds(runs=[("run-a", "linux-1")])
    removed = client.delete("/api/machines/linux-1", headers=bearer(api_token)).status_code == 204

    fake.finish_pipelinerun("run-a")
    control.monitor_runs_finish()
    assert not (removed and "linux-1" in fake.machines()), "DELETE succeeded, then the machine came back as free"


# ---------------- cluster access ----------------

def test_oc_runs_without_a_shell_and_takes_input_on_stdin(monkeypatch, real_oc):
    seen = {}

    def run(args, **kwargs):
        seen.update(kwargs, args=args)
        return subprocess.CompletedProcess(args, 0, "out", "err")

    monkeypatch.setattr(subprocess, "run", run)
    assert real_oc("create", "-f", "-", input='{"kind": "Secret"}') == (0, "out", "err")
    assert seen["args"] == ["oc", "create", "-f", "-"]
    assert seen["input"] == '{"kind": "Secret"}'
    assert not seen.get("shell")
