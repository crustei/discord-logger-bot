# GCP Always Free deploy kit (e2-micro)

Moves the bot from a supervise loop to systemd on one e2-micro VM. Everything is kept: in-bot diag (10 min,
real-time schedule, pause detection), watchdogs, counters, hourly synthetic harness (systemd timer),
bot.log as the single master log (no logrotate), weekly trim + DB maintenance (systemd timer), status.json.

## Steps (owner does 1-3; the rest are scripts)
1. Sign in to the Google account at console.cloud.google.com, create a project, link a billing account
   (needed even for Always Free). On the box: `~/google-cloud-sdk/bin/gcloud auth login --no-launch-browser`
   and `gcloud config set project <PROJECT>`.
2. `export PROJECT=<project-id>`
3. `BILLING_ACCOUNT=$(gcloud billing accounts list --format='value(name)' | head -1) ./budget.sh`
4. `./create_vm.sh`
5. `./sync_code.sh && ./sync_data.sh`
6. `gcloud compute ssh discord-bot --zone us-west1-b --tunnel-through-iap --command 'sudo bash /opt/discord-bot/deploy/gcp/provision.sh'`
7. `./cutover.sh`  (stops the box bot, final online DB backup, starts the VM bot, verifies; ~1-3 min downtime)
8. Point the hourly heartbeat routine at `deploy/gcp/heartbeat_check_gcp.sh` (same output format).

## Costs
- Free: 1 e2-micro (us-west1/us-central1/us-east1), 30 GB-month standard PD, 1 GB/month egress (Premium tier).
- NOT free: external IPv4, ~$0.005/h (~$3.65/month). Required: Discord is IPv4-only; Cloud NAT costs more.
- Standard network tier is used (egress is Gemini/Discord requests, well under the free allowance).
