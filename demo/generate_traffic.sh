#!/usr/bin/env bash
# Generate traffic against the /api/tasks endpoint to trigger the demo bug.
# This script hits the ?filter=broken query parameter, causing 500 errors
# that will be detected by Azure SRE Agent.
#
# Usage: ./demo/generate_traffic.sh <base-url>
# Example: ./demo/generate_traffic.sh https://agentic-devops-demo.azurewebsites.net

set -euo pipefail

BASE_URL="${1:?Usage: ./demo/generate_traffic.sh <base-url>}"

echo "🔥 Generating traffic to trigger SRE incident..."
echo "   Target: ${BASE_URL}/api/tasks?filter=broken"
echo ""

# First, create a few normal tasks
echo "📝 Creating sample tasks..."
for i in {1..3}; do
  curl -s -X POST "${BASE_URL}/api/tasks" \
    -H "Content-Type: application/json" \
    -d "{\"title\": \"Sample Task $i\", \"description\": \"Created for demo\"}" \
    -o /dev/null -w "  Task $i: HTTP %{http_code}\n"
done

echo ""
echo "✅ Normal requests (should return 200):"
curl -s -o /dev/null -w "  GET /api/tasks → HTTP %{http_code}\n" "${BASE_URL}/api/tasks"
curl -s -o /dev/null -w "  GET /health    → HTTP %{http_code}\n" "${BASE_URL}/health"

echo ""
echo "💥 Sending broken requests (should return 500):"
for i in $(seq 1 100); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/api/tasks?filter=broken")
  if [ "$((i % 10))" -eq 0 ]; then
    echo "  Request $i/100 → HTTP $STATUS"
  fi
  sleep 0.2
done

echo ""
echo "🎯 Done! 100 broken requests sent."
echo "   Azure SRE Agent should detect the 5xx spike within 5-10 minutes."
echo "   Monitor Application Insights for the alert trigger."
