import os
import cluster
import json 
import time 
import inform
from log import logger
import configmap

NAMESPACE = os.environ.get("NAMESPACE")
TOKEN_PATH = os.environ.get("TOKEN_PATH")
SERVER = os.environ.get("SERVER")
MACHINE_STATUS_CM = os.environ.get("MACHINE_CM")
LABEL_MACHINE_PR = os.environ.get("LABEL_MACHINE_PR")
LAEBL_MACHINE_TASK = os.environ.get("LAEBL_MACHINE_TASK")
LABEL_TASK = os.environ.get("LABEL_TASK")



def set_project():
    env = dict(os.environ, KUBECONFIG="/tmp/kubeconfig")
    code, out, err = cluster.oc("project", NAMESPACE, env=env)
    if code == 0:
        logger.info(out)
    else:
        logger.error(out)
        logger.error(err)
        raise Exception("can't access to project {}".format(NAMESPACE))

def get_pending_runs():
    runs = [pr for pr in cluster.list_pipelineruns()
            if pr.get("spec", {}).get("status") == "PipelineRunPending"]
    # oldest first, so runs waiting for the same machine are served in order
    runs.sort(key=lambda pr: pr["metadata"].get("creationTimestamp", ""))
    pendings = [pr["metadata"]["name"] for pr in runs]
    if not pendings:
        logger.info("Not find any pending pipelinerun")
    return pendings
    
def get_run_lable(run_name: str, label_name: str):
    run = cluster.get_pipelinerun(run_name)
    if run is not None:
        return run["metadata"].get("labels", {}).get(label_name)
    else:
        return None

def get_machine_status(machine: str):
    data = cluster.get_configmap_data(MACHINE_STATUS_CM)
    if data is not None and machine in data:
        return data[machine]
    else:
        logger.error(f"failed to find {machine} in configmap {MACHINE_STATUS_CM}")
        return None

def change_machine_status(machine: str, status: str):
    if cluster.set_configmap_key(MACHINE_STATUS_CM, machine, status):
        logger.info(f"successfully set {machine} to {status}")
        return True
    else:
        logger.error(f"failed to change {machine} status to {status}")
        return False

def check_run_finish(run: str):
    status = get_run_status(run)
    if status in ("Succeeded", "Failed"):
        return True
    elif status == "Running":
        return False
    elif status == "Running(PipelineRunPending)":
        start_pending_run(run)
        return False
    elif "Failed" in (status or ""): 
        return True
    elif status == None:
        logger.error(f"failed to get {run} status, set it as finish")
        return True
    else:
        logger.error(f"unknow status of {run}: {status}")
        return True

def get_run_status(run: str):
    status = None
    pipelinerun = cluster.get_pipelinerun(run)
    if pipelinerun is not None:
        status = cluster.pipelinerun_status(pipelinerun)
    else:
        logger.error(f"failed to get {run} status")
    return status

def monitor_runs_finish():
    new_wait_list = []
    wait_finish_run = configmap.get_wait_finish_runs()
    if len(wait_finish_run) > 0:
        logger.info("************** Monitor pipelinerun finish **************")
    for run,machine in wait_finish_run:
        logger.info(f"--- {run} ---")
        status = check_run_finish(run)
        if status == True:
            logger.info(f"pipelinerun {run} has finished, set {machine} to free")
            change_machine_status(machine, "free")
            inform.clean_inform_message(machine)
        else:
            logger.info(f"pipelinerun {run} is still running, machine {machine} is busy")
            new_wait_list.append((run, machine))
    configmap.update_wait_finish_run(new_wait_list)
    logger.info("\nWAIT_FINISH_RUNS:")
    logger.info(new_wait_list)


def start_pending_run(run: str):
    if cluster.start_pipelinerun(run):
        logger.info(f"sucessfully start pipelinerun {run}")
    else:
        logger.error("failed to start pipelinerun {}".format(run))  

def monitor_pending_run():
    logger.info("************** Monitor pending run **************")
    pendings = get_pending_runs()
        
    for pend in pendings:
        logger.info(f"--- {pend} ---")
        use_second_machine = False
        status2 = "free"

        machine1 = get_run_lable(pend, LABEL_MACHINE_PR)
        machine2 = get_run_lable(pend, LAEBL_MACHINE_TASK)
        if machine1 != None:
            status1 = get_machine_status(machine1)
        else:
            logger.warning(f"{pend} not correctly config the {LABEL_MACHINE_PR} label")
            continue
        if machine2 != None:
            status2 = get_machine_status(machine2)
            use_second_machine = True
        
        if status1 == "free" and status2 == "free":
            logger.info(f"machine {machine1} is available")
            inform.set_inform_message(machine1, pend)
            change_machine_status(machine1, "busy")
            configmap.append_wait_finish_run((pend,machine1))

            if use_second_machine:
                logger.info(f"machine {machine2} is available")
                inform.set_inform_message(machine2, pend)
                change_machine_status(machine2, "busy")
                configmap.append_wait_task((pend, machine2))
            
            logger.info(f"start pipelinerun {pend}")
            start_pending_run(pend)
            continue

        if status1 == "busy":
            logger.info(f"pipelinerun {pend} waits for machine {machine1}")
        elif status2 == "busy":
            logger.info(f"pipelinerun {pend} waits for machine {machine2}")
        else:
            logger.warning(f"machine {machine1} status is {status1} !!!")
            if use_second_machine:
                logger.warning(f"machine {machine2} status is {status2} !!!")

def check_task_finish(pipelinerun: str, task_name: str):
    taskruns = cluster.get_taskruns(pipelinerun, task_name)
    if taskruns:
        status = cluster.taskrun_succeeded(taskruns[0])
        if status == "True":
            return True
        elif status == "False":
            return True
        else:
            return False
    else:
        logger.info(f"task {task_name} in pipelineRun {pipelinerun} not find")
        if not check_run_finish(pipelinerun):
            return False
        else:
            logger.error(f"pipelineRun {pipelinerun} as finished / not exist")
            logger.error(f"set task {task_name} in pipelineRun {pipelinerun} as finished")
            return True

def monitor_task():
    new_wait_list = []
    wait_task_finish = configmap.get_wait_finish_task()
    if len(wait_task_finish) > 0:
        logger.info("************** Monitor tasks **************")
    for run, machine in wait_task_finish:
        logger.info(f"--- {run} {machine} ---")
        status =  check_task_finish(run, LABEL_TASK)
        if status == True:
            logger.info(f"task {LABEL_TASK} of pipelinerun {run} has finished, set {machine} to free")
            change_machine_status(machine, "free")
            inform.clean_inform_message(machine)
        else:
            logger.info(f"task {LABEL_TASK} of pipelinerun {run} is not finish, machine {machine} is busy")
            new_wait_list.append((run, machine))
    configmap.update_wait_finish_task(new_wait_list)
    logger.info("\nWAIT_TASK_FINISH")
    logger.info(new_wait_list)

if __name__ == "__main__":
    from web import app
    import threading

    web_thread = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=8080))
    web_thread.daemon = True
    web_thread.start()

    set_project()
    
    while True:
        monitor_task()
        monitor_runs_finish() 
        monitor_pending_run()
        logger.info("\n\nsleep 1 minute\n")
        time.sleep(1*60)
