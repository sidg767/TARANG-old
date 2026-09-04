"""
YAML Plugin Registry Loader — TARANG SIH 2026 PS 26067

Walks registry/*.yaml, validates schema, instantiates adapters.

Hot-reload is triggered three ways (§20 Rule 6 — zero code changes per new sensor):
  1. SIGHUP signal:      `kill -HUP <pid>` on POSIX systems
  2. Filesystem watcher: watchdog detects new/modified/deleted YAML files automatically
  3. HTTP endpoint:      POST /api/registry/reload  (demo-friendly, no shell access needed)

This is the concrete implementation of the "extensible design" requirement (§2):
  Adding a new sensor = dropping a new YAML file in registry/. Zero new code.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from collections.abc import Iterator
from pathlib import Path

import yaml

from backend.app.adapters import ADAPTER_REGISTRY, DataSourceAdapter
from backend.app.plugins import discover_plugins

logger = logging.getLogger("tarang.registry")

# Required fields in every manifest
REQUIRED_FIELDS = {"id", "adapter", "source", "variable"}


class RegistryLoader:
    """
    Loads and validates YAML manifests from the registry directory.
    Provides adapter instances keyed by manifest ID.

    Supports three hot-reload triggers:
    - SIGHUP (POSIX kill signal)
    - Filesystem watcher via watchdog (auto-detects YAML changes)
    - HTTP POST /api/registry/reload endpoint (demo-friendly)
    """

    def __init__(self, registry_dir: str):
        self._dir = Path(registry_dir)
        self._manifests: dict[str, dict] = {}
        self._adapters: dict[str, DataSourceAdapter] = {}
        self._observer = None          # watchdog Observer thread
        self._reload_lock = threading.Lock()
        self._reload_count: int = 0    # incremented each reload, useful for debugging

    # ── Load ──────────────────────────────────────────────────────────────────

    def load_all(self) -> None:
        """Load (or hot-reload) all *.yaml files from the registry directory.

        Builds the new manifest/adapter maps into locals and swaps them in
        atomically — accessors (which are lock-free) never see a half-populated
        registry. A reload that yields ZERO manifests while we already have some
        is treated as a transient filesystem read (watchdog phantom event on a
        Docker bind mount, a file caught mid-write) and discarded, so the API
        never goes source-less and stuck until a restart.
        """
        with self._reload_lock:
            if not self._dir.exists():
                logger.warning(f"Registry directory '{self._dir}' not found — keeping current registry")
                return

            new_manifests: dict[str, dict] = {}
            new_adapters: dict[str, DataSourceAdapter] = {}

            yaml_files = sorted(self._dir.glob("*.yaml")) + sorted(self._dir.glob("*.yml"))
            for path in yaml_files:
                try:
                    manifest_id, manifest, adapter = self._parse_one(path)
                    new_manifests[manifest_id] = manifest
                    new_adapters[manifest_id] = adapter
                except Exception as e:
                    logger.error(f"Failed to load manifest '{path.name}': {e}")

            if not new_manifests and self._manifests:
                logger.error(
                    "Registry reload found 0 usable manifests but %d were loaded before — "
                    "keeping the existing set (transient filesystem read, not a real change).",
                    len(self._manifests),
                )
                return

            if not new_manifests:
                logger.warning(f"No YAML manifests found in '{self._dir}'")

            # Atomic swap — a concurrent accessor sees either the old dict or the new one.
            self._manifests = new_manifests
            self._adapters = new_adapters
            self._reload_count += 1
            logger.info(
                f"Registry (reload #{self._reload_count}): "
                f"loaded {len(self._manifests)} manifests: {list(self._manifests.keys())}"
            )

        # ── Register SIGHUP handler (POSIX only) ─────────────────────────────
        try:
            signal.signal(signal.SIGHUP, lambda sig, frame: self.reload())
        except (AttributeError, OSError, ValueError):
            pass  # no SIGHUP on Windows / not the main thread (e.g. under TestClient)

    def _parse_one(self, path: Path) -> tuple[str, dict, DataSourceAdapter]:
        """Parse + validate one YAML manifest. Pure — does not mutate self."""
        discover_plugins()
        with open(path, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)

        if not isinstance(manifest, dict):
            raise ValueError(f"Manifest must be a YAML mapping, got {type(manifest)}")

        missing = REQUIRED_FIELDS - set(manifest.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        manifest_id = manifest["id"]
        adapter_name = manifest["adapter"]

        if adapter_name not in ADAPTER_REGISTRY:
            raise ValueError(
                f"Unknown adapter '{adapter_name}'. "
                f"Available: {list(ADAPTER_REGISTRY.keys())}"
            )

        adapter = ADAPTER_REGISTRY[adapter_name](manifest)
        logger.debug(f"Parsed: {manifest_id} ({adapter_name})")
        return manifest_id, manifest, adapter

    # ── Hot-reload triggers ───────────────────────────────────────────────────

    def reload(self) -> dict:
        """
        Hot-reload all manifests.
        Safe to call from any thread (lock protected).
        Returns summary dict for the HTTP endpoint response.
        """
        logger.info("Hot-reload triggered — re-reading registry YAML files...")
        self.load_all()
        return {
            "status": "reloaded",
            "reload_count": self._reload_count,
            "sources": list(self._manifests.keys()),
        }

    def ensure_loaded(self) -> None:
        """Lazy self-heal: if this worker's registry is somehow empty (a bad
        watcher reload got past the guard on a cold worker, the watcher thread
        died, etc.), reload it now. Cheap no-op in the normal case. Called from
        the read endpoints so a wiped worker recovers on the next request
        instead of serving empty dropdowns until a restart."""
        if not self._manifests:
            logger.warning("Registry empty on access — forcing a reload")
            try:
                self.load_all()
            except Exception as e:
                logger.error(f"ensure_loaded reload failed: {e}")

    def start_watcher(self) -> None:
        """
        Start a watchdog filesystem observer that automatically hot-reloads
        whenever a *.yaml file is created, modified, or deleted in registry/.

        Called from main.py lifespan startup so the watcher runs for the entire
        lifetime of the application — no manual triggers needed during the demo.
        """
        if not self._dir.exists():
            logger.warning(f"Registry directory '{self._dir}' not found — filesystem watcher not started")
            return

        try:
            from watchdog.events import FileSystemEventHandler
            # Native (inotify) observers are unreliable on Docker Desktop bind mounts
            # (gRPC-FUSE / virtiofs) — they miss events and can emit phantom
            # delete/create pairs where a glob briefly sees an empty directory.
            # PollingObserver just stats the (tiny) registry dir on an interval and
            # is rock-solid across mount types. Override with REGISTRY_WATCH_NATIVE=1.
            if os.getenv("REGISTRY_WATCH_NATIVE", "").strip().lower() in ("1", "true", "yes"):
                from watchdog.observers import Observer
            else:
                from watchdog.observers.polling import PollingObserver as Observer
        except ImportError:
            logger.warning("watchdog not installed — filesystem hot-reload disabled. Run: pip install watchdog")
            return

        registry_loader = self  # capture reference for the handler closure

        class YAMLChangeHandler(FileSystemEventHandler):
            """Watchdog event handler — debounced so a burst of events (common on
            Docker Desktop bind mounts, where a single save can emit several
            modified/created events, sometimes with the file briefly unreadable)
            triggers exactly one reload once things go quiet."""

            _DEBOUNCE_SEC = 0.8

            def __init__(self) -> None:
                super().__init__()
                self._timer: threading.Timer | None = None
                self._timer_lock = threading.Lock()
                self._pending: set[str] = set()

            def _is_yaml(self, path: str) -> bool:
                return path.endswith(".yaml") or path.endswith(".yml")

            def _schedule(self, event) -> None:
                if event.is_directory or not self._is_yaml(event.src_path):
                    return
                with self._timer_lock:
                    self._pending.add(Path(event.src_path).name)
                    if self._timer is not None:
                        self._timer.cancel()
                    self._timer = threading.Timer(self._DEBOUNCE_SEC, self._fire)
                    self._timer.daemon = True
                    self._timer.start()

            def _fire(self) -> None:
                with self._timer_lock:
                    changed = sorted(self._pending)
                    self._pending.clear()
                    self._timer = None
                logger.info(f"Registry watcher: {', '.join(changed)} changed — reloading (debounced)")
                try:
                    registry_loader.reload()
                except Exception as e:
                    logger.error(f"Registry watcher: debounced reload failed: {e}")

            on_modified = _schedule
            on_created = _schedule
            on_deleted = _schedule

        observer = Observer()
        observer.schedule(YAMLChangeHandler(), str(self._dir), recursive=False)
        observer.daemon = True  # dies cleanly when main process exits
        observer.start()
        self._observer = observer
        logger.info(
            f"Registry watcher started ({type(observer).__name__}) — watching '{self._dir}'"
        )

    def stop_watcher(self) -> None:
        """Stop the filesystem observer. Called from lifespan shutdown."""
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=3)
            logger.info("Registry watcher stopped")

    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_manifest(self, manifest_id: str) -> dict:
        if manifest_id not in self._manifests:
            raise KeyError(f"Unknown source ID: '{manifest_id}'. Available: {list(self._manifests.keys())}")
        return self._manifests[manifest_id]

    def get_adapter(self, manifest_id: str) -> DataSourceAdapter:
        if manifest_id not in self._adapters:
            raise KeyError(f"Unknown source ID: '{manifest_id}'")
        return self._adapters[manifest_id]

    def manifest_ids(self) -> Iterator[str]:
        return iter(self._manifests.keys())

    def all_manifests(self) -> list[dict]:
        return list(self._manifests.values())

    @property
    def reload_count(self) -> int:
        return self._reload_count
