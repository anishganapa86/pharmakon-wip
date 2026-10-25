import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// Demo-only gate: login.html sets this flag. Not real auth.
function isLoggedIn(): boolean {
  try {
    return localStorage.getItem('pharmakon_logged_in') === 'true'
  } catch {
    return false
  }
}

if (!isLoggedIn()) {
  window.location.replace('/login.html')
} else {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js')
  })
}
