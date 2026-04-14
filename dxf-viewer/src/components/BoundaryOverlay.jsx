import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { Line2 } from 'three/examples/jsm/lines/Line2.js'
import { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js'
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'

function BoundaryOverlay({
  viewer,
  boundary,
  visible = true,
  showInteriors = false,
  colors = { exterior: '#0b3ea8', interior: '#0b3ea8' }
}) {
  const overlayGroupRef = useRef(null)

  useEffect(() => {
    if (!viewer || !boundary) {
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

    const buildLine = (points, color, opacity) => {
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
        linewidth: 4.8
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

    const exteriorLine = buildLine(boundary.exterior, colors.exterior, 0.9)
    if (exteriorLine) {
      overlayGroup.add(exteriorLine)
    }

    if (showInteriors) {
      for (const interior of boundary.interiors ?? []) {
        const interiorLine = buildLine(interior, colors.interior, 0.8)
        if (interiorLine) {
          overlayGroup.add(interiorLine)
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
  }, [boundary, colors.exterior, colors.interior, showInteriors, viewer])

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
