import { useState } from 'react'
import Header from './components/Header'
import Sidebar from './components/Sidebar'
import EditorPanel from './components/EditorPanel'
import AIAssistant from './components/AIAssistant'
import Terminal from './components/Terminal'
import StatusBar from './components/StatusBar'
import CommandPalette from './components/CommandPalette'
import Settings from './components/Settings'
import { File } from './types'

function App() {
  const [openFiles, setOpenFiles] = useState<File[]>([])
  const [activeFile, setActiveFile] = useState<File | null>(null)
  const [showCommandPalette, setShowCommandPalette] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [sidebarWidth, setSidebarWidth] = useState(250)
  const [aiPanelWidth, setAiPanelWidth] = useState(350)
  const [terminalHeight, setTerminalHeight] = useState(200)
  const [showTerminal, setShowTerminal] = useState(true)
  const [showAI, setShowAI] = useState(true)

  const handleOpenFile = (file: File) => {
    if (!openFiles.find(f => f.path === file.path)) {
      setOpenFiles([...openFiles, file])
    }
    setActiveFile(file)
  }

  const handleCloseFile = (file: File) => {
    const newOpenFiles = openFiles.filter(f => f.path !== file.path)
    setOpenFiles(newOpenFiles)
    if (activeFile?.path === file.path) {
      setActiveFile(newOpenFiles[newOpenFiles.length - 1] || null)
    }
  }

  const handleSaveFile = async (file: File, content: string) => {
    if (window.electronAPI) {
      const result = await window.electronAPI.fs.writeFile(file.path, content)
      if (result.success) {
        setOpenFiles(openFiles.map(f => 
          f.path === file.path ? { ...f, content, modified: false } : f
        ))
      }
    }
  }

  return (
    <div className="h-screen flex flex-col bg-white">
      <Header 
        onCommandPalette={() => setShowCommandPalette(true)}
        onSettings={() => setShowSettings(true)}
      />
      
      <div className="flex-1 flex overflow-hidden">
        <Sidebar 
          width={sidebarWidth}
          onResize={setSidebarWidth}
          onOpenFile={handleOpenFile}
          openFiles={openFiles}
        />
        
        <EditorPanel
          activeFile={activeFile}
          openFiles={openFiles}
          onCloseFile={handleCloseFile}
          onSetActiveFile={setActiveFile}
          onSaveFile={handleSaveFile}
          showAI={showAI}
          onResizeAI={setAiPanelWidth}
        />
        
        {showAI && (
          <AIAssistant 
            width={aiPanelWidth}
            onResize={setAiPanelWidth}
            onClose={() => setShowAI(false)}
            activeFile={activeFile}
          />
        )}
      </div>
      
      {showTerminal && (
        <Terminal 
          height={terminalHeight}
          onResize={setTerminalHeight}
          onClose={() => setShowTerminal(false)}
        />
      )}
      
      <StatusBar 
        onToggleTerminal={() => setShowTerminal(!showTerminal)}
        onToggleAI={() => setShowAI(!showAI)}
        showTerminal={showTerminal}
        showAI={showAI}
      />
      
      {showCommandPalette && (
        <CommandPalette 
          onClose={() => setShowCommandPalette(false)}
          onOpenSettings={() => {
            setShowCommandPalette(false)
            setShowSettings(true)
          }}
        />
      )}
      
      {showSettings && (
        <Settings onClose={() => setShowSettings(false)} />
      )}
    </div>
  )
}

export default App
