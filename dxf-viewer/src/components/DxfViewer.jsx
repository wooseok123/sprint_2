import { useRef, useEffect, useState, useCallback } from 'react'
import { DxfViewer } from 'dxf-viewer'
import * as THREE from 'three'
import DxfViewerWorker from './DxfViewerWorker.js?worker'
import BoundaryOverlay from './BoundaryOverlay'
import BoundaryControls from './BoundaryControls'
import { detectBoundary } from '../services/boundaryApi'
import './DxfViewer.css'

const VIEW_PADDING = 0.08

function DxfViewerComponent({ dxfFile }) {
  const containerRef = useRef(null)
  const viewerRef = useRef(null)
  const [bounds, setBounds] = useState(null)

  // Boundary detection state
  const [boundaryData, setBoundaryData] = useState(null)
  const [isDetecting, setIsDetecting] = useState(false)
  const [error, setError] = useState(null)
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

  useEffect(() => {
    if (!containerRef.current || !dxfFile) {
      return
    }

    // Create viewer instance
    const viewer = new DxfViewer(containerRef.current, {
      autoResize: true,
      clearColor: new THREE.Color(0xffffff),
      antialias: true,
      colorCorrection: true
    })
    viewerRef.current = viewer

    // Create blob URL from file
    const url = URL.createObjectURL(dxfFile)

    // Subscribe to events first
    const onLoaded = () => {
      console.log('DXF loaded successfully')
      // Get bounds after loading
      const fileBounds = viewer.GetBounds()
      console.log('Bounds:', fileBounds)
      if (fileBounds) {
        setBounds(fileBounds)

        // Wait for the container to settle so the initial fit uses the final viewport size.
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            fitViewerToBounds(viewer, fileBounds)
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
  }, [dxfFile, fitViewerToBounds])

  // Reset boundary data when file changes
  useEffect(() => {
    setBoundaryData(null)
    setError(null)
  }, [dxfFile])

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
      setBoundaryData(result)  // Store full result, not just boundary
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

  const handleClearBoundary = useCallback(() => {
    setBoundaryData(null)
    setError(null)
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
        overlayVisible={overlayVisible}
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
          visible={overlayVisible}
          colors={{ exterior: '#0B1F5C', interior: '#0B1F5C' }}
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
