import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import type { PromptTemplate } from '@/types/chat'

interface TemplateFormProps {
  template: PromptTemplate | null
  isNew: boolean
  onSave: (data: {
    name: string
    body: string
    description?: string
    category?: string
    slash_command?: string
  }) => Promise<void>
  onDelete: () => Promise<void>
  onInsert: (template: PromptTemplate) => void
}

export function TemplateForm({ template, isNew, onSave, onDelete, onInsert }: TemplateFormProps) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState('')
  const [body, setBody] = useState('')
  const [slashCommand, setSlashCommand] = useState('')
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)
  const [errors, setErrors] = useState<{ name?: string; body?: string }>({})

  useEffect(() => {
    if (isNew) {
      setName('')
      setDescription('')
      setCategory('')
      setBody('')
      setSlashCommand('')
      setErrors({})
    } else if (template) {
      setName(template.name)
      setDescription(template.description)
      setCategory(template.category)
      setBody(template.body)
      setSlashCommand(template.slash_command ?? '')
      setErrors({})
    }
  }, [template, isNew])

  const isDirty = useMemo(() => {
    if (isNew) return true
    if (!template) return false
    return (
      name !== template.name ||
      description !== template.description ||
      category !== template.category ||
      body !== template.body ||
      slashCommand !== (template.slash_command ?? '')
    )
  }, [isNew, template, name, description, category, body, slashCommand])

  const validate = useCallback(() => {
    const e: { name?: string; body?: string } = {}
    if (!name.trim()) e.name = 'Name is required'
    if (!body.trim()) e.body = 'Body is required'
    setErrors(e)
    return Object.keys(e).length === 0
  }, [name, body])

  const handleSave = useCallback(async () => {
    if (!validate()) return
    await onSave({
      name: name.trim(),
      body: body.trim(),
      description: description.trim(),
      category: category.trim(),
      slash_command: slashCommand.trim().replace(/^\/+/, ''),
    })
  }, [validate, onSave, name, body, description, category, slashCommand])

  if (!isNew && !template) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        Select a template or click + New
      </div>
    )
  }

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-4 p-6">
        <div className="flex flex-col gap-1.5">
          <label htmlFor="tpl-name" className="text-sm font-medium">
            Name <span className="text-destructive">*</span>
          </label>
          <Input id="tpl-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} />
          {errors.name && <p className="text-xs text-destructive">{errors.name}</p>}
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="tpl-category" className="text-sm font-medium">
            Category
          </label>
          <Input id="tpl-category" value={category} onChange={(e) => setCategory(e.target.value)} maxLength={50} />
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="tpl-desc" className="text-sm font-medium">
            Description
          </label>
          <Input id="tpl-desc" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} />
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="tpl-slash" className="text-sm font-medium">
            Slash command
          </label>
          <Input
            id="tpl-slash"
            value={slashCommand}
            onChange={(e) => setSlashCommand(e.target.value)}
            maxLength={50}
            placeholder="(optional; defaults to the template name)"
          />
          <p className="text-xs text-muted-foreground">
            Run this template from the chat input as <code>/{slashCommand.trim().replace(/^\/+/, '') || '<name>'}</code>
            . Use placeholders like <code>$1</code>, <code>$2</code>, or <code>$ARGUMENTS</code> in the body.
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="tpl-body" className="text-sm font-medium">
            Body <span className="text-destructive">*</span>
          </label>
          <textarea
            id="tpl-body"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={8}
            className="min-h-[200px] w-full resize-y rounded-md border bg-background px-3 py-2 text-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
          />
          {errors.body && <p className="text-xs text-destructive">{errors.body}</p>}
        </div>

        <div className="flex gap-2">
          <Button onClick={handleSave}>Save</Button>
          {!isNew && template && (
            <>
              <Button variant="destructive" onClick={() => setDeleteConfirmOpen(true)}>
                Delete
              </Button>
              <Button variant="outline" disabled={isDirty} onClick={() => onInsert(template)}>
                Insert to Chat
              </Button>
            </>
          )}
        </div>
      </div>

      <AlertDialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this template?</AlertDialogTitle>
            <AlertDialogDescription>This action cannot be undone.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                onDelete()
                setDeleteConfirmOpen(false)
              }}>
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </ScrollArea>
  )
}
