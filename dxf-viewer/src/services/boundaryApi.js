/**
 * Boundary Detection API Service
 *
 * Communicates with the backend FastAPI service.
 * Uses Vite proxy to avoid CORS issues.
 */

// Use relative URL for Vite proxy, fallback to direct connection
const API_BASE_URL = '/api' // Will be proxied to http://localhost:8000/api
const DETECT_ENDPOINT = `${API_BASE_URL}/detect-boundary`
const PREPROCESS_ENDPOINT = `${API_BASE_URL}/preprocess-dxf`

async function postDxfFile(endpoint, dxfFile) {
  const formData = new FormData()
  formData.append('file', dxfFile)

  const response = await fetch(endpoint, {
    method: 'POST',
    body: formData
  })

  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`)
  }

  const data = await response.json()
  if (!data.success) {
    throw new Error(data.error || 'DXF processing failed')
  }

  return data
}

export async function preprocessDrawing(dxfFile) {
  try {
    const data = await postDxfFile(PREPROCESS_ENDPOINT, dxfFile)
    return {
      preprocessed: data.preprocessed,
      metadata: data.metadata
    }
  } catch (error) {
    if (error.name === 'TypeError' && error.message.includes('fetch')) {
      throw new Error(
        'Cannot connect to backend server. Make sure the server is running at http://localhost:8000'
      )
    }
    throw error
  }
}

/**
 * Detects the outer boundary in a DXF file
 *
 * @param {File} dxfFile - The DXF file to process
 * @returns {Promise<{boundary: {exterior: number[][], interiors: number[][][]}, preprocessed: {segments: number[][][]}, metadata: object}>}
 * @throws {Error} If the API request fails or returns an error
 */
export async function detectBoundary(dxfFile) {
  try {
    const data = await postDxfFile(DETECT_ENDPOINT, dxfFile)

    // Return the boundary data and metadata
    return {
      boundary: data.boundary, // { exterior: [[x,y]...], interiors: [[[x,y]...], ...] }
      preprocessed: data.preprocessed, // { segments: [[[x1,y1],[x2,y2]], ...] }
      metadata: data.metadata // { area, confidence, processing_time_ms, ... }
    }
  } catch (error) {
    // Enhance error messages for common issues
    if (error.name === 'TypeError' && error.message.includes('fetch')) {
      throw new Error(
        'Cannot connect to backend server. Make sure the server is running at http://localhost:8000'
      )
    }
    throw error
  }
}

/**
 * Checks if the backend API is healthy
 *
 * @returns {Promise<boolean>} True if the API is responsive
 */
export async function checkApiHealth() {
  try {
    // Use direct connection for health check (not proxied)
    const response = await fetch('http://localhost:8000/health')
    return response.ok
  } catch {
    return false
  }
}
