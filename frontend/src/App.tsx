import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthGuard } from './components/AuthGuard'
import { BootGate } from './components/BootGate'
import { ChatPage } from './pages/ChatPage'
import { LoginPage } from './pages/LoginPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { PopupPage } from './pages/PopupPage'
import { SidebarPage } from './pages/SidebarPage'

export function App() {
  // BootGate (CTR-0096 v3, UDR-0092 D2) wraps EVERY route, including /login: a
  // login page rendered against an unreachable backend is just as dishonest as a
  // chat page, and its submit would fail with a generic network error. Once the
  // backend answers once, the gate renders children and never intervenes again.
  return (
    <BootGate>
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
    </BootGate>
  )
}
