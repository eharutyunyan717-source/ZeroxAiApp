import { useState } from 'react'
import { X, Monitor, Keyboard, Palette, GitBranch } from 'lucide-react'
import { cn } from '../utils/cn'

interface SettingsProps {
  onClose: () => void
}

interface Tab {
  id: string
  label: string
  icon: React.ReactNode
}

export default function Settings({ onClose }: SettingsProps) {
  const [activeTab, setActiveTab] = useState('appearance')

  const tabs: Tab[] = [
    { id: 'appearance', label: 'Appearance', icon: <Palette size={16} /> },
    { id: 'editor', label: 'Editor', icon: <Monitor size={16} /> },
    { id: 'keybindings', label: 'Keybindings', icon: <Keyboard size={16} /> },
    { id: 'git', label: 'Git', icon: <GitBranch size={16} /> }
  ]

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="w-[800px] h-[600px] bg-white rounded-xl shadow-2xl overflow-hidden flex"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="w-48 border-r border-border bg-background">
          <div className="h-12 border-b border-border flex items-center justify-between px-4">
            <span className="text-sm font-semibold text-text">Settings</span>
            <button onClick={onClose} className="p-1 hover:bg-white rounded transition-colors">
              <X size={16} />
            </button>
          </div>
          
          <div className="py-2">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={cn(
                  'w-full flex items-center gap-3 px-4 py-2 transition-colors',
                  activeTab === tab.id ? 'bg-white text-accent' : 'text-text hover:bg-white/50'
                )}
              >
                {tab.icon}
                <span className="text-sm">{tab.label}</span>
              </button>
            ))}
          </div>
        </div>
        
        <div className="flex-1 overflow-y-auto scrollbar-thin">
          <div className="p-6">
            <h2 className="text-lg font-semibold text-text mb-4">
              {tabs.find(t => t.id === activeTab)?.label}
            </h2>
            
            {activeTab === 'appearance' && (
              <div className="space-y-6">
                <div>
                  <label className="block text-sm font-medium text-text mb-2">Theme</label>
                  <select className="w-full px-3 py-2 border border-border rounded-md bg-white text-text">
                    <option>Light</option>
                    <option>Dark</option>
                    <option>System</option>
                  </select>
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-text mb-2">Font Family</label>
                  <select className="w-full px-3 py-2 border border-border rounded-md bg-white text-text">
                    <option>Inter</option>
                    <option>System UI</option>
                    <option>SF Pro</option>
                  </select>
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-text mb-2">Font Size</label>
                  <input
                    type="number"
                    defaultValue={14}
                    className="w-full px-3 py-2 border border-border rounded-md bg-white text-text"
                  />
                </div>
                
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium text-text">Smooth Animations</div>
                    <div className="text-xs text-text/50">Enable smooth transitions and animations</div>
                  </div>
                  <button className="w-12 h-6 bg-accent rounded-full relative">
                    <div className="absolute right-1 top-1 w-4 h-4 bg-white rounded-full" />
                  </button>
                </div>
              </div>
            )}
            
            {activeTab === 'editor' && (
              <div className="space-y-6">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium text-text">Minimap</div>
                    <div className="text-xs text-text/50">Show minimap on the right side</div>
                  </div>
                  <button className="w-12 h-6 bg-accent rounded-full relative">
                    <div className="absolute right-1 top-1 w-4 h-4 bg-white rounded-full" />
                  </button>
                </div>
                
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium text-text">Line Numbers</div>
                    <div className="text-xs text-text/50">Show line numbers</div>
                  </div>
                  <button className="w-12 h-6 bg-accent rounded-full relative">
                    <div className="absolute right-1 top-1 w-4 h-4 bg-white rounded-full" />
                  </button>
                </div>
                
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium text-text">Word Wrap</div>
                    <div className="text-xs text-text/50">Wrap long lines</div>
                  </div>
                  <button className="w-12 h-6 bg-accent rounded-full relative">
                    <div className="absolute right-1 top-1 w-4 h-4 bg-white rounded-full" />
                  </button>
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-text mb-2">Tab Size</label>
                  <select className="w-full px-3 py-2 border border-border rounded-md bg-white text-text">
                    <option>2 spaces</option>
                    <option>4 spaces</option>
                    <option>Tab</option>
                  </select>
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-text mb-2">Auto Save</label>
                  <select className="w-full px-3 py-2 border border-border rounded-md bg-white text-text">
                    <option>After delay</option>
                    <option>On focus change</option>
                    <option>On window change</option>
                    <option>Never</option>
                  </select>
                </div>
              </div>
            )}
            
            {activeTab === 'keybindings' && (
              <div className="space-y-4">
                <div className="flex items-center justify-between py-2 border-b border-border">
                  <div>
                    <div className="text-sm font-medium text-text">Command Palette</div>
                    <div className="text-xs text-text/50">Open command palette</div>
                  </div>
                  <div className="flex gap-1">
                    <kbd className="px-2 py-1 bg-background border border-border rounded text-xs">Ctrl</kbd>
                    <kbd className="px-2 py-1 bg-background border border-border rounded text-xs">Shift</kbd>
                    <kbd className="px-2 py-1 bg-background border border-border rounded text-xs">P</kbd>
                  </div>
                </div>
                
                <div className="flex items-center justify-between py-2 border-b border-border">
                  <div>
                    <div className="text-sm font-medium text-text">Save File</div>
                    <div className="text-xs text-text/50">Save current file</div>
                  </div>
                  <div className="flex gap-1">
                    <kbd className="px-2 py-1 bg-background border border-border rounded text-xs">Ctrl</kbd>
                    <kbd className="px-2 py-1 bg-background border border-border rounded text-xs">S</kbd>
                  </div>
                </div>
                
                <div className="flex items-center justify-between py-2 border-b border-border">
                  <div>
                    <div className="text-sm font-medium text-text">Find</div>
                    <div className="text-xs text-text/50">Find in file</div>
                  </div>
                  <div className="flex gap-1">
                    <kbd className="px-2 py-1 bg-background border border-border rounded text-xs">Ctrl</kbd>
                    <kbd className="px-2 py-1 bg-background border border-border rounded text-xs">F</kbd>
                  </div>
                </div>
                
                <div className="flex items-center justify-between py-2 border-b border-border">
                  <div>
                    <div className="text-sm font-medium text-text">Toggle Terminal</div>
                    <div className="text-xs text-text/50">Show/hide terminal</div>
                  </div>
                  <div className="flex gap-1">
                    <kbd className="px-2 py-1 bg-background border border-border rounded text-xs">Ctrl</kbd>
                    <kbd className="px-2 py-1 bg-background border border-border rounded text-xs">`</kbd>
                  </div>
                </div>
              </div>
            )}
            
            {activeTab === 'git' && (
              <div className="space-y-6">
                <div>
                  <label className="block text-sm font-medium text-text mb-2">Git Path</label>
                  <input
                    type="text"
                    placeholder="Auto-detected"
                    className="w-full px-3 py-2 border border-border rounded-md bg-white text-text"
                  />
                </div>
                
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium text-text">Enable Git</div>
                    <div className="text-xs text-text/50">Enable Git integration</div>
                  </div>
                  <button className="w-12 h-6 bg-accent rounded-full relative">
                    <div className="absolute right-1 top-1 w-4 h-4 bg-white rounded-full" />
                  </button>
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-text mb-2">Default Branch Name</label>
                  <input
                    type="text"
                    defaultValue="main"
                    className="w-full px-3 py-2 border border-border rounded-md bg-white text-text"
                  />
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
