set -euo pipefail

image_owner=$1
image_tag=$2
stack_base64=$3
.
if [[ -f .env ]]; then
    while IFS='=' read -r key value || [[ -n $key ]]; do
        case "$key" in
            POSTGRES_DB|POSTGRES_USER|OWNCAST_PUBLIC_URL|CHANNEL_TITLE|CHANNEL_DESCRIPTION|SWARM_POSTGRES_VOLUME|SWARM_OWNCAST_VOLUME)
                value=${value%$'\r'}
                if [[ $value == \'*\' || $value == \"*\" ]]; then
                    value=${value:1:-1}
                fi
                if [[ ! -v $key ]]; then
                    export "$key=$value"
                fi
                ;;
        esac
    done < .env
fi

stack_services=$(docker service ls --filter label=com.docker.stack.namespace=streamroom --format '{{.Name}}')
keep_current_volume() {
    local variable=$1 service=$2 target=$3 volume
    if [[ -n ${!variable:-} ]] || ! grep -Fxq "$service" <<< "$stack_services"; then
        return
    fi
    volume=$(docker service inspect --format "{{range .Spec.TaskTemplate.ContainerSpec.Mounts}}{{if and (eq .Type \"volume\") (eq .Target \"$target\")}}{{.Source}}{{end}}{{end}}" "$service")
    if [[ -n $volume ]]; then
        export "$variable=$volume"
    fi
}
keep_current_volume SWARM_POSTGRES_VOLUME streamroom_database /var/lib/postgresql/data
keep_current_volume SWARM_OWNCAST_VOLUME streamroom_owncast /app/data

export BACKEND_IMAGE="ghcr.io/$image_owner/streamroom-backend:$image_tag"
export FRONTEND_IMAGE="ghcr.io/$image_owner/streamroom-frontend:$image_tag"
export GATEWAY_IMAGE="ghcr.io/$image_owner/streamroom-gateway:$image_tag"
printf '%s' "$stack_base64" | base64 --decode |
    docker stack deploy --detach=false --with-registry-auth --compose-file - streamroom
curl --fail --silent --show-error --retry 8 --retry-delay 2 --retry-connrefused \
    --connect-timeout 2 --max-time 5 http://localhost:8080/api/health
printf '\n'
docker stack services streamroom
