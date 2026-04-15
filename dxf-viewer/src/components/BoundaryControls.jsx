import { useState } from 'react'
import './BoundaryControls.css'

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
  aiCleanupSteps = [],
  selectedAiCleanupStepIndex = null,
  onSelectAiCleanupStep,
  baseViewMode = 'original',
  onChangeBaseViewMode,
  overlayVisibility = {},
  onToggleOverlay,
  onClear
}) {
  const [isExpanded, setIsExpanded] = useState(true)

  const formatNumber = (num, decimals = 1) => {
    if (num === null || num === undefined || Number.isNaN(Number(num))) return 'N/A'
    return Number(num).toFixed(decimals)
  }

  const formatLength = (length, unit) => {
    if (length === null || length === undefined) return 'N/A'
    if (unit === 'mm' && length >= 1000) {
      return `${(length / 1000).toFixed(2)} m`
    }
    return `${formatNumber(length, unit === 'mm' ? 0 : 2)} ${unit || ''}`.trim()
  }

  const formatArea = (area, unit) => {
    if (area === null || area === undefined) return 'N/A'
    if (unit === 'mm²' && area >= 1000000) {
      return `${(area / 1000000).toFixed(2)} m²`
    }
    return `${formatNumber(area, unit === 'mm²' ? 0 : 2)} ${unit || ''}`.trim()
  }

  const formatTime = (ms) => {
    if (ms === null || ms === undefined) return 'N/A'
    if (ms >= 1000) {
      return `${(ms / 1000).toFixed(2)} s`
    }
    return `${formatNumber(ms, 0)} ms`
  }

  const hasAnyResult = hasPreprocessed || hasBoundary

  const overlayItems = [
    {
      key: 'initialBoundary',
      label: '초기 외곽선',
      description: 'AI 보정 전 외곽선',
      disabled: !hasBoundary
    },
    {
      key: 'aiDetections',
      label: 'AI 탐지 영역',
      description: '탐지된 후보 형상',
      disabled: !hasBoundary || aiCleanupSteps.length === 0
    },
    {
      key: 'correctedBoundary',
      label: '후보정 외곽선',
      description: '최종 외곽선',
      disabled: !hasBoundary
    }
  ]

  return (
    <div className={`boundary-controls ${isExpanded ? 'expanded' : 'collapsed'}`}>
      <div className="controls-header" onClick={() => setIsExpanded(!isExpanded)}>
        <h3>Boundary Review</h3>
        <button className="toggle-expand">
          {isExpanded ? '▼' : '▶'}
        </button>
      </div>

      {isExpanded && (
        <div className="controls-content">
          <div className="controls-actions">
            <button
              className="detect-button"
              onClick={onPreprocess}
              disabled={!dxfFile || isPreprocessing || isDetecting}
            >
              {isPreprocessing ? 'Preprocessing...' : hasAnyResult ? 'Re-run Preprocess' : 'Run Preprocess'}
            </button>
            <button
              className="detect-button"
              onClick={onDetect}
              disabled={!dxfFile || !hasPreprocessed || isPreprocessing || isDetecting}
            >
              {isDetecting ? 'Detecting...' : hasBoundary ? 'Re-run Boundary' : 'Detect Boundary'}
            </button>
            {hasAnyResult && (
              <button className="clear-button" onClick={onClear}>
                Clear
              </button>
            )}
          </div>

          {hasAnyResult && (
            <div className="control-section">
              <div className="section-title">1. 베이스 도면</div>
              <div className="segmented-group">
                <button
                  className={`segment-button ${baseViewMode === 'original' ? 'active' : ''}`}
                  onClick={() => onChangeBaseViewMode?.('original')}
                >
                  기본 도면
                </button>
                <button
                  className={`segment-button ${baseViewMode === 'preprocessed' ? 'active' : ''}`}
                  onClick={() => onChangeBaseViewMode?.('preprocessed')}
                  disabled={!hasPreprocessed}
                >
                  전처리 도면
                </button>
              </div>
            </div>
          )}

          {hasBoundary && (
            <div className="control-section">
              <div className="section-title">2. 오버레이</div>
              <div className="toggle-list">
                {overlayItems.map((item) => (
                  <button
                    key={item.key}
                    className={`toggle-card ${overlayVisibility[item.key] ? 'active' : ''}`}
                    onClick={() => onToggleOverlay?.(item.key)}
                    disabled={item.disabled}
                  >
                    <span className="toggle-card-title">{item.label}</span>
                    <span className="toggle-card-desc">{item.description}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {aiCleanupSteps.length > 0 && (
            <div className="control-section">
              <div className="section-title">3. AI Step</div>
              <div className="step-actions">
                <button
                  className={`step-filter-button ${selectedAiCleanupStepIndex === null ? 'active' : ''}`}
                  onClick={() => onSelectAiCleanupStep?.(null)}
                >
                  전체 결과
                </button>
              </div>
              <div className="step-list">
                {aiCleanupSteps.map((step, index) => (
                  <button
                    key={step.id}
                    className={`step-card ${selectedAiCleanupStepIndex === index ? 'active' : ''}`}
                    onClick={() => onSelectAiCleanupStep?.(index)}
                  >
                    <div className="step-card-top">
                      <span className="step-card-title">Step {index + 1}</span>
                      <span className={`step-card-badge ${step.decision}`}>
                        {step.decision}
                      </span>
                    </div>
                    <div className="step-card-row">
                      <span className="step-card-label">Kind</span>
                      <span className="step-card-value">{step.kind}</span>
                    </div>
                    <div className="step-card-row">
                      <span className="step-card-label">Confidence</span>
                      <span className="step-card-value">{formatNumber((step.confidence ?? 0) * 100, 1)}%</span>
                    </div>
                    {step.reason && (
                      <div className="step-card-reason">{step.reason}</div>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}

          {error && (
            <div className="error-message">
              <strong>Error:</strong> {error}
            </div>
          )}

          {metadata && (
            <div className="metadata-display compact">
              <div className="section-title">요약</div>
              <div className="metadata-grid">
                <div className="metadata-item">
                  <span className="metadata-label">처리 시간</span>
                  <span className="metadata-value">{formatTime(metadata.processing_time_ms)}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">신뢰도</span>
                  <span className={`metadata-value ${metadata.confidence >= 0.8 ? 'high' : metadata.confidence >= 0.5 ? 'medium' : 'low'}`}>
                    {metadata.confidence !== undefined ? `${formatNumber(metadata.confidence * 100, 1)}%` : 'N/A'}
                  </span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">면적</span>
                  <span className="metadata-value">{formatArea(metadata.area, metadata.area_unit)}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">둘레</span>
                  <span className="metadata-value">{formatLength(metadata.perimeter, metadata.perimeter_unit)}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">외곽선 vertex</span>
                  <span className="metadata-value">{metadata.exterior_vertex_count ?? 'N/A'}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">AI Steps</span>
                  <span className="metadata-value">{aiCleanupSteps.length}</span>
                </div>
              </div>
            </div>
          )}

          {!hasAnyResult && !metadata && (
            <div className="help-text">
              <p>DXF를 불러온 뒤 전처리를 먼저 보고, 그 다음 외곽선을 검출하세요.</p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default BoundaryControls
