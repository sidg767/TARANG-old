"""Adapter package — re-exports for convenience."""
from backend.app.adapters.argo_adapter import ArgoAdapter
from backend.app.adapters.base import (
    CFMetadata,
    DataSourceAdapter,
    SliceResult,
    VolumeResult,
)
from backend.app.adapters.delimited_text_adapter import DelimitedTextAdapter
from backend.app.adapters.netcdf_adapter import NetCDFAdapter
from backend.app.plugins import get_adapter_registry, register_adapter

register_adapter("NetCDFAdapter", NetCDFAdapter)
register_adapter("DelimitedTextAdapter", DelimitedTextAdapter)
register_adapter("ArgoAdapter", ArgoAdapter)

ADAPTER_REGISTRY: dict[str, type[DataSourceAdapter]] = get_adapter_registry(discover=False)

__all__ = [
    "ADAPTER_REGISTRY",
    "ArgoAdapter",
    "CFMetadata",
    "DataSourceAdapter",
    "DelimitedTextAdapter",
    "NetCDFAdapter",
    "SliceResult",
    "VolumeResult",
    "register_adapter",
]
