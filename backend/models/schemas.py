"""
Pydantic Models for Request/Response Validation
"""
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any


class BoundaryData(BaseModel):
    """Detected boundary geometry data."""
    exterior: List[List[float]] = Field(
        ...,
        description="Exterior boundary coordinates as [[x1, y1], [x2, y2], ...]"
    )
    interiors: List[List[List[float]]] = Field(
        default_factory=list,
        description="Interior boundaries (holes) as list of coordinate lists"
    )


class Metadata(BaseModel):
    """Processing metadata and statistics."""
    area: float = Field(..., description="Detected boundary area in square units")
    bbox_area: float = Field(..., description="Bounding box area")
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Detection confidence (HATCH IoU or heuristic)"
    )
    cycles_detected: int = Field(..., ge=0, description="Number of cycles detected")
    processing_time_ms: int = Field(..., ge=0, description="Processing time in milliseconds")
    entity_count: Optional[int] = Field(None, ge=0, description="Total DXF entities processed")
    node_count: Optional[int] = Field(None, ge=0, description="Graph nodes after snapping")
    edge_count: Optional[int] = Field(None, ge=0, description="Graph edges after pruning")


class BoundaryResponse(BaseModel):
    """API response for boundary detection."""
    success: bool = Field(..., description="Whether detection was successful")
    boundary: Optional[BoundaryData] = Field(None, description="Detected boundary data")
    metadata: Optional[Metadata] = Field(None, description="Processing metadata")
    error: Optional[str] = Field(None, description="Error message if failed")


class AIJudgmentRequest(BaseModel):
    """Request for AI judgment at intervention points."""
    point: str = Field(
        ...,
        description="Intervention point: 'graph_anomaly', 'area_filter', or 'sanity_check'"
    )
    data: Dict[str, Any] = Field(..., description="Data to send to AI for judgment")


class AIJudgmentResponse(BaseModel):
    """Response from AI judgment."""
    decision: str = Field(..., description="Decision: 'keep', 'adjust', 'retry', or 'abort'")
    reason: str = Field(..., description="Explanation for the decision")
    params: Optional[Dict[str, Any]] = Field(None, description="Suggested parameter adjustments")
