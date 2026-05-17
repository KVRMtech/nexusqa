"""
Nexus Platform — Core infrastructure services.

The platform layer contains domain-agnostic infrastructure:
    - orchestrator: Workflow DAG engine, chain registry, context resolution
    - auth-service: JWT authentication and RBAC
    - gateway: API gateway and request routing
    - config-service: Centralized configuration management (planned)
    - message-bus: Event bus and message routing (planned)

The platform is product-agnostic. Products (e.g., Insurance QA)
extend platform capabilities through the plugin system.
"""
