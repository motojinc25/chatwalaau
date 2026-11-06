import { FileText, FolderOpen, History } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { WorkspaceFolderPicker } from '@/components/WorkspaceFolderPicker'
import {
  getRecentProjects,
  harnessProjectApi,
  type ProjectFolderListing,
  projectLabel,
  rememberRecentProject,
} from '@/lib/harnessProject'

/**
 * Harness project folder dialog (PRP-0244, CTR-0197 v3, UDR-0225 D1/D4).
 *
 * Chooses the folder of the coding workspace that THIS conversation's harness agent works
 * in: its file tools and shell start there and, when the harness YAML enables
 * `projectRules`, its AGENTS.md / CLAUDE.md apply as project rules. One level of folders at
 * a time (folders only), with Up, "New folder", "Use workspace root" and the five most
 * recently used folders of this browser. A folder with rules files carries a badge. The
 * choice applies from the next turn and only to this conversation; the history continues.
 */
export function HarnessProjectDialog({
  open,
  onOpenChange,
  project,
  onChoose,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The conversation's current folder ("" = workspace root). */
  project: string
  onChoose: (project: string) => void
}) {
  const [recent, setRecent] = useState<string[]>(getRecentProjects)

  const choose = (next: string) => {
    setRecent(rememberRecentProject(next))
    onChoose(next)
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto max-sm:h-[100dvh] max-sm:max-h-none sm:max-w-lg"
        data-testid="harness-project-dialog">
        <DialogHeader>
          <DialogTitle>Project folder</DialogTitle>
          <DialogDescription>
            The harness agent's file tools and shell work in this folder of the workspace. With project rules on in its
            YAML, the folder's AGENTS.md and CLAUDE.md apply too. The change applies from the next message.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium">Current</span>
          <span
            className="min-w-0 truncate rounded-md border px-2 py-0.5 font-mono text-xs"
            data-testid="harness-project-current">
            {projectLabel(project)}
          </span>
          {project !== '' && (
            <Button variant="outline" size="sm" className="h-7" onClick={() => choose('')}>
              Use workspace root
            </Button>
          )}
        </div>
        {recent.filter((p) => p !== project).length > 0 && (
          <div className="space-y-1 text-xs">
            <p className="inline-flex items-center gap-1 font-medium text-muted-foreground">
              <History className="h-3 w-3" aria-hidden="true" />
              Recent
            </p>
            <div className="flex flex-wrap gap-1">
              {recent
                .filter((p) => p !== project)
                .map((p) => (
                  <Button
                    key={p || '(root)'}
                    variant="secondary"
                    size="sm"
                    className="h-6 px-2 font-mono text-xs"
                    onClick={() => choose(p)}>
                    <FolderOpen className="mr-1 h-3 w-3" />
                    {projectLabel(p)}
                  </Button>
                ))}
            </div>
          </div>
        )}
        <WorkspaceFolderPicker<ProjectFolderListing>
          initialDir={project}
          load={harnessProjectApi.list}
          onUse={choose}
          onCancel={() => onOpenChange(false)}
          onCreate={harnessProjectApi.create}
          testId="harness-project-picker"
          badge={(listing, name) => {
            const rules = listing.entries?.find((e) => e.name === name)?.rules ?? []
            return rules.length > 0 ? (
              <span
                className="ml-auto inline-flex shrink-0 items-center gap-0.5 rounded bg-primary/10 px-1 text-[10px] text-primary"
                title={`Has ${rules.join(' and ')}`}>
                <FileText className="h-2.5 w-2.5" aria-hidden="true" />
                {rules.join(' · ')}
              </span>
            ) : null
          }}
        />
      </DialogContent>
    </Dialog>
  )
}
