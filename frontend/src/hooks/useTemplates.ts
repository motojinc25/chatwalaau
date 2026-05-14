import { useCallback, useEffect, useState } from 'react'
import type { PromptTemplate } from '@/types/chat'

interface UseTemplatesReturn {
  templates: PromptTemplate[]
  isLoading: boolean
  error: string | null
  fetchTemplates: () => Promise<void>
  createTemplate: (data: { name: string; body: string; description?: string; category?: string }) => Promise<boolean>
  updateTemplate: (
    id: string,
    data: { name: string; body: string; description?: string; category?: string },
  ) => Promise<boolean>
  deleteTemplate: (id: string) => Promise<boolean>
}

export function useTemplates(): UseTemplatesReturn {
  const [templates, setTemplates] = useState<PromptTemplate[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchTemplates = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/templates')
      if (!res.ok) throw new Error('Failed to fetch templates')
      const data = await res.json()
      setTemplates(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch templates')
    } finally {
      setIsLoading(false)
    }
  }, [])

  const createTemplate = useCallback(
    async (data: { name: string; body: string; description?: string; category?: string }) => {
      setError(null)
      try {
        const res = await fetch('/api/templates', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
        })
        if (!res.ok) throw new Error('Failed to create template')
        await fetchTemplates()
        return true
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to create template')
        return false
      }
    },
    [fetchTemplates],
  )

  const updateTemplate = useCallback(
    async (id: string, data: { name: string; body: string; description?: string; category?: string }) => {
      setError(null)
      try {
        const res = await fetch(`/api/templates/${id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
        })
        if (!res.ok) throw new Error('Failed to update template')
        await fetchTemplates()
        return true
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to update template')
        return false
      }
    },
    [fetchTemplates],
  )

  const deleteTemplate = useCallback(
    async (id: string) => {
      setError(null)
      try {
        const res = await fetch(`/api/templates/${id}`, { method: 'DELETE' })
        if (!res.ok) throw new Error('Failed to delete template')
        await fetchTemplates()
        return true
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to delete template')
        return false
      }
    },
    [fetchTemplates],
  )

  useEffect(() => {
    fetchTemplates()
  }, [fetchTemplates])

  return { templates, isLoading, error, fetchTemplates, createTemplate, updateTemplate, deleteTemplate }
}
