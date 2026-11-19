---
kind: qa_sheet
language: en
---

# Docker interview notes

## Question: What is the difference between an image and a container?

An image is an immutable, layered template that holds the filesystem and the default command of an application. A container is a running instance of an image, with a thin writable layer on top and its own process, network and mount namespaces.

## Question: Why does the order of instructions in a Dockerfile matter?

Docker caches each layer and invalidates every layer after the first one that changed. Copying the dependency manifest and installing dependencies before copying the source code keeps the expensive install layer cached while only the code changes.

```dockerfile
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen
COPY src ./src
```

## Question: What is a multi-stage build used for?

A multi-stage build compiles or installs tools in an early stage and copies only the final artifacts into a small runtime stage. The production image then contains no compiler or build cache, which reduces its size and its attack surface.

## Question: How do you persist data written by a container?

Data written to the container layer disappears with the container. A named volume, managed by Docker, or a bind mount of a host directory keeps the data outside the container lifecycle, so a database survives being recreated.

## Question: What does a health check do in Docker Compose?

A health check runs a command at regular intervals and marks the container healthy or unhealthy. Compose can wait for a dependency to be healthy before starting the services that need it, which avoids races at startup such as an API booting before its database accepts connections.

## Question: How do containers on the same Compose network find each other?

Compose creates a default network and registers every service name in its internal DNS. A service reaches another one simply by using its service name as the hostname, with the container port rather than the port published on the host.
