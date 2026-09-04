"""Plugin discovery and registration for extensible TARANG sensor adapters.

A plugin can register an adapter in either of these forms:

    from backend.app.plugins import register_adapter
    register_adapter("MySensorAdapter", MySensorAdapter)

Or expose ``register_plugin(manager)`` from a module and configure
``TARANG_PLUGIN_MODULES=package.my_sensor``. Installed distributions can expose
entry points in the ``tarang.adapters`` group. The registry loader only needs
the adapter name from a YAML manifest; sensor-specific code stays outside the
core application.
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Mapping
from importlib import metadata
from typing import Any, TypeAlias

logger = logging.getLogger("tarang.plugins")

AdapterClass: TypeAlias = type[Any]

_ADAPTERS: dict[str, AdapterClass] = {}
_DISCOVERED = False


def register_adapter(name: str, adapter_class: AdapterClass) -> AdapterClass:
    """Register an adapter class and return it, enabling decorator-style use."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Plugin adapter name must be a non-empty string")
    if not isinstance(adapter_class, type):
        raise TypeError(f"Plugin '{name}' must be registered with a class")
    if name in _ADAPTERS and _ADAPTERS[name] is not adapter_class:
        raise ValueError(f"Plugin adapter '{name}' is already registered")
    _ADAPTERS[name] = adapter_class
    return adapter_class


def register_adapters(adapters: Mapping[str, AdapterClass]) -> None:
    """Register several adapters exposed by a plugin module."""
    for name, adapter_class in adapters.items():
        register_adapter(name, adapter_class)


def _register_loaded_plugin(plugin: Any, source: str) -> None:
    if isinstance(plugin, type):
        register_adapter(plugin.__name__, plugin)
    elif isinstance(plugin, Mapping):
        register_adapters(plugin)
    elif callable(plugin):
        plugin(register_adapter)
    else:
        raise TypeError(f"Plugin entry point '{source}' returned an unsupported object")


def discover_plugins() -> None:
    """Load configured modules and installed ``tarang.adapters`` entry points once."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True

    module_names = [name.strip() for name in os.getenv("TARANG_PLUGIN_MODULES", "").split(",") if name.strip()]
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
            register_fn = getattr(module, "register_plugin", None)
            if register_fn is not None:
                register_fn(register_adapter)
            else:
                _register_loaded_plugin(getattr(module, "ADAPTERS", module), module_name)
        except Exception as exc:
            logger.error("Failed to load TARANG adapter module '%s': %s", module_name, exc)

    try:
        entry_points = metadata.entry_points()
        selected = entry_points.select(group="tarang.adapters") if hasattr(entry_points, "select") else entry_points.get("tarang.adapters", [])
    except Exception as exc:
        logger.warning("Could not inspect TARANG adapter entry points: %s", exc)
        selected = []

    for entry_point in selected:
        try:
            _register_loaded_plugin(entry_point.load(), entry_point.name)
        except Exception as exc:
            logger.error("Failed to load TARANG adapter plugin '%s': %s", entry_point.name, exc)


def get_adapter_registry(*, discover: bool = True) -> dict[str, AdapterClass]:
    """Return the live adapter map used by manifest loading."""
    if discover:
        discover_plugins()
    return _ADAPTERS


def reset_plugins_for_testing() -> None:
    """Reset discovery state for isolated tests; not intended for application use."""
    global _DISCOVERED
    _ADAPTERS.clear()
    _DISCOVERED = False


__all__ = ["discover_plugins", "get_adapter_registry", "register_adapter", "register_adapters"]
