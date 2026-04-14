/**
 * Boundary Detection API Service
 *
 * Communicates with the backend FastAPI service.
 * Uses Vite proxy to avoid CORS issues.
 */

// Use relative URL for Vite proxy, fallback to direct connection
const API_BASE_URL = '/api' // Will be proxied to http://localhost:8000/api
const DETECT_ENDPOINT = `${API_BASE_URL}/detect-boundary`

/**
 * Detects the outer boundary in a DXF file
 *
 * @param {File} dxfFile - The DXF file to process
 * @returns {Promise<{boundary: {exterior: number[][], interiors: number[][][]}, metadata: object}>}
 * @throws {Error} If the API request fails or returns an error
 */
export async function detectBoundary(dxfFile) {
  try {
    // Create FormData for multipart/form-data upload
    const formData = new FormData()
    formData.append('file', dxfFile)

    // Send POST request to backend
    const response = await fetch(DETECT_ENDPOINT, {
      method: 'POST',
      body: formData,
      // Don't set Content-Type header - let the browser set it with the correct boundary
    })

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`)
    }

    const data = await response.json()

    // Check if the API reports success
    if (!data.success) {
      throw new Error(data.error || 'Boundary detection failed')
    }

    // Return the boundary data and metadata
    return {
      boundary: data.boundary, // { exterior: [[x,y]...], interiors: [[[x,y]...], ...] }
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
