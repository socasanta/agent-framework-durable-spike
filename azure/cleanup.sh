#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./azure/cleanup.sh \
    --subscription-id <SUBSCRIPTION_ID> \
    --resource-group <RESOURCE_GROUP>
EOF
}

subscription_id=""
resource_group=""

while (( $# > 0 )); do
    case "$1" in
        --subscription-id)
            subscription_id="${2:-}"
            shift 2
            ;;
        --resource-group)
            resource_group="${2:-}"
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

if [[ -z "$subscription_id" || -z "$resource_group" ]]; then
    echo "--subscription-id and --resource-group are required." >&2
    usage >&2
    exit 2
fi

if [[ ! "$resource_group" =~ ^rg-[a-z][a-z0-9]{2,14}-af-durable-spike$ ]]; then
    echo "Refusing to delete unexpected resource-group name: $resource_group" >&2
    exit 2
fi

az account set --subscription "$subscription_id"
resolved_id=$(az group show --name "$resource_group" --query id --output tsv)

if [[ -z "$resolved_id" ]]; then
    echo "Resource group '$resource_group' was not found." >&2
    exit 1
fi

echo "Deleting the spike resource group: $resolved_id"
az group delete --name "$resource_group" --yes --no-wait
