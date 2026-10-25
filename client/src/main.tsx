import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import { AppShell } from './components/AppShell.tsx'
import { AuthGate } from './components/AuthGate.tsx'
import './design.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthGate>{(user, logOut) => <AppShell key={user.id} user={user} onLogOut={logOut} />}</AuthGate>
  </StrictMode>,
)

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js')
  })
}
