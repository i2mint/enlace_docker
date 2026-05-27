"""enlace_docker — Docker / docker-compose backend strategies for enlace.

Installing this package adds four new ``mode`` values to ``app.toml`` via
the ``enlace.backend_strategies`` entry-point group:

- ``docker`` — build a Dockerfile, run, supervise.
- ``image`` — pull a pre-built image, run, supervise.
- ``compose`` — ``docker compose up -d`` an app's stack, route to a service.
- ``docker_attached`` — route to an already-running container (no lifecycle).

No enlace code change is required. See README.md for app.toml schemas.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from enlace_docker.lifecycle import (
    ComposeStackLifecycle,
    DockerContainerLifecycle,
)
from enlace_docker.strategies import (
    ComposeStrategy,
    DockerAttachedStrategy,
    DockerStrategy,
    ImageStrategy,
)

try:
    __version__ = _version("enlace_docker")
except PackageNotFoundError:  # editable install with no metadata
    __version__ = "0.0.0+local"

__all__ = [
    "ComposeStackLifecycle",
    "ComposeStrategy",
    "DockerAttachedStrategy",
    "DockerContainerLifecycle",
    "DockerStrategy",
    "ImageStrategy",
]
