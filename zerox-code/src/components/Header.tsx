import { Search, Settings, GitBranch, Play, MoreHorizontal } from 'lucide-react'

interface HeaderProps {
  onCommandPalette: () => void
  onSettings: () => void
}

export default function Header({ onCommandPalette, onSettings }: HeaderProps) {
  return (
    <header className="h-12 bg-white border-b border-border flex items-center justify-between px-4">
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-accent rounded-lg flex items-center justify-center">
            <span className="text-white font-bold text-sm">Z</span>
          </div>
          <span className="font-semibold text-text">Zerox Code</span>
        </div>
        
        <nav className="flex items-center gap-1 ml-4">
          <button className="px-3 py-1.5 text-sm text-text hover:bg-background rounded-md transition-colors">
            File
          </button>
          <button className="px-3 py-1.5 text-sm text-text hover:bg-background rounded-md transition-colors">
            Edit
          </button>
          <button className="px-3 py-1.5 text-sm text-text hover:bg-background rounded-md transition-colors">
            View
          </button>
          <button className="px-3 py-1.5 text-sm text-text hover:bg-background rounded-md transition-colors">
            Go
          </button>
          <button className="px-3 py-1.5 text-sm text-text hover:bg-background rounded-md transition-colors">
            Run
          </button>
          <button className="px-3 py-1.5 text-sm text-text hover:bg-background rounded-md transition-colors">
            Terminal
          </button>
          <button className="px-3 py-1.5 text-sm text-text hover:bg-background rounded-md transition-colors">
            Help
          </button>
        </nav>
      </div>
      
      <div className="flex items-center gap-2">
        <button
          onClick={onCommandPalette}
          className="flex items-center gap-2 px-3 py-1.5 bg-background border border-border rounded-md text-sm text-text hover:border-accent transition-colors"
        >
          <Search size={14} />
          <span>Search</span>
          <kbd className="px-1.5 py-0.5 bg-white border border-border rounded text-xs">Ctrl+P</kbd>
        </button>
        
        <button className="p-2 hover:bg-background rounded-md transition-colors">
          <GitBranch size={18} />
        </button>
        
        <button className="p-2 hover:bg-background rounded-md transition-colors">
          <Play size={18} />
        </button>
        
        <button className="p-2 hover:bg-background rounded-md transition-colors">
          <MoreHorizontal size={18} />
        </button>
        
        <button
          onClick={onSettings}
          className="p-2 hover:bg-background rounded-md transition-colors"
        >
          <Settings size={18} />
        </button>
      </div>
    </header>
  )
}
