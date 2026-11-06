"""Pipeline presets: presets.py and the /api/pipelines and /api/presets endpoints.

Everything runs against FakeCluster, which stands in for oc and Tekton.
"""
import json

import pytest

import control
import presets
import web

PRESET_CM = "pipeline-presets"


@pytest.fixture
def client():
    return web.app.test_client()


@pytest.fixture
def auth(api_token):
    return {"Authorization": f"Bearer {api_token}"}


@pytest.fixture
def setup(fake):
    """Two machines and an e2e pipeline with a required and an optional parameter."""
    fake.set_machine("linux-1", "free")
    fake.set_machine("windows-11", "free")
    fake.add_pipeline("crc-e2e", params=[("crc-version", None), ("bundle", "4.17"), ("tags", ["e2e"], "array")],
                      workspaces=["source", ("cache", True)])
    return fake


def preset(**fields):
    body = {"pipeline": "crc-e2e", "machine": "linux-1", "params": {"crc-version": "2.45.0"},
            "workspaces": {"source": {"type": "volumeClaimTemplate", "value": "1Gi"}}}
    body.update(fields)
    return body


def saved(fake):
    return {k: json.loads(v) for k, v in fake.data(PRESET_CM).items()}


def runs(fake):
    return fake.objects["pipelinerun"]


# ---------------- pipelines ----------------

def test_list_pipelines_shows_params_and_workspaces(client, setup):
    setup.add_pipeline("another", params=[("x", "1")])
    resp = client.get("/api/pipelines")
    assert resp.status_code == 200
    assert [p["name"] for p in resp.json] == ["another", "crc-e2e"]
    e2e = resp.json[1]
    assert e2e["params"] == [
        {"name": "crc-version", "type": "string", "default": None, "description": "the crc-version"},
        {"name": "bundle", "type": "string", "default": "4.17", "description": "the bundle"},
        {"name": "tags", "type": "array", "default": ["e2e"], "description": "the tags"},
    ]
    assert e2e["workspaces"] == [{"name": "source", "optional": False, "description": ""},
                                 {"name": "cache", "optional": True, "description": ""}]


def test_unreadable_pipelines_are_an_error(client, fake):
    fake.fail("get", "pipeline", stderr='Error from server (Forbidden): pipelines.tekton.dev is forbidden')
    resp = client.get("/api/pipelines")
    assert resp.status_code == 502
    assert resp.json == {"error": "can't read the pipelines from the cluster"}


# ---------------- saving ----------------

def test_save_preset(client, setup, auth):
    resp = client.put("/api/presets/e2e-linux", json=preset(), headers=auth)
    assert resp.status_code == 200
    assert saved(setup) == {"e2e-linux": {
        "pipeline": "crc-e2e", "machine": "linux-1", "task_machine": "", "params": {"crc-version": "2.45.0"},
        "workspaces": {"source": {"type": "volumeClaimTemplate", "value": "1Gi"}},
        "service_account": "", "timeout": ""}}
    assert client.get("/api/presets").json == saved(setup)


def test_first_save_creates_the_configmap(client, setup, auth):
    assert PRESET_CM not in setup.objects["configmap"]
    client.put("/api/presets/e2e-linux", json=preset(), headers=auth)
    assert ("create", "configmap", PRESET_CM) in [args for args, _ in setup.calls]
    client.put("/api/presets/e2e-other", json=preset(), headers=auth)
    assert sorted(saved(setup)) == ["e2e-linux", "e2e-other"]


def test_save_replaces_a_preset(client, setup, auth):
    client.put("/api/presets/e2e-linux", json=preset(), headers=auth)
    client.put("/api/presets/e2e-linux", json=preset(params={"crc-version": "2.46.0"}), headers=auth)
    assert saved(setup)["e2e-linux"]["params"] == {"crc-version": "2.46.0"}


def test_empty_params_fall_back_to_the_default(client, setup, auth):
    client.put("/api/presets/p", json=preset(params={"crc-version": "2.45.0", "bundle": ""}), headers=auth)
    assert saved(setup)["p"]["params"] == {"crc-version": "2.45.0"}


def test_list_presets_without_any(client, setup):
    assert client.get("/api/presets").json == {}


def test_broken_preset_in_the_configmap_is_skipped(client, setup, auth, log_file):
    client.put("/api/presets/good", json=preset(), headers=auth)
    setup.data(PRESET_CM)["broken"] = "{not json"
    assert list(client.get("/api/presets").json) == ["good"]
    assert "preset broken" in (log_file.parent / "error.log").read_text()


@pytest.mark.parametrize("name", ["a", "e2e-linux", "x" * 57])
def test_preset_names_that_fit_a_run_name(client, setup, auth, name):
    assert client.put(f"/api/presets/{name}", json=preset(), headers=auth).status_code == 200


@pytest.mark.parametrize("name", ["E2E", "e2e_linux", "-e2e", "e2e-", "x" * 58, "e2e.linux"])
def test_preset_names_that_do_not(client, setup, auth, name):
    resp = client.put(f"/api/presets/{name}", json=preset(), headers=auth)
    assert resp.status_code == 400
    assert "invalid preset name" in resp.json["error"]


@pytest.mark.parametrize("fields,error", [
    ({"pipeline": None}, "pipeline is required"),
    ({"pipeline": "nope"}, "pipeline nope doesn't exist"),
    ({"pipeline": 5}, "pipeline must be a string"),
    ({"machine": None}, "machine is required"),
    ({"machine": "mac-9"}, "machine mac-9 isn't registered"),
    ({"task_machine": "mac-9"}, "machine mac-9 isn't registered"),
    ({"task_machine": "linux-1"}, "the task machine must differ from the machine"),
    ({"params": {}}, "parameter crc-version is required, the pipeline has no default for it"),
    ({"params": {"crc-version": ""}}, "parameter crc-version is required, the pipeline has no default for it"),
    ({"params": {"crc-version": "2.45.0", "nope": "1"}}, "pipeline crc-e2e has no parameter nope"),
    ({"params": {"crc-version": "2.45.0", "tags": "x"}}, "parameter tags is an array, only strings can be set"),
    ({"params": {"crc-version": 2}}, "params must map parameter names to strings"),
    ({"params": ["crc-version"]}, "params must map parameter names to strings"),
    ({"workspaces": {}}, "workspace source must be bound"),
    ({"workspaces": {"source": {"type": "hostPath", "value": "/"}}}, "workspace source needs a type"),
    ({"workspaces": {"source": {"type": "volumeClaimTemplate", "value": "lots"}}},
     "workspace source: size must look like 500Mi, 1Gi or 2Ti"),
    ({"workspaces": {"source": {"type": "secret", "value": "Not_A_Name"}}},
     "workspace source: 'Not_A_Name' isn't a valid object name"),
    ({"workspaces": {"source": {"type": "secret"}}}, "workspace source needs a name"),
    ({"workspaces": {"source": {"type": "volumeClaimTemplate", "value": ""}}}, "workspace source needs a size"),
    ({"workspaces": {"source": {"type": "emptyDir"}, "nope": {"type": "emptyDir"}}}, "pipeline crc-e2e has no workspace nope"),
    ({"service_account": "Bad Name"}, "'Bad Name' isn't a valid service account name"),
    ({"timeout": "soon"}, "timeout must look like 3h, 90m or 1h30m"),
    ({"timeout": "h"}, "timeout must look like 3h, 90m or 1h30m"),
])
def test_invalid_presets_are_refused(client, setup, auth, fields, error):
    resp = client.put("/api/presets/p", json=preset(**fields), headers=auth)
    assert resp.status_code == 400
    assert error in resp.json["error"]
    assert PRESET_CM not in setup.objects["configmap"] or saved(setup) == {}


@pytest.mark.parametrize("body", [None, ["p"], "p", 42])
def test_preset_must_be_a_json_object(client, setup, auth, body):
    kwargs = {"data": "not json", "content_type": "application/json"} if body is None else {"json": body}
    resp = client.put("/api/presets/p", headers=auth, **kwargs)
    assert resp.status_code == 400
    assert resp.json == {"error": "the preset must be a JSON object"}


def test_optional_workspace_can_be_left_unbound_or_bound(client, setup, auth):
    body = preset(workspaces={"source": {"type": "persistentVolumeClaim", "value": "crc-src"},
                              "cache": {"type": "emptyDir"}})
    assert client.put("/api/presets/p", json=body, headers=auth).status_code == 200
    assert saved(setup)["p"]["workspaces"]["cache"] == {"type": "emptyDir", "value": ""}


def test_delete_preset(client, setup, auth):
    client.put("/api/presets/a", json=preset(), headers=auth)
    client.put("/api/presets/b", json=preset(), headers=auth)
    assert client.delete("/api/presets/a", headers=auth).status_code == 204
    assert list(saved(setup)) == ["b"]


def test_delete_unknown_preset_is_404(client, setup, auth):
    resp = client.delete("/api/presets/nope", headers=auth)
    assert resp.status_code == 404
    assert resp.json == {"error": "preset nope not found"}


def test_saving_and_deleting_is_logged(client, setup, auth, log_file):
    client.put("/api/presets/p", json=preset(), headers=auth)
    client.delete("/api/presets/p", headers=auth)
    assert "preset p saved (crc-e2e on linux-1)" in log_file.read_text()
    assert "preset p deleted" in log_file.read_text()


# ---------------- starting ----------------

def test_start_creates_a_pending_run_for_the_controller(client, setup, auth):
    client.put("/api/presets/e2e-linux", json=preset(), headers=auth)
    resp = client.post("/api/presets/e2e-linux/start", headers=auth)
    assert resp.status_code == 201
    name = resp.json["pipelinerun"]
    assert name.startswith("e2e-linux-") and len(name) == len("e2e-linux-") + 5
    assert resp.json == {"pipelinerun": name, "machine": "linux-1", "task_machine": ""}
    run = runs(setup)[name]
    assert run["apiVersion"] == "tekton.dev/v1"
    assert run["metadata"]["labels"] == {"machine": "linux-1", "concurrency-control/preset": "e2e-linux"}
    assert run["spec"] == {
        "status": "PipelineRunPending",
        "pipelineRef": {"name": "crc-e2e"},
        "params": [{"name": "crc-version", "value": "2.45.0"}],
        "workspaces": [{"name": "source", "volumeClaimTemplate": {"spec": {
            "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}}}],
    }


def test_each_start_is_a_new_run(client, setup, auth):
    client.put("/api/presets/p", json=preset(), headers=auth)
    names = {client.post("/api/presets/p/start", headers=auth).json["pipelinerun"] for _ in range(3)}
    assert len(names) == 3 and names == set(runs(setup))


def test_started_run_is_picked_up_by_the_controller(client, setup, auth, motd):
    client.put("/api/presets/p", json=preset(), headers=auth)
    name = client.post("/api/presets/p/start", headers=auth).json["pipelinerun"]
    control.monitor_pending_run()
    assert setup.machines()["linux-1"] == "busy"
    assert runs(setup)[name]["spec"]["status"] == ""
    assert motd == [("set", "linux-1", name)]


def test_start_with_everything_set(client, setup, auth):
    body = preset(task_machine="windows-11", service_account="pipeline", timeout="3h",
                  params={"crc-version": "2.45.0", "bundle": "4.18"},
                  workspaces={"source": {"type": "persistentVolumeClaim", "value": "crc-src"},
                              "cache": {"type": "secret", "value": "pull-secret"}})
    client.put("/api/presets/p", json=body, headers=auth)
    run = runs(setup)[client.post("/api/presets/p/start", headers=auth).json["pipelinerun"]]
    assert run["metadata"]["labels"] == {"machine": "linux-1", "task-machine": "windows-11",
                                         "concurrency-control/preset": "p"}
    assert run["spec"]["params"] == [{"name": "bundle", "value": "4.18"}, {"name": "crc-version", "value": "2.45.0"}]
    assert run["spec"]["workspaces"] == [{"name": "cache", "secret": {"secretName": "pull-secret"}},
                                         {"name": "source", "persistentVolumeClaim": {"claimName": "crc-src"}}]
    assert run["spec"]["taskRunTemplate"] == {"serviceAccountName": "pipeline"}
    assert run["spec"]["timeouts"] == {"pipeline": "3h"}


@pytest.mark.parametrize("ws,expected", [
    ({"type": "configMap", "value": "settings"}, {"configMap": {"name": "settings"}}),
    ({"type": "emptyDir"}, {"emptyDir": {}}),
])
def test_other_workspace_bindings(client, setup, auth, ws, expected):
    client.put("/api/presets/p", json=preset(workspaces={"source": ws}), headers=auth)
    run = runs(setup)[client.post("/api/presets/p/start", headers=auth).json["pipelinerun"]]
    assert run["spec"]["workspaces"] == [{"name": "source", **expected}]


def test_start_unknown_preset_is_404(client, setup, auth):
    resp = client.post("/api/presets/nope/start", headers=auth)
    assert resp.status_code == 404
    assert runs(setup) == {}


def test_start_after_the_machine_was_removed(client, setup, auth):
    client.put("/api/presets/p", json=preset(), headers=auth)
    del setup.data(setup.machine_cm)["linux-1"]
    resp = client.post("/api/presets/p/start", headers=auth)
    assert resp.status_code == 409
    assert resp.json == {"error": "preset p no longer fits: machine linux-1 isn't registered, edit it first"}
    assert runs(setup) == {}


def test_start_after_the_pipeline_gained_a_required_parameter(client, setup, auth):
    client.put("/api/presets/p", json=preset(), headers=auth)
    setup.add_pipeline("crc-e2e", params=[("crc-version", None), ("arch", None)], workspaces=["source"])
    resp = client.post("/api/presets/p/start", headers=auth)
    assert resp.status_code == 409
    assert "parameter arch is required" in resp.json["error"]


def test_start_works_on_a_busy_machine(client, setup, auth):
    # the run just waits; that's the controller's job
    client.put("/api/presets/p", json=preset(), headers=auth)
    setup.set_machine("linux-1", "busy")
    assert client.post("/api/presets/p/start", headers=auth).status_code == 201


def test_failed_create_is_reported(client, setup, auth):
    client.put("/api/presets/p", json=preset(), headers=auth)
    setup.fail("create", "-f", "-", "-o", "json", stderr='Error from server (Forbidden): pipelineruns.tekton.dev is forbidden')
    resp = client.post("/api/presets/p/start", headers=auth)
    assert resp.status_code == 500
    assert resp.json == {"error": "failed to create the PipelineRun: "
                                  "Error from server (Forbidden): pipelineruns.tekton.dev is forbidden"}


def test_start_is_logged(client, setup, auth, log_file):
    client.put("/api/presets/p", json=preset(), headers=auth)
    name = client.post("/api/presets/p/start", headers=auth).json["pipelinerun"]
    assert f"preset p started pipelinerun {name}, waiting for linux-1" in log_file.read_text()


# ---------------- turned off ----------------

@pytest.mark.parametrize("method,path", [("GET", "/api/presets"), ("PUT", "/api/presets/p"),
                                         ("DELETE", "/api/presets/p"), ("POST", "/api/presets/p/start")])
def test_presets_are_off_without_preset_cm(client, setup, auth, monkeypatch, method, path):
    monkeypatch.setattr(presets, "PRESET_CM", None)
    resp = client.open(path, method=method, json=preset(), headers=auth)
    assert resp.status_code == 403
    assert resp.json == {"error": "presets are turned off, set PRESET_CM to enable them"}


def test_dashboard_works_without_preset_cm(client, setup, monkeypatch):
    monkeypatch.setattr(presets, "PRESET_CM", None)
    assert client.get("/").status_code == 200
