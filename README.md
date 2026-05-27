# enlace_docker

Docker / docker-compose backend strategies for [enlace](https://github.com/i2mint/enlace).

Installing this package adds four new `mode` values to `app.toml`:

| Mode | What enlace does |
|------|------------------|
| `docker` | Build the app's `Dockerfile`, run the container, supervise, route HTTP to it |
| `image` | Pull a pre-built image, run it, supervise, route HTTP to it (no build step) |
| `compose` | `docker compose up -d` the app's stack, route HTTP to a declared service+port, `down` on shutdown |
| `docker_attached` | Route HTTP to an already-running container by name (no lifecycle management) |

No code change in enlace is needed — `enlace_docker` registers via the
`enlace.backend_strategies` entry-point group. Installing the package is
enough.

## Status

Scope: **dev orchestration only.** Production runs `docker compose up` out of
band; enlace routes to it via the built-in `mode=external`. This package
covers `enlace serve` / `enlace dev`. See
[enlace#3](https://github.com/i2mint/enlace/issues/3) for the design.

## Install

```bash
pip install enlace_docker
```

Requires the `docker` CLI on `PATH`. Compose support requires `docker compose`
(the v2 plugin, not the legacy `docker-compose`).

## Usage

### Dockerfile-per-app

```toml
# apps/myapp/app.toml
[app]
mode = "docker"
port = 8080            # in-container port to expose
dockerfile = "Dockerfile"   # default
context = "."               # default
build_args = { ENV = "dev" }
env = { LOG_LEVEL = "info" }
```

### Pre-built image

```toml
[app]
mode = "image"
image = "ghcr.io/myorg/myapp:1.2.3"
port = 8080
env = { LOG_LEVEL = "info" }
```

### docker-compose

```toml
[app]
mode = "compose"
compose_file = "docker-compose.yml"   # default
service = "web"                       # which service receives HTTP
port = 8080                           # service's internal port
```

### Already-running container

```toml
[app]
mode = "docker_attached"
container = "my-running-container"
port = 8080
```

## How it works

Each mode registers a `BackendStrategy` against enlace's open/closed
extension point. The strategy provides:

- a TOML field overlay so app.toml carries docker-specific config;
- a `make_asgi` that proxies HTTP to the container's published host port;
- a `make_lifecycle` (for the supervisable modes) that shells out to
  `docker` / `docker compose` for start, stop, health, and log streaming.

## License

Apache-2.0.
