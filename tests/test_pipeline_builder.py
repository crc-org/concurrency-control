"""Pipelines made from ci-definitions Task templates: task_templates.py,
pipeline_builder.py and the /api/templates and /api/pipelines/<name> endpoints.

Templates come from tests/templates (TEMPLATE_DIR), the cluster from FakeCluster.
"""
import json

import pytest

import pipeline_builder
import task_templates
import web

CHECK = "crc-support/tkn/check-bundle-url.yaml"
BUILD = "crc-builder/tkn/crc-builder.yaml"
E2E = "crc-e2e/task.yaml"
SNC = "snc-runner/tkn/task.yaml"
REPO = "https://github.com/crc-org/ci-definitions"


@pytest.fixture
def client():
    return web.app.test_client()


@pytest.fixture
def auth(api_token):
    return {"Authorization": f"Bearer {api_token}"}


def step(name, template, run_after=(), finally_=False, **inputs):
    return {"name": name, "template": template, "run_after": list(run_after), "finally": finally_,
            "inputs": {k.replace("_", "-"): v for k, v in inputs.items()}}


START = {"from": "start"}


def fixed(value):
    return {"from": "fixed", "value": value}


def output(step_name, result):
    return {"from": "step", "step": step_name, "result": result}


def qe_pipeline():
    """check-bundle and build in parallel, then the tests, then a step at the end."""
    return {"description": "quick QE", "steps": [
        step("check-bundle", CHECK, bundle_base_url=START, bundle_name=START),
        step("build", BUILD),
        step("test", E2E, run_after=["check-bundle", "build"], secret_host=START, os=fixed("linux"),
             bundle_location=output("check-bundle", "bundle-base-url")),
        step("report", BUILD, finally_=True, debug=fixed("true")),
    ]}


def ref(path):
    return {"resolver": "git", "params": [{"name": "url", "value": REPO}, {"name": "revision", "value": "main"},
                                          {"name": "pathInRepo", "value": path}]}


# ---------------- the template catalog ----------------

def test_catalog_lists_every_task_in_the_repo(client):
    resp = client.get("/api/templates")
    assert resp.status_code == 200
    assert [t["path"] for t in resp.json] == [BUILD, E2E, CHECK, SNC]  # not the tpl/ file, not the ConfigMap


def test_catalog_describes_inputs_outputs_and_workspaces(client):
    check = {t["path"]: t for t in client.get("/api/templates").json}[CHECK]
    assert check["name"] == "check-bundle-url"
    assert check["description"] == "Checks the bundle can be downloaded and returns the url it was found at"
    assert check["params"][:2] == [
        {"name": "bundle-base-url", "type": "string", "default": None, "description": "base url to download the bundle from"},
        {"name": "bundle-base-url-fallbacks", "type": "array", "default": [], "description": ""},
    ]
    assert check["results"] == [{"name": "bundle-base-url", "description": "the url the bundle was found at"}]
    snc = {t["path"]: t for t in client.get("/api/templates").json}[SNC]
    assert snc["workspaces"] == [{"name": "storage", "optional": False, "description": ""},
                                 {"name": "ocp-pullsecret", "optional": True, "description": ""}]


def test_catalog_is_cached(client, monkeypatch):
    client.get("/api/templates")
    monkeypatch.setattr(task_templates, "read_file", lambda path: (_ for _ in ()).throw(AssertionError("read again")))
    assert len(client.get("/api/templates").json) == 4


def test_catalog_from_github(client, monkeypatch):
    task = (task_templates.os.path.join(task_templates.TEMPLATE_DIR, CHECK))
    fetched = []

    def fake_get(url):
        fetched.append(url)
        if url.startswith("https://api.github.com/"):
            return json.dumps({"tree": [
                {"path": CHECK, "type": "blob"},
                {"path": "crc-support/tkn/tpl/check-bundle-url.tpl.yaml", "type": "blob"},
                {"path": ".github/workflows/build.yaml", "type": "blob"},
                {"path": "crc-support/tkn", "type": "tree"},
                {"path": "README.md", "type": "blob"},
            ]})
        return open(task).read()

    monkeypatch.setattr(task_templates, "TEMPLATE_DIR", None)
    monkeypatch.setattr(task_templates, "http_get", fake_get)
    assert [t["path"] for t in client.get("/api/templates").json] == [CHECK]
    assert fetched == [f"https://api.github.com/repos/crc-org/ci-definitions/git/trees/main?recursive=1",
                       f"https://raw.githubusercontent.com/crc-org/ci-definitions/main/{CHECK}"]


def test_unreachable_github_is_an_error(client, monkeypatch):
    def offline(url):
        raise OSError("Network is unreachable")
    monkeypatch.setattr(task_templates, "TEMPLATE_DIR", None)
    monkeypatch.setattr(task_templates, "http_get", offline)
    resp = client.get("/api/templates")
    assert resp.status_code == 502
    assert resp.json == {"error": f"can't list the templates in {REPO} at main: Network is unreachable"}


def test_broken_template_file_is_skipped(client, tmp_path, monkeypatch, log_file):
    (tmp_path / "good.yaml").write_text(open(task_templates.os.path.join(task_templates.TEMPLATE_DIR, BUILD)).read())
    (tmp_path / "broken.yaml").write_text("kind: Task\n  metadata: [unclosed\n")
    monkeypatch.setattr(task_templates, "TEMPLATE_DIR", str(tmp_path))
    assert [t["path"] for t in client.get("/api/templates").json] == ["good.yaml"]
    assert "template broken.yaml can't be read" in (log_file.parent / "error.log").read_text()


def test_no_templates_at_all_is_an_error(client, tmp_path, monkeypatch):
    monkeypatch.setattr(task_templates, "TEMPLATE_DIR", str(tmp_path))
    resp = client.get("/api/templates")
    assert resp.status_code == 502
    assert resp.json == {"error": f"no Task templates found in {tmp_path}"}


# ---------------- building a pipeline ----------------

def test_save_builds_a_pipeline_that_points_at_the_templates(client, fake, auth):
    resp = client.put("/api/pipelines/qe-quick", json=qe_pipeline(), headers=auth)
    assert resp.status_code == 200, resp.json
    pipeline = fake.objects["pipeline"]["qe-quick"]
    assert pipeline["apiVersion"] == "tekton.dev/v1"
    assert pipeline["metadata"]["labels"] == {"concurrency-control/built": "true"}
    spec = pipeline["spec"]
    assert spec["description"] == "quick QE"
    assert spec["params"] == [
        {"name": "bundle-base-url", "type": "string", "description": "base url to download the bundle from"},
        {"name": "bundle-name", "type": "string", "description": "bundle name"},
        {"name": "secret-host", "type": "string", "description": "secret with the machine's host info"},
    ]
    assert spec["tasks"] == [
        {"name": "check-bundle", "taskRef": ref(CHECK), "params": [
            {"name": "bundle-base-url", "value": "$(params.bundle-base-url)"},
            {"name": "bundle-name", "value": "$(params.bundle-name)"}]},
        {"name": "build", "taskRef": ref(BUILD)},
        {"name": "test", "taskRef": ref(E2E), "runAfter": ["check-bundle", "build"], "params": [
            {"name": "secret-host", "value": "$(params.secret-host)"},
            {"name": "os", "value": "linux"},
            {"name": "bundle-location", "value": "$(tasks.check-bundle.results.bundle-base-url)"}]},
    ]
    assert spec["finally"] == [{"name": "report", "taskRef": ref(BUILD), "params": [{"name": "debug", "value": "true"}]}]
    assert "workspaces" not in spec


def test_saved_pipeline_reads_back_as_it_was_built(client, fake, auth):
    client.put("/api/pipelines/qe-quick", json=qe_pipeline(), headers=auth)
    model = client.get("/api/pipelines/qe-quick").json
    expected = qe_pipeline()
    for s in expected["steps"]:
        s["inputs"] = {k: v for k, v in s["inputs"].items()}
    by_name = {s["name"]: s for s in model["steps"]}
    assert model["description"] == "quick QE"
    assert [s["name"] for s in model["steps"]] == ["check-bundle", "build", "test", "report"]
    assert by_name["test"]["run_after"] == ["check-bundle", "build"]
    assert by_name["test"]["inputs"] == {"secret-host": START, "os": fixed("linux"),
                                        "bundle-location": output("check-bundle", "bundle-base-url")}
    assert by_name["report"]["finally"] is True
    assert by_name["build"]["inputs"] == {}  # every input uses the template default


def test_shared_start_input_becomes_one_parameter(client, fake, auth):
    body = {"steps": [step("a", BUILD, debug=START), step("b", E2E, secret_host=fixed("m"), os=fixed("linux"),
                                                         bundle_location=fixed("/b"), debug=START)]}
    client.put("/api/pipelines/p", json=body, headers=auth)
    params = fake.objects["pipeline"]["p"]["spec"]["params"]
    assert params == [{"name": "debug", "type": "string", "description": "", "default": "false"}]


def test_array_input_asked_when_starting(client, fake, auth):
    body = {"steps": [step("check", CHECK, bundle_base_url=fixed("u"), bundle_name=fixed("n"),
                           bundle_base_url_fallbacks=START)]}
    client.put("/api/pipelines/p", json=body, headers=auth)
    spec = fake.objects["pipeline"]["p"]["spec"]
    assert spec["params"] == [{"name": "bundle-base-url-fallbacks", "type": "array", "description": "", "default": []}]
    assert {"name": "bundle-base-url-fallbacks", "value": ["$(params.bundle-base-url-fallbacks[*])"]} in spec["tasks"][0]["params"]
    assert client.get("/api/pipelines/p").json["steps"][0]["inputs"]["bundle-base-url-fallbacks"] == START


def test_template_workspaces_become_pipeline_workspaces(client, fake, auth):
    client.put("/api/pipelines/p", json={"steps": [step("snc", SNC)]}, headers=auth)
    spec = fake.objects["pipeline"]["p"]["spec"]
    assert spec["workspaces"] == [{"name": "storage"}, {"name": "ocp-pullsecret", "optional": True}]
    assert spec["tasks"][0]["workspaces"] == [{"name": "storage", "workspace": "storage"},
                                              {"name": "ocp-pullsecret", "workspace": "ocp-pullsecret"}]


def test_save_replaces_a_pipeline_made_here(client, fake, auth):
    client.put("/api/pipelines/p", json=qe_pipeline(), headers=auth)
    client.put("/api/pipelines/p", json={"steps": [step("build", BUILD)]}, headers=auth)
    assert [t["name"] for t in fake.objects["pipeline"]["p"]["spec"]["tasks"]] == ["build"]
    assert "finally" not in fake.objects["pipeline"]["p"]["spec"]


def test_pipeline_made_elsewhere_cannot_be_overwritten(client, fake, auth):
    fake.add_pipeline("crc-qe-latest", params=[("x", "1")])
    resp = client.put("/api/pipelines/crc-qe-latest", json=qe_pipeline(), headers=auth)
    assert resp.status_code == 409
    assert resp.json == {"error": "pipeline crc-qe-latest already exists and wasn't made here, pick another name"}
    assert fake.objects["pipeline"]["crc-qe-latest"]["spec"]["params"][0]["name"] == "x"


def test_pipeline_made_elsewhere_can_only_be_viewed(client, fake, auth):
    fake.add_pipeline("crc-qe-latest")
    assert client.get("/api/pipelines/crc-qe-latest").status_code == 409
    assert client.delete("/api/pipelines/crc-qe-latest", headers=auth).status_code == 409
    assert "crc-qe-latest" in fake.objects["pipeline"]


def test_pipelines_list_says_which_were_made_here(client, fake, auth):
    fake.add_pipeline("crc-qe-latest")
    client.put("/api/pipelines/qe-quick", json=qe_pipeline(), headers=auth)
    listed = {p["name"]: p for p in client.get("/api/pipelines").json}
    assert (listed["crc-qe-latest"]["built"], listed["qe-quick"]["built"]) == (False, True)
    assert listed["qe-quick"]["steps"] == 4


def test_delete_pipeline_made_here(client, fake, auth):
    client.put("/api/pipelines/p", json=qe_pipeline(), headers=auth)
    assert client.delete("/api/pipelines/p", headers=auth).status_code == 204
    assert "p" not in fake.objects["pipeline"]


def test_unknown_pipeline_is_404(client, fake, auth):
    assert client.get("/api/pipelines/nope").status_code == 404
    assert client.delete("/api/pipelines/nope", headers=auth).status_code == 404


def test_built_pipeline_can_be_used_by_a_preset(client, fake, auth):
    fake.set_machine("linux-1", "free")
    client.put("/api/pipelines/qe-quick", json=qe_pipeline(), headers=auth)
    preset = {"pipeline": "qe-quick", "machine": "linux-1",
              "params": {"bundle-base-url": "https://b", "bundle-name": "crc.bundle", "secret-host": "linux-1"}}
    assert client.put("/api/presets/qe", json=preset, headers=auth).status_code == 200
    assert client.post("/api/presets/qe/start", headers=auth).status_code == 201


def test_saving_and_deleting_is_logged(client, fake, auth, log_file):
    client.put("/api/pipelines/p", json=qe_pipeline(), headers=auth)
    client.delete("/api/pipelines/p", headers=auth)
    assert "pipeline p saved with 4 step(s)" in log_file.read_text()
    assert "pipeline p deleted" in log_file.read_text()


# ---------------- what's refused ----------------

@pytest.mark.parametrize("name", ["Qe", "qe_quick", "-qe", "qe-", "x" * 64])
def test_invalid_pipeline_names(client, fake, auth, name):
    resp = client.put(f"/api/pipelines/{name}", json=qe_pipeline(), headers=auth)
    assert resp.status_code == 400
    assert "invalid pipeline name" in resp.json["error"]


@pytest.mark.parametrize("steps,error", [
    ([], "a pipeline needs at least one step"),
    (["build"], "step 1 must be a JSON object"),
    ([step("Build", BUILD)], "step 1: 'Build' isn't a valid step name"),
    ([step("b", BUILD), step("b", BUILD)], "two steps are named b"),
    ([step("b", "nope.yaml")], "step b: template 'nope.yaml' isn't in the catalog"),
    ([step("b", BUILD, run_after=["later"]), step("later", BUILD)], "step b can only wait for an earlier step, not 'later'"),
    ([step("b", BUILD, run_after=["b"])], "step b can only wait for an earlier step, not 'b'"),
    ([step("b", BUILD), step("r", BUILD, run_after=["b"], finally_=True)],
     "step r runs at the end, it can't wait for particular steps"),
    ([step("r", BUILD, finally_=True), step("b", BUILD)], "step b must come before the steps that run at the end"),
    ([step("r", BUILD, finally_=True)], "at least one step must run before the steps that run at the end"),
    ([step("b", BUILD, nope=fixed("x"))], "step b: template crc-builder/tkn/crc-builder.yaml has no input nope"),
    ([step("t", E2E)], "step t: input secret-host has no default, choose a value for it"),
    ([step("t", E2E, secret_host={"from": "default"})], "step t: input secret-host has no default, choose a value for it"),
    ([step("b", BUILD, debug={"from": "magic"})], "step b: input debug needs a source"),
    ([step("b", BUILD, debug={"from": "fixed"})], "step b: input debug needs a fixed value"),
    ([step("c", CHECK, bundle_base_url=fixed("u"), bundle_name=fixed("n"), bundle_base_url_fallbacks=fixed("x"))],
     "step c: input bundle-base-url-fallbacks is an array, it can only use the default or be asked when starting"),
    ([step("t", E2E, secret_host=START, os=START, bundle_location=output("b", "x")), step("b", BUILD)],
     "step t: input bundle-location can only use the output of an earlier step, not 'b'"),
    ([step("b", BUILD), step("t", E2E, secret_host=START, os=START, bundle_location=output("b", "nope"))],
     "step t: input bundle-location: step b has no output 'nope'"),
])
def test_invalid_steps_are_refused(client, fake, auth, steps, error):
    resp = client.put("/api/pipelines/p", json={"steps": steps}, headers=auth)
    assert resp.status_code == 400
    assert error in resp.json["error"]
    assert "p" not in fake.objects["pipeline"]


def test_finally_step_can_use_outputs_of_main_steps(client, fake, auth):
    body = {"steps": [step("b", BUILD),
                      step("r", E2E, finally_=True, secret_host=START, os=START,
                           bundle_location=output("b", "distributable-name"))]}
    assert client.put("/api/pipelines/p", json=body, headers=auth).status_code == 200


@pytest.mark.parametrize("body", [None, ["x"], "x", 3])
def test_pipeline_must_be_a_json_object(client, fake, auth, body):
    kwargs = {"data": "nope", "content_type": "application/json"} if body is None else {"json": body}
    resp = client.put("/api/pipelines/p", headers=auth, **kwargs)
    assert resp.status_code == 400
    assert resp.json == {"error": "the pipeline must be a JSON object"}


def test_failed_apply_is_reported(client, fake, auth):
    fake.fail("apply", stderr="Error from server (Forbidden): pipelines.tekton.dev is forbidden")
    resp = client.put("/api/pipelines/p", json=qe_pipeline(), headers=auth)
    assert resp.status_code == 500
    assert resp.json == {"error": "failed to save pipeline p: Error from server (Forbidden): pipelines.tekton.dev is forbidden"}


def test_templates_are_never_written(client, fake, auth):
    # pipelines only point at the templates; nothing is created from the template files themselves
    client.put("/api/pipelines/p", json=qe_pipeline(), headers=auth)
    kinds = {json.loads(stdin)["kind"] for args, stdin in fake.calls if stdin}
    assert kinds == {"Pipeline"}
