"""Resolve one inference target before Django or the SSH supervisor starts."""

import os

PROFILE_KEYS = (
    "VLLM_BASE_URL", "VLLM_API_KEY", "VLLM_TEXT_MODEL", "VLLM_PLANNER_MODEL",
    "VLLM_EMBEDDING_URL", "VLLM_EMBEDDING_MODEL",
    "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL",
    "RERANKER_BASE_URL", "RERANKER_API_KEY", "RERANKER_MODEL",
    "DOC_PROCESSOR_URL", "DOC_PROCESSOR_API_KEY", "AI_INFERENCE_MAX_CONCURRENT_REQUESTS",
    "VDR_DURABLE_REPORT_CONCURRENCY",
    "AI_SLOT_TRANSPORT_ENABLED", "AI_VM_CONTROL_ENABLED", "AI_VM_TARGET_LABEL",
    "AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP", "AZURE_VM_NAME",
    "INFERENCE_TEXT_CONTAINER", "INFERENCE_SSH_HOST", "INFERENCE_SSH_USER",
    "INFERENCE_SSH_FORWARD_PORTS",
    "CHAT_MODEL_CONTEXT_TOKENS", "VDR_REPORT_SECTION_EVIDENCE_TOKENS",
    "VDR_REPORT_SECTION_INPUT_TOKENS", "VDR_REPORT_SECTION_MAX_TOKENS",
    "VDR_REPORT_SECTION_CANDIDATES", "VDR_REPORT_SECTION_MAX_CHUNKS",
    "VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS", "VDR_ARTIFACT_SEGMENT_INPUT_TOKENS",
    "VDR_ARTIFACT_SEGMENT_MAX_TOKENS", "VDR_ARTIFACT_SPREADSHEET_SOURCE_TOKENS",
)
REQUIRED_KEYS = (
    "VLLM_BASE_URL", "VLLM_TEXT_MODEL", "EMBEDDING_BASE_URL",
    "RERANKER_BASE_URL", "AI_SLOT_TRANSPORT_ENABLED",
    "CHAT_MODEL_CONTEXT_TOKENS",
)


def apply_inference_target(getter=None, environ=None):
    environ = os.environ if environ is None else environ
    getter = environ.get if getter is None else getter
    target = getter("AI_INFERENCE_TARGET", "").strip().lower()
    if not target:
        return ""  # Existing deployments retain their unprefixed configuration.
    if target not in {"h100", "t4"}:
        raise ValueError("AI_INFERENCE_TARGET must be h100 or t4.")
    prefix = target.upper() + "_"
    values = {key: getter(prefix + key, None) for key in PROFILE_KEYS}
    missing = [prefix + key for key in REQUIRED_KEYS if not values[key]]
    if missing:
        raise ValueError("Missing inference profile settings: " + ", ".join(missing))
    # A T4 profile must never inherit the H100 tunnel or planner model.
    values["INFERENCE_SSH_HOST"] = values["INFERENCE_SSH_HOST"] or ""
    for key in ("VLLM_API_KEY", "EMBEDDING_API_KEY", "RERANKER_API_KEY", "DOC_PROCESSOR_API_KEY"):
        values[key] = values[key] or ""
    values["VLLM_PLANNER_MODEL"] = values["VLLM_PLANNER_MODEL"] or values["VLLM_TEXT_MODEL"]
    for key, value in values.items():
        if value is not None:
            environ[key] = str(value)
    environ["AI_INFERENCE_TARGET"] = target
    return target
