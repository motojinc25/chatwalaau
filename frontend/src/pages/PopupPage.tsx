import { MessageCircle, X } from 'lucide-react'
import { useState } from 'react'
import { ChatPanel } from '@/components/ChatPanel'
import { Button } from '@/components/ui/button'

export function PopupPage() {
  const [isOpen, setIsOpen] = useState(false)

  return (
    <div className="flex h-screen flex-col">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <h1 className="text-sm font-semibold">ChatWalaʻau - Popup</h1>
      </header>

      <div className="flex flex-1 items-center justify-center text-muted-foreground">
        <p className="text-sm">Click the chat icon in the bottom-right corner to start.</p>
      </div>

      {isOpen && (
        <div className="fixed right-4 bottom-20 z-50 flex h-[500px] w-[380px] flex-col rounded-xl border bg-background shadow-lg">
          <div className="flex h-10 shrink-0 items-center justify-between border-b px-3">
            <span className="text-sm font-medium">ChatWalaʻau</span>
            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setIsOpen(false)}>
              <X className="h-4 w-4" />
            </Button>
          </div>
          <ChatPanel compact />
        </div>
      )}

      <Button
        className="fixed right-4 bottom-4 z-50 h-14 w-14 rounded-full shadow-lg"
        size="icon"
        onClick={() => setIsOpen((prev) => !prev)}
        aria-label={isOpen ? 'Close chat' : 'Open chat'}>
        {isOpen ? <X className="h-6 w-6" /> : <MessageCircle className="h-6 w-6" />}
      </Button>
    </div>
  )
}
