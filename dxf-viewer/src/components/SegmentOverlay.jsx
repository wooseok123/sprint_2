import { useEffect, useRef } from 'react'
import * as THREE from 'three'

function SegmentOverlay({
  viewer,
  segments = [],
  visible = true,
  name = 'preprocessed-overlay',
  color = '#0f766e',
  opacity = 0.95,
  linewidth = 1.5
}) {
  const overlayRef = useRef(null)

  useEffect(() => {
    if (!viewer || !Array.isArray(segments) || segments.length === 0) {
      return undefined
    }

    const scene = viewer.GetScene()
    const origin = viewer.GetOrigin()

    if (!scene) {
      return undefined
    }

    const positions = []
    for (const segment of segments) {
      if (!Array.isArray(segment) || segment.length < 2) {
        continue
      }

      const [start, end] = segment
      if (!Array.isArray(start) || !Array.isArray(end) || start.length < 2 || end.length < 2) {
        continue
      }

      positions.push(start[0], start[1], 0, end[0], end[1], 0)
    }

    if (positions.length === 0) {
      return undefined
    }

    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))

    const material = new THREE.LineBasicMaterial({
      color,
      transparent: opacity < 1,
      opacity,
      depthTest: false,
      depthWrite: false,
      linewidth
    })

    const overlay = new THREE.LineSegments(geometry, material)
    overlay.name = name
    overlay.visible = visible
    overlay.renderOrder = 998

    if (origin) {
      overlay.position.set(-origin.x, -origin.y, 0)
    }

    scene.add(overlay)
    overlayRef.current = overlay
    viewer.Render()

    return () => {
      scene.remove(overlay)
      geometry.dispose()
      material.dispose()
      overlayRef.current = null
      viewer.Render()
    }
  }, [color, linewidth, name, opacity, segments, viewer, visible])

  useEffect(() => {
    if (!overlayRef.current || !viewer) {
      return
    }

    overlayRef.current.visible = visible
    viewer.Render()
  }, [viewer, visible])

  return null
}

export default SegmentOverlay
