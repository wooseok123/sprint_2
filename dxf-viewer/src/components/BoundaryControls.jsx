import { useState } from 'react'
import './BoundaryControls.css'

/**
 * BoundaryControls - UI for boundary detection and metadata display
 *
 * @param {object} props
 * @param {File|null} props.dxfFile - The loaded DXF file
 * @param {function} props.onDetect - Callback to trigger boundary detection
 * @param {boolean} props.isDetecting - Whether detection is in progress
 * @param {object|null} props.metadata - Detection result metadata
 * @param {string|null} props.error - Error message if detection failed
 * @param {boolean} props.hasBoundary - Whether boundary data exists
 * @param {boolean} props.overlayVisible - Whether overlay is currently visible
 * @param {function} props.onToggleOverlay - Callback to toggle overlay visibility
 * @param {function} props.onClear - Callback to clear boundary data
 */
function BoundaryControls({
  dxfFile,
  onDetect,
  isDetecting = false,
  metadata = null,
  error = null,
  hasBoundary = false,
  overlayVisible = true,
  onToggleOverlay,
  onClear
}) {
  const [isExpanded, setIsExpanded] = useState(true)

  const formatNumber = (num, decimals = 2) => {
    if (num === null || num === undefined) return 'N/A'
    return num.toFixed(decimals)
  }

  const formatArea = (area) => {
    if (area === null || area === undefined) return 'N/A'
    if (area >= 1000000) {
      return `${(area / 1000000).toFixed(2)} km²`
    } else if (area >= 10000) {
      return `${(area / 10000).toFixed(2)} ha`
    } else if (area >= 1) {
      return `${area.toFixed(2)} m²`
    } else {
      return `${(area * 10000).toFixed(2)} cm²`
    }
  }

  const formatTime = (ms) => {
    if (ms === null || ms === undefined) return 'N/A'
    if (ms >= 1000) {
      return `${(ms / 1000).toFixed(2)}s`
    }
    return `${ms.toFixed(0)}ms`
  }

  return (
    <div className={`boundary-controls ${isExpanded ? 'expanded' : 'collapsed'}`}>
      <div className="controls-header" onClick={() => setIsExpanded(!isExpanded)}>
        <h3>Boundary Detection</h3>
        <button className="toggle-expand">
          {isExpanded ? '▼' : '▶'}
        </button>
      </div>

      {isExpanded && (
        <div className="controls-content">
          {/* Action Buttons */}
          <div className="controls-actions">
            {!hasBoundary ? (
              <button
                className="detect-button"
                onClick={onDetect}
                disabled={!dxfFile || isDetecting}
              >
                {isDetecting ? 'Detecting...' : 'Detect Boundary'}
              </button>
            ) : (
              <>
                <button
                  className="toggle-overlay-button"
                  onClick={onToggleOverlay}
                >
                  {overlayVisible ? 'Hide Overlay' : 'Show Overlay'}
                </button>
                <button
                  className="clear-button"
                  onClick={onClear}
                >
                  Clear
                </button>
              </>
            )}
          </div>

          {/* Error Message */}
          {error && (
            <div className="error-message">
              <strong>Error:</strong> {error}
            </div>
          )}

          {/* Metadata Display */}
          {metadata && (
            <div className="metadata-display">
              <h4>Boundary Metadata</h4>
              <div className="metadata-grid">
                <div className="metadata-item">
                  <span className="metadata-label">Area:</span>
                  <span className="metadata-value">{formatArea(metadata.area)}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">Confidence:</span>
                  <span className={`metadata-value ${metadata.confidence >= 0.8 ? 'high' : metadata.confidence >= 0.5 ? 'medium' : 'low'}`}>
                    {formatNumber(metadata.confidence * 100, 1)}%
                  </span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">Exterior Vertices:</span>
                  <span className="metadata-value">{metadata.exterior_vertex_count || 'N/A'}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">Interior Holes:</span>
                  <span className="metadata-value">{metadata.interior_hole_count || 0}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">Processing Time:</span>
                  <span className="metadata-value">{formatTime(metadata.processing_time_ms)}</span>
                </div>
                {metadata.convex_hull_ratio !== undefined && (
                  <div className="metadata-item">
                    <span className="metadata-label">Compactness:</span>
                    <span className="metadata-value">{formatNumber(metadata.convex_hull_ratio * 100, 1)}%</span>
                  </div>
                )}
                {metadata.hatch_iou !== undefined && (
                  <div className="metadata-item">
                    <span className="metadata-label">HATCH Match:</span>
                    <span className="metadata-value">{formatNumber(metadata.hatch_iou * 100, 1)}%</span>
                  </div>
                )}
              </div>

              {/* Processing Details (if available) */}
              {metadata.processing_details && (
                <details className="processing-details">
                  <summary>Processing Details</summary>
                  <div className="details-content">
                    {Object.entries(metadata.processing_details).map(([key, value]) => (
                      <div key={key} className="detail-item">
                        <span className="detail-key">{key}:</span>
                        <span className="detail-value">{String(value)}</span>
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          )}

          {/* Help Text */}
          {!hasBoundary && !metadata && (
            <div className="help-text">
              <p>Load a DXF file and click "Detect Boundary" to identify the outer boundary.</p>
              <p className="help-note">
                Make sure the backend server is running at <code>http://localhost:8000</code>
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default BoundaryControls
