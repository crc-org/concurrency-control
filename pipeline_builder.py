import re
import cluster
import machines
import task_templates
from log import logger

# Pipelines made in the web UI. Every step is a Task template from
# ci-definitions, referenced with the git resolver at main, so the templates
# themselves can't be changed from here. Steps run in parallel unless they wait
# for earlier steps; "finally" steps run at the end even when others fail.
#
# The page works with this model, one entry per step:
#   {"name": "build", "template": "crc-builder/tkn/crc-builder.yaml",
#    "finally": False, "run_after": ["check-bundle"],
#    "inputs": {"<template param>": {"from": "fixed", "value": "..."}}}
# where an input comes "from":
#   default - the template's default (the param is left out)
#   fixed   - the same value on every run
#   start   - a pipeline parameter of the same name, filled in when starting
#   step    - a result of an earlier step: {"step": "...", "result": "..."}

BUILT_LABEL = "concurrency-control/built"
NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
SOURCES = ("default", "fixed", "start", "step")
START_RE = re.compile(r"\$\(params\.([-\w.]+)(\[\*\])?\)")
STEP_RE = re.compile(r"\$\(tasks\.([-\w]+)\.results\.([-\w.]+)\)")


class BuilderError(machines.MachineError):
    pass


def text(body: dict, key: str, where: str):
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BuilderError(f"{where}{key} must be a string")
    return value.strip()


# ---------------- validation ----------------

def validate_input(step: dict, param: dict, source, earlier: dict):
    where = f"step {step['name']}: input {param['name']}"
    if source is None:
        source = {"from": "default"} if param["default"] is not None else None
    if not isinstance(source, dict) or source.get("from") not in SOURCES:
        if source is None:
            raise BuilderError(f"{where} has no default, choose a value for it")
        raise BuilderError(f"{where} needs a source: {', '.join(SOURCES)}")
    kind = source["from"]
    if kind == "default":
        if param["default"] is None:
            raise BuilderError(f"{where} has no default, choose a value for it")
        return {"from": "default"}
    if kind == "start":
        return {"from": "start"}
    if param["type"] != "string":
        raise BuilderError(f"{where} is an {param['type']}, it can only use the default or be asked when starting")
    if kind == "fixed":
        value = source.get("value")
        if not isinstance(value, str):
            raise BuilderError(f"{where} needs a fixed value")
        return {"from": "fixed", "value": value}
    ref, result = source.get("step"), source.get("result")
    if ref not in earlier:
        raise BuilderError(f"{where} can only use the output of an earlier step, not {ref!r}")
    if result not in [r["name"] for r in earlier[ref]["results"]]:
        raise BuilderError(f"{where}: step {ref} has no output {result!r}")
    return {"from": "step", "step": ref, "result": result}


def validate(body, templates: list):
    """Checks a pipeline from the page against the templates; returns it normalized."""
    if not isinstance(body, dict):
        raise BuilderError("the pipeline must be a JSON object")
    description = text(body, "description", "")
    steps = body.get("steps")
    if not isinstance(steps, list) or not steps:
        raise BuilderError("a pipeline needs at least one step")

    normalized, earlier, main_steps = [], {}, set()
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise BuilderError(f"step {index} must be a JSON object")
        name = text(step, "name", f"step {index}: ")
        if not NAME_RE.fullmatch(name):
            raise BuilderError(f"step {index}: {name!r} isn't a valid step name, use lowercase letters, digits and -")
        if name in earlier:
            raise BuilderError(f"two steps are named {name}")
        template = task_templates.get_template(text(step, "template", f"step {name}: "), templates)
        if template is None:
            raise BuilderError(f"step {name}: template {step.get('template')!r} isn't in the catalog")
        is_finally = bool(step.get("finally"))
        run_after = step.get("run_after") or []
        if not isinstance(run_after, list) or not all(isinstance(r, str) for r in run_after):
            raise BuilderError(f"step {name}: run_after must be a list of step names")
        if is_finally and run_after:
            raise BuilderError(f"step {name} runs at the end, it can't wait for particular steps")
        for ref in run_after:
            if ref not in main_steps:
                raise BuilderError(f"step {name} can only wait for an earlier step, not {ref!r}")
        if not is_finally and any(s["finally"] for s in normalized):
            raise BuilderError(f"step {name} must come before the steps that run at the end")

        inputs = step.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise BuilderError(f"step {name}: inputs must map input names to sources")
        declared = {p["name"]: p for p in template["params"]}
        for unknown in set(inputs) - set(declared):
            raise BuilderError(f"step {name}: template {template['path']} has no input {unknown}")
        current = {"name": name}
        checked = {p["name"]: validate_input(current, p, inputs.get(p["name"]), earlier) for p in template["params"]}

        normalized.append({"name": name, "template": template["path"], "finally": is_finally,
                           "run_after": list(dict.fromkeys(run_after)), "inputs": checked})
        earlier[name] = template
        if not is_finally:
            main_steps.add(name)

    if all(s["finally"] for s in normalized):
        raise BuilderError("at least one step must run before the steps that run at the end")
    pipeline_params(normalized, templates)  # raises on conflicting inputs
    return {"description": description, "steps": normalized}


def pipeline_params(steps: list, templates: list):
    """The pipeline parameters: one per input asked when starting, shared by name across steps."""
    params = {}
    for step in steps:
        template = task_templates.get_template(step["template"], templates)
        for param in template["params"]:
            if step["inputs"][param["name"]]["from"] != "start":
                continue
            known = params.get(param["name"])
            if known is None:
                params[param["name"]] = {"name": param["name"], "type": param["type"],
                                         "description": param["description"],
                                         **({} if param["default"] is None else {"default": param["default"]})}
            elif known["type"] != param["type"]:
                raise BuilderError(f"input {param['name']} is a {known['type']} in one step and a "
                                   f"{param['type']} in another, they can't share one value")
    return list(params.values())


# ---------------- model <-> Tekton Pipeline ----------------

def task_entry(step: dict, templates: list):
    template = task_templates.get_template(step["template"], templates)
    entry = {"name": step["name"], "taskRef": {"resolver": "git", "params": [
        {"name": "url", "value": task_templates.REPO},
        {"name": "revision", "value": task_templates.REVISION},
        {"name": "pathInRepo", "value": step["template"]},
    ]}}
    if step["run_after"]:
        entry["runAfter"] = step["run_after"]
    params = []
    for param in template["params"]:
        source = step["inputs"][param["name"]]
        if source["from"] == "fixed":
            params.append({"name": param["name"], "value": source["value"]})
        elif source["from"] == "start":
            value = f"$(params.{param['name']})"
            params.append({"name": param["name"], "value": [value[:-1] + "[*])"] if param["type"] == "array" else value})
        elif source["from"] == "step":
            params.append({"name": param["name"], "value": f"$(tasks.{source['step']}.results.{source['result']})"})
    if params:
        entry["params"] = params
    if template["workspaces"]:
        entry["workspaces"] = [{"name": w["name"], "workspace": w["name"]} for w in template["workspaces"]]
    return entry


def to_pipeline(name: str, model: dict, templates: list):
    workspaces = {}
    for step in model["steps"]:
        for w in task_templates.get_template(step["template"], templates)["workspaces"]:
            # a pipeline workspace is optional only if every step marks it optional
            workspaces[w["name"]] = workspaces.get(w["name"], True) and w["optional"]
    spec = {
        "description": model["description"],
        "params": pipeline_params(model["steps"], templates),
        "tasks": [task_entry(s, templates) for s in model["steps"] if not s["finally"]],
    }
    finally_steps = [task_entry(s, templates) for s in model["steps"] if s["finally"]]
    if finally_steps:
        spec["finally"] = finally_steps
    if workspaces:
        spec["workspaces"] = [{"name": w, **({"optional": True} if optional else {})} for w, optional in workspaces.items()]
    return {"apiVersion": "tekton.dev/v1", "kind": "Pipeline",
            "metadata": {"name": name, "labels": {BUILT_LABEL: "true"}}, "spec": spec}


def parse_value(value):
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        match = START_RE.fullmatch(value[0])
        if match and match.group(2):
            return {"from": "start"}
    if isinstance(value, str):
        if START_RE.fullmatch(value):
            return {"from": "start"}
        match = STEP_RE.fullmatch(value)
        if match:
            return {"from": "step", "step": match.group(1), "result": match.group(2)}
        return {"from": "fixed", "value": value}
    return {"from": "fixed", "value": str(value)}


def from_pipeline(pipeline: dict):
    """The page's model of a pipeline made here."""
    spec = pipeline.get("spec") or {}
    steps = []
    for is_finally, entries in ((False, spec.get("tasks") or []), (True, spec.get("finally") or [])):
        for entry in entries:
            ref = {p["name"]: p["value"] for p in (entry.get("taskRef") or {}).get("params") or []}
            steps.append({"name": entry["name"], "template": ref.get("pathInRepo", ""), "finally": is_finally,
                          "run_after": entry.get("runAfter") or [],
                          "inputs": {p["name"]: parse_value(p["value"]) for p in entry.get("params") or []}})
    return {"description": spec.get("description", ""), "steps": steps}


# ---------------- changes ----------------

def made_here(pipeline: dict):
    return (pipeline.get("metadata", {}).get("labels") or {}).get(BUILT_LABEL) == "true"


def get_pipeline(name: str):
    pipeline = cluster.get_pipeline(name)
    if pipeline is None:
        raise BuilderError(f"pipeline {name} not found", 404)
    return pipeline


def get_model(name: str):
    pipeline = get_pipeline(name)
    if not made_here(pipeline):
        raise BuilderError(f"pipeline {name} wasn't made here, it can only be viewed", 409)
    return from_pipeline(pipeline)


def save_pipeline(name: str, body):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise BuilderError(f"invalid pipeline name {name!r}: lowercase letters, digits and -, at most 63 characters")
    templates = task_templates.list_templates()
    model = validate(body, templates)
    existing = cluster.get_pipeline(name)
    if existing is not None and not made_here(existing):
        raise BuilderError(f"pipeline {name} already exists and wasn't made here, pick another name", 409)
    ok, err = cluster.apply(to_pipeline(name, model, templates))
    if not ok:
        raise BuilderError(f"failed to save pipeline {name}: {err}", 500)
    logger.info(f"pipeline {name} saved with {len(model['steps'])} step(s)")
    return model


def delete_pipeline(name: str):
    if not made_here(get_pipeline(name)):
        raise BuilderError(f"pipeline {name} wasn't made here, it can't be deleted from here", 409)
    if not cluster.delete("pipeline", name):
        raise BuilderError(f"failed to delete pipeline {name}", 500)
    logger.info(f"pipeline {name} deleted")
