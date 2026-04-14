import { useState } from 'react'
import DxfUploader from './components/DxfUploader'
import DxfViewer from './components/DxfViewer'
import './App.css'

function App() {
  const [dxfFile, setDxfFile] = useState(null)
  const [fileName, setFileName] = useState(null)

  const handleFileLoad = (file, name) => {
    setDxfFile(file)
    setFileName(name)
  }

  const handleClear = () => {
    setDxfFile(null)
    setFileName(null)
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>DXF Viewer</h1>
        {fileName && (
          <div className="file-info">
            <span className="file-name">{fileName}</span>
            <button onClick={handleClear} className="clear-button">
              Clear
            </button>
          </div>
        )}
      </header>

      <main className="app-main">
        {!dxfFile ? (
          <DxfUploader onFileLoad={handleFileLoad} />
        ) : (
          <DxfViewer dxfFile={dxfFile} />
        )}
      </main>
    </div>
  )
}

export default App
