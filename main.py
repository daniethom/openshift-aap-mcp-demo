import os
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Extra
from typing import Dict, Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

# --- Configuration ---
# Load configuration for AAP from environment variables
AAP_CONTROLLER_URL = os.getenv("AAP_CONTROLLER_URL")
AAP_API_TOKEN = os.getenv("AAP_API_TOKEN")

# Load Kubernetes in-cluster configuration
try:
    config.load_incluster_config()
except config.ConfigException:
    # Fallback to kube-config for local development
    try:
        config.load_kube_config()
    except config.ConfigException:
        raise Exception("Could not configure kubernetes client")

# --- Pydantic Models ---
class AAPJob(BaseModel):
    job_template_name: str
    extra_vars: Dict[str, Any] = {}

class VirtualMachine(BaseModel):
    vm_name: str
    namespace: str

# --- FastAPI Application Initialization ---
app = FastAPI(
    title="MCP Server for OpenShift Administration",
    description="An API server to bridge AI agents with OpenShift and Ansible Automation Platform.",
    version="2.0.0",
)

# --- Helper Functions ---
def get_job_template_id(template_name: str, headers: Dict[str, str]) -> int:
    if not AAP_CONTROLLER_URL:
        raise HTTPException(status_code=500, detail="AAP_CONTROLLER_URL is not configured.")
    
    url = f"{AAP_CONTROLLER_URL}/api/v2/job_templates/"
    query_params = {'name': template_name}
    
    try:
        response = requests.get(url, headers=headers, params=query_params, verify=False)
        response.raise_for_status()
        data = response.json()

        if data['count'] == 1:
            return data['results'][0]['id']
        elif data['count'] > 1:
            raise HTTPException(status_code=409, detail=f"Conflict: Multiple job templates found with name '{template_name}'.")
        else:
            raise HTTPException(status_code=404, detail=f"Not Found: No job template found with name '{template_name}'.")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Service Unavailable: Could not connect to AAP. Error: {e}")

# --- API Endpoints ---
@app.get("/", summary="Root endpoint to check server status.")
async def root():
    """Provides a simple status message to confirm the server is running."""
    return {"message": "MCP Server for OpenShift Administration is running"}

@app.post("/create_vm", summary="Create a Virtual Machine in OpenShift Virtualization.")
async def create_vm(vm: VirtualMachine):
    """
    Creates a VirtualMachine custom resource using the Kubernetes API.
    The VM is configured with a RHEL ContainerDisk and is not started automatically.
    """
    custom_objects_api = client.CustomObjectsApi()

    # Define the VirtualMachine body
    vm_manifest = {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {"name": vm.vm_name},
        "spec": {
            "running": False,
            "template": {
                "spec": {
                    "domain": {
                        "devices": {
                            "disks": [{
                                "name": "containerdisk",
                                "disk": {"bus": "virtio"}
                            }]
                        },
                        "resources": {"requests": {"memory": "2Gi"}}
                    },
                    "volumes": [{
                        "name": "containerdisk",
                        "containerDisk": {
                            "image": "quay.io/containerdisks/rhel:9"
                        }
                    }]
                }
            }
        }
    }

    try:
        custom_objects_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=vm.namespace,
            plural="virtualmachines",
            body=vm_manifest,
        )
        return {
            "status": "success",
            "message": f"VirtualMachine '{vm.vm_name}' created successfully in namespace '{vm.namespace}'.",
        }
    except ApiException as e:
        raise HTTPException(
            status_code=e.status,
            detail=f"Kubernetes API Error: Failed to create VM. Reason: {e.reason}",
        )

@app.post("/launch_aap_job", summary="Launch a Job Template in Ansible Automation Platform.")
async def launch_aap_job(job: AAPJob):
    """
    Launches a pre-defined Job Template in AAP by name.
    """
    if not AAP_API_TOKEN:
        raise HTTPException(status_code=500, detail="AAP_API_TOKEN is not configured.")

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {AAP_API_TOKEN}'
    }

    template_id = get_job_template_id(job.job_template_name, headers)
    launch_url = f"{AAP_CONTROLLER_URL}/api/v2/job_templates/{template_id}/launch/"
    
    try:
        launch_response = requests.post(launch_url, headers=headers, json={"extra_vars": job.extra_vars}, verify=False)
        launch_response.raise_for_status()
        
        job_data = launch_response.json()
        return {
            "status": "success",
            "message": f"Successfully launched job template '{job.job_template_name}'.",
            "job_id": job_data.get('job'),
            "aap_url": f"{AAP_CONTROLLER_URL}/#/jobs/playbook/{job_data.get('job')}"
        }
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Service Unavailable: Failed to launch job in AAP. Error: {e}")
