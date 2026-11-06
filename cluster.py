import json
import subprocess
from typing import Optional
from log import logger

# All cluster access goes through oc with an argument list (no shell), so
# names and values are never interpreted by a shell and output is parsed as
# JSON instead of grepped from tables.

def oc(*args, input: Optional[str] = None, env=None):
    result = subprocess.run(["oc", *args], input=input, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr

def get_json(kind: str, name: Optional[str] = None, selector: Optional[str] = None):
    args = ["get", kind] + ([name] if name else []) + (["-l", selector] if selector else []) + ["-o", "json"]
    code, out, err = oc(*args)
    if code != 0:
        logger.error(f"oc {' '.join(args)} failed: {err.strip()}")
        return None
    return json.loads(out)

# ---------------- configmaps ----------------

def get_configmap_data(name: str):
    cm = get_json("configmap", name)
    return None if cm is None else cm.get("data") or {}

def set_configmap_key(name: str, key: str, value: str):
    code, _, err = oc("set", "data", "configmap", name, f"{key}={value}")
    if code != 0:
        logger.error(f"failed to set {key} in configmap {name}: {err.strip()}")
    return code == 0

def remove_configmap_key(name: str, key: str):
    code, _, err = oc("set", "data", "configmap", name, f"{key}-")
    if code != 0:
        logger.error(f"failed to remove {key} from configmap {name}: {err.strip()}")
    return code == 0

def create_configmap(name: str):
    code, _, err = oc("create", "configmap", name)
    if code != 0:
        logger.error(f"failed to create configmap {name}: {err.strip()}")
    return code == 0

# ---------------- tekton ----------------

def get_pipeline(name: str):
    return get_json("pipeline", name)

def apply(manifest: dict):
    # creates the object or replaces the one with the same name
    code, _, err = oc("apply", "-f", "-", input=json.dumps(manifest))
    if code != 0:
        logger.error(f"failed to apply {manifest['kind']} {manifest['metadata']['name']}: {err.strip()}")
    return code == 0, err.strip()

def delete(kind: str, name: str):
    code, _, err = oc("delete", kind, name)
    if code != 0:
        logger.error(f"failed to delete {kind} {name}: {err.strip()}")
    return code == 0

def list_pipelines():
    # None when the list can't be read, so callers can tell that apart from "no pipelines"
    pipelines = get_json("pipeline")
    return None if pipelines is None else pipelines.get("items", [])

def create_pipelinerun(manifest: dict):
    # -o json returns the created object, which holds the name generateName picked
    code, out, err = oc("create", "-f", "-", "-o", "json", input=json.dumps(manifest))
    if code != 0:
        logger.error(f"failed to create pipelinerun: {err.strip()}")
        return None, err.strip()
    return json.loads(out)["metadata"]["name"], ""

def get_pipelinerun(name: str):
    return get_json("pipelinerun", name)

def list_pipelineruns():
    prs = get_json("pipelinerun")
    return [] if prs is None else prs.get("items", [])

def pipelinerun_status(pipelinerun: dict):
    # same text tkn prints in its STATUS column, e.g. "Running(PipelineRunPending)"
    conditions = pipelinerun.get("status", {}).get("conditions") or []
    if not conditions:
        return "---"
    condition = conditions[0]
    status = {"True": "Succeeded", "False": "Failed"}.get(condition.get("status"), "Running")
    reason = condition.get("reason")
    if reason and reason != status:
        status = f"{status}({reason})"
    return status

def start_pipelinerun(name: str):
    code, _, err = oc("patch", "pipelinerun", name, "--type=merge", "-p", '{"spec":{"status":""}}')
    if code != 0:
        logger.error(f"failed to start pipelinerun {name}: {err.strip()}")
    return code == 0

def get_taskruns(pipelinerun: str, pipeline_task: str):
    taskruns = get_json("taskrun", selector=f"tekton.dev/pipelineRun={pipelinerun},tekton.dev/pipelineTask={pipeline_task}")
    return [] if taskruns is None else taskruns.get("items", [])

def taskrun_succeeded(taskrun: dict):
    # "True", "False" or "Unknown", like the SUCCEEDED column of oc get taskrun
    for condition in taskrun.get("status", {}).get("conditions") or []:
        if condition.get("type") == "Succeeded":
            return condition.get("status")
    return None

# ---------------- secrets ----------------

def create_secret(name: str, string_data: dict, labels: dict):
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "labels": labels},
        "type": "Opaque",
        "stringData": string_data,
    }
    # via stdin so the private key never lands on disk or in argv
    code, _, err = oc("create", "-f", "-", input=json.dumps(manifest))
    if code != 0:
        logger.error(f"failed to create secret {name}: {err.strip()}")
    return code == 0, err.strip()

def get_secret(name: str):
    return get_json("secret", name)

def delete_secret(name: str):
    code, _, err = oc("delete", "secret", name)
    if code != 0:
        logger.error(f"failed to delete secret {name}: {err.strip()}")
    return code == 0
