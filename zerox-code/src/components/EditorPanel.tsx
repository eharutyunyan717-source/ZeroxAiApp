import { useState, useEffect } from 'react'
import Editor from '@monaco-editor/react'
import { X, Split } from 'lucide-react'
import { cn } from '../utils/cn'
import { File as FileType } from '../types'

interface EditorPanelProps {
  activeFile: FileType | null
  openFiles: FileType[]
  onCloseFile: (file: FileType) => void
  onSetActiveFile: (file: FileType) => void
  onSaveFile: (file: FileType, content: string) => void
  showAI: boolean
  onResizeAI: (width: number) => void
}

export default function EditorPanel({
  activeFile,
  openFiles,
  onCloseFile,
  onSetActiveFile,
  onSaveFile,
  showAI,
  onResizeAI
}: EditorPanelProps) {
  const [editorContent, setEditorContent] = useState('')
  const [isResizing, setIsResizing] = useState(false)

  useEffect(() => {
    if (activeFile) {
      setEditorContent(activeFile.content)
    }
  }, [activeFile])

  const handleEditorChange = (value: string | undefined) => {
    if (value !== undefined) {
      setEditorContent(value)
    }
  }

  const handleSave = () => {
    if (activeFile) {
      onSaveFile(activeFile, editorContent)
    }
  }

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault()
        handleSave()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [activeFile, editorContent])

  const handleMouseDown = (e: React.MouseEvent) => {
    setIsResizing(true)
    e.preventDefault()
  }

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (isResizing) {
        const containerWidth = window.innerWidth - 250
        const newWidth = containerWidth - e.clientX
        if (newWidth >= 250 && newWidth <= 600) {
          onResizeAI(newWidth)
        }
      }
    }

    const handleMouseUp = () => {
      setIsResizing(false)
    }

    if (isResizing) {
      document.addEventListener('mousemove', handleMouseMove)
      document.addEventListener('mouseup', handleMouseUp)
    }

    return () => {
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
    }
  }, [isResizing])

  return (
    <div className="flex-1 flex flex-col bg-white overflow-hidden">
      <div className="h-10 border-b border-border flex items-center bg-background">
        <div className="flex items-center overflow-x-auto scrollbar-thin">
          {openFiles.map((file) => (
            <div
              key={file.path}
              className={cn(
                'flex items-center gap-2 px-3 py-2 border-r border-border cursor-pointer transition-colors min-w-max',
                activeFile?.path === file.path ? 'bg-white border-b-2 border-b-accent' : 'hover:bg-white/50'
              )}
              onClick={() => onSetActiveFile(file)}
            >
              <span className="text-sm text-text truncate max-w-32">{file.name}</span>
              {file.modified && <span className="w-2 h-2 bg-accent rounded-full" />}
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  onCloseFile(file)
                }}
                className="p-0.5 hover:bg-background rounded transition-colors"
              >
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
        
        <div className="ml-auto flex items-center gap-1 px-2">
          <button className="p-1.5 hover:bg-background rounded transition-colors">
            <Split size={16} />
          </button>
        </div>
      </div>
      
      <div className="flex-1 flex overflow-hidden">
        <div className="flex-1 overflow-hidden">
          {activeFile ? (
            <Editor
              height="100%"
              language={activeFile.language}
              value={editorContent}
              onChange={handleEditorChange}
              theme="vs-light"
              options={{
                minimap: { enabled: true },
                fontSize: 14,
                lineNumbers: 'on',
                roundedSelection: false,
                scrollBeyondLastLine: false,
                automaticLayout: true,
                tabSize: 2,
                wordWrap: 'on',
                folding: true,
                foldingStrategy: 'auto',
                showFoldingControls: 'always',
                formatOnPaste: true,
                formatOnType: true,
                autoIndent: 'full',
                suggest: {
                  showKeywords: true,
                  showSnippets: true,
                },
              }}
            />
          ) : (
            <div className="h-full flex items-center justify-center text-text/50">
              <div className="text-center">
                <p className="text-lg font-medium mb-2">No file open</p>
                <p className="text-sm">Open a file from the sidebar to start editing</p>
              </div>
            </div>
          )}
        </div>
        
        {showAI && (
          <div
            className="w-1 hover:bg-accent cursor-col-resize transition-colors"
            onMouseDown={handleMouseDown}
          />
        )}
      </div>
    </div>
  )
}
