import { useState, useEffect, useRef } from 'react'
import { Search, File, Folder, Settings, Terminal, Sparkles } from 'lucide-react'
import { cn } from '../utils/cn'

interface CommandPaletteProps {
  onClose: () => void
  onOpenSettings: () => void
}

interface Command {
  id: string
  label: string
  icon: React.ReactNode
  action: () => void
  category: string
}

export default function CommandPalette({ onClose, onOpenSettings }: CommandPaletteProps) {
  const [query, setQuery] = useState('')
  const [selectedIndex, setSelectedIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  const commands: Command[] = [
    {
      id: 'settings',
      label: 'Open Settings',
      icon: <Settings size={16} />,
      action: onOpenSettings,
      category: 'General'
    },
    {
      id: 'new-file',
      label: 'New File',
      icon: <File size={16} />,
      action: () => {},
      category: 'File'
    },
    {
      id: 'new-folder',
      label: 'New Folder',
      icon: <Folder size={16} />,
      action: () => {},
      category: 'File'
    },
    {
      id: 'toggle-terminal',
      label: 'Toggle Terminal',
      icon: <Terminal size={16} />,
      action: () => {},
      category: 'View'
    },
    {
      id: 'toggle-ai',
      label: 'Toggle AI Assistant',
      icon: <Sparkles size={16} />,
      action: () => {},
      category: 'View'
    }
  ]

  const filteredCommands = commands.filter(cmd =>
    cmd.label.toLowerCase().includes(query.toLowerCase())
  )

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose()
      } else if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedIndex((prev) => (prev + 1) % filteredCommands.length)
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedIndex((prev) => (prev - 1 + filteredCommands.length) % filteredCommands.length)
      } else if (e.key === 'Enter' && filteredCommands.length > 0) {
        filteredCommands[selectedIndex].action()
        onClose()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [filteredCommands, selectedIndex, onClose])

  const handleCommandClick = (command: Command) => {
    command.action()
    onClose()
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-start justify-center pt-[20vh] z-50" onClick={onClose}>
      <div
        className="w-full max-w-2xl bg-white rounded-xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 px-4 py-3 border-b border-border">
          <Search size={20} className="text-text/50" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value)
              setSelectedIndex(0)
            }}
            placeholder="Type a command or search..."
            className="flex-1 bg-transparent border-none outline-none text-text placeholder:text-text/50"
          />
          <kbd className="px-2 py-1 bg-background border border-border rounded text-xs text-text/70">ESC</kbd>
        </div>
        
        <div className="max-h-96 overflow-y-auto scrollbar-thin">
          {filteredCommands.length === 0 ? (
            <div className="py-8 text-center text-text/50">
              No commands found
            </div>
          ) : (
            <div className="py-2">
              {filteredCommands.map((command, index) => (
                <button
                  key={command.id}
                  onClick={() => handleCommandClick(command)}
                  className={cn(
                    'w-full flex items-center gap-3 px-4 py-2 transition-colors',
                    index === selectedIndex ? 'bg-background' : 'hover:bg-background/50'
                  )}
                >
                  <div className="w-8 h-8 flex items-center justify-center text-text/70">
                    {command.icon}
                  </div>
                  <div className="flex-1 text-left">
                    <div className="text-sm text-text">{command.label}</div>
                    <div className="text-xs text-text/50">{command.category}</div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
        
        <div className="px-4 py-2 border-t border-border bg-background flex items-center gap-4 text-xs text-text/50">
          <div className="flex items-center gap-1">
            <kbd className="px-1.5 py-0.5 bg-white border border-border rounded">↑↓</kbd>
            <span>Navigate</span>
          </div>
          <div className="flex items-center gap-1">
            <kbd className="px-1.5 py-0.5 bg-white border border-border rounded">Enter</kbd>
            <span>Select</span>
          </div>
          <div className="flex items-center gap-1">
            <kbd className="px-1.5 py-0.5 bg-white border border-border rounded">ESC</kbd>
            <span>Close</span>
          </div>
        </div>
      </div>
    </div>
  )
}
