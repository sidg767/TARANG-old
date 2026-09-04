"""
GET /api/instruments?bbox=&type=argo|glider|ctd|bgc

Returns instrument positions within a bounding box from PostGIS.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["instruments"])


@router.get("/instruments")
async def get_instruments(
    request: Request,
    bbox: str = Query("80,5,100,25"),
    type: str | None = Query(None, description="argo | glider | ctd | bgc"),
    limit: int = Query(500),
):
    db = request.app.state.db
    if not db:
        return JSONResponse(content={"instruments": [], "note": "Database not configured"})

    try:
        parts = [float(x) for x in bbox.split(",")]
        bbox_tuple = (parts[0], parts[1], parts[2], parts[3])
    except Exception:
        return JSONResponse(content={"error": "Invalid bbox"}, status_code=400)

    rows = await db.query_instruments(bbox_tuple, instrument_type=type, limit=limit)

    # Serialise datetime fields
    instruments = []
    for row in rows:
        instruments.append({
            "platform_id":   row["platform_id"],
            "type":          row["type"],
            "lat":           row["lat"],
            "lon":           row["lon"],
            "time_start":    str(row["time_start"]) if row.get("time_start") else None,
            "time_end":      str(row["time_end"])   if row.get("time_end")   else None,
            "cycle_number":  row.get("cycle_number"),
        })

    return JSONResponse(content={"instruments": instruments, "count": len(instruments)})
