import uvicorn
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from kubernetes import client, config
from datetime import datetime
from typing import Optional

# Initialize FastAPI application
app = FastAPI(
    title="K8s Management MCP Bridge",
    description="Bridge for Elastic AI Agent to manage Kubernetes deployments via local kubeconfig"
)

# SECURITY: Shared secret token that must match the Elastic Workflow 'mcp_token'
# In production, store this in an environment variable or secret manager
API_TOKEN = "YOUR TOKEN"

# KUBERNETES SETUP:
# Grants the script the same permissions as your local 'kubectl'
try:
    # Use load_kube_config() for local development (Minikube/Desktop)
    # Use load_incluster_config() if running inside the K8s cluster
    config.load_kube_config()
    apps_v1 = client.AppsV1Api()
    print("✅ Successfully connected to Kubernetes context.")
except Exception as e:
    print(f"❌ Critical Error: Could not load K8s config: {e}")

# DATA MODEL: Defines the schema for incoming requests from Elastic AI Agent/Workflows
class ManageRequest(BaseModel):
    action: str             # Options: "scale", "restart", "status", "update_resources"
    deployment: str         # The name of the deployment (e.g., "accounting")
    namespace: str = "default"
    replicas: Optional[int] = 1       # Used for 'scale'
    memory_limit: Optional[str] = None # Used for 'update_resources' (e.g., "256Mi", "1Gi")

@app.post("/manage")
async def manage_deployment(request: ManageRequest, authorization: str = Header(None)):
    """
    Primary endpoint for K8s operations. 
    Handles authentication and routes requests to the appropriate K8s API call.
    """
    
    # 1. AUTHENTICATION: Check if the Bearer token matches our static secret
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid MCP Token")

    try:
        # --- ACTION: UPDATE_RESOURCES (Vertical Scaling) ---
        # Adjusts the resource limits of the first container in the deployment.
        # This triggers a Rolling Update in Kubernetes automatically.
        if request.action == "update_resources":
            if not request.memory_limit:
                raise HTTPException(status_code=400, detail="memory_limit is required for this action")

            # Fetch the current deployment to identify the primary container name
            deploy = apps_v1.read_namespaced_deployment(name=request.deployment, namespace=request.namespace)
            container_name = deploy.spec.template.spec.containers[0].name

            # Strategic Merge Patch for resource limits
            resource_patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": container_name,
                                    "resources": {
                                        "limits": {
                                            "memory": request.memory_limit
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            }
            
            apps_v1.patch_namespaced_deployment(
                name=request.deployment, 
                namespace=request.namespace, 
                body=resource_patch
            )
            return {
                "status": "success", 
                "message": f"Vertical scaling applied: '{request.deployment}' memory limit updated to {request.memory_limit}."
            }

        # --- ACTION: SCALE (Horizontal Scaling) ---
        # Adjusts the number of desired pod replicas.
        elif request.action == "scale":
            scale_patch = {"spec": {"replicas": request.replicas}}
            apps_v1.patch_namespaced_deployment_scale(
                name=request.deployment, 
                namespace=request.namespace, 
                body=scale_patch
            )
            return {
                "status": "success", 
                "message": f"Horizontal scaling applied: '{request.deployment}' set to {request.replicas} replicas."
            }

        # --- ACTION: RESTART (Force Rollout) ---
        # Triggers a restart by updating a metadata annotation, forcing K8s to cycle pods.
        elif request.action == "restart":
            timestamp = datetime.utcnow().isoformat() + "Z"
            restart_patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": timestamp
                            }
                        }
                    }
                }
            }
            apps_v1.patch_namespaced_deployment(
                name=request.deployment, 
                namespace=request.namespace, 
                body=restart_patch
            )
            return {
                "status": "success", 
                "message": f"Rollout restart triggered for '{request.deployment}' at {timestamp}."
            }

        # --- ACTION: STATUS (Diagnostic) ---
        # Returns current availability metrics.
        elif request.action == "status":
            deploy_info = apps_v1.read_namespaced_deployment(
                name=request.deployment, 
                namespace=request.namespace
            )
            s = deploy_info.status
            return {
                "status": "success",
                "message": f"Status: {s.available_replicas or 0}/{s.replicas} pods ready."
            }

        else:
            raise HTTPException(status_code=400, detail=f"Action '{request.action}' not supported.")

    except client.exceptions.ApiException as k8s_err:
        # Standardize K8s error messages for the Elastic AI Agent
        raise HTTPException(status_code=500, detail=f"Kubernetes API Error: {k8s_err.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected Error: {str(e)}")

if __name__ == "__main__":
    # Runs the server locally. Ensure your ngrok or bridge points to this port.
    uvicorn.run(app, host="0.0.0.0", port=8000)