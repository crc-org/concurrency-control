import json
import os
import re
import cluster
import machines
from log import logger

# A preset holds everything needed to start a pipeline on a machine, so a run
# is one click instead of a hand-written PipelineRun. Presets are stored as
# JSON, one key per preset, in the PRESET_CM configmap.

PRESET_CM = os.environ.get("PRESET_CM")
LABEL_MACHINE_PR = os.environ.get("LABEL_MACHINE_PR")
LAEBL_MACHINE_TASK = os.environ.get("LAEBL_MACHINE_TASK")
PRESET_LABEL = "concurrency-control/preset"

# the preset name is the run's generateName prefix: name + "-" + 5 random characters must fit in 63
NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,55}[a-z0-9])?")
OBJECT_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
SIZE_RE = re.compile(r"[1-9][0-9]*(Mi|Gi|Ti)")
TIMEOUT_RE = re.compile(r"([0-9]+h)?([0-9]+m)?([0-9]+s)?")

# how a workspace can be bound, and what its value means
WORKSPACE_TYPES = {
    "volumeClaimTemplate": "size",       # a new volume for each run, e.g. 1Gi
    "persistentVolumeClaim": "name",     # an existing PVC
    "secret": "name",
    "configMap": "name",
    "emptyDir": None,
}


class PresetError(machines.MachineError):
    pass


def enabled():
    return bool(PRESET_CM)


def require_enabled():
    if not enabled():
        raise PresetError("presets are turned off, set PRESET_CM to enable them", 403)


def valid_name(name):
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name))


# ---------------- pipelines ----------------

def describe_pipeline(pipeline: dict):
    spec = pipeline.get("spec") or {}
    return {
        "name": pipeline["metadata"]["name"],
        "description": spec.get("description", ""),
        # made in the web UI, so it can be edited and deleted there
        "built": (pipeline["metadata"].get("labels") or {}).get("concurrency-control/built") == "true",
        "steps": len(spec.get("tasks") or []) + len(spec.get("finally") or []),
        "params": [{"name": p["name"], "type": p.get("type", "string"), "default": p.get("default"),
                    "description": p.get("description", "")} for p in spec.get("params") or []],
        "workspaces": [{"name": w["name"], "optional": bool(w.get("optional")),
                        "description": w.get("description", "")} for w in spec.get("workspaces") or []],
    }


def list_pipelines():
    pipelines = cluster.list_pipelines()
    if pipelines is None:
        raise PresetError("can't read the pipelines from the cluster", 502)
    return sorted((describe_pipeline(p) for p in pipelines), key=lambda p: p["name"])


# ---------------- storage ----------------

def list_presets():
    require_enabled()
    data = cluster.get_configmap_data(PRESET_CM) or {}  # missing until the first save
    presets = {}
    for name, value in data.items():
        try:
            presets[name] = json.loads(value)
        except ValueError:
            logger.error(f"preset {name} in configmap {PRESET_CM} isn't valid JSON, skipping it")
    return presets


def get_preset(name: str):
    preset = list_presets().get(name)
    if preset is None:
        raise PresetError(f"preset {name} not found", 404)
    return preset


# ---------------- validation ----------------

def text(body: dict, key: str, required=False):
    value = body.get(key)
    if value is None or value == "":
        if required:
            raise PresetError(f"{key} is required")
        return ""
    if not isinstance(value, str):
        raise PresetError(f"{key} must be a string")
    return value.strip()


def validate(body, pipelines: list, machine_names):
    """Checks a preset against the current pipelines and machines; returns it normalized."""
    if not isinstance(body, dict):
        raise PresetError("the preset must be a JSON object")
    pipeline_name = text(body, "pipeline", required=True)
    pipeline = next((p for p in pipelines if p["name"] == pipeline_name), None)
    if pipeline is None:
        raise PresetError(f"pipeline {pipeline_name} doesn't exist")

    machine = text(body, "machine", required=True)
    if machine not in machine_names:
        raise PresetError(f"machine {machine} isn't registered")
    task_machine = text(body, "task_machine")
    if task_machine and task_machine not in machine_names:
        raise PresetError(f"machine {task_machine} isn't registered")
    if task_machine == machine:
        raise PresetError("the task machine must differ from the machine")

    params = body.get("params") or {}
    if not isinstance(params, dict) or not all(isinstance(v, str) for v in params.values()):
        raise PresetError("params must map parameter names to strings")
    declared = {p["name"]: p for p in pipeline["params"]}
    for name in params:
        if name not in declared:
            raise PresetError(f"pipeline {pipeline_name} has no parameter {name}")
        if declared[name]["type"] != "string":
            raise PresetError(f"parameter {name} is an {declared[name]['type']}, only strings can be set")
    params = {name: value for name, value in params.items() if value != ""}  # empty means "use the default"
    for p in pipeline["params"]:
        if p["default"] is None and p["name"] not in params:
            raise PresetError(f"parameter {p['name']} is required, the pipeline has no default for it")

    workspaces = body.get("workspaces") or {}
    if not isinstance(workspaces, dict):
        raise PresetError("workspaces must map workspace names to bindings")
    declared_ws = {w["name"]: w for w in pipeline["workspaces"]}
    bound = {}
    for name, ws in workspaces.items():
        if name not in declared_ws:
            raise PresetError(f"pipeline {pipeline_name} has no workspace {name}")
        if not isinstance(ws, dict) or ws.get("type") not in WORKSPACE_TYPES:
            raise PresetError(f"workspace {name} needs a type: {', '.join(WORKSPACE_TYPES)}")
        kind = WORKSPACE_TYPES[ws["type"]]
        value = text(ws, "value") if kind else ""
        if kind and not value:
            raise PresetError(f"workspace {name} needs a {kind}")
        if kind == "size" and not SIZE_RE.fullmatch(value):
            raise PresetError(f"workspace {name}: size must look like 500Mi, 1Gi or 2Ti")
        if kind == "name" and not OBJECT_NAME_RE.fullmatch(value):
            raise PresetError(f"workspace {name}: {value!r} isn't a valid object name")
        bound[name] = {"type": ws["type"], "value": value}
    for w in pipeline["workspaces"]:
        if not w["optional"] and w["name"] not in bound:
            raise PresetError(f"workspace {w['name']} must be bound")

    service_account = text(body, "service_account")
    if service_account and not OBJECT_NAME_RE.fullmatch(service_account):
        raise PresetError(f"{service_account!r} isn't a valid service account name")
    timeout = text(body, "timeout")
    if timeout and not (TIMEOUT_RE.fullmatch(timeout) and any(c.isdigit() for c in timeout)):
        raise PresetError("timeout must look like 3h, 90m or 1h30m")

    return {"pipeline": pipeline_name, "machine": machine, "task_machine": task_machine, "params": params,
            "workspaces": bound, "service_account": service_account, "timeout": timeout}


# ---------------- changes ----------------

def save_preset(name: str, body):
    require_enabled()
    if not valid_name(name):
        raise PresetError(f"invalid preset name {name!r}: lowercase letters, digits and -, at most 57 characters")
    preset = validate(body, list_pipelines(), machines.list_machines())
    if cluster.get_configmap_data(PRESET_CM) is None:
        cluster.create_configmap(PRESET_CM)
    if not cluster.set_configmap_key(PRESET_CM, name, json.dumps(preset, sort_keys=True)):
        raise PresetError(f"failed to save preset {name}", 500)
    logger.info(f"preset {name} saved ({preset['pipeline']} on {preset['machine']})")
    return preset


def delete_preset(name: str):
    get_preset(name)
    if not cluster.remove_configmap_key(PRESET_CM, name):
        raise PresetError(f"failed to delete preset {name}", 500)
    logger.info(f"preset {name} deleted")


def binding(name: str, ws: dict):
    value = ws["value"]
    return {"name": name, **{
        "volumeClaimTemplate": lambda: {"volumeClaimTemplate": {"spec": {
            "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": value}}}}},
        "persistentVolumeClaim": lambda: {"persistentVolumeClaim": {"claimName": value}},
        "secret": lambda: {"secret": {"secretName": value}},
        "configMap": lambda: {"configMap": {"name": value}},
        "emptyDir": lambda: {"emptyDir": {}},
    }[ws["type"]]()}


def pipelinerun_manifest(name: str, preset: dict):
    labels = {LABEL_MACHINE_PR: preset["machine"], PRESET_LABEL: name}
    if preset["task_machine"]:
        labels[LAEBL_MACHINE_TASK] = preset["task_machine"]
    spec = {
        # created pending, so the controller starts it once the machine is free
        "status": "PipelineRunPending",
        "pipelineRef": {"name": preset["pipeline"]},
        "params": [{"name": k, "value": v} for k, v in sorted(preset["params"].items())],
        "workspaces": [binding(k, v) for k, v in sorted(preset["workspaces"].items())],
    }
    if preset["service_account"]:
        spec["taskRunTemplate"] = {"serviceAccountName": preset["service_account"]}
    if preset["timeout"]:
        spec["timeouts"] = {"pipeline": preset["timeout"]}
    return {"apiVersion": "tekton.dev/v1", "kind": "PipelineRun",
            "metadata": {"generateName": f"{name}-", "labels": labels}, "spec": spec}


def start_preset(name: str):
    stored = get_preset(name)
    # the pipeline or the machines may have changed since the preset was saved
    try:
        preset = validate(stored, list_pipelines(), machines.list_machines())
    except PresetError as e:
        if e.http_code != 400:
            raise
        raise PresetError(f"preset {name} no longer fits: {e}, edit it first", 409)
    run, err = cluster.create_pipelinerun(pipelinerun_manifest(name, preset))
    if run is None:
        raise PresetError(f"failed to create the PipelineRun: {err}", 500)
    logger.info(f"preset {name} started pipelinerun {run}, waiting for {preset['machine']}")
    return {"pipelinerun": run, "machine": preset["machine"], "task_machine": preset["task_machine"]}
