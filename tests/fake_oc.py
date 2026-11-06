"""In-memory stand-in for the oc CLI, so tests never reach a real cluster.

FakeCluster.oc takes the same arguments and returns the same
(returncode, stdout, stderr) tuple as cluster.oc. It understands only the oc
commands cluster.py runs and raises on anything else, so a new kind of
cluster call fails loudly instead of being silently ignored.
"""
import base64
import copy
import json
import random
import re
import threading

# resource names as the API server prints them in errors
RESOURCES = {
    "configmap": "configmaps",
    "secret": "secrets",
    "pipeline": "pipelines.tekton.dev",
    "pipelinerun": "pipelineruns.tekton.dev",
    "taskrun": "taskruns.tekton.dev",
}

# what the API server accepts as an object name or a configmap key
DNS1123_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
CONFIG_KEY = re.compile(r"[-._a-zA-Z0-9]+")

# the characters the API server picks generateName suffixes from
NAME_SUFFIX_CHARS = "bcdfghjklmnpqrstvwxz2456789"

# Go's name for a JSON value it can't unmarshal into a string
JSON_TYPES = [(bool, "bool"), ((int, float), "number"), (dict, "object"), (list, "array")]


def merge_patch(target: dict, patch: dict):
    # RFC 7386, what oc patch --type=merge does
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_patch(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class FakeCluster:
    def __init__(self, machine_cm: str, pipeline_cm: str):
        self.machine_cm = machine_cm
        self.pipeline_cm = pipeline_cm
        self.objects = {kind: {} for kind in RESOURCES}
        self.calls = []     # (args, stdin) of every oc call, in order
        self.failures = []  # (args prefix, stderr) of calls that should fail
        self.lock = threading.RLock()
        self.add("configmap", machine_cm, data={})
        self.add("configmap", pipeline_cm, data={"wait_finish_runs": "[]", "wait_finish_task": "[]"})

    # ---------------- setting up and inspecting state ----------------

    def add(self, kind: str, name: str, labels=None, **fields):
        metadata = {"name": name, "labels": labels or {}, "creationTimestamp": "2026-11-04T10:00:00Z"}
        self.objects[kind][name] = {"metadata": metadata, **fields}

    def data(self, configmap: str):
        """The live data dict of a configmap, for direct edits."""
        return self.objects["configmap"][configmap].setdefault("data", {})

    def set_machine(self, name: str, status: str):
        self.data(self.machine_cm)[name] = status

    def machines(self):
        return dict(self.data(self.machine_cm))

    def set_holds(self, runs=(), tasks=()):
        # stored the way the controller writes them: str() of a list of tuples
        self.data(self.pipeline_cm)["wait_finish_runs"] = str(list(runs))
        self.data(self.pipeline_cm)["wait_finish_task"] = str(list(tasks))

    def add_secret(self, name: str, labels=None, **values):
        data = {key: base64.b64encode(value.encode()).decode() for key, value in values.items()}
        self.add("secret", name, labels=labels, type="Opaque", data=data)

    def secret(self, name: str):
        """Labels and decoded data of a secret, or None if it doesn't exist."""
        secret = self.objects["secret"].get(name)
        if secret is None:
            return None
        data = {key: base64.b64decode(value).decode() for key, value in secret.get("data", {}).items()}
        return {"labels": secret["metadata"].get("labels") or {}, "data": data}

    def add_pipeline(self, name: str, params=(), workspaces=(), description=""):
        """A Pipeline; params are (name, default) or (name, default, type), default None means required;
        workspaces are names, or (name, optional)."""
        spec = {"description": description, "tasks": [{"name": "run", "taskRef": {"name": "run"}}]}
        spec["params"] = [{"name": p[0], "type": p[2] if len(p) > 2 else "string",
                           **({} if p[1] is None else {"default": p[1]}), "description": f"the {p[0]}"}
                          for p in params]
        spec["workspaces"] = [{"name": w, "optional": False} if isinstance(w, str) else {"name": w[0], "optional": w[1]}
                              for w in workspaces]
        self.add("pipeline", name, spec=spec)

    def add_pipelinerun(self, name: str, labels=None, pending=True):
        reason = "PipelineRunPending" if pending else "Running"
        self.add("pipelinerun", name, labels=labels,
                 spec={"status": "PipelineRunPending"} if pending else {},
                 status={"conditions": [{"type": "Succeeded", "status": "Unknown", "reason": reason}]})

    def finish_pipelinerun(self, name: str, succeeded=True):
        status, reason = ("True", "Succeeded") if succeeded else ("False", "Failed")
        self.objects["pipelinerun"][name]["status"]["conditions"] = [
            {"type": "Succeeded", "status": status, "reason": reason}]

    def add_taskrun(self, name: str, pipelinerun: str, pipeline_task: str, succeeded="Unknown"):
        labels = {"tekton.dev/pipelineRun": pipelinerun, "tekton.dev/pipelineTask": pipeline_task}
        self.add("taskrun", name, labels=labels,
                 status={"conditions": [{"type": "Succeeded", "status": succeeded}]})

    def fail(self, *prefix, stderr="error: injected failure"):
        """Make every later oc call whose arguments start with prefix fail."""
        self.failures.append((prefix, stderr))

    # ---------------- oc ----------------

    def oc(self, *args, input=None, env=None):
        with self.lock:
            self.calls.append((args, input))
            for prefix, stderr in self.failures:
                if args[:len(prefix)] == prefix:
                    return 1, "", stderr + "\n"
            if args[0] == "get" and len(args) >= 2:
                return self._get(args[1], list(args[2:]))
            if args[:3] == ("set", "data", "configmap") and len(args) == 5:
                return self._set_data(args[3], args[4])
            if args == ("create", "-f", "-"):
                return self._create(json.loads(input))
            if args == ("apply", "-f", "-"):
                return self._apply(json.loads(input))
            if args == ("create", "-f", "-", "-o", "json"):
                return self._create_pipelinerun(json.loads(input))
            if args[:2] == ("create", "configmap") and len(args) == 3:
                return self._create_configmap(args[2])
            if args[0] == "delete" and len(args) == 3:
                return self._delete(args[1], args[2])
            if args[0] == "patch" and len(args) == 6 and args[3:5] == ("--type=merge", "-p"):
                return self._patch(args[1], args[2], json.loads(args[5]))
            if args[0] == "project" and len(args) == 2:
                return 0, f'Now using project "{args[1]}" on server "https://api.fake:6443".\n', ""
            raise AssertionError(f"FakeCluster doesn't support `oc {' '.join(args)}`")

    def _not_found(self, kind, name):
        return 1, "", f'Error from server (NotFound): {RESOURCES[kind]} "{name}" not found\n'

    def _get(self, kind, rest):
        if kind not in RESOURCES or rest[-2:] != ["-o", "json"]:
            raise AssertionError(f"FakeCluster doesn't support `oc get {kind} {' '.join(rest)}`")
        rest = rest[:-2]
        name = rest.pop(0) if rest and rest[0] != "-l" else None
        selector = rest[1] if rest[:1] == ["-l"] else None
        if name is not None:
            if name not in self.objects[kind]:
                return self._not_found(kind, name)
            return 0, json.dumps(self.objects[kind][name]), ""
        wanted = dict(pair.split("=", 1) for pair in selector.split(",")) if selector else {}
        items = [obj for obj in self.objects[kind].values()
                 if all(obj["metadata"]["labels"].get(k) == v for k, v in wanted.items())]
        return 0, json.dumps({"apiVersion": "v1", "kind": "List", "items": items}), ""

    def _set_data(self, name, pair):
        if name not in self.objects["configmap"]:
            return self._not_found("configmap", name)
        key, assign, value = pair.partition("=")
        if not assign:
            assert key.endswith("-"), f"oc set data expects key=value or key-, got {pair!r}"
            key = key[:-1]
        if not CONFIG_KEY.fullmatch(key):
            return 1, "", f'error: "{key}" is not a valid key name for a ConfigMap\n'
        if assign:
            self.data(name)[key] = value
        else:
            self.data(name).pop(key, None)
        return 0, f"configmap/{name} data updated\n", ""

    def _create(self, manifest):
        assert manifest.get("kind") == "Secret", "the code only ever creates secrets"
        name = manifest["metadata"]["name"]
        if not (len(name) <= 253 and DNS1123_SUBDOMAIN.fullmatch(name)):
            return 1, "", (f'The Secret "{name}" is invalid: metadata.name: Invalid value: "{name}": '
                           "a lowercase RFC 1123 subdomain must consist of lower case alphanumeric "
                           "characters, '-' or '.'\n")
        string_data = manifest.get("stringData") or {}
        for value in string_data.values():
            for types, json_type in JSON_TYPES:
                if isinstance(value, types):
                    return 1, "", ('Error from server (BadRequest): error when creating "STDIN": Secret in '
                                   'version "v1" cannot be handled as a Secret: json: cannot unmarshal '
                                   f"{json_type} into Go struct field Secret.stringData of type string\n")
        if name in self.objects["secret"]:
            return 1, "", (f'Error from server (AlreadyExists): error when creating "STDIN": '
                           f'secrets "{name}" already exists\n')
        data = {key: base64.b64encode((value or "").encode()).decode() for key, value in string_data.items()}
        self.add("secret", name, labels=manifest["metadata"].get("labels"),
                 type=manifest.get("type", "Opaque"), data=data)
        return 0, f"secret/{name} created\n", ""

    def _apply(self, manifest):
        assert manifest.get("kind") == "Pipeline", "only Pipelines are applied"
        name = manifest["metadata"]["name"]
        verb = "configured" if name in self.objects["pipeline"] else "created"
        self.add("pipeline", name, labels=manifest["metadata"].get("labels"), apiVersion=manifest["apiVersion"],
                 spec=copy.deepcopy(manifest["spec"]))
        return 0, f"pipeline.tekton.dev/{name} {verb}\n", ""

    def _create_configmap(self, name):
        if name in self.objects["configmap"]:
            return 1, "", f'error: failed to create configmap: configmaps "{name}" already exists\n'
        self.add("configmap", name, data={})
        return 0, f"configmap/{name} created\n", ""

    def _create_pipelinerun(self, manifest):
        assert manifest.get("kind") == "PipelineRun", "only PipelineRuns are created with -o json"
        metadata = manifest["metadata"]
        assert "generateName" in metadata and "name" not in metadata, "runs are created with generateName"
        while True:
            name = metadata["generateName"] + "".join(random.choice(NAME_SUFFIX_CHARS) for _ in range(5))
            if name not in self.objects["pipelinerun"]:
                break
        # what Tekton reports for a run created pending
        self.add("pipelinerun", name, labels=metadata.get("labels"), apiVersion=manifest["apiVersion"],
                 spec=copy.deepcopy(manifest["spec"]),
                 status={"conditions": [{"type": "Succeeded", "status": "Unknown", "reason": "PipelineRunPending"}]})
        return 0, json.dumps(self.objects["pipelinerun"][name]), ""

    def _delete(self, kind, name):
        if name not in self.objects[kind]:
            return self._not_found(kind, name)
        del self.objects[kind][name]
        return 0, f'{kind} "{name}" deleted\n', ""

    def _patch(self, kind, name, patch):
        obj = self.objects[kind].get(name)
        if obj is None:
            return self._not_found(kind, name)
        was_pending = obj.get("spec", {}).get("status") == "PipelineRunPending"
        merge_patch(obj, patch)
        # stand in for Tekton: a run whose pending status is cleared starts running
        if kind == "pipelinerun" and was_pending and not obj["spec"].get("status"):
            obj["status"]["conditions"] = [{"type": "Succeeded", "status": "Unknown", "reason": "Running"}]
        return 0, f"{RESOURCES[kind]}/{name} patched\n", ""
