#!/usr/bin/env bash
# Offline plan by default. No group creation/deletion; demo-reader RBAC is a separate opt-in.
set -euo pipefail
set +x

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RG="" LOCATION="" SUBSCRIPTION="" PARAMETERS="" MODE="plan" CONFIRM=""
usage() {
  echo "Usage: $0 --resource-group rg-sre-demo-<unique> --location <region> --subscription <id> --parameters <file> [--what-if|--apply] [--confirm-resource-group <exact-name>]"
}
while (($#)); do
  case "$1" in
    --resource-group) RG="${2:?Missing resource group}"; shift 2 ;;
    --location) LOCATION="${2:?Missing location}"; shift 2 ;;
    --subscription) SUBSCRIPTION="${2:?Missing subscription}"; shift 2 ;;
    --parameters) PARAMETERS="${2:?Missing parameters file}"; shift 2 ;;
    --confirm-resource-group) CONFIRM="${2:?Missing confirmation}"; shift 2 ;;
    --what-if|--apply)
      [[ "$MODE" == "plan" ]] || { echo "Choose one live mode." >&2; exit 2; }
      MODE="${1#--}"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n "$RG" && -n "$LOCATION" && -n "$SUBSCRIPTION" && -n "$PARAMETERS" ]] || {
  usage >&2; exit 2;
}
[[ "$SUBSCRIPTION" =~ ^[a-fA-F0-9]{8}-([a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$ ]] || {
  echo "Invalid subscription ID." >&2; exit 2;
}
[[ "$LOCATION" =~ ^[a-z0-9]+$ ]] || { echo "Invalid region." >&2; exit 2; }
python3 "$ROOT/infrastructure/safety.py" resource-group "$RG"
python3 "$ROOT/infrastructure/safety.py" parameters "$PARAMETERS"
printf 'Plan: subscription=%s resource-group=%s region=%s mode=%s\n' "$SUBSCRIPTION" "$RG" "$LOCATION" "$MODE"
if [[ "$MODE" == "plan" ]]; then
  echo "OFFLINE: no Azure calls. Review parameters, costs, bootstrap, and the dedicated group tags."
  echo "A live what-if/apply requires the exact --confirm-resource-group value."
  exit 0
fi
[[ "$CONFIRM" == "$RG" ]] || { echo "Exact resource-group confirmation required." >&2; exit 2; }
export AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no
az group show --subscription "$SUBSCRIPTION" --name "$RG" --output json --only-show-errors |
  python3 "$ROOT/infrastructure/safety.py" group "$RG"
az resource list --subscription "$SUBSCRIPTION" --resource-group "$RG" --output json --only-show-errors |
  python3 "$ROOT/infrastructure/safety.py" inventory "$RG"

if [[ "$MODE" == "what-if" ]]; then
  az deployment group what-if --subscription "$SUBSCRIPTION" --resource-group "$RG" \
    --template-file "$ROOT/infrastructure/main.bicep" --parameters "@$PARAMETERS" \
    --parameters location="$LOCATION" --mode Incremental \
    --result-format ResourceIdOnly --only-show-errors
else
  az deployment group create --subscription "$SUBSCRIPTION" --resource-group "$RG" \
    --template-file "$ROOT/infrastructure/main.bicep" --parameters "@$PARAMETERS" \
    --parameters location="$LOCATION" --mode Incremental --output none --only-show-errors
  echo "Infrastructure deployed. Bootstrap, migrations, secret access, and live readiness remain operator gates."
fi
