import { useState } from 'react'
import './BoundaryControls.css'

/**
 * BoundaryControls - UI for boundary detection and metadata display
 *
 * @param {object} props
 * @param {File|null} props.dxfFile - The loaded DXF file
 * @param {function} props.onPreprocess - Callback to trigger preprocessing
 * @param {function} props.onDetect - Callback to trigger boundary detection
 * @param {boolean} props.isPreprocessing - Whether preprocessing is in progress
 * @param {boolean} props.isDetecting - Whether detection is in progress
 * @param {object|null} props.metadata - Detection result metadata
 * @param {string|null} props.error - Error message if detection failed
 * @param {boolean} props.hasBoundary - Whether boundary data exists
 * @param {boolean} props.hasPreprocessed - Whether preprocessed preview data exists
 * @param {'original'|'preprocessed'|'boundary'|'overlay'} props.viewMode - Active visualization mode
 * @param {function} props.onChangeViewMode - Callback to switch visualization mode
 * @param {function} props.onClear - Callback to clear boundary data
 */
function BoundaryControls({
  dxfFile,
  onPreprocess,
  onDetect,
  isPreprocessing = false,
  isDetecting = false,
  metadata = null,
  error = null,
  hasBoundary = false,
  hasPreprocessed = false,
  viewMode = 'original',
  onChangeViewMode,
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
  const preprocessing = metadata?.processing_details?.preprocessing
  const extensionItems = endpointExtension?.applied_extensions ?? []
  const hasAnyResult = hasPreprocessed || hasBoundary

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
            {!hasAnyResult ? (
              <>
                <button
                  className="detect-button"
                  onClick={onPreprocess}
                  disabled={!dxfFile || isPreprocessing || isDetecting}
                >
                  {isPreprocessing ? 'Preprocessing...' : 'Run Preprocess'}
                </button>
                <button
                  className="detect-button"
                  onClick={onDetect}
                  disabled={!dxfFile || !hasPreprocessed || isPreprocessing || isDetecting}
                >
                  {isDetecting ? 'Detecting...' : 'Detect Boundary'}
                </button>
              </>
            ) : (
              <>
                <button
                  className="detect-button"
                  onClick={onPreprocess}
                  disabled={!dxfFile || isPreprocessing || isDetecting}
                >
                  {isPreprocessing ? 'Preprocessing...' : 'Re-run Preprocess'}
                </button>
                <button
                  className="detect-button"
                  onClick={onDetect}
                  disabled={!dxfFile || !hasPreprocessed || isPreprocessing || isDetecting}
                >
                  {isDetecting ? 'Detecting...' : hasBoundary ? 'Re-run Boundary' : 'Detect Boundary'}
                </button>
                <div className="view-mode-group">
                  <button
                    className={`view-mode-button ${viewMode === 'original' ? 'active' : ''}`}
                    onClick={() => onChangeViewMode?.('original')}
                  >
                    Original
                  </button>
                  <button
                    className={`view-mode-button ${viewMode === 'preprocessed' ? 'active' : ''}`}
                    onClick={() => onChangeViewMode?.('preprocessed')}
                    disabled={!hasPreprocessed}
                  >
                    Preprocessed
                  </button>
                  <button
                    className={`view-mode-button ${viewMode === 'boundary' ? 'active' : ''}`}
                    onClick={() => onChangeViewMode?.('boundary')}
                    disabled={!hasBoundary}
                  >
                    Boundary
                  </button>
                  <button
                    className={`view-mode-button ${viewMode === 'overlay' ? 'active' : ''}`}
                    onClick={() => onChangeViewMode?.('overlay')}
                    disabled={!hasBoundary}
                  >
                    Overlay Only
                  </button>
                </div>
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
              <h4>{hasBoundary ? 'Boundary Metadata' : 'Preprocess Metadata'}</h4>
              <div className="metadata-grid">
                <div className="metadata-item">
                  <span className="metadata-label">Processing Time:</span>
                  <span className="metadata-value">{formatTime(metadata.processing_time_ms)}</span>
                </div>
                {hasBoundary && (
                  <div className="metadata-item">
                    <span className="metadata-label">Perimeter:</span>
                    <span className="metadata-value">{formatLength(metadata.perimeter, metadata.perimeter_unit)}</span>
                  </div>
                )}
                {hasBoundary && (
                  <div className="metadata-item">
                    <span className="metadata-label">Confidence:</span>
                    <span className={`metadata-value ${metadata.confidence >= 0.8 ? 'high' : metadata.confidence >= 0.5 ? 'medium' : 'low'}`}>
                      {formatNumber(metadata.confidence * 100, 1)}%
                    </span>
                  </div>
                )}
                {hasBoundary && (
                  <div className="metadata-item">
                    <span className="metadata-label">Area:</span>
                    <span className="metadata-value">{formatArea(metadata.area, metadata.area_unit)}</span>
                  </div>
                )}
                {hasBoundary && (
                  <div className="metadata-item">
                    <span className="metadata-label">Exterior Vertices:</span>
                    <span className="metadata-value">{metadata.exterior_vertex_count || 'N/A'}</span>
                  </div>
                )}
                {hasBoundary && (
                  <div className="metadata-item">
                    <span className="metadata-label">Interior Holes:</span>
                    <span className="metadata-value">{metadata.interior_hole_count || 0}</span>
                  </div>
                )}
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

              {preprocessing && (
                <div className="processing-panel">
                  <div className="processing-panel-header">
                    <h5>Preprocessing</h5>
                  </div>
                  <div className="processing-summary-grid">
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Flattened</span>
                      <span className="processing-summary-value">{preprocessing.flattened_entities}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Preview Segments</span>
                      <span className="processing-summary-value">{preprocessing.segments_after_preprocessing}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Type Removed</span>
                      <span className="processing-summary-value">{preprocessing.removed_by_type}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Linetype Removed</span>
                      <span className="processing-summary-value">{preprocessing.removed_by_linetype}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Title Block Removed</span>
                      <span className="processing-summary-value">{preprocessing.removed_by_title_block}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Border Removed</span>
                      <span className="processing-summary-value">{preprocessing.removed_by_border_frame}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Title Confirmed</span>
                      <span className="processing-summary-value">{preprocessing.title_block_confirmed ? 'Yes' : 'No'}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Short Removed</span>
                      <span className="processing-summary-value">{preprocessing.removed_short_segments}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Isolated Removed</span>
                      <span className="processing-summary-value">{preprocessing.removed_isolated_segments}</span>
                    </div>
                    <div className="processing-summary-item">
                      <span className="processing-summary-label">Title Signals</span>
                      <span className="processing-summary-value">{preprocessing.title_block_debug?.signal_count ?? 0}</span>
                    </div>
                  </div>

                  {(preprocessing.work_area_bbox || preprocessing.title_block_candidate_bbox || preprocessing.title_block_bbox) && (
                    <div className="processing-details">
                      {preprocessing.work_area_bbox && (
                        <div className="detail-item">
                          <span className="detail-key">work_area_bbox:</span>
                          <span className="detail-value">{formatDetailValue(preprocessing.work_area_bbox)}</span>
                        </div>
                      )}
                      {preprocessing.title_block_candidate_bbox && (
                        <div className="detail-item">
                          <span className="detail-key">title_block_candidate_bbox:</span>
                          <span className="detail-value">{formatDetailValue(preprocessing.title_block_candidate_bbox)}</span>
                        </div>
                      )}
                      {preprocessing.title_block_bbox && (
                        <div className="detail-item">
                          <span className="detail-key">title_block_bbox:</span>
                          <span className="detail-value">{formatDetailValue(preprocessing.title_block_bbox)}</span>
                        </div>
                      )}
                    </div>
                  )}
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
              <p>Load a DXF file, run preprocessing first, then detect the boundary after reviewing the preview.</p>
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
