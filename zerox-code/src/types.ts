export interface File {
  name: string
  path: string
  content: string
  language: string
  isDirectory?: boolean
  modified?: boolean
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
}

export interface TerminalTab {
  id: string
  title: string
  type: 'powershell' | 'cmd'
}
