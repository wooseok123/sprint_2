import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { Line2 } from 'three/examples/jsm/lines/Line2.js'
import { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js'
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'

function BoundaryOverlay({
  viewer,
  initialBoundary = null,
  correctedBoundary = null,
  aiCleanupHighlights = [],
  aiCleanupStep = null,
  showInitialBoundary = false,
  showAiDetections = false,
  showCorrectedBoundary = true,
  visible = true,
  colors = {
    initial: '#64748b',
    corrected: '#0b3ea8',
    aiRemove: '#dc2626',
    aiKeep: '#f59e0b',
    aiUncertain: '#6b7280',
    aiHighlight: '#dc2626'
  }
}) {
  const overlayGroupRef = useRef(null)

  useEffect(() => {
    if (!viewer) {
      return undefined
    }

    const scene = viewer.GetScene()
    const origin = viewer.GetOrigin()

    if (!scene) {
      return undefined
    }

    const overlayGroup = new THREE.Group()
    overlayGroup.name = 'boundary-overlay'
    overlayGroup.visible = visible
    overlayGroup.renderOrder = 999
    const fatLineMaterials = []

    if (origin) {
      overlayGroup.position.set(-origin.x, -origin.y, 0)
    }

    const buildLine = (points, color, opacity, linewidth = 4.8) => {
      if (!points || points.length < 2) {
        return null
      }

      const normalizedPoints = [...points]
      const firstPoint = normalizedPoints[0]
      const lastPoint = normalizedPoints[normalizedPoints.length - 1]

      if (
        normalizedPoints.length > 2 &&
        firstPoint[0] === lastPoint[0] &&
        firstPoint[1] === lastPoint[1]
      ) {
        normalizedPoints.pop()
      }

      const linePoints = [...normalizedPoints]
      if (linePoints.length > 2) {
        linePoints.push([...linePoints[0]])
      }

      const geometry = new LineGeometry()
      geometry.setPositions(linePoints.flatMap(([x, y]) => [x, y, 0]))

      const material = new LineMaterial({
        color,
        transparent: opacity < 1,
        opacity,
        depthTest: false,
        depthWrite: false,
        linewidth
      })

      fatLineMaterials.push(material)
      const line = new Line2(geometry, material)
      line.computeLineDistances()
      line.renderOrder = 999
      return line
    }

    const updateResolution = () => {
      const renderer = viewer.GetRenderer()
      const canvas = renderer?.domElement
      const width = canvas?.clientWidth || canvas?.width || 1
      const height = canvas?.clientHeight || canvas?.height || 1

      for (const material of fatLineMaterials) {
        material.resolution.set(width, height)
      }
    }

    const activeInitialBoundary = aiCleanupStep?.beforeBoundary ?? initialBoundary
    const activeCorrectedBoundary = aiCleanupStep?.afterBoundary ?? correctedBoundary

    if (showInitialBoundary) {
      const initialLine = buildLine(activeInitialBoundary, colors.initial, 0.74, 4.0)
      if (initialLine) {
        overlayGroup.add(initialLine)
      }
    }

    if (showCorrectedBoundary) {
      const correctedLine = buildLine(activeCorrectedBoundary, colors.corrected, 0.94, 5.0)
      if (correctedLine) {
        overlayGroup.add(correctedLine)
      }
    }

    const aiColorByDecision = {
      remove: colors.aiRemove,
      keep: colors.aiKeep,
      uncertain: colors.aiUncertain
    }

    if (showAiDetections) {
      const visibleHighlights = aiCleanupStep
        ? [{
            decision: aiCleanupStep.decision,
            ring: aiCleanupStep.highlightRing
          }]
        : aiCleanupHighlights

      for (const highlight of visibleHighlights) {
        const aiLine = buildLine(
          highlight.ring,
          aiColorByDecision[highlight.decision] ?? colors.aiHighlight ?? colors.aiUncertain,
          highlight.decision === 'remove' ? 0.95 : 0.82,
          highlight.decision === 'remove' ? 6.8 : 5.2
        )

        if (aiLine) {
          overlayGroup.add(aiLine)
        }
      }
    }

    scene.add(overlayGroup)
    overlayGroupRef.current = overlayGroup
    updateResolution()
    viewer.Subscribe('resized', updateResolution)
    viewer.Render()

    return () => {
      viewer.Unsubscribe('resized', updateResolution)
      scene.remove(overlayGroup)
      overlayGroup.traverse((object) => {
        if (object.geometry) {
          object.geometry.dispose()
        }
        if (object.material) {
          object.material.dispose()
        }
      })
      overlayGroupRef.current = null
      viewer.Render()
    }
  }, [
    aiCleanupStep,
    aiCleanupHighlights,
    colors.aiKeep,
    colors.aiHighlight,
    colors.aiRemove,
    colors.aiUncertain,
    colors.corrected,
    colors.initial,
    correctedBoundary,
    initialBoundary,
    showAiDetections,
    showCorrectedBoundary,
    showInitialBoundary,
    viewer
  ])

  useEffect(() => {
    if (!overlayGroupRef.current || !viewer) {
      return
    }

    overlayGroupRef.current.visible = visible
    viewer.Render()
  }, [viewer, visible])

  return null
}

export default BoundaryOverlay
