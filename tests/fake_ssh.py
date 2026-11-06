"""Stand-in for checks.Session, so machine checks never open an SSH connection.

FakeSSH.session takes the same arguments as checks.Session and returns a
session whose run() answers from FakeSSH.answers. A command missing from the
table fails the way bash does when a program isn't installed.
"""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

# what a healthy machine prints for each command checks.py runs
LINUX_OK = {
    "uname -s": (0, "Linux\n", ""),
    "git --version": (0, "git version 2.45.1\n", ""),
    "make --version | head -n 1": (0, "GNU Make 4.4.1\n", ""),
    "go version": (0, "go version go1.23.2 linux/amd64\n", ""),
    "sudo -n true && echo ok": (0, "ok\n", ""),
}
WINDOWS_OK = {
    "uname -s": (0, "MINGW64_NT-10.0-22631\n", ""),
    "git --version": (0, "git version 2.45.1.windows.1\n", ""),
    "make --version | head -n 1": (0, "GNU Make 4.4.1\n", ""),
    "go version": (0, "go version go1.23.2 windows/amd64\n", ""),
    "dotnet --version": (0, "8.0.403\n", ""),
}


def private_key(kind="ed25519"):
    """A fresh private key in the OpenSSH format ssh-keygen writes."""
    generate = {
        "ed25519": ed25519.Ed25519PrivateKey.generate,
        "rsa": lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "ecdsa": lambda: ec.generate_private_key(ec.SECP256R1()),
    }[kind]
    return generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                    serialization.NoEncryption()).decode()


class FakeSSH:
    def __init__(self):
        self.answers = dict(LINUX_OK)  # command -> (exit code, stdout, stderr), or an exception to raise
        self.connect_error = None      # raised on login when set
        self.logins = []               # (host, user, key type) of every login
        self.commands = []             # every command run, in order
        self.closed = 0

    def session(self, host, user, key):
        self.logins.append((host, user, key.get_name()))
        if self.connect_error is not None:
            raise self.connect_error
        return FakeSession(self)


class FakeSession:
    def __init__(self, ssh: FakeSSH):
        self.ssh = ssh

    def run(self, command: str):
        self.ssh.commands.append(command)
        answer = self.ssh.answers.get(command)
        if answer is None:
            return 127, "", f"bash: {command.split()[0]}: command not found\n"
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        self.ssh.closed += 1
