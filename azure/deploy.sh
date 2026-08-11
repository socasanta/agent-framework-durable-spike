#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./azure/deploy.sh \
    --subscription-id <SUBSCRIPTION_ID> \
    --name-prefix <LOWERCASE_PREFIX> \
    [--location eastus2] \
    [--resource-group <RESOURCE_GROUP>] \
    [--operator-assignee <USER_OR_OBJECT_ID>] \
    [--task-hub agent-framework-spike] \
    [--workflow-version 1.0.0]

The name prefix must start with a lowercase letter, contain only lowercase
letters and digits, and be 3-15 characters long.
EOF
}

subscription_id=""
name_prefix=""
location="eastus2"
resource_group=""
operator_assignee=""
task_hub="agent-framework-spike"
workflow_version="1.0.0"

while (( $# > 0 )); do
    case "$1" in
        --subscription-id)
            subscription_id="${2:-}"
            shift 2
            ;;
        --name-prefix)
            name_prefix="${2:-}"
            shift 2
            ;;
        --location)
            location="${2:-}"
            shift 2
            ;;
        --resource-group)
            resource_group="${2:-}"
            shift 2
            ;;
        --operator-assignee)
            operator_assignee="${2:-}"
            shift 2
            ;;
        --task-hub)
            task_hub="${2:-}"
            shift 2
            ;;
        --workflow-version)
            workflow_version="${2:-}"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$subscription_id" || -z "$name_prefix" ]]; then
    echo "--subscription-id and --name-prefix are required." >&2
    usage >&2
    exit 2
fi

if [[ ! "$name_prefix" =~ ^[a-z][a-z0-9]{2,14}$ ]]; then
    echo "Invalid --name-prefix: $name_prefix" >&2
    usage >&2
    exit 2
fi

if [[ ! "$workflow_version" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]]; then
    echo "Invalid --workflow-version: '$workflow_version'. Use one to three integer components, for example 1.0.0." >&2
    exit 2
fi

if [[ -z "$resource_group" ]]; then
    resource_group="rg-${name_prefix}-af-durable-spike"
fi

scheduler="${name_prefix}-dts"
acr="${name_prefix}acr"
container_environment="${name_prefix}-ca-env"
identity="${name_prefix}-worker-id"
container_app="${name_prefix}-worker"
image_tag="${acr}.azurecr.io/agent-framework-durable-spike:0.1.4"

az account set --subscription "$subscription_id"
az extension add --name durabletask --allow-preview true --upgrade
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.DurableTask --wait
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.ContainerRegistry --wait

mapfile -t supported_locations < <(
    az provider show \
        --namespace Microsoft.DurableTask \
        --query "resourceTypes[?resourceType=='schedulers'].locations | [0][]" \
        --output tsv
)

requested_location="${location//[[:space:]]/}"
requested_location="${requested_location,,}"
location_supported=false
for supported_location in "${supported_locations[@]}"; do
    normalized_location="${supported_location//[[:space:]]/}"
    normalized_location="${normalized_location,,}"
    if [[ "$normalized_location" == "$requested_location" ]]; then
        location_supported=true
        break
    fi
done

if [[ "$location_supported" != true ]]; then
    supported_csv=$(IFS=,; echo "${supported_locations[*]}")
    echo "Durable Task Scheduler is not reported in '$location'. Supported locations: $supported_csv" >&2
    exit 1
fi

az group create \
    --name "$resource_group" \
    --location "$location" \
    --output none

az durabletask scheduler create \
    --resource-group "$resource_group" \
    --name "$scheduler" \
    --location "$location" \
    --ip-allowlist '[0.0.0.0/0]' \
    --sku-name Consumption \
    --output none

az durabletask taskhub create \
    --resource-group "$resource_group" \
    --scheduler-name "$scheduler" \
    --name "$task_hub" \
    --output none

az acr create \
    --resource-group "$resource_group" \
    --name "$acr" \
    --location "$location" \
    --sku Basic \
    --output none

az containerapp env create \
    --resource-group "$resource_group" \
    --name "$container_environment" \
    --location "$location" \
    --output none

az identity create \
    --resource-group "$resource_group" \
    --name "$identity" \
    --location "$location" \
    --output none

identity_id=$(az identity show --resource-group "$resource_group" --name "$identity" --query id --output tsv)
identity_principal_id=$(az identity show --resource-group "$resource_group" --name "$identity" --query principalId --output tsv)
identity_client_id=$(az identity show --resource-group "$resource_group" --name "$identity" --query clientId --output tsv)
acr_id=$(az acr show --resource-group "$resource_group" --name "$acr" --query id --output tsv)

az role assignment create \
    --assignee-object-id "$identity_principal_id" \
    --assignee-principal-type ServicePrincipal \
    --role AcrPull \
    --scope "$acr_id" \
    --output none

az acr build \
    --registry "$acr" \
    --image 'agent-framework-durable-spike:0.1.4' \
    --file Dockerfile \
    .

endpoint=$(az durabletask scheduler show \
    --resource-group "$resource_group" \
    --name "$scheduler" \
    --query properties.endpoint \
    --output tsv)

az containerapp create \
    --resource-group "$resource_group" \
    --name "$container_app" \
    --environment "$container_environment" \
    --image "$image_tag" \
    --user-assigned "$identity_id" \
    --registry-server "${acr}.azurecr.io" \
    --registry-identity "$identity_id" \
    --min-replicas 1 \
    --max-replicas 1 \
    --cpu 0.5 \
    --memory 1.0Gi \
    --env-vars \
        "ENDPOINT=$endpoint" \
        "TASKHUB=$task_hub" \
        "WORKFLOW_VERSION=$workflow_version" \
        "AZURE_MANAGED_IDENTITY_CLIENT_ID=$identity_client_id" \
    --output none

container_app_id=$(az containerapp show \
    --resource-group "$resource_group" \
    --name "$container_app" \
    --query id \
    --output tsv)

az durabletask scheduler attach \
    --resource-group "$resource_group" \
    --name "$scheduler" \
    --task-hub-name "$task_hub" \
    --role-type worker \
    --target "$container_app_id" \
    --identity "$identity_id" \
    --output none

if [[ -z "$operator_assignee" ]]; then
    operator_assignee=$(az account show --query user.name --output tsv)
fi

task_hub_scope="/subscriptions/${subscription_id}/resourceGroups/${resource_group}/providers/Microsoft.DurableTask/schedulers/${scheduler}/taskHubs/${task_hub}"
az role assignment create \
    --assignee "$operator_assignee" \
    --role 'Durable Task Data Contributor' \
    --scope "$task_hub_scope" \
    --output none

python - \
    "$resource_group" \
    "$scheduler" \
    "$task_hub" \
    "$endpoint" \
    "$container_app" \
    "$identity" \
    "$acr" \
    "$workflow_version" <<'PY'
import json
import sys

keys = (
    "RESOURCE_GROUP",
    "SCHEDULER",
    "TASKHUB",
    "ENDPOINT",
    "CONTAINER_APP",
    "IDENTITY",
    "ACR",
    "WORKFLOW_VERSION",
)
print(json.dumps(dict(zip(keys, sys.argv[1:])), indent=2))
PY
