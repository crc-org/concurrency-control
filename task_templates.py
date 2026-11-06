import json
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import yaml
from log import logger

# The building blocks for pipelines made in the web UI: every Tekton Task in
# the ci-definitions repo, always at main. They're only read here; pipelines
# point at them with Tekton's git resolver, so they can't be changed from here.

REPO = os.environ.get("TEMPLATE_REPO", "https://github.com/crc-org/ci-definitions")
REVISION = "main"
# read the templates from a local clone instead of GitHub (demo, offline work)
TEMPLATE_DIR = os.environ.get("TEMPLATE_DIR")
CACHE_SECONDS = 600
FETCH_TIMEOUT = 10
# generated or example files that aren't templates
SKIP_DIRS = {".git", ".github", "tpl", "samples"}


class TemplateError(Exception):
    def __init__(self, message, http_code=502):
        super().__init__(message)
        self.http_code = http_code


_cache = {"at": 0.0, "templates": None}
_lock = threading.Lock()


def github_repo():
    match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+?)(\.git)?/?", REPO)
    if not match:
        raise TemplateError(f"TEMPLATE_REPO {REPO} isn't a github.com repository", 500)
    return match.group(1), match.group(2)


def http_get(url: str):
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as resp:
        return resp.read().decode()


def candidate_paths():
    """Repo paths of the YAML files that might hold a Task."""
    if TEMPLATE_DIR:
        paths = []
        for dirpath, dirs, files in os.walk(TEMPLATE_DIR):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            paths += [os.path.relpath(os.path.join(dirpath, f), TEMPLATE_DIR)
                      for f in sorted(files) if f.endswith((".yaml", ".yml"))]
        return paths
    owner, repo = github_repo()
    tree = json.loads(http_get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{REVISION}?recursive=1"))
    return [item["path"] for item in tree.get("tree", [])
            if item.get("type") == "blob" and item["path"].endswith((".yaml", ".yml"))
            and not SKIP_DIRS & set(item["path"].split("/")[:-1])]


def read_file(path: str):
    if TEMPLATE_DIR:
        with open(os.path.join(TEMPLATE_DIR, path)) as f:
            return f.read()
    owner, repo = github_repo()
    return http_get(f"https://raw.githubusercontent.com/{owner}/{repo}/{REVISION}/{path}")


def describe(path: str, task: dict):
    spec = task.get("spec") or {}
    return {
        "path": path,
        "name": task["metadata"]["name"],
        "description": (spec.get("description") or "").strip(),
        "params": [{"name": p["name"], "type": p.get("type", "string"), "default": p.get("default"),
                    "description": (p.get("description") or "").strip()} for p in spec.get("params") or []],
        "results": [{"name": r["name"], "description": (r.get("description") or "").strip()}
                    for r in spec.get("results") or []],
        "workspaces": [{"name": w["name"], "optional": bool(w.get("optional")),
                        "description": (w.get("description") or "").strip()} for w in spec.get("workspaces") or []],
    }


def load(path: str):
    """The Task in a file, described, or None if the file holds no Task or can't be read."""
    try:
        docs = [d for d in yaml.safe_load_all(read_file(path)) if isinstance(d, dict)]
    except Exception as e:
        logger.error(f"template {path} can't be read: {e}")
        return None
    tasks = [d for d in docs if d.get("kind") == "Task" and isinstance(d.get("metadata"), dict)]
    return describe(path, tasks[0]) if tasks else None


def list_templates(refresh=False):
    with _lock:
        if not refresh and _cache["templates"] is not None and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["templates"]
        source = TEMPLATE_DIR or f"{REPO} at {REVISION}"
        try:
            paths = candidate_paths()
        except TemplateError:
            raise
        except Exception as e:
            raise TemplateError(f"can't list the templates in {source}: {e}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            templates = sorted((t for t in pool.map(load, paths) if t is not None), key=lambda t: t["path"])
        if not templates:
            raise TemplateError(f"no Task templates found in {source}")
        _cache.update(at=time.time(), templates=templates)
        return templates


def get_template(path: str, templates=None):
    for template in templates if templates is not None else list_templates():
        if template["path"] == path:
            return template
    return None
