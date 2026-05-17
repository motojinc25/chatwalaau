import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthGuard } from './components/AuthGuard'
import { ChatPage } from './pages/ChatPage'
import { LoginPage } from './pages/LoginPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { PopupPage } from './pages/PopupPage'
import { SidebarPage } from './pages/SidebarPage'

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/chat" replace />} />
        <Route
          path="/chat"
          element={
            <AuthGuard>
              <ChatPage />
            </AuthGuard>
          }
        />
        <Route
          path="/popup"
          element={
            <AuthGuard>
              <PopupPage />
            </AuthGuard>
          }
        />
        <Route
          path="/sidebar"
          element={
            <AuthGuard>
              <SidebarPage />
            </AuthGuard>
          }
        />
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </BrowserRouter>
  )
}
