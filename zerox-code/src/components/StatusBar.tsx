import { Terminal, Bot, CheckCircle, AlertCircle, GitBranch, Clock } from 'lucide-react'
import { cn } from '../utils/cn'

interface StatusBarProps {
  onToggleTerminal: () => void
  onToggleAI: () => void
  showTerminal: boolean
  showAI: boolean
}

export default function StatusBar({ onToggleTerminal, onToggleAI, showTerminal, showAI }: StatusBarProps) {
  return (
    <div className="h-6 bg-background border-t border-border flex items-center justify-between px-4 text-xs text-text">
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5">
          <GitBranch size={12} />
          <span>main</span>
        </div>
        
        <div className="flex items-center gap-1.5">
          <CheckCircle size={12} className="text-green-500" />
          <span>0 Errors</span>
        </div>
        
        <div className="flex items-center gap-1.5">
          <AlertCircle size={12} className="text-yellow-500" />
          <span>0 Warnings</span>
        </div>
      </div>
      
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5">
          <span>Ln 1, Col 1</span>
        </div>
        
        <div className="flex items-center gap-1.5">
          <span>Spaces: 2</span>
        </div>
        
        <div className="flex items-center gap-1.5">
          <span>UTF-8</span>
        </div>
        
        <div className="flex items-center gap-1.5">
          <Clock size={12} />
          <span>{new Date().toLocaleTimeString()}</span>
        </div>
        
        <button
          onClick={onToggleAI}
          className={cn(
            'flex items-center gap-1.5 px-2 py-0.5 rounded transition-colors',
            showAI ? 'bg-accent text-white' : 'hover:bg-white'
          )}
        >
          <Bot size={12} />
          <span>AI</span>
        </button>
        
        <button
          onClick={onToggleTerminal}
          className={cn(
            'flex items-center gap-1.5 px-2 py-0.5 rounded transition-colors',
            showTerminal ? 'bg-accent text-white' : 'hover:bg-white'
          )}
        >
          <Terminal size={12} />
          <span>Terminal</span>
        </button>
      </div>
    </div>
  )
}
