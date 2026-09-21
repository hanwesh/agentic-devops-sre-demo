#!/usr/bin/env bash
# Operator only. An exact confirmation, ownership checks, and inventory precede deletion.
set -euo pipefail
set +x

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RG="" SUBSCRIPTION="" MODE="plan" CONFIRM="" CONFIRM_SUBSCRIPTION=""
usage() {
  echo "Usage: $0 --resource-group rg-sre-demo-<unique> --subscription <id> [--list|--delete] --confirm-resource-group <exact-name> [--confirm-subscription <exact-id>]"
}
while (($#)); do
  case "$1" in
    --resource-group) RG="${2:?Missing resource group}"; shift 2 ;;
    --subscription) SUBSCRIPTION="${2:?Missing subscription}"; shift 2 ;;
    --confirm-resource-group) CONFIRM="${2:?Missing confirmation}"; shift 2 ;;
    --confirm-subscription) CONFIRM_SUBSCRIPTION="${2:?Missing confirmation}"; shift 2 ;;
    --list|--delete)
      [[ "$MODE" == "plan" ]] || { echo "Choose one live mode." >&2; exit 2; }
      MODE="${1#--}"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n "$RG" && -n "$SUBSCRIPTION" ]] || { usage >&2; exit 2; }
[[ "$SUBSCRIPTION" =~ ^[a-fA-F0-9]{8}-([a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$ ]] || {
  echo "Invalid subscription ID." >&2; exit 2;
}
python3 "$ROOT/infrastructure/safety.py" resource-group "$RG"
printf 'Teardown plan: subscription=%s resource-group=%s mode=%s\n' "$SUBSCRIPTION" "$RG" "$MODE"
if [[ "$MODE" == "plan" ]]; then
  echo "OFFLINE: nothing queried or deleted. Use --list and exact group confirmation to inspect."
  exit 0
fi
[[ "$CONFIRM" == "$RG" ]] || { echo "Exact resource-group confirmation required." >&2; exit 2; }
if [[ "$MODE" == "delete" && "$CONFIRM_SUBSCRIPTION" != "$SUBSCRIPTION" ]]; then
  echo "Deletion also requires exact --confirm-subscription." >&2; exit 2
fi
export AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no
az group show --subscription "$SUBSCRIPTION" --name "$RG" --output json --only-show-errors |
  python3 "$ROOT/infrastructure/safety.py" group "$RG" --disposable
INVENTORY="$(az resource list --subscription "$SUBSCRIPTION" --resource-group "$RG" --output json --only-show-errors)"
printf '%s' "$INVENTORY" | python3 -c '
import json, sys
for resource in json.load(sys.stdin):
    print(resource["id"], resource["type"])
'
printf '%s' "$INVENTORY" | python3 "$ROOT/infrastructure/safety.py" inventory "$RG"
if [[ "$MODE" == "delete" ]]; then
  echo "Deleting ONLY the confirmed disposable resource group shown above."
  az group delete --subscription "$SUBSCRIPTION" --name "$RG" --yes --only-show-errors
else
  echo "READ-ONLY: inventory shown; nothing deleted."
fi
