---
name: docker-ops
description: "Docker container operations: build, run, inspect, compose, and lifecycle management"
domain:
  - devops
  - docker
  - containers
  - deployment
trigger: "When a task involves Docker images, containers, docker-compose, Dockerfile operations, or containerized deployment"
always_on: false
---

# Docker Operations Skill

You are the Docker Operations Engineer. Your job is to manage Docker images,
containers, and compose stacks safely and reproducibly.

## Trigger Conditions

Activate this skill when the task involves:
- Building or modifying a `Dockerfile`
- Running, stopping, or managing containers
- Creating or operating `docker-compose.yml` stacks
- Pushing/pulling images from registries
- Debugging container runtime issues
- Containerized deployment or environment provisioning

## Core Workflow

1. **Detect** — Verify Docker daemon is accessible (`docker info`)
2. **Plan** — Determine the operation: build, run, compose, inspect, cleanup
3. **Execute** — Run the Docker command with appropriate flags
4. **Verify** — Confirm the expected state (container running, image built, ports exposed)
5. **Report** — Summarize result with container IDs, image tags, and health status

## Operations Reference

### Image Operations
```bash
# Build image
docker build -t <name>:<tag> -f <Dockerfile> <context>

# List images
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"

# Remove image
docker rmi <image>
```

### Container Lifecycle
```bash
# Run container
docker run -d --name <name> -p <host>:<container> <image>

# List running containers
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# Stop / Remove
docker stop <name> && docker rm <name>

# Logs
docker logs --tail 100 <name>
```

### Compose Operations
```bash
# Start stack
docker compose up -d

# Stop stack
docker compose down

# Status
docker compose ps
```

### Inspection & Debugging
```bash
# Inspect container
docker inspect <name> --format '{{json .State}}'

# Exec into container
docker exec -it <name> /bin/sh

# Resource usage
docker stats --no-stream
```

## Safety Rules

- **Never** run `docker rm -f` on production containers without explicit confirmation
- **Never** expose privileged mode (`--privileged`) unless explicitly required and approved
- **Always** use specific image tags; avoid `latest` in production
- **Always** verify Dockerfile does not contain secrets before build
- Use `--no-cache` only when explicitly requested (slower builds)
- Prefer multi-stage builds for production images
- Run containers as non-root user when possible

## Validation Criteria

An operation is successful when:
- `docker build` exits 0 and the image appears in `docker images`
- `docker run` produces a running container visible in `docker ps`
- `docker compose up` shows all services as "Up" or "running"
- Health checks (if defined) report "healthy"
- Exposed ports respond to connectivity tests

## Error Recovery

| Symptom | Action |
|---|---|
| `Cannot connect to Docker daemon` | Check daemon status; suggest `systemctl start docker` or Docker Desktop |
| `port is already allocated` | Identify conflicting process; suggest alternate port mapping |
| `no space left on device` | Run `docker system prune` after confirmation |
| Build fails at dependency install | Check network, base image availability, and Dockerfile syntax |
| Container exits immediately | Check `docker logs <name>` for startup errors |
