import { useState, useRef, useEffect } from 'react'
import { Terminal as TerminalIcon, Plus, X, ChevronDown } from 'lucide-react'
import { Terminal as XTerm } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import { WebLinksAddon } from 'xterm-addon-web-links'
import { cn } from '../utils/cn'
import { TerminalTab } from '../types'

interface TerminalProps {
  height: number
  onResize: (height: number) => void
  onClose: () => void
}

export default function Terminal({ height, onResize, onClose }: TerminalProps) {
  const [tabs, setTabs] = useState<TerminalTab[]>([
    { id: '1', title: 'PowerShell', type: 'powershell' }
  ])
  const [activeTab, setActiveTab] = useState<TerminalTab>(tabs[0])
  const terminalRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<XTerm | null>(null)
  const fitAddonRef = useRef<FitAddon | null>(null)
  const [isResizing, setIsResizing] = useState(false)

  useEffect(() => {
    if (terminalRef.current && !xtermRef.current) {
      const xterm = new XTerm({
        fontFamily: 'JetBrains Mono, Consolas, monospace',
        fontSize: 14,
        cursorBlink: true,
        cursorStyle: 'block',
        scrollback: 1000,
      })

      const fitAddon = new FitAddon()
      const webLinksAddon = new WebLinksAddon()

      xterm.loadAddon(fitAddon)
      xterm.loadAddon(webLinksAddon)

      xterm.open(terminalRef.current)
      fitAddon.fit()

      xtermRef.current = xterm
      fitAddonRef.current = fitAddon

      xterm.write('PowerShell 7.4.0\r\nCopyright (c) Microsoft Corporation.\r\n\r\n')

      xterm.onData((data: string) => {
        // In a real implementation, this would send data to the IPC handler
        xterm.write(data)
      })

      const handleResize = () => {
        fitAddon.fit()
      }

      window.addEventListener('resize', handleResize)

      return () => {
        window.removeEventListener('resize', handleResize)
        xterm.dispose()
        xtermRef.current = null
      }
    }
  }, [])

  useEffect(() => {
    if (fitAddonRef.current) {
      setTimeout(() => fitAddonRef.current?.fit(), 100)
    }
  }, [height])

  const handleAddTab = () => {
    const newTab: TerminalTab = {
      id: Date.now().toString(),
      title: tabs.length === 1 ? 'CMD' : `Terminal ${tabs.length + 1}`,
      type: tabs.length === 1 ? 'cmd' : 'powershell'
    }
    setTabs([...tabs, newTab])
    setActiveTab(newTab)
  }

  const handleCloseTab = (tab: TerminalTab) => {
    if (tabs.length === 1) {
      onClose()
      return
    }
    const newTabs = tabs.filter(t => t.id !== tab.id)
    setTabs(newTabs)
    setActiveTab(newTabs[0])
  }

  const handleMouseDown = (e: React.MouseEvent) => {
    setIsResizing(true)
    e.preventDefault()
  }

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (isResizing) {
        const newHeight = window.innerHeight - e.clientY
        if (newHeight >= 100 && newHeight <= 500) {
          onResize(newHeight)
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
    <div className="flex flex-col bg-white border-t border-border">
      <div
        className="h-1 hover:bg-accent cursor-row-resize transition-colors"
        onMouseDown={handleMouseDown}
      />
      
      <div style={{ height }} className="flex flex-col">
        <div className="h-10 border-b border-border flex items-center justify-between px-3 bg-background">
          <div className="flex items-center gap-1">
            <TerminalIcon size={16} className="text-text" />
            {tabs.map((tab) => (
              <div
                key={tab.id}
                className={cn(
                  'flex items-center gap-2 px-3 py-1.5 cursor-pointer transition-colors border-l border-border',
                  activeTab.id === tab.id ? 'bg-white' : 'hover:bg-white/50'
                )}
                onClick={() => setActiveTab(tab)}
              >
                <span className="text-sm text-text">{tab.title}</span>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    handleCloseTab(tab)
                  }}
                  className="p-0.5 hover:bg-background rounded transition-colors"
                >
                  <X size={12} />
                </button>
              </div>
            ))}
            <button
              onClick={handleAddTab}
              className="p-1.5 hover:bg-white rounded transition-colors"
            >
              <Plus size={14} />
            </button>
          </div>
          
          <div className="flex items-center gap-2">
            <button className="p-1.5 hover:bg-white rounded transition-colors">
              <ChevronDown size={14} />
            </button>
          </div>
        </div>
        
        <div className="flex-1 overflow-hidden p-2">
          <div ref={terminalRef} className="h-full" />
        </div>
      </div>
    </div>
  )
}
