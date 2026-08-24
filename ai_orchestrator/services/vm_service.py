import logging
import os
from dataclasses import dataclass, field
from typing import Dict
from urllib.error import URLError
from urllib.request import Request, urlopen
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from decouple import config

logger = logging.getLogger(__name__)


@dataclass
class VMControlSnapshot:
    control_enabled: bool
    configured: bool
    target_label: str
    power_state: str = "unknown"
    service_state: str = "unknown"
    services: Dict[str, str] = field(default_factory=dict)
    error: str = ""

class VMControlService:
    """
    Server-side control for the single verified inference VM.

    The service stays unavailable until the explicit feature flag, target
    identity, and dedicated VM credentials are all present.
    """

    def __init__(self):
        self.enabled = self._env('AI_VM_CONTROL_ENABLED').lower() == 'true'
        self.target_label = self._env('AI_VM_TARGET_LABEL')
        self.subscription_id = self._env('AZURE_SUBSCRIPTION_ID')
        self.resource_group = self._env('AZURE_RESOURCE_GROUP')
        self.vm_name = self._env('AZURE_VM_NAME')
        self.tenant_id = self._env('AZURE_VM_TENANT_ID')
        self.client_id = self._env('AZURE_VM_CLIENT_ID')
        self.client_secret = self._env('AZURE_VM_CLIENT_SECRET')
        self.inference_urls = {
            "text": self._env('VLLM_BASE_URL'),
            "embedding": self._env('EMBEDDING_BASE_URL'),
            "reranker": self._env('RERANKER_BASE_URL'),
        }
        self.configured = bool(self.target_label and all([
            self.subscription_id, self.resource_group, self.vm_name,
            self.tenant_id, self.client_id, self.client_secret,
        ]))
        
        try:
            if self.configured:
                self.credential = ClientSecretCredential(
                    tenant_id=self.tenant_id,
                    client_id=self.client_id,
                    client_secret=self.client_secret
                )
                self.compute_client = ComputeManagementClient(self.credential, self.subscription_id)
            else:
                logger.info("Azure VM control is disabled or incompletely configured.")
                self.compute_client = None
        except Exception as e:
            logger.error("Failed to initialize Azure Compute Client: %s", type(e).__name__)
            self.compute_client = None

    @staticmethod
    def _env(name):
        return os.environ.get(name) or config(name, default='')

    @property
    def available(self):
        return self.enabled and self.read_available

    @property
    def read_available(self):
        return self.configured and self.compute_client is not None

    @property
    def target_resource_id(self):
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.Compute/virtualMachines/{self.vm_name}"
        )

    def get_status(self) -> str:
        if not self.read_available:
            return "unavailable"
            
        try:
            vm = self.compute_client.virtual_machines.instance_view(self.resource_group, self.vm_name)
            for status in vm.statuses:
                if status.code.startswith('PowerState/'):
                    return status.code.split("/", 1)[1].lower()
            return "unknown"
        except Exception as e:
            logger.warning("Azure VM status lookup failed: %s", type(e).__name__)
            return "unknown"

    def _probe(self, url):
        if not url:
            return "unknown"
        try:
            request = Request(url.rstrip("/") + "/health", method="GET")
            with urlopen(request, timeout=3) as response:
                return "ready" if 200 <= response.status < 300 else "degraded"
        except (OSError, URLError):
            return "offline"

    def snapshot(self):
        if not self.read_available:
            return VMControlSnapshot(False, self.configured, self.target_label, error="VM control is not configured.")
        power_state = self.get_status()
        services = {name: ("offline" if power_state != "running" else self._probe(url)) for name, url in self.inference_urls.items()}
        service_state = "ready" if power_state == "running" and services and all(value == "ready" for value in services.values()) else ("offline" if power_state != "running" else "warming")
        return VMControlSnapshot(self.enabled, True, self.target_label, power_state, service_state, services)

    def start_vm(self):
        if not self.available:
            raise RuntimeError("VM control is not available")
        try:
            logger.info(f"Triggering Start for VM: {self.vm_name}")
            self.compute_client.virtual_machines.begin_start(self.resource_group, self.vm_name)
            # We don't wait for completion here to avoid timing out the API request
            return {"status": "submitted"}
        except Exception as e:
            logger.warning("Failed to start VM: %s", type(e).__name__)
            raise RuntimeError("Azure rejected the start request") from e

    def stop_vm(self):
        if not self.available:
            raise RuntimeError("VM control is not available")
        try:
            logger.info(f"Triggering Deallocate for VM: {self.vm_name}")
            # deallocate is better than stop because it stops billing for the GPU
            self.compute_client.virtual_machines.begin_deallocate(self.resource_group, self.vm_name)
            return {"status": "submitted"}
        except Exception as e:
            logger.warning("Failed to deallocate VM: %s", type(e).__name__)
            raise RuntimeError("Azure rejected the deallocate request") from e
