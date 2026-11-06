import os
import cluster
import ast
from log import logger

PIPELINE_CM = os.environ.get("PIPELINE_CM")
MACHINE_STATUS_CM = os.environ.get("MACHINE_CM")
FINISH_RUN_KEY = "wait_finish_runs"
FINISH_TASK_KEY = "wait_finish_task"

def get_wait_list(key):
    data = cluster.get_configmap_data(PIPELINE_CM)
    if data is not None and key in data:
        return ast.literal_eval(data[key])
    else:
        print(f"failed to find {key} in configmap {PIPELINE_CM}")
        return None

def get_wait_finish_runs():
    return get_wait_list(FINISH_RUN_KEY)

def get_wait_finish_task():
    return get_wait_list(FINISH_TASK_KEY)

def write_cm(cm_name, key, value):
    if cluster.set_configmap_key(cm_name, key, str(value)):
        return True
    else:
        logger.error(f"failed to change {key} status to {value}")
        return False

def remove_cm_key(cm_name, key):
    return cluster.remove_configmap_key(cm_name, key)

def append_wait_finish_run(new_elelment):
    origi_list = get_wait_finish_runs()
    origi_list.append(new_elelment)
    update_wait_finish_run(origi_list)

def update_wait_finish_run(wait_finish_list):
    if not write_cm(PIPELINE_CM, FINISH_RUN_KEY, wait_finish_list):
        logger.error(f"failed to update config map {PIPELINE_CM} {FINISH_RUN_KEY}")

def append_wait_task(new_elelment):
    origi_list = get_wait_finish_task()
    origi_list.append(new_elelment)
    update_wait_finish_task(origi_list)

def get_all_machine_statuses():
    data = cluster.get_configmap_data(MACHINE_STATUS_CM)
    if data is None:
        logger.error(f"Failed to get configmap {MACHINE_STATUS_CM}")
        return {}
    return data

def update_wait_finish_task(wait_finish_list):
    if not write_cm(PIPELINE_CM, FINISH_TASK_KEY, wait_finish_list):
        logger.error(f"failed to update config map {PIPELINE_CM} {FINISH_TASK_KEY}")
