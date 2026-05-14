import { MessageCircle, X } from 'lucide-react'
import { useState } from 'react'
import { ChatPanel } from '@/components/ChatPanel'
import { Button } from '@/components/ui/button'

export function SidebarPage() {
  const [isOpen, setIsOpen] = useState(false)

  return (
    <div className="flex h-screen">
      <div className="flex flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center border-b px-4">
          <h1 className="text-sm font-semibold">ChatWalaʻau - Sidebar</h1>
          <Button variant="ghost" size="icon" className="ml-auto" onClick={() => setIsOpen((prev) => !prev)}>
            {isOpen ? <X className="h-4 w-4" /> : <MessageCircle className="h-4 w-4" />}
          </Button>
        </header>
        <div className="flex flex-1 items-center justify-center text-muted-foreground">
          <p className="text-sm">Click the chat icon in the header to open the sidebar panel.</p>
        </div>
      </div>

      {isOpen && (
        <aside className="flex h-full w-[380px] shrink-0 flex-col border-l bg-background">
          <div className="flex h-12 shrink-0 items-center justify-between border-b px-3">
            <span className="text-sm font-medium">ChatWalaʻau</span>
            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setIsOpen(false)}>
              <X className="h-4 w-4" />
            </Button>
          </div>
          <ChatPanel compact />
        </aside>
      )}
    </div>
  )
}
