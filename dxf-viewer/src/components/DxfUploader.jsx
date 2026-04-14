import { useRef } from 'react'
import './DxfUploader.css'

function DxfUploader({ onFileLoad }) {
  const fileInputRef = useRef(null)

  const handleFileChange = (event) => {
    const file = event.target.files[0]
    if (!file) return

    if (!file.name.toLowerCase().endsWith('.dxf')) {
      alert('Please select a DXF file')
      return
    }

    onFileLoad(file, file.name)
  }

  const handleDragOver = (e) => {
    e.preventDefault()
    e.stopPropagation()
  }

  const handleDrop = (e) => {
    e.preventDefault()
    e.stopPropagation()

    const file = e.dataTransfer.files[0]
    if (!file) return

    if (!file.name.toLowerCase().endsWith('.dxf')) {
      alert('Please drop a DXF file')
      return
    }

    onFileLoad(file, file.name)
  }

  const handleClick = () => {
    fileInputRef.current?.click()
  }

  return (
    <div
      className="dxf-uploader"
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      onClick={handleClick}
    >
      <input
        ref={fileInputRef}
        type="file"
        accept=".dxf"
        onChange={handleFileChange}
        style={{ display: 'none' }}
      />
      <div className="upload-icon">📁</div>
      <h2>Upload DXF File</h2>
      <p>Drag and drop a DXF file here, or click to select</p>
    </div>
  )
}

export default DxfUploader
