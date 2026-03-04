import logging
import os
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from decouple import config

logger = logging.getLogger(__name__)

class VMControlService:
    """
    Service to manage the Azure AI VM (Start/Stop/Status).
    Requires: AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, AZURE_VM_NAME
    """

    def __init__(self):
        # Prefer os.environ for compatibility
        import os
        self.subscription_id = os.environ.get('AZURE_SUBSCRIPTION_ID') or config('AZURE_SUBSCRIPTION_ID', default='')
        self.resource_group = os.environ.get('AZURE_RESOURCE_GROUP') or config('AZURE_RESOURCE_GROUP', default='')
        self.vm_name = os.environ.get('AZURE_VM_NAME') or config('AZURE_VM_NAME', default='')
        
        # Explicit credentials
        self.tenant_id = os.environ.get('AZURE_TENANT_ID') or config('AZURE_TENANT_ID', default='')
        self.client_id = os.environ.get('AZURE_CLIENT_ID') or config('AZURE_CLIENT_ID', default='')
        self.client_secret = os.environ.get('AZURE_CLIENT_SECRET') or config('AZURE_CLIENT_SECRET', default='')
        
        try:
            if all([self.subscription_id, self.tenant_id, self.client_id, self.client_secret]):
                self.credential = ClientSecretCredential(
                    tenant_id=self.tenant_id,
                    client_id=self.client_id,
                    client_secret=self.client_secret
                )
                self.compute_client = ComputeManagementClient(self.credential, self.subscription_id)
            else:
                logger.warning("Azure credentials missing. VM Control disabled.")
                self.compute_client = None
        except Exception as e:
            logger.error(f"Failed to initialize Azure Compute Client: {str(e)}")
            self.compute_client = None

    def get_status(self) -> str:
        """
        Returns the power state of the VM.
        """
        if not self.compute_client or not all([self.resource_group, self.vm_name]):
            return "unknown"
            
        try:
            vm = self.compute_client.virtual_machines.instance_view(self.resource_group, self.vm_name)
            for status in vm.statuses:
                if status.code.startswith('PowerState/'):
                    return status.display_status.lower() # e.g. "vm running", "vm deallocated"
            return "unknown"
        except Exception as e:
            logger.error(f"Failed to get VM status: {str(e)}")
            return "offline"

    def start_vm(self) -> bool:
        """Allocates and starts the VM."""
        try:
            logger.info(f"Triggering Start for VM: {self.vm_name}")
            async_poller = self.compute_client.virtual_machines.begin_start(self.resource_group, self.vm_name)
            # We don't wait for completion here to avoid timing out the API request
            return True
        except Exception as e:
            logger.error(f"Failed to start VM: {str(e)}")
            return False

    def stop_vm(self) -> bool:
        """Deallocates the VM to save costs."""
        try:
            logger.info(f"Triggering Deallocate for VM: {self.vm_name}")
            # deallocate is better than stop because it stops billing for the GPU
            async_poller = self.compute_client.virtual_machines.begin_deallocate(self.resource_group, self.vm_name)
            return True
        except Exception as e:
            logger.error(f"Failed to deallocate VM: {str(e)}")
            return False
