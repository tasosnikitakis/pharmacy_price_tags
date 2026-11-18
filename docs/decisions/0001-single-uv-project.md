# 0001 One uv project for core, gateway and shared

**Decision.** One `pyproject.toml` with optional extras `core`, `gateway` and `dev`, not
separate packages. The Docker image installs only `core`; the shop box installs
`gateway` for the native gateway.

**Why.** Shared contracts in `shared/` stay in one place without publishing a package,
and one lock file keeps CI simple. Separation is enforced by imports, not packaging:
the gateway imports only `shared/` and `gateway/`, never `core/`.
