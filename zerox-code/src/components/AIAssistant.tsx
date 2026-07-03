import { useState, useRef, useEffect } from 'react'
import { Send, Paperclip, Sparkles, Trash2, X } from 'lucide-react'
import { cn } from '../utils/cn'
import { File as FileType, ChatMessage } from '../types'

interface AIAssistantProps {
  width: number
  onResize: (width: number) => void
  onClose: () => void
  activeFile: FileType | null
}

export default function AIAssistant({ width, onResize, onClose, activeFile }: AIAssistantProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: '1',
      role: 'assistant',
      content: 'Hello! I\'m your AI coding assistant. I can help you with:\n\n• Writing and generating code\n• Debugging and fixing errors\n• Refactoring and optimizing\n• Explaining code\n• Creating new files\n\nHow can I help you today?',
      timestamp: new Date()
    }
  ])
  const [input, setInput] = useState('')
  const [isTyping, setIsTyping] = useState(false)
  const [attachedFiles, setAttachedFiles] = useState<FileType[]>([])
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const [isResizing, setIsResizing] = useState(false)

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  useEffect(() => {
    scrollToBottom()
  }, [messages])

  const handleSendMessage = () => {
    if (!input.trim()) return

    const userMessage: ChatMessage = {
      id: Date.now().toString(),
      role: 'user',
      content: input,
      timestamp: new Date()
    }

    setMessages([...messages, userMessage])
    setInput('')
    setIsTyping(true)

    setTimeout(() => {
      const aiResponse: ChatMessage = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content: 'I understand your request. Let me help you with that. This is a simulated response - in a real implementation, this would connect to an AI API like OpenAI, Claude, or a local model.',
        timestamp: new Date()
      }
      setMessages(prev => [...prev, aiResponse])
      setIsTyping(false)
    }, 1500)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSendMessage()
    }
  }

  const handleAttachFile = () => {
    if (activeFile && !attachedFiles.find(f => f.path === activeFile.path)) {
      setAttachedFiles([...attachedFiles, activeFile])
    }
  }

  const handleRemoveAttachment = (file: FileType) => {
    setAttachedFiles(attachedFiles.filter(f => f.path !== file.path))
  }

  const handleClearChat = () => {
    setMessages([
      {
        id: '1',
        role: 'assistant',
        content: 'Chat cleared. How can I help you?',
        timestamp: new Date()
      }
    ])
  }

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

  return (
    <div className="flex bg-white border-l border-border">
      <div style={{ width }} className="flex flex-col">
        <div className="h-10 border-b border-border flex items-center justify-between px-3 bg-background">
          <div className="flex items-center gap-2">
            <Sparkles size={16} className="text-accent" />
            <span className="text-xs font-semibold text-text uppercase tracking-wide">AI Assistant</span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={handleClearChat}
              className="p-1 hover:bg-white rounded transition-colors"
              title="Clear chat"
            >
              <Trash2 size={14} />
            </button>
            <button
              onClick={onClose}
              className="p-1 hover:bg-white rounded transition-colors"
            >
              <X size={14} />
            </button>
          </div>
        </div>
        
        <div className="flex-1 overflow-y-auto scrollbar-thin p-4 space-y-4">
          {messages.map((message) => (
            <div
              key={message.id}
              className={cn(
                'flex gap-3',
                message.role === 'user' ? 'justify-end' : 'justify-start'
              )}
            >
              {message.role === 'assistant' && (
                <div className="w-8 h-8 bg-accent rounded-lg flex items-center justify-center flex-shrink-0">
                  <Sparkles size={16} className="text-white" />
                </div>
              )}
              <div
                className={cn(
                  'max-w-[calc(100%-3rem)] rounded-lg p-3',
                  message.role === 'user'
                    ? 'bg-accent text-white'
                    : 'bg-background text-text'
                )}
              >
                <p className="text-sm whitespace-pre-wrap">{message.content}</p>
              </div>
            </div>
          ))}
          {isTyping && (
            <div className="flex gap-3 justify-start">
              <div className="w-8 h-8 bg-accent rounded-lg flex items-center justify-center flex-shrink-0">
                <Sparkles size={16} className="text-white" />
              </div>
              <div className="bg-background rounded-lg p-3">
                <div className="flex gap-1">
                  <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" />
                  <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce delay-100" />
                  <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce delay-200" />
                </div>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>
        
        {attachedFiles.length > 0 && (
          <div className="px-3 py-2 border-t border-border bg-background">
            <div className="flex flex-wrap gap-2">
              {attachedFiles.map((file) => (
                <div
                  key={file.path}
                  className="flex items-center gap-1 px-2 py-1 bg-white border border-border rounded-md text-xs"
                >
                  <span className="truncate max-w-32">{file.name}</span>
                  <button
                    onClick={() => handleRemoveAttachment(file)}
                    className="p-0.5 hover:bg-background rounded"
                  >
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}
        
        <div className="p-3 border-t border-border">
          <div className="flex items-end gap-2 bg-background rounded-lg border border-border p-2">
            <button
              onClick={handleAttachFile}
              className="p-2 hover:bg-white rounded transition-colors"
              title="Attach current file"
              disabled={!activeFile}
            >
              <Paperclip size={18} className={cn(!activeFile && 'opacity-50')} />
            </button>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask AI anything..."
              className="flex-1 bg-transparent border-none outline-none resize-none text-sm text-text placeholder:text-text/50 min-h-[60px] max-h-[200px]"
              rows={1}
            />
            <button
              onClick={handleSendMessage}
              disabled={!input.trim()}
              className={cn(
                'p-2 rounded-lg transition-colors',
                input.trim() ? 'bg-accent text-white hover:opacity-90' : 'bg-gray-200 text-gray-400'
              )}
            >
              <Send size={18} />
            </button>
          </div>
        </div>
      </div>
      
      <div
        className="w-1 hover:bg-accent cursor-col-resize transition-colors"
        onMouseDown={handleMouseDown}
      />
    </div>
  )
}
