#!/usr/bin/env bash
# Creates the Always Free e2-micro VM. Run once, after `gcloud auth login` and billing is linked.
# Free tier: 1 e2-micro in us-west1/us-central1/us-east1, 30 GB-month standard PD, 1 GB egress/month.
# NOT free: the external IPv4 (~$0.005/h = ~$3.65/mo). It is required: Discord's API/gateway is IPv4-only,
# and the only alternative (Cloud NAT) costs more. No static IP is reserved (ephemeral only).
set -euo pipefail
. "$(dirname "$0")/config.sh"
gcloud services enable compute.googleapis.com iap.googleapis.com --project "$PROJECT"
gcloud compute instances create "$VM" --project "$PROJECT" --zone "$ZONE" \
  --machine-type e2-micro --image-family debian-12 --image-project debian-cloud \
  --boot-disk-size 30GB --boot-disk-type pd-standard \
  --network-tier STANDARD \
  --no-shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
  --metadata enable-oslogin=TRUE \
  --labels app=discord-bot
# No inbound except SSH from Google's IAP range (35.235.240.0/20). Default-network allow rules are removed.
gcloud compute firewall-rules create allow-ssh-iap --project "$PROJECT" --network default \
  --direction INGRESS --action allow --rules tcp:22 --source-ranges 35.235.240.0/20 2>/dev/null || true
for r in default-allow-ssh default-allow-rdp default-allow-icmp; do
  gcloud compute firewall-rules delete "$r" --project "$PROJECT" --quiet 2>/dev/null || true
done
echo "VM created. Next: ./sync_code.sh && ./sync_data.sh && ssh in and run provision.sh (see README.md)."
