#!/usr/bin/env bash
# Bring up TigerGraph Community Edition locally, for anyone without a Savanna workspace.
#
#   ./graph/local_tigergraph.sh up       start the container and the database
#   ./graph/local_tigergraph.sh gsql     open a GSQL shell
#   ./graph/local_tigergraph.sh status   gadmin status
#   ./graph/local_tigergraph.sh down     stop the container (the volume survives)
#
# On Apple Silicon the image is amd64, so it runs under Rosetta inside a Lima VM:
#   brew install colima docker
#   colima start --vm-type vz --vz-rosetta --cpu 6 --memory 11 --disk 100
#
# Then put these in .env:
#   TG_HOST=http://localhost
#   TG_GRAPH=FraudInvestigation
#   TG_USERNAME=tigergraph
#   TG_PASSWORD=tigergraph
set -euo pipefail

IMAGE=${TG_IMAGE:-tigergraph/community:4.2.2}
NAME=${TG_CONTAINER:-tigergraph}
# gadmin is not on PATH for a non-interactive exec, and the image ships no profile that
# puts it there, so every call goes through the absolute path.
GADMIN=/home/tigergraph/tigergraph/app/cmd/gadmin

tg() { docker exec -u tigergraph "$NAME" "$GADMIN" "$@"; }

up() {
  if ! docker inspect "$NAME" >/dev/null 2>&1; then
    echo "-- creating $NAME from $IMAGE"
    docker run -d --init --platform linux/amd64 \
      -p 14022:22 -p 9000:9000 -p 14240:14240 \
      --name "$NAME" --ulimit nofile=1000000:1000000 \
      -v tg-data:/home/tigergraph \
      -t "$IMAGE"
  else
    docker start "$NAME" >/dev/null
  fi

  # the image's own entrypoint already runs `gadmin start all`; this is the wait, and a
  # second start for the case where the container was restarted rather than created.
  echo "-- starting services (several minutes on first boot)"
  tg start all || true

  echo "-- waiting for RESTPP"
  for _ in $(seq 1 180); do
    if curl -sf http://localhost:9000/echo >/dev/null 2>&1; then
      echo "   RESTPP is up"; break
    fi
    sleep 5
  done
  tg status | tail -25
}

case "${1:-up}" in
  up)     up ;;
  gsql)   docker exec -it -u tigergraph "$NAME" /home/tigergraph/tigergraph/app/cmd/gsql ;;
  status) tg status ;;
  down)   docker stop "$NAME" ;;
  *)      echo "usage: $0 {up|gsql|status|down}"; exit 2 ;;
esac
