import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ChatPage } from './pages/ChatPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { PopupPage } from './pages/PopupPage'
import { SidebarPage } from './pages/SidebarPage'

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/chat" replace />} />
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/popup" element={<PopupPage />} />
        <Route path="/sidebar" element={<SidebarPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </BrowserRouter>
  )
}
