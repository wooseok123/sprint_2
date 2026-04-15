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
const VIEW_MODES = {
  ORIGINAL: 'original',
  PREPROCESSED: 'preprocessed',
  BOUNDARY: 'boundary',
  OVERLAY: 'overlay'
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

function transformExtensionHighlightsToViewerUnits(metadata) {
  const extensions = metadata?.processing_details?.endpoint_extension?.applied_extensions

  if (!Array.isArray(extensions) || extensions.length === 0) {
    return []
  }

  const scale = metadata?.unit_scale_to_mm ?? DEFAULT_UNIT_SCALE_TO_MM
  const normalizePoint = (point) => {
    if (!Array.isArray(point) || point.length < 2) {
      return null
    }

    if (!Number.isFinite(scale) || scale === 0 || scale === 1) {
      return [point[0], point[1]]
    }

    return [point[0] / scale, point[1] / scale]
  }

  return extensions
    .map((item) => {
      const fromPoint = normalizePoint(item.from_point)
      const toPoint = normalizePoint(item.to_point)

      if (!fromPoint || !toPoint) {
        return null
      }

      return {
        ...item,
        fromPoint,
        toPoint
      }
    })
    .filter(Boolean)
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

function bboxToSegments(bbox, scale = DEFAULT_UNIT_SCALE_TO_MM) {
  if (!bbox) {
    return []
  }

  const normalize = (value) => (!Number.isFinite(scale) || scale === 0 || scale === 1 ? value : value / scale)
  const minX = normalize(bbox.minX)
  const minY = normalize(bbox.minY)
  const maxX = normalize(bbox.maxX)
  const maxY = normalize(bbox.maxY)

  return [
    [[minX, minY], [maxX, minY]],
    [[maxX, minY], [maxX, maxY]],
    [[maxX, maxY], [minX, maxY]],
    [[minX, maxY], [minX, minY]]
  ]
}

function transformPreprocessingDebugToViewerUnits(metadata) {
  const scale = metadata?.unit_scale_to_mm ?? DEFAULT_UNIT_SCALE_TO_MM
  const preprocessing = metadata?.processing_details?.preprocessing

  return {
    workAreaSegments: bboxToSegments(preprocessing?.work_area_bbox, scale),
    titleBlockSegments: bboxToSegments(preprocessing?.title_block_bbox, scale),
    titleBlockCandidateSegments: bboxToSegments(preprocessing?.title_block_candidate_bbox, scale)
  }
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
  const [viewMode, setViewMode] = useState(VIEW_MODES.ORIGINAL)

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
    setViewMode(VIEW_MODES.ORIGINAL)
  }, [dxfFile])

  useEffect(() => {
    if (!viewerRef.current) {
      return
    }
    applyDrawingVisibility(
      viewerRef.current,
      viewMode !== VIEW_MODES.PREPROCESSED && viewMode !== VIEW_MODES.OVERLAY
    )
  }, [applyDrawingVisibility, viewMode])

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
        extensionHighlights: transformExtensionHighlightsToViewerUnits(result.metadata),
        preprocessingDebug: transformPreprocessingDebugToViewerUnits(result.metadata)
      })
      setViewMode(VIEW_MODES.PREPROCESSED)
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
      setProcessingData({
        ...result,
        boundary: transformBoundaryToViewerUnits(result.boundary, result.metadata),
        preprocessed: transformPreprocessedToViewerUnits(result.preprocessed, result.metadata),
        extensionHighlights: transformExtensionHighlightsToViewerUnits(result.metadata),
        preprocessingDebug: transformPreprocessingDebugToViewerUnits(result.metadata)
      })
      setViewMode(VIEW_MODES.BOUNDARY)
      console.log('Boundary detected:', result)
    } catch (err) {
      console.error('Boundary detection failed:', err)
      setError(err.message || 'Failed to detect boundary')
    } finally {
      setIsDetecting(false)
    }
  }, [dxfFile])

  const handleChangeViewMode = useCallback((nextMode) => {
    setViewMode(nextMode)
  }, [])

  const handleClearBoundary = useCallback(() => {
    setProcessingData(null)
    setError(null)
    setViewMode(VIEW_MODES.ORIGINAL)
  }, [])

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
        viewMode={viewMode}
        onChangeViewMode={handleChangeViewMode}
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
          visible={viewMode === VIEW_MODES.PREPROCESSED}
          name="preprocessed-overlay"
          color="#147d64"
          opacity={0.96}
        />
      )}

      {viewerRef.current && processingData?.preprocessingDebug?.workAreaSegments?.length > 0 && (
        <SegmentOverlay
          viewer={viewerRef.current}
          segments={processingData.preprocessingDebug.workAreaSegments}
          visible={viewMode === VIEW_MODES.PREPROCESSED}
          name="work-area-overlay"
          color="#2563eb"
          opacity={0.9}
        />
      )}

      {viewerRef.current && processingData?.preprocessingDebug?.titleBlockCandidateSegments?.length > 0 && (
        <SegmentOverlay
          viewer={viewerRef.current}
          segments={processingData.preprocessingDebug.titleBlockCandidateSegments}
          visible={viewMode === VIEW_MODES.PREPROCESSED}
          name="title-block-candidate-overlay"
          color="#f59e0b"
          opacity={0.88}
        />
      )}

      {viewerRef.current && processingData?.preprocessingDebug?.titleBlockSegments?.length > 0 && (
        <SegmentOverlay
          viewer={viewerRef.current}
          segments={processingData.preprocessingDebug.titleBlockSegments}
          visible={viewMode === VIEW_MODES.PREPROCESSED}
          name="title-block-overlay"
          color="#dc2626"
          opacity={0.92}
        />
      )}

      {/* Boundary overlay rendered in the same Three.js scene as the DXF */}
      {viewerRef.current && processingData?.boundary && (
        <BoundaryOverlay
          viewer={viewerRef.current}
          boundary={processingData.boundary}
          extensionHighlights={processingData.extensionHighlights}
          visible={viewMode === VIEW_MODES.BOUNDARY || viewMode === VIEW_MODES.OVERLAY}
          showInteriors={false}
          colors={{ exterior: '#0B3EA8', interior: '#0B3EA8', extension: '#FF7A00' }}
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
