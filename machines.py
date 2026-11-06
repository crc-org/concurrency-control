import base64
import os
import re
import cluster
import configmap
from log import logger

# DNS-1123 label: valid both as a secret name and as a configmap key
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
SECRET_LABEL_KEY = "concurrency-control/machine"
KEY_CACHE_DIR = "/tmp/machines"


class MachineError(Exception):
    def __init__(self, message, http_code=400):
        super().__init__(message)
        self.http_code = http_code


def valid_name(name: str):
    return bool(name) and bool(NAME_RE.match(name))


def list_machines():
    return configmap.get_all_machine_statuses()


def add_machine(name: str, host: str, username: str, private_key: str):
    if not valid_name(name):
        raise MachineError(f"invalid machine name {name!r}")
    if not host or not username or not private_key:
        raise MachineError("host, username and private_key are required")
    if name in list_machines():
        raise MachineError(f"machine {name} already exists", 409)

    if not private_key.endswith("\n"):
        private_key += "\n"
    created, err = cluster.create_secret(
        name, {"host": host, "username": username, "id_rsa": private_key}, {SECRET_LABEL_KEY: "true"})
    if not created:
        raise MachineError(f"failed to create secret {name}: {err}", 500)

    if not configmap.write_cm(configmap.MACHINE_STATUS_CM, name, "free"):
        cluster.delete_secret(name)
        raise MachineError(f"failed to register {name} in configmap", 500)
    logger.info(f"machine {name} ({username}@{host}) added")


def remove_machine(name: str):
    if not valid_name(name):
        raise MachineError(f"invalid machine name {name!r}")
    status = list_machines().get(name)
    if status is None:
        raise MachineError(f"machine {name} not found", 404)
    # the controller sets the key back to free when the run finishes, so a
    # busy machine would reappear right after removal
    if status == "busy":
        raise MachineError(f"machine {name} is busy, retry when it is free", 409)

    if not configmap.remove_cm_key(configmap.MACHINE_STATUS_CM, name):
        raise MachineError(f"failed to remove {name} from configmap", 500)
    # only delete secrets created by the API; mounted ones belong to the deployment
    secret = cluster.get_secret(name)
    if secret is not None and secret["metadata"].get("labels", {}).get(SECRET_LABEL_KEY) == "true":
        cluster.delete_secret(name)
    logger.info(f"machine {name} removed")


def fetch_secret(name: str):
    secret = cluster.get_secret(name)
    if secret is None:
        raise MachineError(f"secret {name} not found", 404)
    data = secret.get("data", {})
    decode = lambda k: base64.b64decode(data.get(k, "")).decode()

    key_dir = os.path.join(KEY_CACHE_DIR, name)
    os.makedirs(key_dir, mode=0o700, exist_ok=True)
    keypath = os.path.join(key_dir, "id_rsa")
    fd = os.open(keypath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(decode("id_rsa"))
    return decode("host").strip(), decode("username").strip(), keypath
