# Iris containers

SearXNG is the one long-running service Iris depends on (FeaturesChecklist 5.1). It answers
general, image, video and news searches on `http://127.0.0.1:8888/search?format=json` and is bound
to loopback only, so nothing outside this machine can reach it.

Start it (Docker Desktop must be running in Linux containers mode):

    docker compose -f docker/docker-compose.yml up -d

Stop it:

    docker compose -f docker/docker-compose.yml down

Settings live in `searxng/settings.yml`. `search.formats` must keep `json`, or Iris gets HTML back.
