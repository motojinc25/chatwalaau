import { Plus, Search } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { TemplateForm } from '@/components/templates/TemplateForm'
import { TemplateList } from '@/components/templates/TemplateList'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useTemplates } from '@/hooks/useTemplates'
import type { PromptTemplate } from '@/types/chat'

interface PromptTemplatesModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onInsert: (body: string) => void
  onNotify: (message: string, type: 'success' | 'error') => void
}

export function PromptTemplatesModal({ open, onOpenChange, onInsert, onNotify }: PromptTemplatesModalProps) {
  const { templates, fetchTemplates, createTemplate, updateTemplate, deleteTemplate } = useTemplates()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')

  useEffect(() => {
    if (open) {
      fetchTemplates()
      setSelectedId(null)
      setIsNew(false)
      setSearchQuery('')
    }
  }, [open, fetchTemplates])

  const selectedTemplate = templates.find((t) => t.id === selectedId) ?? null

  const handleNew = useCallback(() => {
    setSelectedId(null)
    setIsNew(true)
  }, [])

  const handleSelect = useCallback((id: string) => {
    setSelectedId(id)
    setIsNew(false)
  }, [])

  const handleSave = useCallback(
    async (data: { name: string; body: string; description?: string; category?: string }) => {
      let success: boolean
      if (isNew) {
        success = await createTemplate(data)
        if (success) {
          onNotify('Template created', 'success')
          setIsNew(false)
        }
      } else if (selectedId) {
        success = await updateTemplate(selectedId, data)
        if (success) onNotify('Template updated', 'success')
      } else {
        return
      }
    },
    [isNew, selectedId, createTemplate, updateTemplate, onNotify],
  )

  const handleDelete = useCallback(async () => {
    if (!selectedId) return
    const success = await deleteTemplate(selectedId)
    if (success) {
      onNotify('Template deleted', 'success')
      setSelectedId(null)
    }
  }, [selectedId, deleteTemplate, onNotify])

  const handleInsert = useCallback(
    (template: PromptTemplate) => {
      onInsert(template.body)
      onOpenChange(false)
    },
    [onInsert, onOpenChange],
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[80vh] max-w-4xl flex-col gap-0 p-0">
        <DialogHeader className="shrink-0 border-b px-6 py-4">
          <div className="flex items-center gap-3">
            <DialogTitle className="flex-1">Prompt Templates</DialogTitle>
            <Button size="sm" variant="outline" className="mr-8" onClick={handleNew}>
              <Plus className="mr-1 h-3.5 w-3.5" />
              New
            </Button>
          </div>
          <div className="relative mt-2">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search templates..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-9"
            />
          </div>
        </DialogHeader>

        <div className="flex min-h-0 flex-1">
          <div className="w-64 shrink-0 border-r">
            <TemplateList
              templates={templates}
              selectedId={selectedId}
              searchQuery={searchQuery}
              onSelect={handleSelect}
            />
          </div>
          <div className="min-w-0 flex-1">
            <TemplateForm
              template={isNew ? null : selectedTemplate}
              isNew={isNew}
              onSave={handleSave}
              onDelete={handleDelete}
              onInsert={handleInsert}
            />
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
