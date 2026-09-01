"""
Platform Services — Test conftest.

Provides module-scoped fixture that isolates the `app` package imports
across different platform services (API, Auth, Gateway, Orchestrator)
that all share the `app/` package name.
"""
import os
import sys
import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Ensure SDK is available for all platform tests
_SDK_PATH = os.path.join(_PROJECT_ROOT, "sdk", "nexus-sdk")
if _SDK_PATH not in sys.path:
    sys.path.insert(0, _SDK_PATH)

# Map test file keywords to service directories
_SERVICE_PATHS = {
    "platform_api": os.path.join(_PROJECT_ROOT, "platform", "api"),
    "qi_portal": os.path.join(_PROJECT_ROOT, "platform", "api"),
    "auth_service": os.path.join(_PROJECT_ROOT, "platform", "auth-service"),
    "gateway": os.path.join(_PROJECT_ROOT, "platform", "gateway"),
    "orchestrator": os.path.join(_PROJECT_ROOT, "products", "qa-orchestrator"),
}


@pytest.fixture(autouse=True, scope="module")
def _isolate_app_module(request):
    """
    Module-scoped autouse fixture that ensures each test module gets
    the correct `app` and `main` packages by manipulating sys.path
    and sys.modules before and after each test module.
    """
    module_name = request.module.__name__

    # Determine service path based on test file name
    service_path = None
    for key, path in _SERVICE_PATHS.items():
        if key in module_name:
            service_path = path
            break

    if service_path is None:
        yield
        return

    # Save state
    saved_path = sys.path[:]
    saved_app_modules = {
        k: v for k, v in sys.modules.items()
        if k == "app" or k.startswith("app.") or k == "main"
    }

    # Clear cached app/main modules
    for k in list(sys.modules.keys()):
        if k == "app" or k.startswith("app.") or k == "main":
            del sys.modules[k]

    # Set the correct service path first
    if service_path in sys.path:
        sys.path.remove(service_path)
    sys.path.insert(0, service_path)

    yield

    # Restore: clear any modules loaded by this test module
    for k in list(sys.modules.keys()):
        if k == "app" or k.startswith("app.") or k == "main":
            del sys.modules[k]
    # Restore previously cached modules
    sys.modules.update(saved_app_modules)
    sys.path[:] = saved_path
