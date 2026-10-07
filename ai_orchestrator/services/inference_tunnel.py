"""Supervise private model API forwarding for a hosted application container."""

import logging
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading

logger = logging.getLogger(__name__)


def build_command(env, key_path, known_hosts_path):
    host = env.get("INFERENCE_SSH_HOST", "")
    user = env.get("INFERENCE_SSH_USER", "")
    if not host or not user or host.startswith("-") or user.startswith("-"):
        raise ValueError("Set INFERENCE_SSH_HOST and INFERENCE_SSH_USER.")
    forwards = env.get("INFERENCE_SSH_FORWARD_PORTS", "8002,8081,8082").split(",")
    ports = [int(port.strip()) for port in forwards]
    if not ports or any(port < 1 or port > 65535 for port in ports):
        raise ValueError("INFERENCE_SSH_FORWARD_PORTS must contain valid TCP ports.")
    command = [
        "ssh", "-F", "/dev/null", "-N", "-T", "-i", str(key_path),
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts_path}",
        "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
    ]
    for port in ports:
        command.extend(["-L", f"127.0.0.1:{port}:127.0.0.1:{port}"])
    command.append(f"{user}@{host}")
    return command


def main():
    logging.basicConfig(level=logging.INFO)
    from decouple import config
    from .inference_target import apply_inference_target
    apply_inference_target(lambda key, default: config(key, default=default))
    if not os.environ.get("INFERENCE_SSH_HOST"):
        return
    key = os.environ.get("INFERENCE_SSH_PRIVATE_KEY", "")
    known_hosts = os.environ.get("INFERENCE_SSH_KNOWN_HOSTS", "")
    if not key or not known_hosts:
        raise ValueError("The inference tunnel requires a private key and pinned SSH host keys.")
    stopped = threading.Event()
    process = None

    def stop(_signum, _frame):
        stopped.set()
        if process is not None and process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with tempfile.TemporaryDirectory(prefix="inference-ssh-") as directory:
        key_path = Path(directory) / "key"
        known_hosts_path = Path(directory) / "known_hosts"
        key_path.write_text(key.strip() + "\n")
        key_path.chmod(0o600)
        known_hosts_path.write_text(known_hosts.strip() + "\n")
        command = build_command(os.environ, key_path, known_hosts_path)
        while not stopped.is_set():
            logger.info("Connecting the private inference tunnel.")
            process = subprocess.Popen(command)
            exit_code = process.wait()
            if not stopped.is_set():
                logger.warning("Inference tunnel exited with code %s; reconnecting in 10 seconds.", exit_code)
                stopped.wait(10)


if __name__ == "__main__":
    main()
