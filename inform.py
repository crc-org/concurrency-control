import paramiko
import json
import io
import os
import machines

def load_key(keypath: str):
    # any key type ssh-keygen makes (RSA, ECDSA, Ed25519), not only RSA
    return paramiko.PKey.from_path(keypath)

def get_machine_secret(secret_name:str):
    # machines added through the API have no mounted secret, read it from the cluster
    if not os.path.isdir(f"/opt/{secret_name}"):
        return machines.fetch_secret(secret_name)
    with open(f"/opt/{secret_name}/username") as f:
        username = f.read().strip()
    with open(f"/opt/{secret_name}/host") as f:
        host = f.read().strip()
    keypath = f"/opt/{secret_name}/id_rsa"
    return host, username, keypath

def set_inform_message(machine:str, piprlinerun: str):
    message = "current machine is occuied by pipelinerun {}\nPlease not modify anything affects testing".format(piprlinerun)
    # a missing or unreadable secret must not stop the controller loop
    try:
        host, user, keypath = get_machine_secret(machine)
    except Exception as e:
        print(f"Error reading secret of {machine}, skip setting motd: {e}")
        return
    if "windows" in machine:
        os = "windows"
    else:
        os = "linux"
    set_remote_motd(host, user, keypath, message, os)

def clean_inform_message(machine:str):
    try:
        host, user, keypath = get_machine_secret(machine)
    except Exception as e:
        print(f"Error reading secret of {machine}, skip clearing motd: {e}")
        return
    if "windows" in machine:
        os = "windows"
    else:
        os = "linux"
    clear_remote_motd(host, user, keypath, os)
    
def set_remote_motd(host, user, keypath, message, os_type='linux'):
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        private_key = load_key(keypath)
        ssh.connect(host, username=user, pkey=private_key)   
    except Exception as e:
        print(f"Unexpected error ssh to {host}: {e}")
        return
    
    lines = message.splitlines()
    max_len = max(len(line) for line in lines)
    border_char = "#"
    border = border_char * (max_len + 6) 
    motd_content = border + "\n"
    for line in lines:
        if os_type.lower() == 'linux': 
            colored_line = f"\033[1;31m{line.ljust(max_len)}\033[0m"
        else:
            colored_line = f"{line.ljust(max_len)}"
        motd_content += f"{border_char}  {colored_line}  {border_char}\n"
    motd_content += border

    if os_type.lower() == 'linux':
        MOTD_FILE_PATH = "/etc/motd"

        cmd = f'echo -e "{motd_content}" | sudo tee /etc/motd'
    else:
        MOTD_FILE_PATH = f"C:\\Users\\{user}\\ssh_motd.txt"
        bash_friendly_banner = motd_content.replace('"', '\\"')
        cmd = f"echo \"{bash_friendly_banner}\" > {MOTD_FILE_PATH}"

    stdin, stdout, stderr = ssh.exec_command(cmd)
    stderr_output = stderr.read().decode().strip()
    if stderr_output:
        print(f"Error when write banner: {stderr_output}")
    else:
        print("finish remote setup for {}".format(host))

    if os_type.lower() == 'linux':
        cmd = f"""
        for tty in $(who | awk '{{print $2}}'); do
            echo -e "\\033[1;31m{message}\\033[0m" > /dev/$tty
        done
        """
        ssh.exec_command(cmd)
        print("message broadcast to all login user of {}".format(host))
    ssh.close()

def clear_remote_motd(host, user, keypath,os_type='linux'):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if os_type.lower() == 'windows':
        MOTD_FILE_PATH = f"C:\\Users\\{user}\\ssh_motd.txt"
    else:
        MOTD_FILE_PATH = "/etc/motd"

    try:
        private_key = load_key(keypath)
        ssh.connect(host, username=user, pkey=private_key)
    except Exception as e:
        print(f"Error clear motd for {host}: {e}")
        return

    if os_type.lower() == 'linux':
        clear_cmd = f'sudo sh -c "echo > {MOTD_FILE_PATH}"'
    else:
        clear_cmd = f"echo \"\" > {MOTD_FILE_PATH}"
    stdin, stdout, stderr = ssh.exec_command(clear_cmd)
    stderr_output = stderr.read().decode().strip()
    if stderr.read():
        print(f"   - Error when clear motd of {host}: {stderr.read().decode().strip()}")
            
    ssh.close()
