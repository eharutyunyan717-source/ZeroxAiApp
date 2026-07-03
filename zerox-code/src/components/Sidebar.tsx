import { useState, useEffect } from 'react'
import { ChevronRight, ChevronDown, Folder, FileCode, Plus, X } from 'lucide-react'
import { cn } from '../utils/cn'
import { File as FileType } from '../types'

interface SidebarProps {
  width: number
  onResize: (width: number) => void
  onOpenFile: (file: FileType) => void
  openFiles: FileType[]
}

export default function Sidebar({ width, onResize, onOpenFile, openFiles }: SidebarProps) {
  const [projectPath, setProjectPath] = useState<string>('')
  const [files, setFiles] = useState<any[]>([])
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set())
  const [isResizing, setIsResizing] = useState(false)

  useEffect(() => {
    loadProject()
  }, [])

  const loadProject = async () => {
    if (window.electronAPI) {
      const result = await window.electronAPI.dialog.openDirectory()
      if (result && !result.canceled && result.filePaths.length > 0) {
        const path = result.filePaths[0]
        setProjectPath(path)
        loadFiles(path)
      }
    }
  }

  const loadFiles = async (path: string) => {
    if (window.electronAPI) {
      const result = await window.electronAPI.fs.readDirectory(path)
      if (result.success && result.files) {
        setFiles(result.files)
      }
    }
  }

  const toggleFolder = (path: string) => {
    const newExpanded = new Set(expandedFolders)
    if (newExpanded.has(path)) {
      newExpanded.delete(path)
    } else {
      newExpanded.add(path)
      loadFiles(path)
    }
    setExpandedFolders(newExpanded)
  }

  const handleFileClick = async (file: any) => {
    if (file.isDirectory) {
      toggleFolder(file.path)
    } else {
      let content = ''
      if (window.electronAPI) {
        const result = await window.electronAPI.fs.readFile(file.path)
        if (result.success && result.content) {
          content = result.content
        }
      }
      const language = getLanguage(file.name)
      onOpenFile({
        name: file.name,
        path: file.path,
        content,
        language,
        modified: false
      })
    }
  }

  const getLanguage = (filename: string): string => {
    const ext = filename.split('.').pop()?.toLowerCase()
    const langMap: Record<string, string> = {
      'js': 'javascript',
      'jsx': 'javascript',
      'ts': 'typescript',
      'tsx': 'typescript',
      'py': 'python',
      'html': 'html',
      'css': 'css',
      'json': 'json',
      'md': 'markdown',
      'lua': 'lua',
      'cs': 'csharp',
      'cpp': 'cpp',
      'c': 'c',
      'java': 'java',
      'go': 'go',
      'rs': 'rust',
      'php': 'php'
    }
    return langMap[ext || ''] || 'plaintext'
  }

  const handleMouseDown = (e: React.MouseEvent) => {
    setIsResizing(true)
    e.preventDefault()
  }

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (isResizing) {
        const newWidth = e.clientX
        if (newWidth >= 150 && newWidth <= 500) {
          onResize(newWidth)
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

  const renderFileTree = (items: any[], level: number = 0) => {
    return items.map((item, index) => {
      const isOpen = openFiles.some(f => f.path === item.path)
      const isExpanded = expandedFolders.has(item.path)
      
      return (
        <div key={index}>
          <div
            className={cn(
              'flex items-center gap-2 py-1 px-2 hover:bg-background cursor-pointer transition-colors',
              level > 0 && 'pl-4',
              isOpen && 'bg-background'
            )}
            style={{ paddingLeft: `${level * 16 + 8}px` }}
            onClick={() => handleFileClick(item)}
          >
            {item.isDirectory ? (
              <>
                {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                <Folder size={16} className={cn(isExpanded ? 'text-accent' : 'text-gray-400')} />
              </>
            ) : (
              <>
                <span className="w-4" />
                <FileCode size={16} className="text-gray-400" />
              </>
            )}
            <span className="text-sm text-text truncate">{item.name}</span>
          </div>
        </div>
      )
    })
  }

  return (
    <div className="flex bg-white border-r border-border">
      <div style={{ width }} className="flex flex-col">
        <div className="h-10 border-b border-border flex items-center justify-between px-3">
          <span className="text-xs font-semibold text-text uppercase tracking-wide">Explorer</span>
          <div className="flex items-center gap-1">
            <button className="p-1 hover:bg-background rounded transition-colors">
              <Plus size={14} />
            </button>
            <button className="p-1 hover:bg-background rounded transition-colors">
              <X size={14} />
            </button>
          </div>
        </div>
        
        <div className="flex-1 overflow-y-auto scrollbar-thin">
          {projectPath ? (
            <div className="py-2">
              <div className="flex items-center gap-2 py-1 px-2 hover:bg-background cursor-pointer">
                <ChevronDown size={14} />
                <Folder size={16} className="text-accent" />
                <span className="text-sm font-medium text-text">
                  {projectPath.split('\\').pop() || projectPath.split('/').pop()}
                </span>
              </div>
              {renderFileTree(files, 1)}
            </div>
          ) : (
            <div className="p-4 text-center">
              <button
                onClick={loadProject}
                className="px-4 py-2 bg-accent text-white rounded-lg text-sm font-medium hover:opacity-90 transition-opacity"
              >
                Open Project
              </button>
            </div>
          )}
        </div>
      </div>
      
      <div
        className="w-1 hover:bg-accent cursor-col-resize transition-colors"
        onMouseDown={handleMouseDown}
      />
    </div>
  )
}
