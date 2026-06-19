/**
 * About dialog (CTR-0101, FEAT-0029, PRP-0068).
 *
 * Static, client-rendered provenance surface opened from the sidebar
 * footer info button. Content is brand / provenance copy (UDR-0044 D3):
 * it is NOT served from the backend. Only the version subtitle is
 * dynamic, sourced from useAuth (CTR-0094 v5).
 */

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'

// Public marketing/documentation site (PRP-0085, UDR-0063 D1). Static frontend
// constant -- About copy is not backend-served and not env-configurable (UDR-0044 D3).
const WEBSITE_URL = 'https://chatwalaau.com'

interface AboutDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  version: string | null
}

export function AboutDialog({ open, onOpenChange, version }: AboutDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <img src="/favicon.svg" alt="" className="h-5 w-5" />
            ChatWalaʻau
            {version && <span className="text-xs font-normal text-muted-foreground">v{version}</span>}
          </DialogTitle>
          <DialogDescription>
            ChatWalaʻau is a localhost-first AI agent runtime that packages a polished chat experience -- streaming
            responses, voice, image generation, RAG, and tool use -- into a single installable app.
          </DialogDescription>
        </DialogHeader>

        <dl className="space-y-3 text-sm">
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Framework</dt>
            <dd className="mt-0.5">
              Built on Microsoft Agent Framework (Python).
              <br />
              Frontend: React 19 + Vite. Backend: FastAPI.
            </dd>
          </div>
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Developer</dt>
            <dd className="mt-0.5">Jingun Jung</dd>
          </div>
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Backed by</dt>
            <dd className="mt-0.5">WeDX Digital Twins Solutions, a DBA of Motojin Investment, Inc. (USA)</dd>
          </div>
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Website</dt>
            <dd className="mt-0.5">
              <a
                href={WEBSITE_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary underline-offset-2 hover:underline">
                chatwalaau.com
              </a>
            </dd>
          </div>
        </dl>
      </DialogContent>
    </Dialog>
  )
}
