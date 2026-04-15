import { useRef, useEffect, useState, useCallback } from 'react'
import { DxfViewer } from 'dxf-viewer'
import * as THREE from 'three'
import DxfViewerWorker from './DxfViewerWorker.js?worker'
import BoundaryOverlay from './BoundaryOverlay'
import SegmentOverlay from './SegmentOverlay'
import BoundaryControls from './BoundaryControls'
import { detectBoundary, preprocessDrawing } from '../services/boundaryApi'
import './DxfViewer.css'

const VIEW_PADDING = 0.08
const DEFAULT_UNIT_SCALE_TO_MM = 1
const BASE_VIEW_MODES = {
  ORIGINAL: 'original',
  PREPROCESSED: 'preprocessed'
}

function transformBoundaryToViewerUnits(boundary, metadata) {
  if (!boundary) {
    return boundary
  }

  const scale = metadata?.unit_scale_to_mm ?? DEFAULT_UNIT_SCALE_TO_MM
  if (!Number.isFinite(scale) || scale === 0 || scale === 1) {
    return boundary
  }

  const rescaleRing = (ring) => ring.map(([x, y]) => [x / scale, y / scale])

  return {
    exterior: rescaleRing(boundary.exterior),
    interiors: (boundary.interiors ?? []).map(rescaleRing)
  }
}

function transformPreprocessedToViewerUnits(preprocessed, metadata) {
  if (!preprocessed?.segments) {
    return preprocessed
  }

  const scale = metadata?.unit_scale_to_mm ?? DEFAULT_UNIT_SCALE_TO_MM
  if (!Number.isFinite(scale) || scale === 0 || scale === 1) {
    return preprocessed
  }

  return {
    ...preprocessed,
    segments: preprocessed.segments.map(([start, end]) => [
      [start[0] / scale, start[1] / scale],
      [end[0] / scale, end[1] / scale]
    ])
  }
}

function transformVisionCleanupHighlightsToViewerUnits(metadata) {
  const attempts = metadata?.processing_details?.outline_extraction?.vision_cleanup?.attempts

  if (!Array.isArray(attempts) || attempts.length === 0) {
    return []
  }

  const scale = metadata?.unit_scale_to_mm ?? DEFAULT_UNIT_SCALE_TO_MM
  const normalize = (value) => (!Number.isFinite(scale) || scale === 0 || scale === 1 ? value : value / scale)

  return attempts
    .map((attempt, index) => {
      const highlightRing = attempt?.highlight_ring
      const normalizedRing = Array.isArray(highlightRing) && highlightRing.length >= 3
        ? highlightRing.map((point) => [normalize(point[0]), normalize(point[1])])
        : null
      const bounds = attempt?.bounds
      if ((!Array.isArray(bounds) || bounds.length < 4) && !normalizedRing) {
        return null
      }

      const ring = normalizedRing ?? (() => {
        const [minX, minY, maxX, maxY] = bounds.map(normalize)
        return [
          [minX, minY],
          [maxX, minY],
          [maxX, maxY],
          [minX, maxY],
          [minX, minY]
        ]
      })()

      return {
        id: `${attempt.source ?? 'candidate'}-${attempt.kind ?? 'unknown'}-${index}`,
        decision: attempt.decision ?? 'uncertain',
        confidence: attempt.confidence ?? 0,
        source: attempt.source ?? 'unknown',
        kind: attempt.kind ?? 'unknown',
        reason: attempt.reason ?? '',
        ring
      }
    })
    .filter(Boolean)
}

function transformCleanupBoundariesToViewerUnits(metadata, fallbackBoundary = null) {
  const cleanup = metadata?.processing_details?.outline_extraction?.vision_cleanup
  const scale = metadata?.unit_scale_to_mm ?? DEFAULT_UNIT_SCALE_TO_MM
  const normalize = (value) => (!Number.isFinite(scale) || scale === 0 || scale === 1 ? value : value / scale)
  const normalizeRing = (ring) => (
    Array.isArray(ring) && ring.length >= 2
      ? ring.map((point) => [normalize(point[0]), normalize(point[1])])
      : null
  )

  return {
    initialBoundary: normalizeRing(cleanup?.initial_boundary_exterior),
    correctedBoundary: normalizeRing(cleanup?.final_boundary_exterior)
      ?? fallbackBoundary?.exterior
      ?? null
  }
}

function transformAiCleanupStepsToViewerUnits(metadata) {
  const attempts = metadata?.processing_details?.outline_extraction?.vision_cleanup?.attempts

  if (!Array.isArray(attempts) || attempts.length === 0) {
    return []
  }

  const scale = metadata?.unit_scale_to_mm ?? DEFAULT_UNIT_SCALE_TO_MM
  const normalize = (value) => (!Number.isFinite(scale) || scale === 0 || scale === 1 ? value : value / scale)
  const normalizeRing = (ring) => (
    Array.isArray(ring) && ring.length >= 2
      ? ring.map((point) => [normalize(point[0]), normalize(point[1])])
      : null
  )

  return attempts.map((attempt, index) => ({
    id: `${attempt.source ?? 'candidate'}-${attempt.kind ?? 'unknown'}-${index}`,
    stepIndex: index,
    decision: attempt.decision ?? 'uncertain',
    confidence: attempt.confidence ?? 0,
    source: attempt.source ?? 'unknown',
    kind: attempt.kind ?? 'unknown',
    reason: attempt.reason ?? '',
    highlightRing: normalizeRing(attempt.highlight_ring),
    beforeBoundary: normalizeRing(attempt.before_boundary_exterior),
    afterBoundary: normalizeRing(attempt.after_boundary_exterior)
  }))
}

function DxfViewerComponent({ dxfFile }) {
  const containerRef = useRef(null)
  const viewerRef = useRef(null)
  const [bounds, setBounds] = useState(null)

  // Boundary detection state
  const [processingData, setProcessingData] = useState(null)
  const [isPreprocessing, setIsPreprocessing] = useState(false)
  const [isDetecting, setIsDetecting] = useState(false)
  const [error, setError] = useState(null)
  const [baseViewMode, setBaseViewMode] = useState(BASE_VIEW_MODES.ORIGINAL)
  const [selectedAiCleanupStepIndex, setSelectedAiCleanupStepIndex] = useState(null)
  const [overlayVisibility, setOverlayVisibility] = useState({
    initialBoundary: true,
    aiDetections: true,
    correctedBoundary: true
  })

  const fitViewerToBounds = useCallback((viewer, nextBounds) => {
    if (!viewer || !nextBounds) {
      return
    }

    const origin = viewer.GetOrigin()
    const offsetX = origin?.x ?? 0
    const offsetY = origin?.y ?? 0

    viewer.FitView(
      nextBounds.minX - offsetX,
      nextBounds.maxX - offsetX,
      nextBounds.minY - offsetY,
      nextBounds.maxY - offsetY,
      VIEW_PADDING
    )
  }, [])

  const applyLayerVisibility = useCallback((viewer) => {
    const dxf = viewer?.GetDxf()
    const layers = dxf?.tables?.layer?.layers

    if (!layers) {
      return
    }

    for (const layer of Object.values(layers)) {
      if (layer?.visible === false) {
        viewer.ShowLayer(layer.name, false)
      }
    }
  }, [])

  const applyDrawingVisibility = useCallback((viewer, showDrawing) => {
    const dxf = viewer?.GetDxf()
    const scene = viewer?.GetScene()
    const layers = dxf?.tables?.layer?.layers
    const isOverlayObject = (object) => {
      let current = object
      while (current) {
        if (
          current.name === 'boundary-overlay'
          || current.name === 'preprocessed-overlay'
          || current.name === 'work-area-overlay'
          || current.name === 'title-block-overlay'
          || current.name === 'title-block-candidate-overlay'
        ) {
          return true
        }
        current = current.parent
      }
      return false
    }

    if (viewer?.layers instanceof Map && layers) {
      for (const layer of viewer.layers.values()) {
        const layerDefinition = layers[layer.name]
        const shouldShow = showDrawing && layerDefinition?.visible !== false
        for (const obj of layer.objects) {
          obj.visible = shouldShow
        }
      }
      viewer.Render()
      return
    }

    if (!scene) {
      return
    }

    scene.traverse((object) => {
      if (!object.parent || isOverlayObject(object)) {
        return
      }
      if (object.isScene || object.isCamera || object.isLight) {
        return
      }
      object.visible = showDrawing
    })
    viewer.Render()
  }, [])

  useEffect(() => {
    if (!containerRef.current || !dxfFile) {
      return
    }

    // Create viewer instance
    const viewer = new DxfViewer(containerRef.current, {
      autoResize: true,
      clearColor: new THREE.Color(0xffffff),
      antialias: true,
      colorCorrection: true,
      suppressPaperSpace: true
    })
    viewerRef.current = viewer

    // Create blob URL from file
    const url = URL.createObjectURL(dxfFile)

    // Subscribe to events first
    const onLoaded = () => {
      console.log('DXF loaded successfully')
      applyLayerVisibility(viewer)

      // Get bounds after loading
      const fileBounds = viewer.GetBounds()
      console.log('Bounds:', fileBounds)
      if (fileBounds) {
        setBounds(fileBounds)

        // Wait for the container to settle so the initial fit uses the final viewport size.
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            fitViewerToBounds(viewer, fileBounds)
            applyDrawingVisibility(viewer, true)
            viewer.Render()
          })
        })
      }
    }

    viewer.Subscribe('loaded', onLoaded)

    // Load DXF file with Vite worker
    viewer.Load({
      url,
      workerFactory: () => new DxfViewerWorker()
    })
      .catch((error) => {
        console.error('Error loading DXF:', error)
      })

    // Cleanup
    return () => {
      URL.revokeObjectURL(url)
      viewer.Unsubscribe('loaded', onLoaded)
      viewer.Destroy()
    }
  }, [applyDrawingVisibility, applyLayerVisibility, dxfFile, fitViewerToBounds])

  // Reset boundary data when file changes
  useEffect(() => {
    setProcessingData(null)
    setError(null)
    setBaseViewMode(BASE_VIEW_MODES.ORIGINAL)
    setSelectedAiCleanupStepIndex(null)
    setOverlayVisibility({
      initialBoundary: true,
      aiDetections: true,
      correctedBoundary: true
    })
  }, [dxfFile])

  useEffect(() => {
    if (!viewerRef.current) {
      return
    }
    applyDrawingVisibility(
      viewerRef.current,
      baseViewMode !== BASE_VIEW_MODES.PREPROCESSED
    )
  }, [applyDrawingVisibility, baseViewMode])

  const handleResetView = () => {
    if (viewerRef.current && bounds) {
      fitViewerToBounds(viewerRef.current, bounds)
    }
  }

  const handlePreprocess = useCallback(async () => {
    if (!dxfFile) return

    setIsPreprocessing(true)
    setError(null)

    try {
      const result = await preprocessDrawing(dxfFile)
      setProcessingData({
        boundary: null,
        preprocessed: transformPreprocessedToViewerUnits(result.preprocessed, result.metadata),
        metadata: result.metadata,
        aiCleanupHighlights: transformVisionCleanupHighlightsToViewerUnits(result.metadata),
        aiCleanupSteps: transformAiCleanupStepsToViewerUnits(result.metadata),
        cleanupBoundaries: transformCleanupBoundariesToViewerUnits(result.metadata)
      })
      setSelectedAiCleanupStepIndex(null)
      setBaseViewMode(BASE_VIEW_MODES.PREPROCESSED)
      console.log('Preprocessing complete:', result)
    } catch (err) {
      console.error('Preprocessing failed:', err)
      setError(err.message || 'Failed to preprocess drawing')
    } finally {
      setIsPreprocessing(false)
    }
  }, [dxfFile])

  const handleDetectBoundary = useCallback(async () => {
    if (!dxfFile) return

    setIsDetecting(true)
    setError(null)

    try {
      const result = await detectBoundary(dxfFile)
      const boundary = transformBoundaryToViewerUnits(result.boundary, result.metadata)
      setProcessingData({
        ...result,
        boundary,
        preprocessed: transformPreprocessedToViewerUnits(result.preprocessed, result.metadata),
        aiCleanupHighlights: transformVisionCleanupHighlightsToViewerUnits(result.metadata),
        aiCleanupSteps: transformAiCleanupStepsToViewerUnits(result.metadata),
        cleanupBoundaries: transformCleanupBoundariesToViewerUnits(result.metadata, boundary)
      })
      setSelectedAiCleanupStepIndex(null)
      setBaseViewMode(BASE_VIEW_MODES.ORIGINAL)
      console.log('Boundary detected:', result)
    } catch (err) {
      console.error('Boundary detection failed:', err)
      setError(err.message || 'Failed to detect boundary')
    } finally {
      setIsDetecting(false)
    }
  }, [dxfFile])

  const handleChangeBaseViewMode = useCallback((nextMode) => {
    setBaseViewMode(nextMode)
  }, [])

  const handleToggleOverlay = useCallback((overlayKey) => {
    setOverlayVisibility((current) => ({
      ...current,
      [overlayKey]: !current[overlayKey]
    }))
  }, [])

  const handleClearBoundary = useCallback(() => {
    setProcessingData(null)
    setError(null)
    setBaseViewMode(BASE_VIEW_MODES.ORIGINAL)
    setSelectedAiCleanupStepIndex(null)
    setOverlayVisibility({
      initialBoundary: true,
      aiDetections: true,
      correctedBoundary: true
    })
  }, [])

  const handleSelectAiCleanupStep = useCallback((stepIndex) => {
    setSelectedAiCleanupStepIndex(stepIndex)
  }, [])

  const selectedAiCleanupStep = selectedAiCleanupStepIndex === null
    ? null
    : processingData?.aiCleanupSteps?.[selectedAiCleanupStepIndex] ?? null

  return (
    <div className="dxf-viewer">
      {/* Boundary Controls */}
      <BoundaryControls
        dxfFile={dxfFile}
        onPreprocess={handlePreprocess}
        onDetect={handleDetectBoundary}
        isPreprocessing={isPreprocessing}
        isDetecting={isDetecting}
        metadata={processingData ? {
          area: processingData.metadata?.area,
          area_unit: processingData.metadata?.area_unit,
          perimeter: processingData.metadata?.perimeter,
          perimeter_unit: processingData.metadata?.perimeter_unit,
          confidence: processingData.metadata?.confidence,
          exterior_vertex_count: processingData.metadata?.exterior_vertex_count,
          interior_hole_count: processingData.boundary?.interiors?.length || 0,
          processing_time_ms: processingData.metadata?.processing_time_ms,
          convex_hull_ratio: processingData.metadata?.convex_hull_ratio,
          hatch_iou: processingData.metadata?.hatch_iou,
          processing_details: processingData.metadata?.processing_details
        } : null}
        error={error}
        hasBoundary={Boolean(processingData?.boundary?.exterior?.length)}
        hasPreprocessed={Boolean(processingData?.preprocessed?.segments?.length)}
        aiCleanupSteps={processingData?.aiCleanupSteps ?? []}
        selectedAiCleanupStepIndex={selectedAiCleanupStepIndex}
        onSelectAiCleanupStep={handleSelectAiCleanupStep}
        baseViewMode={baseViewMode}
        onChangeBaseViewMode={handleChangeBaseViewMode}
        overlayVisibility={overlayVisibility}
        onToggleOverlay={handleToggleOverlay}
        onClear={handleClearBoundary}
      />

      {/* Original Viewer Controls */}
      <div className="viewer-controls">
        <button onClick={handleResetView} className="control-button">
          Reset View
        </button>
        {bounds && (
          <span className="bounds-info">
            Bounds: ({bounds.minX.toFixed(2)}, {bounds.minY.toFixed(2)}) to (
            {bounds.maxX.toFixed(2)}, {bounds.maxY.toFixed(2)})
          </span>
        )}
      </div>

      <div className="viewer-stage">
        {/* Three.js Canvas Container */}
        <div ref={containerRef} className="viewer-container" />
      </div>

      {viewerRef.current && processingData?.preprocessed?.segments?.length > 0 && (
        <SegmentOverlay
          viewer={viewerRef.current}
          segments={processingData.preprocessed.segments}
          visible={baseViewMode === BASE_VIEW_MODES.PREPROCESSED}
          name="preprocessed-overlay"
          color="#147d64"
          opacity={0.96}
        />
      )}

      {viewerRef.current && (
        <BoundaryOverlay
          viewer={viewerRef.current}
          initialBoundary={processingData?.cleanupBoundaries?.initialBoundary}
          correctedBoundary={processingData?.cleanupBoundaries?.correctedBoundary}
          aiCleanupHighlights={processingData?.aiCleanupHighlights ?? []}
          aiCleanupStep={selectedAiCleanupStep}
          visible={
            overlayVisibility.initialBoundary
            || overlayVisibility.aiDetections
            || overlayVisibility.correctedBoundary
          }
          showInitialBoundary={overlayVisibility.initialBoundary}
          showAiDetections={overlayVisibility.aiDetections}
          showCorrectedBoundary={overlayVisibility.correctedBoundary}
          colors={{
            initial: '#64748B',
            corrected: '#0B3EA8',
            aiRemove: '#DC2626',
            aiKeep: '#F59E0B',
            aiUncertain: '#6B7280',
            aiHighlight: '#DC2626'
          }}
        />
      )}

      {/* Viewer Info */}
      <div className="viewer-info">
        <p>DXF Viewer by vagran</p>
        <p>Left-click + drag to rotate • Right-click + drag to pan • Scroll to zoom</p>
      </div>
    </div>
  )
}

export default DxfViewerComponent
