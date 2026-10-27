#!/usr/bin/env bash
# $1/month budget alert (email to billing admins at 50/90/100%). Needs the billing account id:
#   gcloud billing accounts list   ->  BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX ./budget.sh
# Note: the external IPv4 alone is ~$3.65/mo, so a $1 budget WILL alert every month; use AMOUNT=5 to alert only on surprises.
set -euo pipefail
. "$(dirname "$0")/config.sh"
: "${BILLING_ACCOUNT:?set BILLING_ACCOUNT}"
AMOUNT="${AMOUNT:-1}"
gcloud services enable billingbudgets.googleapis.com --project "$PROJECT"
gcloud billing budgets create --billing-account "$BILLING_ACCOUNT" --display-name "discord-bot \$$AMOUNT" \
  --budget-amount "${AMOUNT}USD" --filter-projects "projects/$PROJECT" \
  --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0
