import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'

interface SaveAsTemplateDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  initialBody: string
  onSave: (data: { name: string; body: string; description?: string; category?: string }) => Promise<boolean>
  onNotify: (message: string, type: 'success' | 'error') => void
}

export function SaveAsTemplateDialog({ open, onOpenChange, initialBody, onSave, onNotify }: SaveAsTemplateDialogProps) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState('')
  const [body, setBody] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (open) {
      setBody(initialBody)
      setName(initialBody.slice(0, 50).replace(/\n/g, ' ').trim())
      setDescription('')
      setCategory('')
    }
  }, [open, initialBody])

  const handleSave = useCallback(async () => {
    if (!name.trim() || !body.trim()) return
    setSaving(true)
    const success = await onSave({
      name: name.trim(),
      body: body.trim(),
      description: description.trim(),
      category: category.trim(),
    })
    setSaving(false)
    if (success) {
      onNotify('Template saved', 'success')
      onOpenChange(false)
    } else {
      onNotify('Failed to save template', 'error')
    }
  }, [name, body, description, category, onSave, onNotify, onOpenChange])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Save as Template</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <label htmlFor="sat-name" className="text-sm font-medium">
              Name <span className="text-destructive">*</span>
            </label>
            <Input id="sat-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="sat-category" className="text-sm font-medium">
              Category
            </label>
            <Input id="sat-category" value={category} onChange={(e) => setCategory(e.target.value)} maxLength={50} />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="sat-desc" className="text-sm font-medium">
              Description
            </label>
            <Input id="sat-desc" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="sat-body" className="text-sm font-medium">
              Body <span className="text-destructive">*</span>
            </label>
            <textarea
              id="sat-body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={5}
              className="w-full resize-y rounded-md border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
        </div>
        <DialogFooter>
          <Button onClick={handleSave} disabled={saving || !name.trim() || !body.trim()}>
            {saving ? 'Saving...' : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
