/// <reference types="vite/client" />

interface ElectronAPI {
  dialog: {
    openDirectory: () => Promise<{ canceled: boolean; filePaths: string[] }>
    openFile: () => Promise<{ canceled: boolean; filePaths: string[] }>
    saveFile: () => Promise<{ canceled: boolean; filePath?: string }>
  }
  fs: {
    readFile: (filePath: string) => Promise<{ success: boolean; content?: string; error?: string }>
    writeFile: (filePath: string, content: string) => Promise<{ success: boolean; error?: string }>
    readDirectory: (dirPath: string) => Promise<{ success: boolean; files?: any[]; error?: string }>
    createDirectory: (dirPath: string) => Promise<{ success: boolean; error?: string }>
    deleteFile: (filePath: string) => Promise<{ success: boolean; error?: string }>
    renameFile: (oldPath: string, newPath: string) => Promise<{ success: boolean; error?: string }>
  }
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI
  }
}

export {}
