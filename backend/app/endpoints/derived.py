"""
Derived-product plugin surface — TARANG SIH 2026 PS 26067.

The PS asks for a "plugin-style module for … machine-learning derived products".
This is that surface: a small catalog of analysis products computed on demand
from the model fields, each with a stable URL and a declared method.

  GET /api/derived                  → list available products
  GET /api/derived/water_masses     → unsupervised (k-means) water-mass
                                       classification on the (T, S) field

The eddy (Okubo–Weiss) and thermal-front detectors already live at /api/eddy
and /api/front; they are listed here too so the catalog is the one place to
discover every derived product. Adding a new product = one function + one
catalog entry, no other code touched.

`water_masses` is genuine unsupervised ML — k-means over standardized
temperature/salinity — not a heuristic threshold. It runs in <1 s on a slice
with a fixed seed, so it is deterministic and needs no model artifact.
"""

from __future__ import annotations

import logging

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from backend.app.endpoints.binary import parse_bbox

logger = logging.getLogger("tarang.endpoint.derived")
router = APIRouter(tags=["analytics"])

_TEMP_NAMES = {"water_temp", "thetao", "temp", "temperature", "sea_water_temperature",
               "sea_water_potential_temperature", "analysed_sst"}
_SAL_NAMES = {"salinity", "so", "psal", "sea_water_salinity",
              "sea_water_practical_salinity"}


def _pick(available: list[str], cf: dict, wanted: set[str], keyword: str) -> str | None:
    for v in available:
        if v.lower() in wanted:
            return v
        sn = (cf.get(v, {}).get("standard_name") or "").lower()
        if keyword in sn:
            return v
    return None


def _kmeans(x: np.ndarray, k: int, iters: int = 40, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Plain k-means. x: (n_samples, n_features), already standardized. Deterministic."""
    rng = np.random.default_rng(seed)
    # k-means++ style seeding for a stable, well-spread start.
    centers = [x[rng.integers(len(x))]]
    for _ in range(1, k):
        d2 = np.min([np.sum((x - c) ** 2, axis=1) for c in centers], axis=0)
        probs = d2 / d2.sum()
        centers.append(x[rng.choice(len(x), p=probs)])
    C = np.array(centers)

    labels = np.zeros(len(x), dtype=np.int32)
    for _ in range(iters):
        dists = np.linalg.norm(x[:, None, :] - C[None, :, :], axis=2)
        new_labels = np.argmin(dists, axis=1).astype(np.int32)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for j in range(k):
            member = x[labels == j]
            if len(member):
                C[j] = member.mean(axis=0)
    return labels, C


@router.get("/derived")
async def list_derived(request: Request):
    """Catalog of derived analysis products."""
    return JSONResponse({
        "products": [
            {
                "id": "water_masses",
                "name": "Water-mass classification",
                "method": "unsupervised ML — k-means over standardized (temperature, salinity)",
                "endpoint": "/api/derived/water_masses?source=&time=&depth=&bbox=&k=4",
                "output": "categorical label grid + per-class T/S centroids",
            },
            {
                "id": "eddies",
                "name": "Mesoscale eddy detection",
                "method": "Okubo–Weiss parameter on the geostrophic velocity field (deterministic)",
                "endpoint": "/api/eddy?source=&time=&bbox=",
                "output": "eddy centres with polarity + radius",
            },
            {
                "id": "fronts",
                "name": "Thermal / haline front detection",
                "method": "normalized gradient magnitude threshold (deterministic)",
                "endpoint": "/api/front?source=&var=&time=&bbox=",
                "output": "front cell locations",
            },
        ]
    })


@router.get("/derived/water_masses")
async def water_masses(
    request: Request,
    source: str = Query(..., description="Registry source ID with both temperature and salinity"),
    time: int = Query(0, description="Time step index"),
    depth: float = Query(0.0, description="Depth in metres"),
    bbox: str = Query("80,5,100,25", description="minLon,minLat,maxLon,maxLat"),
    k: int = Query(4, ge=2, le=8, description="Number of water-mass classes"),
):
    registry = request.app.state.registry
    try:
        adapter = registry.get_adapter(source)
    except KeyError:
        raise HTTPException(404, f"Unknown source '{source}'")

    try:
        bbox_tuple = parse_bbox(bbox)
    except ValueError as e:
        raise HTTPException(400, str(e))

    def compute():
        meta = adapter.get_metadata()
        avail = meta.get("available_variables", [])
        cf = meta.get("cf_metadata", {})
        tvar = _pick(avail, cf, _TEMP_NAMES, "temperature")
        svar = _pick(avail, cf, _SAL_NAMES, "salinity")
        if not tvar or not svar:
            raise HTTPException(422, f"Source '{source}' needs both temperature and salinity; has {avail}")

        t_res = adapter.get_slice(tvar, depth, time, bbox_tuple)
        s_res = adapter.get_slice(svar, depth, time, bbox_tuple)
        T = np.asarray(t_res.data, dtype=np.float64)
        S = np.asarray(s_res.data, dtype=np.float64)
        if T.shape != S.shape:
            raise HTTPException(422, "temperature and salinity grids differ in shape")

        miss_t = getattr(t_res.meta, "missing_value", np.nan)
        miss_s = getattr(s_res.meta, "missing_value", np.nan)
        valid = np.isfinite(T) & np.isfinite(S) & (T != miss_t) & (S != miss_s) & (T > -50) & (T < 60)
        if valid.sum() < k * 10:
            raise HTTPException(422, "not enough valid ocean cells in this region for classification")

        feats = np.column_stack([T[valid], S[valid]])
        mu, sigma = feats.mean(axis=0), feats.std(axis=0)
        sigma[sigma == 0] = 1.0
        z = (feats - mu) / sigma

        labels_flat, centers_z = _kmeans(z, k)
        centers = centers_z * sigma + mu  # back to physical T/S

        # Order classes by density (surface/light → deep/dense) using potential density proxy:
        # colder + saltier ranks "deeper". Gives stable, interpretable class ids.
        rank = np.argsort(centers[:, 1] - centers[:, 0])  # sal - temp ascending
        remap = np.zeros(k, dtype=np.int32)
        for new_id, old_id in enumerate(rank):
            remap[old_id] = new_id

        label_grid = np.full(T.shape, -1, dtype=np.int32)
        label_grid[valid] = remap[labels_flat]

        centroids = []
        for old_id in range(k):
            new_id = int(remap[old_id])
            cnt = int((labels_flat == old_id).sum())
            tc, sc = float(centers[old_id, 0]), float(centers[old_id, 1])
            centroids.append({
                "label": new_id,
                "temperature": round(tc, 3),
                "salinity": round(sc, 3),
                "count": cnt,
                "fraction": round(cnt / len(labels_flat), 4),
            })
        centroids.sort(key=lambda c: c["label"])

        return {
            "product": "water_masses",
            "method": "k-means (unsupervised) over standardized (temperature, salinity)",
            "source": source,
            "variables": {"temperature": tvar, "salinity": svar},
            "depth_m": round(float(t_res.depth_m), 2) if hasattr(t_res, "depth_m") else depth,
            "k": k,
            "shape": list(T.shape),
            "bounds": {
                "lon": [bbox_tuple[0], bbox_tuple[2]],
                "lat": [bbox_tuple[1], bbox_tuple[3]],
            },
            "labels": label_grid.flatten().tolist(),
            "centroids": centroids,
        }

    import asyncio
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, compute)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("water_masses failed")
        raise HTTPException(500, f"water-mass classification failed: {e}")
    return JSONResponse(result)
