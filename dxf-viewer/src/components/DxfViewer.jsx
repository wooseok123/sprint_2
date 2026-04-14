import { useRef, useEffect, useState, useCallback } from 'react'
import { DxfViewer } from 'dxf-viewer'
import * as THREE from 'three'
import DxfViewerWorker from './DxfViewerWorker.js?worker'
import BoundaryOverlay from './BoundaryOverlay'
import BoundaryControls from './BoundaryControls'
import { detectBoundary } from '../services/boundaryApi'
import './DxfViewer.css'

const VIEW_PADDING = 0.08
const DEFAULT_UNIT_SCALE_TO_MM = 1

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

function DxfViewerComponent({ dxfFile }) {
  const containerRef = useRef(null)
  const viewerRef = useRef(null)
  const [bounds, setBounds] = useState(null)

  // Boundary detection state
  const [boundaryData, setBoundaryData] = useState(null)
  const [isDetecting, setIsDetecting] = useState(false)
  const [error, setError] = useState(null)
  const [drawingVisible, setDrawingVisible] = useState(true)
  const [overlayVisible, setOverlayVisible] = useState(true)

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
        if (current.name === 'boundary-overlay') {
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
    setBoundaryData(null)
    setError(null)
    setDrawingVisible(true)
    setOverlayVisible(true)
  }, [dxfFile])

  useEffect(() => {
    if (!viewerRef.current) {
      return
    }
    applyDrawingVisibility(viewerRef.current, drawingVisible)
  }, [applyDrawingVisibility, drawingVisible])

  const handleResetView = () => {
    if (viewerRef.current && bounds) {
      fitViewerToBounds(viewerRef.current, bounds)
    }
  }

  const handleDetectBoundary = useCallback(async () => {
    if (!dxfFile) return

    setIsDetecting(true)
    setError(null)

    try {
      const result = await detectBoundary(dxfFile)
      setBoundaryData({
        ...result,
        boundary: transformBoundaryToViewerUnits(result.boundary, result.metadata),
        extensionHighlights: transformExtensionHighlightsToViewerUnits(result.metadata)
      })
      console.log('Boundary detected:', result)
    } catch (err) {
      console.error('Boundary detection failed:', err)
      setError(err.message || 'Failed to detect boundary')
    } finally {
      setIsDetecting(false)
    }
  }, [dxfFile])

  const handleToggleOverlay = useCallback(() => {
    setOverlayVisible(prev => !prev)
  }, [])

  const handleToggleDrawing = useCallback(() => {
    setDrawingVisible(prev => !prev)
  }, [])

  const handleClearBoundary = useCallback(() => {
    setBoundaryData(null)
    setError(null)
    setDrawingVisible(true)
    setOverlayVisible(true)
  }, [])

  return (
    <div className="dxf-viewer">
      {/* Boundary Controls */}
      <BoundaryControls
        dxfFile={dxfFile}
        onDetect={handleDetectBoundary}
        isDetecting={isDetecting}
        metadata={boundaryData ? {
          area: boundaryData.metadata?.area,
          area_unit: boundaryData.metadata?.area_unit,
          perimeter: boundaryData.metadata?.perimeter,
          perimeter_unit: boundaryData.metadata?.perimeter_unit,
          confidence: boundaryData.metadata?.confidence,
          exterior_vertex_count: boundaryData.metadata?.exterior_vertex_count,
          interior_hole_count: boundaryData.boundary?.interiors?.length || 0,
          processing_time_ms: boundaryData.metadata?.processing_time_ms,
          convex_hull_ratio: boundaryData.metadata?.convex_hull_ratio,
          hatch_iou: boundaryData.metadata?.hatch_iou,
          processing_details: boundaryData.metadata?.processing_details
        } : null}
        error={error}
        hasBoundary={!!boundaryData}
        drawingVisible={drawingVisible}
        overlayVisible={overlayVisible}
        onToggleDrawing={handleToggleDrawing}
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

      {/* Boundary overlay rendered in the same Three.js scene as the DXF */}
      {viewerRef.current && boundaryData && (
        <BoundaryOverlay
          viewer={viewerRef.current}
          boundary={boundaryData.boundary}
          extensionHighlights={boundaryData.extensionHighlights}
          visible={overlayVisible}
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
