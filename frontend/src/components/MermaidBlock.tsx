import { Code, Image } from 'lucide-react'
import mermaid from 'mermaid'
import { useEffect, useRef, useState } from 'react'
import { CodeBlock } from '@/components/CodeBlock'
import { Button } from '@/components/ui/button'

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  securityLevel: 'loose',
})

let renderCounter = 0

interface MermaidBlockProps {
  chart: string
}

export function MermaidBlock({ chart }: MermaidBlockProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [rendered, setRendered] = useState(false)
  const [error, setError] = useState(false)
  const [view, setView] = useState<'diagram' | 'code'>('diagram')

  useEffect(() => {
    let cancelled = false
    const id = `mermaid-${++renderCounter}`

    setRendered(false)
    setError(false)

    ;(async () => {
      try {
        const result = await mermaid.render(id, chart)
        if (!cancelled && containerRef.current) {
          containerRef.current.innerHTML = result.svg
          setRendered(true)
        }
      } catch {
        if (!cancelled) {
          setError(true)
        }
        // mermaid.render creates a temp element with id `d${id}` on error; clean it up
        document.getElementById(`d${id}`)?.remove()
      }
    })()

    return () => {
      cancelled = true
    }
  }, [chart])

  if (error) {
    return <CodeBlock language="mermaid" value={chart} />
  }

  return (
    <div className="my-4">
      <div
        ref={containerRef}
        className={`justify-center overflow-auto bg-white [&_svg]:max-w-full [&_svg]:h-auto ${view === 'diagram' ? 'flex' : 'hidden'}`}
      />
      {!rendered && (
        <div className={`flex justify-center py-8 text-muted-foreground text-sm ${view === 'diagram' ? '' : 'hidden'}`}>
          Rendering diagram…
        </div>
      )}
      <div className={view === 'code' ? '' : 'hidden'}>
        <CodeBlock language="mermaid" value={chart} />
      </div>
      <div className="flex items-center justify-end gap-1 mt-1">
        <Button
          variant="ghost"
          size="icon"
          className={`h-6 w-6 ${view === 'diagram' ? 'text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
          onClick={() => setView('diagram')}
          aria-label="Show diagram">
          <Image className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className={`h-6 w-6 ${view === 'code' ? 'text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
          onClick={() => setView('code')}
          aria-label="Show code">
          <Code className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  )
}
