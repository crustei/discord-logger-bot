# Shared settings for the GCP deploy kit. Override any of these in the environment.
# Set PROJECT_ID, VM_NAME, and ZONE. PROJECT and VM are accepted as aliases.
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
if [ -z "${PROJECT_ID:-}" ] && [ -n "${PROJECT:-}" ]; then
  PROJECT_ID="$PROJECT"
fi
if [ -z "${PROJECT_ID:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/project" ]; then
  PROJECT_ID="$(cat "$(dirname "${BASH_SOURCE[0]}")/project")"
fi
PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to the GCP project id}"
ZONE="${ZONE:-us-west1-b}"
VM_NAME="${VM_NAME:-${VM:-discord-bot}}"
PROJECT="$PROJECT_ID"
VM="$VM_NAME"
APP_DIR="${APP_DIR:-/opt/discord-bot}"     # on the VM
APP_USER="${APP_USER:-botsvc}"             # on the VM
LOCAL_DIR="${LOCAL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SSH=(gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet)
SCP=(gcloud compute scp --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet)
