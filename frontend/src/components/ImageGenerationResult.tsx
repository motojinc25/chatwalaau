import { Paintbrush } from 'lucide-react'
import { useMemo } from 'react'
import { Button } from '@/components/ui/button'
import type { ToolCall } from '@/types/chat'

interface GeneratedImage {
  url: string
  filename: string
  revised_prompt?: string
  size?: string
}

interface ImageGenResult {
  images: GeneratedImage[]
  count: number
  tool: string
  source_image?: string
  error?: string
}

const IMAGE_TOOLS = new Set(['generate_image', 'edit_image'])

function ImageResultCard({ toolCall, onMaskEdit }: { toolCall: ToolCall; onMaskEdit?: (imageUrl: string) => void }) {
  const parsed = useMemo<ImageGenResult | null>(() => {
    try {
      return JSON.parse(toolCall.result ?? '')
    } catch {
      return null
    }
  }, [toolCall.result])

  if (!parsed || parsed.error || !parsed.images?.length) return null

  const isSingle = parsed.images.length === 1

  return (
    <div className="my-2 flex flex-col gap-2">
      <div className={isSingle ? '' : 'grid grid-cols-2 gap-2'}>
        {parsed.images.map((img) => (
          <div key={img.filename} className="group/img relative">
            <a href={img.url} target="_blank" rel="noopener noreferrer" className="block">
              <img
                src={img.url}
                alt={img.revised_prompt ?? 'Generated image'}
                className="max-w-full rounded-lg border border-border/50 shadow-sm transition-shadow hover:shadow-md"
              />
            </a>
            {onMaskEdit && (
              <Button
                variant="secondary"
                size="sm"
                className="absolute bottom-2 right-2 h-7 gap-1 px-2 text-xs opacity-0 shadow-md transition-opacity group-hover/img:opacity-100"
                onClick={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  onMaskEdit(img.url)
                }}
                aria-label="Edit with mask">
                <Paintbrush className="h-3 w-3" />
                Edit
              </Button>
            )}
          </div>
        ))}
      </div>
      {parsed.images[0]?.revised_prompt && (
        <p className="text-xs text-muted-foreground/70 italic">{parsed.images[0].revised_prompt}</p>
      )}
    </div>
  )
}

export function ImageGenerationResults({
  toolCalls,
  onMaskEdit,
}: {
  toolCalls: ToolCall[]
  onMaskEdit?: (imageUrl: string) => void
}) {
  const imageResults = useMemo(
    () => toolCalls.filter((tc) => IMAGE_TOOLS.has(tc.name) && tc.status === 'completed' && tc.result),
    [toolCalls],
  )

  if (imageResults.length === 0) return null

  return (
    <div className="flex flex-col gap-1">
      {imageResults.map((tc) => (
        <ImageResultCard key={tc.id} toolCall={tc} onMaskEdit={onMaskEdit} />
      ))}
    </div>
  )
}
