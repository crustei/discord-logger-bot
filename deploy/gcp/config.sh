# Shared settings for the GCP deploy kit. Override any of these in the environment.
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
[ -z "${PROJECT:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/project" ] && PROJECT="$(cat "$(dirname "${BASH_SOURCE[0]}")/project")"
PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
ZONE="${ZONE:-us-west1-b}"
VM="${VM:-discord-bot}"
APP_DIR="${APP_DIR:-/opt/discord-bot}"     # on the VM
APP_USER="${APP_USER:-botsvc}"             # on the VM
LOCAL_DIR="${LOCAL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SSH=(gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet)
SCP=(gcloud compute scp --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet)
