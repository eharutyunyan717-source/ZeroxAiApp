import { app, BrowserWindow, ipcMain, dialog } from 'electron'
import * as path from 'path'
import * as fs from 'fs'

let mainWindow: BrowserWindow | null = null

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 600,
    frame: true,
    titleBarStyle: 'default',
    backgroundColor: '#FFFFFF',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    },
  })

  // Load the app
  if (process.env.NODE_ENV === 'development') {
    mainWindow.loadURL('http://localhost:5173')
    mainWindow.webContents.openDevTools()
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist-react/index.html'))
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

app.whenReady().then(createWindow)

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow()
  }
})

// IPC Handlers
ipcMain.handle('dialog:openDirectory', async () => {
  const result = await dialog.showOpenDialog(mainWindow!, {
    properties: ['openDirectory']
  })
  return result
})

ipcMain.handle('dialog:openFile', async () => {
  const result = await dialog.showOpenDialog(mainWindow!, {
    properties: ['openFile', 'multiSelections']
  })
  return result
})

ipcMain.handle('dialog:saveFile', async () => {
  const result = await dialog.showSaveDialog(mainWindow!)
  return result
})

ipcMain.handle('fs:readFile', async (_: Electron.IpcMainInvokeEvent, filePath: string) => {
  try {
    const content = await fs.promises.readFile(filePath, 'utf-8')
    return { success: true, content }
  } catch (error: any) {
    return { success: false, error: String(error) }
  }
})

ipcMain.handle('fs:writeFile', async (_: Electron.IpcMainInvokeEvent, filePath: string, content: string) => {
  try {
    await fs.promises.writeFile(filePath, content, 'utf-8')
    return { success: true }
  } catch (error: any) {
    return { success: false, error: String(error) }
  }
})

ipcMain.handle('fs:readDirectory', async (_: Electron.IpcMainInvokeEvent, dirPath: string) => {
  try {
    const items = await fs.promises.readdir(dirPath, { withFileTypes: true })
    const files = items.map((item: fs.Dirent) => ({
      name: item.name,
      isDirectory: item.isDirectory(),
      path: path.join(dirPath, item.name)
    }))
    return { success: true, files }
  } catch (error: any) {
    return { success: false, error: String(error) }
  }
})

ipcMain.handle('fs:createDirectory', async (_: Electron.IpcMainInvokeEvent, dirPath: string) => {
  try {
    await fs.promises.mkdir(dirPath, { recursive: true })
    return { success: true }
  } catch (error: any) {
    return { success: false, error: String(error) }
  }
})

ipcMain.handle('fs:deleteFile', async (_: Electron.IpcMainInvokeEvent, filePath: string) => {
  try {
    await fs.promises.unlink(filePath)
    return { success: true }
  } catch (error: any) {
    return { success: false, error: String(error) }
  }
})

ipcMain.handle('fs:renameFile', async (_: Electron.IpcMainInvokeEvent, oldPath: string, newPath: string) => {
  try {
    await fs.promises.rename(oldPath, newPath)
    return { success: true }
  } catch (error: any) {
    return { success: false, error: String(error) }
  }
})
