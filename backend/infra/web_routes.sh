#!/usr/bin/env bash
# Put the webapp (axelrod-web, port 3000) behind the existing API Gateway next to the API (port 8000).
# Runs on the laptop, from the repo root, with workshop credentials. Idempotent: safe to rerun.
#
#   bash backend/infra/web_routes.sh            # do it
#   bash backend/infra/web_routes.sh --rollback # back to the single ANY /{proxy+} route to port 8000
#
# Before: one route, ANY /{proxy+} -> http://<dns>:8000/{proxy} (integration $OLD_INTEGRATION).
# After:
#   ANY /api/{proxy+}, GET /healthz, ANY /test-pages/{proxy+}, GET /docs, GET /openapi.json
#       -> http://<dns>:8000 with overwrite:path=$request.path       (the API, unchanged behavior)
#   $default -> http://<dns>:3000 with overwrite:path=$request.path  (pages and /_next/ assets)
#   security group axelrod-api-sg: TCP 3000 from anywhere, like 8000 (API Gateway reaches the
#   instance on its public DNS, no VPC link)
#   ANY /{proxy+} is deleted last, once the specific API routes answer and port 3000 answers,
#   because while it exists it catches every path but / and $default never sees them.
set -euo pipefail
export MSYS_NO_PATHCONV=1 AWS_PAGER=""
R=us-east-1
API_ID=ppgcikvoi0
SG=sg-0616205545740f0aa
IID=i-0dec5cc5824a11aec
OLD_INTEGRATION=2gdw490
URL=https://$API_ID.execute-api.$R.amazonaws.com
API_KEYS=('ANY /api/{proxy+}' 'GET /healthz' 'ANY /test-pages/{proxy+}' 'GET /docs' 'GET /openapi.json')

[ "$(aws sts get-caller-identity --query Account --output text | tr -d '\r')" = 506556958358 ] || { echo "wrong AWS account" >&2; exit 1; }
DNS="$(aws ec2 describe-instances --region $R --instance-ids $IID --query 'Reservations[0].Instances[0].PublicDnsName' --output text | tr -d '\r')"
echo "instance dns: $DNS"

route_id() {  # route key -> id, empty if missing
  aws apigatewayv2 get-routes --api-id $API_ID --region $R --output text \
    --query "Items[?RouteKey=='$1'].RouteId | [0]" | tr -d '\r' | sed 's/^None$//'
}
integration_for() {  # uri -> id of an HTTP_PROXY integration with overwrite:path, created if missing
  local uri="$1" id
  id="$(aws apigatewayv2 get-integrations --api-id $API_ID --region $R --output text \
    --query "Items[?IntegrationUri=='$uri' && RequestParameters].IntegrationId | [0]" | tr -d '\r' | sed 's/^None$//')"
  if [ -z "$id" ]; then
    id="$(aws apigatewayv2 create-integration --api-id $API_ID --region $R --integration-type HTTP_PROXY \
      --integration-method ANY --integration-uri "$uri" --payload-format-version 1.0 --timeout-in-millis 30000 \
      --request-parameters 'overwrite:path=$request.path' --query IntegrationId --output text | tr -d '\r')"
    echo "created integration $id -> $uri" >&2
  fi
  printf '%s' "$id"
}
ensure_route() {  # route key, integration id
  local id; id="$(route_id "$1")"
  if [ -z "$id" ]; then
    aws apigatewayv2 create-route --api-id $API_ID --region $R --route-key "$1" --target "integrations/$2" --query RouteId --output text >/dev/null
    echo "created route $1 -> $2"
  else
    aws apigatewayv2 update-route --api-id $API_ID --region $R --route-id "$id" --target "integrations/$2" --query RouteId --output text >/dev/null
    echo "route $1 -> $2 (exists, target set)"
  fi
}
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$1" || true; }   # never trips set -e
wait_200() { local c=""; for _ in $(seq 1 15); do c="$(code "$1")"; [ "$c" = 200 ] && break; sleep 2; done; printf '%s' "$c"; }

if [ "${1:-}" = --rollback ]; then
  [ -n "$(route_id 'ANY /{proxy+}')" ] || aws apigatewayv2 create-route --api-id $API_ID --region $R \
    --route-key 'ANY /{proxy+}' --target "integrations/$OLD_INTEGRATION" --query RouteId --output text
  d="$(route_id '$default')"; [ -z "$d" ] || aws apigatewayv2 delete-route --api-id $API_ID --region $R --route-id "$d"
  echo "rolled back: ANY /{proxy+} -> port 8000, no \$default. healthz: $(code "$URL/healthz")"
  exit 0
fi

# 1. The API's own routes, more specific than ANY /{proxy+}, so the API keeps answering throughout.
API8="$(integration_for "http://$DNS:8000")"
for k in "${API_KEYS[@]}"; do ensure_route "$k" "$API8"; done
sleep 3
echo "check api: healthz $(code "$URL/healthz"), runs $(code "$URL/api/v1/runs"), test page $(code "$URL/test-pages/injection.html")"
[ "$(code "$URL/healthz")" = 200 ] && [ "$(code "$URL/api/v1/runs")" = 200 ] || { echo "API routes do not answer 200, stopping before any web change" >&2; exit 1; }

# 2. Port 3000, like 8000.
if aws ec2 describe-security-groups --region $R --group-ids $SG --output text \
     --query "SecurityGroups[0].IpPermissions[?FromPort==\`3000\`].FromPort" | grep -q 3000; then
  echo "port 3000 already open on $SG"
else
  aws ec2 authorize-security-group-ingress --region $R --group-id $SG \
    --ip-permissions 'IpProtocol=tcp,FromPort=3000,ToPort=3000,IpRanges=[{CidrIp=0.0.0.0/0,Description=axelrod-web via API Gateway (demo)}]' \
    --query 'Return' --output text
  echo "opened port 3000 on $SG"
fi
web_direct="$(wait_200 "http://$DNS:3000/")"   # a new rule takes a few seconds to apply
echo "check webapp on the instance: $web_direct"
[ "$web_direct" = 200 ] || { echo "the webapp does not answer on :3000 yet (deploy_web.sh first), stopping before the route change" >&2; exit 1; }

# 3. $default to the webapp, then drop the catch-all so $default sees every non-API path.
WEB="$(integration_for "http://$DNS:3000")"
ensure_route '$default' "$WEB"
old="$(route_id 'ANY /{proxy+}')"
if [ -n "$old" ]; then
  aws apigatewayv2 delete-route --api-id $API_ID --region $R --route-id "$old"
  echo "deleted route ANY /{proxy+} (rollback: bash backend/infra/web_routes.sh --rollback)"
fi
sleep 3
echo "check: / $(code "$URL/"), /runs/run_20261108_rzpoj7 $(code "$URL/runs/run_20261108_rzpoj7"), healthz $(code "$URL/healthz"), /api/v1/runs $(code "$URL/api/v1/runs")"
aws apigatewayv2 get-routes --api-id $API_ID --region $R --output text --query 'Items[].[RouteKey,Target]'
