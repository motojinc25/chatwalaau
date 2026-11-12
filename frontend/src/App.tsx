import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthGuard } from './components/AuthGuard'
import { BootGate } from './components/BootGate'
import { PrivacyScreenProvider } from './components/PrivacyScreenProvider'
import { ChatPage } from './pages/ChatPage'
import { LoginPage } from './pages/LoginPage'
import { NotFoundPage } from './pages/NotFoundPage'

export function App() {
  // BootGate (CTR-0096 v3, UDR-0092 D2) wraps EVERY route, including /login: a
  // login page rendered against an unreachable backend is just as dishonest as a
  // chat page, and its submit would fail with a generic network error. Once the
  // backend answers once, the gate renders children and never intervenes again.
  //
  // PrivacyScreenProvider (CTR-0190, UDR-0107 D11) sits OUTSIDE the gate and the
  // router: the redaction EFFECT is app-wide.
  return (
    <PrivacyScreenProvider>
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
            <Route path="/login" element={<LoginPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </BrowserRouter>
      </BootGate>
    </PrivacyScreenProvider>
  )
}
