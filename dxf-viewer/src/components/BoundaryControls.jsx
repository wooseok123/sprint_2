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
 * @param {boolean} props.drawingVisible - Whether the DXF drawing is currently visible
 * @param {boolean} props.overlayVisible - Whether overlay is currently visible
 * @param {function} props.onToggleDrawing - Callback to toggle base drawing visibility
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
  drawingVisible = true,
  overlayVisible = true,
  onToggleDrawing,
  onToggleOverlay,
  onClear
}) {
  const [isExpanded, setIsExpanded] = useState(true)

  const formatNumber = (num, decimals = 2) => {
    if (num === null || num === undefined) return 'N/A'
    return num.toFixed(decimals)
  }

  const formatArea = (area, unit) => {
    if (area === null || area === undefined) return 'N/A'

    if (unit === 'mm²') {
      if (area >= 1000000) {
        return `${(area / 1000000).toFixed(2)} m²`
      }
      return `${area.toFixed(0)} mm²`
    }

    if (unit === 'm²' || !unit) {
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

    return `${area.toFixed(2)} ${unit}`
  }

  const formatLength = (length, unit) => {
    if (length === null || length === undefined) return 'N/A'

    if (unit === 'mm') {
      if (length >= 1000) {
        return `${(length / 1000).toFixed(2)} m`
      }
      return `${length.toFixed(0)} mm`
    }

    if (unit === 'm' || !unit) {
      return `${length.toFixed(2)} m`
    }

    return `${length.toFixed(2)} ${unit}`
  }

  const formatTime = (ms) => {
    if (ms === null || ms === undefined) return 'N/A'
    if (ms >= 1000) {
      return `${(ms / 1000).toFixed(2)}s`
    }
    return `${ms.toFixed(0)}ms`
  }

  const formatDetailValue = (value) => {
    if (value === null || value === undefined) return 'N/A'
    if (typeof value === 'object') {
      return JSON.stringify(value)
    }
    return String(value)
  }

  const formatPoint = (point) => {
    if (!Array.isArray(point) || point.length < 2) return 'N/A'
    return `(${formatNumber(point[0], 1)}, ${formatNumber(point[1], 1)})`
  }

  const endpointExtension = metadata?.processing_details?.endpoint_extension
  const graphPruning = metadata?.processing_details?.graph_pruning
  const extensionItems = endpointExtension?.applied_extensions ?? []

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
                  className="toggle-drawing-button"
                  onClick={onToggleDrawing}
                >
                  {drawingVisible ? 'Outline Only' : 'Show Drawing'}
                </button>
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
                  <span className="metadata-label">Perimeter:</span>
                  <span className="metadata-value">{formatLength(metadata.perimeter, metadata.perimeter_unit)}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">Confidence:</span>
                  <span className={`metadata-value ${metadata.confidence >= 0.8 ? 'high' : metadata.confidence >= 0.5 ? 'medium' : 'low'}`}>
                    {formatNumber(metadata.confidence * 100, 1)}%
                  </span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">Area:</span>
                  <span className="metadata-value">{formatArea(metadata.area, metadata.area_unit)}</span>
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

              {endpointExtension && (
                <div className="processing-panel">
                  <div className="processing-panel-header">
                    <h5>Endpoint Extensions</h5>
                    <span className={`processing-badge ${endpointExtension.applied_count > 0 ? 'active' : 'idle'}`}>
                      {endpointExtension.applied_count > 0 ? `${endpointExtension.applied_count} applied` : 'No extensions'}
                    </span>
                  </div>
                  <div className="processing-summary-grid">
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Attempted</span>
                      <span className="processing-summary-value">{endpointExtension.attempted ? 'Yes' : 'No'}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Base Length</span>
                      <span className="processing-summary-value">{formatLength(endpointExtension.extension_length, 'mm')}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Method</span>
                      <span className="processing-summary-value">{endpointExtension.estimate_method || 'N/A'}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Skipped</span>
                      <span className="processing-summary-value">{endpointExtension.skipped_reason || 'No'}</span>
                    </div>
                  </div>

                  {extensionItems.length > 0 && (
                    <div className="extension-list">
                      {extensionItems.map((item, index) => (
                        <div key={`${item.source_segment_index}-${item.source_endpoint}-${index}`} className="extension-card">
                          <div className="extension-card-top">
                            <span className="extension-card-title">
                              Seg #{item.source_segment_index} · {item.source_endpoint}
                            </span>
                            <span className="extension-card-length">
                              +{formatLength(item.extension_mm, 'mm')}
                            </span>
                          </div>
                          <div className="extension-card-row">
                            <span className="extension-card-label">From</span>
                            <span className="extension-card-value">{formatPoint(item.from_point)}</span>
                          </div>
                          <div className="extension-card-row">
                            <span className="extension-card-label">To</span>
                            <span className="extension-card-value">{formatPoint(item.to_point)}</span>
                          </div>
                          <div className="extension-card-row">
                            <span className="extension-card-label">Target</span>
                            <span className="extension-card-value">
                              {item.target_kind}
                              {item.target_segment_index !== undefined ? ` #${item.target_segment_index}` : ''}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {graphPruning && (
                <div className="processing-panel">
                  <div className="processing-panel-header">
                    <h5>Graph Pruning</h5>
                  </div>
                  <div className="processing-summary-grid">
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Pruned Edges</span>
                      <span className="processing-summary-value">{graphPruning.pruned_edges}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Pruned %</span>
                      <span className="processing-summary-value">{formatNumber(graphPruning.pruned_percent, 1)}%</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Components</span>
                      <span className="processing-summary-value">{graphPruning.components}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Max Degree</span>
                      <span className="processing-summary-value">{graphPruning.max_degree}</span>
                    </div>
                  </div>
                </div>
              )}

              {/* Raw Processing Details (if available) */}
              {metadata.processing_details && (
                <details className="processing-details">
                  <summary>Raw Processing Details</summary>
                  <div className="details-content">
                    {Object.entries(metadata.processing_details).map(([key, value]) => (
                      <div key={key} className="detail-item">
                        <span className="detail-key">{key}:</span>
                        <span className="detail-value">{formatDetailValue(value)}</span>
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
