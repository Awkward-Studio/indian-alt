"""Change the hosted inference selector without exposing credentials."""

import argparse
import json
from pathlib import Path
import subprocess

PROJECT = "23cbff0c-48a5-4afc-868a-d93aaecdcae4"
ENVIRONMENT = "599bfb06-8376-4e6b-9556-4bb2568a9db6"
SERVICES = ("4feb8da8-c056-4c95-9205-8d7c345718b6", "1b233bcd-ada5-49a3-9daf-01de8efb8917")
ROOT = Path(__file__).resolve().parent.parent


def railway(*args):
    result = subprocess.run(["railway", *args], cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("Railway command failed. Check CLI authentication and deployment status.")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("h100", "t4"))
    options = parser.parse_args()
    project = json.loads(railway("status", "--json"))
    if project.get("id") != PROJECT:
        raise RuntimeError("Link this repository to the same-front Railway project first.")
    railway("environment", "production")
    for service in SERVICES:
        variables = json.loads(railway("variable", "list", "--service", service, "--environment", ENVIRONMENT, "--json"))
        prefix = options.target.upper() + "_"
        required = ("VLLM_BASE_URL", "VLLM_TEXT_MODEL", "EMBEDDING_BASE_URL", "RERANKER_BASE_URL", "AI_SLOT_TRANSPORT_ENABLED", "CHAT_MODEL_CONTEXT_TOKENS")
        if not all(variables.get(prefix + key) for key in required):
            raise RuntimeError("The selected inference profile is incomplete. Neither service was changed.")
    for service in SERVICES:
        railway("variable", "set", "AI_INFERENCE_TARGET=" + options.target,
                "--service", service, "--environment", ENVIRONMENT, "--skip-deploys")
    for service in SERVICES:
        railway("redeploy", "--service", service, "--yes")
    print("Both hosted services are redeploying with " + options.target.upper() + ". Wait for healthy deployments before regenerating reports.")


if __name__ == "__main__":
    main()
