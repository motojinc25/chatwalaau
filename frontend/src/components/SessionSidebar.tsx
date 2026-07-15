import {
  closestCenter,
  DndContext,
  type DragEndEvent,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  Archive,
  Brain,
  Check,
  ChevronDown,
  ChevronRight,
  Clock,
  Download,
  Folder,
  FolderOpen,
  FolderPlus,
  FolderTree,
  GripVertical,
  Image as ImageIcon,
  Info,
  Loader2,
  LogOut,
  MessageSquare,
  MoreHorizontal,
  Network,
  Palette,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Trash2,
  Upload,
  Webhook,
  Workflow,
  X,
} from 'lucide-react'
import {
  type ChangeEvent,
  type KeyboardEvent,
  memo,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { AboutDialog } from '@/components/AboutDialog'
import { DeclarativeAgentManager } from '@/components/DeclarativeAgentManager'
import { ModelSettingsManager } from '@/components/ModelSettingsManager'
import { PermissionsDisabledBanner } from '@/components/PermissionsDisabledBanner'
import { SessionSearchDialog } from '@/components/SessionSearchDialog'
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { useAuth } from '@/hooks/useAuth'
import { formatSessionDateTime } from '@/lib/datetime'
import { cn } from '@/lib/utils'
import {
  DEFAULT_FOLDER_COLOR,
  FOLDER_COLORS,
  type FolderColor,
  type ImportResult,
  type SessionFolder,
  type SessionSummary,
} from '@/types/chat'

interface SessionSidebarProps {
  sessions: SessionSummary[]
  folders: SessionFolder[]
  currentThreadId: string
  creatingFolder: boolean
  deletingFolderId: string | null
  updatingFolderId: string | null
  movingSessionId: string | null
  importing: boolean
  onSwitch: (threadId: string) => void
  onDelete: (threadId: string) => void
  onExport: (threadId: string) => void
  onImport: (file: File) => Promise<ImportResult>
  onDeleteFolder: (folderId: string) => Promise<boolean>
  onCreateFolder: (name: string, color: FolderColor) => Promise<boolean>
  onUpdateFolderColor: (folderId: string, color: FolderColor) => Promise<boolean>
  onReorderFolders: (orderedIds: string[]) => Promise<boolean>
  onMoveToFolder: (threadId: string, folderId: string | null) => Promise<boolean>
  onRename: (threadId: string, title: string) => void
  onArchive: (threadId: string) => void
  onPin: (threadId: string, pinned: boolean) => void
  onCreate: () => void
  onClose: () => void
  /**
   * Session list pagination (CTR-0016 v6, PRP-0112 Part 4 / UDR-0091 D3+D4).
   * `sessions` holds only what has been LOADED: the root pages fetched so far plus
   * the sessions of every expanded folder. It arrives already sorted by the server
   * (pinned first, then newest) and MUST NOT be re-sorted here.
   */
  hasMoreSessions: boolean
  isLoadingMoreSessions: boolean
  onLoadMoreSessions: () => void
  /** Fetch a folder's sessions COMPLETE when it is expanded (never paginated). */
  onLoadFolderSessions: (folderId: string) => void
  /** Cron scheduler launcher (CTR-0135, PRP-0089): footer icon next to App Info. */
  cronAvailable?: boolean
  onOpenCron?: () => void
  /** File Explorer launcher (CTR-0137, PRP-0091): footer icon next to Cron. */
  fileExplorerAvailable?: boolean
  onOpenFiles?: () => void
  /** Pipeline jobs launcher (CTR-0148, PRP-0096): footer icon next to Declarative Agents. */
  pipelineAvailable?: boolean
  onOpenPipeline?: () => void
  webhookAvailable?: boolean
  onOpenWebhook?: () => void
  /** Memory Management launcher (CTR-0167, PRP-0101): footer icon; always shown. */
  onOpenMemory?: () => void
  /** Ontology manager launcher (CTR-0173, PRP-0105): footer icon next to Declarative Agents. */
  ontologyAvailable?: boolean
  onOpenOntology?: () => void
}

// Per-device open/closed state (UDR-0046 D4): the set of explicitly-expanded
// folder ids is stored here; unknown / new folders default collapsed.
const FOLDER_EXPANDED_STORAGE_KEY = 'chatwalaau:folders-expanded'

// Section-level collapse (CTR-0016 v5, UDR-0091 D8). This is a DIFFERENT LAYER
// from FOLDER_EXPANDED_STORAGE_KEY above: that one records *which folders are
// open*, this one records *whether the Folders / Chats sections themselves are
// open*. Overloading one key for both would make "collapse the Folders section"
// and "close every folder" the same stored fact, which they are not -- hence the
// separate key. Absent value => both sections expanded (the pre-PRP-0112 look).
const SECTION_COLLAPSED_STORAGE_KEY = 'chatwalaau:sidebar-sections'

type SidebarSection = 'folders' | 'chats'

function loadCollapsedSections(): Set<SidebarSection> {
  try {
    const raw = localStorage.getItem(SECTION_COLLAPSED_STORAGE_KEY)
    if (!raw) return new Set()
    const parsed = JSON.parse(raw)
    if (Array.isArray(parsed)) {
      return new Set(parsed.filter((s): s is SidebarSection => s === 'folders' || s === 'chats'))
    }
  } catch {
    // Corrupt value -> fall back to the default (both expanded).
  }
  return new Set()
}

// Palette token -> theme-controlled classes (UDR-0046 D2). Written as literal
// strings so the Tailwind scanner includes them. `neutral` is the uncolored
// (pre-PRP-0070) look.
const FOLDER_COLOR_CLASSES: Record<FolderColor, { border: string; icon: string; swatch: string }> = {
  neutral: { border: 'border-l-transparent', icon: 'text-muted-foreground', swatch: 'bg-muted-foreground/40' },
  red: { border: 'border-l-red-500', icon: 'text-red-500', swatch: 'bg-red-500' },
  orange: { border: 'border-l-orange-500', icon: 'text-orange-500', swatch: 'bg-orange-500' },
  amber: { border: 'border-l-amber-500', icon: 'text-amber-500', swatch: 'bg-amber-500' },
  green: { border: 'border-l-green-500', icon: 'text-green-500', swatch: 'bg-green-500' },
  blue: { border: 'border-l-blue-500', icon: 'text-blue-500', swatch: 'bg-blue-500' },
  violet: { border: 'border-l-violet-500', icon: 'text-violet-500', swatch: 'bg-violet-500' },
  pink: { border: 'border-l-pink-500', icon: 'text-pink-500', swatch: 'bg-pink-500' },
}

function loadExpandedFolderIds(): Set<string> {
  try {
    const raw = localStorage.getItem(FOLDER_EXPANDED_STORAGE_KEY)
    if (!raw) return new Set()
    const parsed = JSON.parse(raw)
    if (Array.isArray(parsed)) {
      return new Set(parsed.filter((id): id is string => typeof id === 'string'))
    }
  } catch {
    // Corrupt localStorage value -> ignore and fall back to default-collapsed
    // (HEAL-1 client side); the next persist overwrites it with a clean value.
  }
  return new Set()
}

// NOTE: there is deliberately NO client-side sort here any more (UDR-0091 D1).
// The server returns sessions already ordered -- pinned first, then updated_at
// descending -- because a paginated client only holds a page and cannot produce a
// globally correct order from it (a pinned but old chat would sit on page 4 and
// never reach the top). Re-sorting here would silently reintroduce that bug.

/**
 * Placeholder rows shown while the next page of chats is being fetched
 * (CTR-0016 v7, v0.106.1).
 *
 * A skeleton rather than a spinner: it reserves the exact space the incoming rows
 * will occupy and shows their SHAPE, so the list does not jump when the page lands
 * and the user can see that *chats* are loading -- not merely that something is.
 * The count matches the page size the sentinel is about to request.
 */
function SessionRowSkeleton() {
  return (
    <div
      className="flex w-full items-start gap-2 border-b border-l-2 border-l-transparent border-border/30 px-3 py-1"
      aria-hidden="true">
      <div className="min-w-0 flex-1 space-y-1.5 py-0.5">
        <Skeleton className="h-3 w-[70%]" />
        <Skeleton className="h-2.5 w-[45%]" />
      </div>
    </div>
  )
}

/**
 * The meta line: timestamp + message/image counts (CTR-0016 v5/v7, UDR-0091 D10+D14).
 *
 * The counts render as ICONS, not the words "msgs" / "imgs", and they live inside
 * the SAME <p> as the timestamp. The row height with counts MUST equal the row
 * height without them -- so no wrapper, no badge, no extra line. Each pair carries
 * an accessible name because the word it replaced is gone.
 */
function SessionMeta({ session }: { session: SessionSummary }) {
  return (
    <>
      <span className="truncate">{formatSessionDateTime(session.updated_at)}</span>
      {session.message_count > 0 && (
        <span className="flex shrink-0 items-center gap-0.5" title={`${session.message_count} messages`}>
          <MessageSquare className="h-3 w-3" aria-hidden="true" />
          {session.message_count}
          {/* The word the icon replaced still reaches a screen reader, so the count
              is never announced as a bare number (UDR-0091 D10). */}
          <span className="sr-only">messages</span>
        </span>
      )}
      {session.image_count > 0 && (
        <span className="flex shrink-0 items-center gap-0.5" title={`${session.image_count} images`}>
          <ImageIcon className="h-3 w-3" aria-hidden="true" />
          {session.image_count}
          <span className="sr-only">images</span>
        </span>
      )}
    </>
  )
}

/**
 * The rename editor, handed ONLY to the row currently being renamed.
 *
 * This is the crux of the memoization (UDR-0091 D5). `renameValue` changes on
 * every keystroke, so if it -- or any callback closing over it -- were passed to
 * every row, React.memo would compare unequal props on all N rows and re-render
 * the whole list per character (the pre-PRP-0112 behavior: `renderSessionRow`'s
 * useCallback dep array contained `renameValue`). By confining the churning props
 * to a single object given to exactly one row, every other row sees `rename ===
 * undefined` on both renders and bails out.
 */
interface RenameEditor {
  value: string
  inputRef: React.RefObject<HTMLInputElement | null>
  onChange: (value: string) => void
  onKeyDown: (event: KeyboardEvent<HTMLInputElement>) => void
  onCommit: () => void
}

interface SessionRowProps {
  session: SessionSummary
  nested: boolean
  isActive: boolean
  isMoving: boolean
  folders: SessionFolder[]
  /** Present only while THIS row is being renamed (see RenameEditor). */
  rename?: RenameEditor
  onSwitch: (threadId: string) => void
  onPin: (threadId: string, pinned: boolean) => void
  onArchive: (threadId: string) => void
  onExport: (threadId: string) => void
  onMoveToFolder: (threadId: string, folderId: string | null) => Promise<boolean>
  onStartRename: (session: SessionSummary) => void
  onRequestDelete: (session: SessionSummary) => void
  onDragStart: (threadId: string) => void
  onDragEnd: () => void
}

const SessionRow = memo(function SessionRow({
  session,
  nested,
  isActive,
  isMoving,
  folders,
  rename,
  onSwitch,
  onPin,
  onArchive,
  onExport,
  onMoveToFolder,
  onStartRename,
  onRequestDelete,
  onDragStart,
  onDragEnd,
}: SessionRowProps) {
  const isRenaming = rename !== undefined
  const availableFolders = folders.filter((folder) => folder.id !== session.folder_id)

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: drag-and-drop requires drag events on the row container
    <div
      className={cn(
        // PRP-0055 follow-up: transparent baseline left-border on every row keeps
        // layout stable; the active row swaps it to primary for a clear affordance,
        // combined with the stronger bg-accent fill.
        // PRP-0112 (UDR-0091 D9): py-2.5 -> py-1 and leading-tight on both text lines
        // takes the row from ~57px to ~41px (measured in a real browser; py-1.5 left
        // it at 45.5px, over the 42px budget). The row stays TWO lines -- the density
        // is bought from padding, never by dropping the timestamp.
        'group flex w-full items-start gap-2 border-b border-l-2 border-l-transparent border-border/30 px-3 py-1 transition-colors hover:bg-muted/50',
        nested && 'border-b-0 bg-background/50 pl-9',
        isActive && 'border-l-primary bg-accent hover:bg-accent',
      )}
      draggable={!isRenaming}
      onDragStart={(event) => {
        if (isRenaming) {
          event.preventDefault()
          return
        }
        event.dataTransfer.effectAllowed = 'move'
        event.dataTransfer.setData('text/plain', session.thread_id)
        onDragStart(session.thread_id)
      }}
      onDragEnd={onDragEnd}>
      <button
        type="button"
        className="min-w-0 flex-1 cursor-pointer text-left"
        onClick={() => !isRenaming && onSwitch(session.thread_id)}>
        {rename ? (
          <Input
            ref={rename.inputRef}
            type="text"
            value={rename.value}
            onChange={(e) => rename.onChange(e.target.value)}
            onKeyDown={rename.onKeyDown}
            onBlur={rename.onCommit}
            className="h-8"
          />
        ) : (
          <>
            <p className="flex items-center gap-1 truncate text-sm leading-tight">
              {session.pinned_at && <Pin className="h-3 w-3 shrink-0 text-muted-foreground" />}
              <span className="truncate">{session.title || 'New session'}</span>
              {/* Auto Session Title in progress (PRP-0077, CTR-0109): a small
                  spinner until the background title task finalizes (cleared by
                  the CTR-0110 push). */}
              {session.auto_title_pending && (
                <Loader2 className="h-3 w-3 shrink-0 animate-spin text-muted-foreground" />
              )}
              {isMoving && <Loader2 className="h-3 w-3 shrink-0 animate-spin" />}
            </p>
            {/*
              The meta line is REVEALED ON HOVER (CTR-0016 v7, UDR-0091 D14, v0.106.1).
              The dense two-line row read as cluttered, so at rest a row shows only its
              title.

              Two constraints shape this:

              1. NO LAYOUT SHIFT. The line is faded with `opacity`, never unmounted and
                 never `hidden` -- its box keeps its height, so the row is a constant
                 41.5px whether hovered or not. If it collapsed, hovering would grow the
                 row, push the rows below it, and move the cursor onto a different row --
                 a jitter loop.

              2. NO MEDIA-QUERY "TOUCH ESCAPE". v0.106.1 first shipped a hover-media escape
                 (an arbitrary variant on the CSS `hover: none` feature) so that a touch
                 device -- where hovering cannot happen -- would still show the timestamp. It
                 was WRONG, and it broke the feature on ordinary desktops: a TOUCHSCREEN
                 LAPTOP reports `hover: none`, because its PRIMARY pointer is the screen,
                 even though a mouse is attached and hovering works perfectly. Measured in
                 Chromium, a desktop-sized window with a touchscreen reports `none` for BOTH
                 the `hover` and the `any-hover` feature, so neither can tell a phone apart
                 from a laptop with a touchscreen. The escape therefore pinned the line
                 permanently visible on exactly the machines this app runs on. Both variants
                 are removed: the reveal is hover-only, unconditionally.

                 (Do not re-add one. Beyond being wrong, Tailwind scans this file as plain
                 text and would generate the utility from the prose alone.)

                 What keeps this acceptable on a genuinely touch-only device: the line is
                 only FADED, never removed, so it stays in the accessibility tree and its
                 `title` tooltips (D10) remain; and recency -- the reason D9 wanted the
                 timestamp visible -- is already encoded positionally by the newest-first
                 ordering of D1, so scanning for a recent chat means reading the ORDER, not
                 the timestamps.

              The ACTIVE row also keeps it visible: you should not have to hover the chat
              you already have open to see when it was last touched.

              ONLY THE DETAIL FADES. The `API` / `Teams` origin badges stay at full opacity
              and lead the line. They are a CLASSIFICATION -- "this conversation did not come
              from the web UI" -- not a detail you go looking for, so hiding them until hover
              would cost the scan value they exist for. The timestamp and the counts are the
              details; those are what the operator asked to quieten.
            */}
            <p className="flex items-center gap-1.5 text-xs leading-tight text-muted-foreground">
              {session.source === 'openai-api' && (
                <span className="inline-block shrink-0 rounded bg-blue-100 px-1 py-0.5 text-[10px] font-medium leading-none text-blue-700 dark:bg-blue-900 dark:text-blue-300">
                  API
                </span>
              )}
              {session.source === 'teams' && (
                <span className="inline-block shrink-0 rounded bg-indigo-100 px-1 py-0.5 text-[10px] font-medium leading-none text-indigo-700 dark:bg-indigo-900 dark:text-indigo-300">
                  Teams
                </span>
              )}
              <span
                className={cn(
                  'flex min-w-0 items-center gap-1.5 transition-opacity duration-150',
                  'opacity-0 group-hover:opacity-100',
                  isActive && 'opacity-100',
                )}>
                <SessionMeta session={session} />
              </span>
            </p>
          </>
        )}
      </button>
      {!isRenaming && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            {/* h-6 w-6 is kept deliberately: the row got shorter (UDR-0091 D9) but the
                menu must retain its 24px hit target. */}
            {/* biome-ignore lint/a11y/useSemanticElements: nested interactive, span is intentional */}
            <span
              role="button"
              tabIndex={-1}
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              aria-label="Session options">
              <MoreHorizontal className="h-3 w-3" />
            </span>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            <DropdownMenuItem onClick={() => onPin(session.thread_id, !session.pinned_at)}>
              {session.pinned_at ? (
                <>
                  <PinOff className="mr-2 h-3.5 w-3.5" />
                  Unpin
                </>
              ) : (
                <>
                  <Pin className="mr-2 h-3.5 w-3.5" />
                  Pin
                </>
              )}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => onStartRename(session)}>
              <Pencil className="mr-2 h-3.5 w-3.5" />
              Rename
            </DropdownMenuItem>
            {folders.length > 0 ? (
              <DropdownMenuSub>
                <DropdownMenuSubTrigger>
                  <FolderPlus className="mr-2 h-3.5 w-3.5" />
                  Move to folder
                </DropdownMenuSubTrigger>
                <DropdownMenuSubContent className="w-44">
                  {availableFolders.length > 0 ? (
                    availableFolders.map((folder) => (
                      <DropdownMenuItem
                        key={folder.id}
                        disabled={isMoving}
                        onClick={() => void onMoveToFolder(session.thread_id, folder.id)}>
                        <Folder
                          className={cn(
                            'mr-2 h-3.5 w-3.5',
                            (FOLDER_COLOR_CLASSES[folder.color] ?? FOLDER_COLOR_CLASSES[DEFAULT_FOLDER_COLOR]).icon,
                          )}
                        />
                        {folder.name}
                      </DropdownMenuItem>
                    ))
                  ) : (
                    <DropdownMenuItem disabled>No other folders</DropdownMenuItem>
                  )}
                </DropdownMenuSubContent>
              </DropdownMenuSub>
            ) : (
              <DropdownMenuItem disabled>
                <FolderPlus className="mr-2 h-3.5 w-3.5" />
                No folders yet
              </DropdownMenuItem>
            )}
            {session.folder_id && (
              <DropdownMenuItem disabled={isMoving} onClick={() => void onMoveToFolder(session.thread_id, null)}>
                <FolderOpen className="mr-2 h-3.5 w-3.5" />
                Remove from folder
              </DropdownMenuItem>
            )}
            <DropdownMenuItem onClick={() => onArchive(session.thread_id)}>
              <Archive className="mr-2 h-3.5 w-3.5" />
              Archive
            </DropdownMenuItem>
            {/* Session Export (PRP-0084, CTR-0016 v4): download this chat as
                a self-contained ZIP bundle (session JSON + its uploads). */}
            <DropdownMenuItem onClick={() => onExport(session.thread_id)}>
              <Download className="mr-2 h-3.5 w-3.5" />
              Export
            </DropdownMenuItem>
            <DropdownMenuItem
              className="text-destructive focus:text-destructive"
              onClick={() => onRequestDelete(session)}>
              <Trash2 className="mr-2 h-3.5 w-3.5" />
              Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  )
})

/** Collapsible section header for the Folders / Chats sections (UDR-0091 D8). */
function SectionHeader({
  icon,
  label,
  collapsed,
  onToggle,
  children,
}: {
  icon: ReactNode
  label: string
  collapsed: boolean
  onToggle: () => void
  children?: ReactNode
}) {
  return (
    <div className="flex items-center justify-between px-3 pb-1">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        className="flex min-w-0 flex-1 items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground transition-colors hover:text-foreground">
        {collapsed ? (
          <ChevronRight className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 shrink-0" />
        )}
        {icon}
        {label}
      </button>
      {children}
    </div>
  )
}

interface PaletteSwatchesProps {
  value: FolderColor
  onSelect: (color: FolderColor) => void
  disabled?: boolean
}

function PaletteSwatches({ value, onSelect, disabled }: PaletteSwatchesProps) {
  return (
    <div className="flex flex-wrap gap-2">
      {FOLDER_COLORS.map((color) => (
        <button
          key={color}
          type="button"
          disabled={disabled}
          onClick={() => onSelect(color)}
          aria-label={`Color ${color}`}
          aria-pressed={value === color}
          className={cn(
            'flex h-7 w-7 items-center justify-center rounded-full border transition-transform hover:scale-110 disabled:opacity-50',
            FOLDER_COLOR_CLASSES[color].swatch,
            value === color ? 'ring-2 ring-ring ring-offset-1 ring-offset-background' : 'border-transparent',
          )}>
          {value === color && <Check className="h-3.5 w-3.5 text-white drop-shadow" />}
        </button>
      ))}
    </div>
  )
}

interface FolderGroupProps {
  folder: SessionFolder
  groupedSessions: SessionSummary[]
  isCollapsed: boolean
  isDropTarget: boolean
  deletingFolderId: string | null
  updatingFolderId: string | null
  draggedSessionId: string | null
  onToggle: (folderId: string) => void
  onDragOverFolder: (folderId: string) => void
  onDragLeaveFolder: (folderId: string) => void
  onDropSession: (folderId: string) => void
  onOpenColor: (folder: SessionFolder) => void
  onDeleteFolder: (folder: SessionFolder) => void
  renderSessionRow: (session: SessionSummary, nested?: boolean) => ReactNode
}

function FolderGroup({
  folder,
  groupedSessions,
  isCollapsed,
  isDropTarget,
  deletingFolderId,
  updatingFolderId,
  draggedSessionId,
  onToggle,
  onDragOverFolder,
  onDragLeaveFolder,
  onDropSession,
  onOpenColor,
  onDeleteFolder,
  renderSessionRow,
}: FolderGroupProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: folder.id })
  const colorClasses = FOLDER_COLOR_CLASSES[folder.color] ?? FOLDER_COLOR_CLASSES[DEFAULT_FOLDER_COLOR]
  const style = { transform: CSS.Transform.toString(transform), transition }

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={cn('border-t border-border/20 first:border-t-0', isDragging && 'z-10 opacity-80')}>
      {/* biome-ignore lint/a11y/noStaticElementInteractions: folder row is a drag target for native session DnD */}
      <div
        className={cn(
          'group flex items-center gap-1 border-l-2 px-2 py-2 transition-colors',
          colorClasses.border,
          isDropTarget && 'bg-accent/80',
        )}
        onDragOver={(event) => {
          if (!draggedSessionId) return
          event.preventDefault()
          event.dataTransfer.dropEffect = 'move'
          onDragOverFolder(folder.id)
        }}
        onDragLeave={() => onDragLeaveFolder(folder.id)}
        onDrop={(event) => {
          if (!draggedSessionId) return
          event.preventDefault()
          onDropSession(folder.id)
        }}>
        {/* @dnd-kit drag handle: `attributes` injects role="button" + aria-roledescription at runtime, which Biome cannot see statically. */}
        {/* biome-ignore lint/a11y/useAriaPropsSupportedByRole: role is supplied at runtime by @dnd-kit `attributes` */}
        <span
          aria-label="Reorder folder"
          className="flex h-6 w-4 shrink-0 cursor-grab touch-none items-center justify-center text-muted-foreground/60 hover:text-foreground active:cursor-grabbing"
          {...attributes}
          {...listeners}>
          <GripVertical className="h-3.5 w-3.5" />
        </span>
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          onClick={() => onToggle(folder.id)}>
          {isCollapsed ? (
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          )}
          <Folder className={cn('h-3.5 w-3.5 shrink-0', colorClasses.icon)} />
          <span className="truncate text-sm font-medium">{folder.name}</span>
          {/* The count comes from the SERVER (folder.session_count), not from the loaded
              sessions. Since PRP-0112 a folder's chats are fetched only when it is expanded
              (UDR-0091 D4), so `groupedSessions.length` counts only what happens to be
              loaded -- which is ZERO for every collapsed folder. That was the v0.106.1 bug:
              every folder read "0" until you clicked it. */}
          <span className="rounded-full bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {folder.session_count}
          </span>
        </button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            {/* biome-ignore lint/a11y/useSemanticElements: nested interactive, span is intentional */}
            <span
              role="button"
              tabIndex={-1}
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              aria-label="Folder options">
              {deletingFolderId === folder.id || updatingFolderId === folder.id ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <MoreHorizontal className="h-3 w-3" />
              )}
            </span>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-40">
            <DropdownMenuItem onClick={() => onOpenColor(folder)}>
              <Palette className="mr-2 h-3.5 w-3.5" />
              Change color
            </DropdownMenuItem>
            <DropdownMenuItem
              className="text-destructive focus:text-destructive"
              disabled={deletingFolderId === folder.id}
              onClick={() => onDeleteFolder(folder)}>
              <Trash2 className="mr-2 h-3.5 w-3.5" />
              Delete folder
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {!isCollapsed && (
        <div className="pb-1">
          {groupedSessions.length > 0 ? (
            groupedSessions.map((session) => renderSessionRow(session, true))
          ) : folder.session_count > 0 ? (
            // Expanded, and the server says it HAS chats -- so the fetch is still in
            // flight (UDR-0091 D4 loads a folder lazily). Show skeletons, not the
            // "empty folder" message, which would be a lie for a moment.
            Array.from({ length: Math.min(folder.session_count, 3) }, (_, i) => (
              <SessionRowSkeleton key={`skeleton-${folder.id}-${i}`} />
            ))
          ) : (
            <div className="px-9 py-2 text-xs text-muted-foreground">Drop chats here or use the menu</div>
          )}
        </div>
      )}
    </div>
  )
}

export function SessionSidebar({
  sessions,
  folders,
  currentThreadId,
  creatingFolder,
  deletingFolderId,
  updatingFolderId,
  movingSessionId,
  importing,
  onSwitch,
  onDelete,
  onExport,
  onImport,
  onDeleteFolder,
  onCreateFolder,
  onUpdateFolderColor,
  onReorderFolders,
  onMoveToFolder,
  onRename,
  onArchive,
  onPin,
  onCreate,
  onClose,
  hasMoreSessions,
  isLoadingMoreSessions,
  onLoadMoreSessions,
  onLoadFolderSessions,
  cronAvailable,
  onOpenCron,
  fileExplorerAvailable,
  onOpenFiles,
  pipelineAvailable,
  onOpenPipeline,
  webhookAvailable,
  onOpenWebhook,
  onOpenMemory,
  ontologyAvailable,
  onOpenOntology,
}: SessionSidebarProps) {
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null)
  const [deleteFolderTarget, setDeleteFolderTarget] = useState<SessionFolder | null>(null)
  const [colorTarget, setColorTarget] = useState<SessionFolder | null>(null)
  const [searchOpen, setSearchOpen] = useState(false)
  const [aboutOpen, setAboutOpen] = useState(false)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [createFolderOpen, setCreateFolderOpen] = useState(false)
  const [folderName, setFolderName] = useState('')
  const [newFolderColor, setNewFolderColor] = useState<FolderColor>(DEFAULT_FOLDER_COLOR)
  // UDR-0046 D4: default collapsed. We track the explicitly-EXPANDED set, so a
  // new / unknown folder defaults collapsed without an extra write.
  const [expandedFolderIds, setExpandedFolderIds] = useState<Set<string>>(loadExpandedFolderIds)
  // Section-level collapse (UDR-0091 D8): a SEPARATE layer from the per-folder set
  // above, under its own storage key. We track the explicitly-COLLAPSED sections so
  // an absent preference means "both expanded" -- the pre-PRP-0112 look.
  const [collapsedSections, setCollapsedSections] = useState<Set<SidebarSection>>(loadCollapsedSections)
  const [draggedSessionId, setDraggedSessionId] = useState<string | null>(null)
  const [dropFolderId, setDropFolderId] = useState<string | null>(null)
  // Infinite-scroll sentinel for the Chats section (UDR-0091 D3/D5). Pagination
  // bounds the DOM to what the user actually scrolled to, so no virtualization
  // dependency is needed (UDR-0050's deferral upheld).
  const loadMoreSentinelRef = useRef<HTMLDivElement>(null)
  const renameRef = useRef<HTMLInputElement>(null)
  const folderNameRef = useRef<HTMLInputElement>(null)
  // Session Import file picker (PRP-0084, CTR-0016 v4).
  const importInputRef = useRef<HTMLInputElement>(null)
  // Import outcome notice (CTR-0016 v5): a hard failure or a partial-import
  // warning is shown in a dialog instead of being silently swallowed.
  const [importNotice, setImportNotice] = useState<{
    kind: 'error' | 'warning'
    messages: string[]
  } | null>(null)

  const handleImportChange = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0]
      // Reset the input so re-selecting the same file fires onChange again.
      event.target.value = ''
      if (!file) return
      const result = await onImport(file)
      if (!result.ok) {
        setImportNotice({ kind: 'error', messages: [result.error ?? 'Import failed.'] })
      } else if (result.warnings && result.warnings.length > 0) {
        setImportNotice({ kind: 'warning', messages: result.warnings })
      }
    },
    [onImport],
  )

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  // Web SPA auth (CTR-0096, PRP-0057): the logout entry is only meaningful
  // when the operator has enabled the ID/PW lane AND the current visitor
  // is authenticated via that lane. For loopback / API_KEY-only / open
  // modes the button is hidden.
  const auth = useAuth()
  const showLogout = auth.mode === 'login-required' && auth.authenticated
  const handleLogout = useCallback(async () => {
    await auth.logout()
    window.location.assign('/login')
  }, [auth])

  const folderMap = useMemo(() => new Map(folders.map((folder) => [folder.id, folder])), [folders])
  // No sort here (UDR-0091 D1): `sessions` arrives from the server already ordered
  // pinned-first then newest-first. Re-sorting a partially-loaded list would be
  // wrong anyway -- a pinned but old chat may not be on the loaded page.
  const rootSessions = useMemo(
    () => sessions.filter((session) => !session.folder_id || !folderMap.has(session.folder_id)),
    [folderMap, sessions],
  )
  // Folders keep their persisted manual order (UDR-0046 D1/D6); `folders`
  // already arrives sorted by `order` ascending from CTR-0015.
  const folderGroups = useMemo(
    () =>
      folders.map((folder) => ({
        folder,
        sessions: sessions.filter((session) => session.folder_id === folder.id),
      })),
    [folders, sessions],
  )
  // The delete-folder confirmation tells the operator how many chats the action touches,
  // so it MUST use the server's count. Deriving it from the loaded sessions (as it did
  // before v0.106.2) made a collapsed folder report "0 sessions inside it" no matter how
  // many it actually held -- a destructive action under-reporting its own blast radius.
  const deleteFolderSessionCount = useMemo(
    () => folders.find((folder) => folder.id === deleteFolderTarget?.id)?.session_count ?? 0,
    [deleteFolderTarget?.id, folders],
  )

  // Keep the color modal's preview in sync with the live folder record.
  const colorTargetLive = useMemo(
    () => (colorTarget ? (folderMap.get(colorTarget.id) ?? colorTarget) : null),
    [colorTarget, folderMap],
  )

  useEffect(() => {
    if (renamingId) renameRef.current?.focus()
  }, [renamingId])

  useEffect(() => {
    if (createFolderOpen) folderNameRef.current?.focus()
  }, [createFolderOpen])

  // Persist the expanded set to localStorage (per device, UDR-0046 D4).
  useEffect(() => {
    try {
      localStorage.setItem(FOLDER_EXPANDED_STORAGE_KEY, JSON.stringify([...expandedFolderIds]))
    } catch {
      // ignore storage failures (private mode / quota)
    }
  }, [expandedFolderIds])

  // Persist the collapsed SECTION set under its own key (UDR-0091 D8).
  useEffect(() => {
    try {
      localStorage.setItem(SECTION_COLLAPSED_STORAGE_KEY, JSON.stringify([...collapsedSections]))
    } catch {
      // ignore storage failures (private mode / quota)
    }
  }, [collapsedSections])

  // An expanded folder is fetched COMPLETE (UDR-0091 D4). Runs for folders already
  // expanded from a previous session too, since the set is restored from
  // localStorage; onLoadFolderSessions de-dupes so repeats are free.
  useEffect(() => {
    for (const folderId of expandedFolderIds) {
      if (folderMap.has(folderId)) onLoadFolderSessions(folderId)
    }
  }, [expandedFolderIds, folderMap, onLoadFolderSessions])

  // Infinite scroll for the Chats section (UDR-0091 D3): append the next page when
  // the sentinel below the last row scrolls into view.
  useEffect(() => {
    const node = loadMoreSentinelRef.current
    if (!node || !hasMoreSessions || collapsedSections.has('chats')) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) onLoadMoreSessions()
      },
      { rootMargin: '120px' },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [hasMoreSessions, collapsedSections, onLoadMoreSessions])

  const toggleSection = useCallback((section: SidebarSection) => {
    setCollapsedSections((prev) => {
      const next = new Set(prev)
      if (next.has(section)) next.delete(section)
      else next.add(section)
      return next
    })
  }, [])

  // Auto-expand the active session's folder so the current chat is always visible.
  useEffect(() => {
    const activeFolderId = sessions.find((session) => session.thread_id === currentThreadId)?.folder_id
    if (!activeFolderId) return
    setExpandedFolderIds((prev) => {
      if (prev.has(activeFolderId)) return prev
      const next = new Set(prev)
      next.add(activeFolderId)
      return next
    })
  }, [currentThreadId, sessions])

  const startRename = useCallback((session: SessionSummary) => {
    setRenamingId(session.thread_id)
    setRenameValue(session.title || '')
  }, [])

  const commitRename = useCallback(() => {
    if (!renamingId) return
    const trimmed = renameValue.trim()
    if (trimmed) {
      onRename(renamingId, trimmed)
    }
    setRenamingId(null)
    setRenameValue('')
  }, [renamingId, renameValue, onRename])

  const cancelRename = useCallback(() => {
    setRenamingId(null)
    setRenameValue('')
  }, [])

  const handleRenameKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      // Skip while an IME composition is in progress (CJK conversion).
      if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
        e.preventDefault()
        commitRename()
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        cancelRename()
      }
    },
    [commitRename, cancelRename],
  )

  const handleCreateFolder = useCallback(async () => {
    const created = await onCreateFolder(folderName, newFolderColor)
    if (!created) return
    setCreateFolderOpen(false)
    setFolderName('')
    setNewFolderColor(DEFAULT_FOLDER_COLOR)
  }, [folderName, newFolderColor, onCreateFolder])

  const handleCreateFolderKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      // Skip while an IME composition is in progress (CJK conversion).
      if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
        e.preventDefault()
        void handleCreateFolder()
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setCreateFolderOpen(false)
        setFolderName('')
      }
    },
    [handleCreateFolder],
  )

  const toggleFolder = useCallback((folderId: string) => {
    setExpandedFolderIds((prev) => {
      const next = new Set(prev)
      if (next.has(folderId)) next.delete(folderId)
      else next.add(folderId)
      return next
    })
  }, [])

  const handleFolderDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event
      if (!over || active.id === over.id) return
      const ids = folderGroups.map((group) => group.folder.id)
      const oldIndex = ids.indexOf(active.id as string)
      const newIndex = ids.indexOf(over.id as string)
      if (oldIndex < 0 || newIndex < 0) return
      void onReorderFolders(arrayMove(ids, oldIndex, newIndex))
    },
    [folderGroups, onReorderFolders],
  )

  const handleColorSelect = useCallback(
    async (color: FolderColor) => {
      if (!colorTarget) return
      const ok = await onUpdateFolderColor(colorTarget.id, color)
      if (ok) setColorTarget(null)
    },
    [colorTarget, onUpdateFolderColor],
  )

  const handleSessionDragStart = useCallback((threadId: string) => {
    setDraggedSessionId(threadId)
  }, [])

  const handleSessionDragEnd = useCallback(() => {
    setDraggedSessionId(null)
    setDropFolderId(null)
  }, [])

  const handleDragOverFolder = useCallback((folderId: string) => setDropFolderId(folderId), [])
  const handleDragLeaveFolder = useCallback(
    (folderId: string) => setDropFolderId((prev) => (prev === folderId ? null : prev)),
    [],
  )
  const handleDropSession = useCallback(
    (folderId: string) => {
      setDropFolderId(null)
      const sessionId = draggedSessionId
      setDraggedSessionId(null)
      if (sessionId) void onMoveToFolder(sessionId, folderId)
    },
    [draggedSessionId, onMoveToFolder],
  )

  // The churning rename state is bundled here and handed to exactly ONE row, so
  // every other row's props stay referentially equal across a keystroke and
  // React.memo bails them out (UDR-0091 D5).
  const renameEditor = useMemo<RenameEditor>(
    () => ({
      value: renameValue,
      inputRef: renameRef,
      onChange: setRenameValue,
      onKeyDown: handleRenameKeyDown,
      onCommit: commitRename,
    }),
    [renameValue, handleRenameKeyDown, commitRename],
  )

  const renderSessionRow = useCallback(
    (session: SessionSummary, nested = false) => (
      <SessionRow
        key={session.thread_id}
        session={session}
        nested={nested}
        isActive={session.thread_id === currentThreadId}
        isMoving={movingSessionId === session.thread_id}
        folders={folders}
        rename={renamingId === session.thread_id ? renameEditor : undefined}
        onSwitch={onSwitch}
        onPin={onPin}
        onArchive={onArchive}
        onExport={onExport}
        onMoveToFolder={onMoveToFolder}
        onStartRename={startRename}
        onRequestDelete={setDeleteTarget}
        onDragStart={handleSessionDragStart}
        onDragEnd={handleSessionDragEnd}
      />
    ),
    [
      currentThreadId,
      folders,
      handleSessionDragEnd,
      handleSessionDragStart,
      movingSessionId,
      onArchive,
      onExport,
      onMoveToFolder,
      onPin,
      onSwitch,
      renameEditor,
      renamingId,
      startRename,
    ],
  )

  return (
    <aside className="flex h-full w-[307px] shrink-0 flex-col border-r bg-muted/30">
      <div className="flex h-12 shrink-0 items-center justify-between border-b px-3">
        <div className="flex items-center gap-2">
          <img src="/favicon.svg" alt="ChatWalaʻau" className="h-5 w-5" />
          <span className="text-sm font-medium">ChatWalaʻau</span>
          {auth.demoMode && (
            <span
              className="rounded-sm bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-300"
              title="Demo Mode: every LLM / TTS / STT / image / embedding call is scripted. Open-Meteo (Weather) is the only live external API.">
              Demo
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={() => setSearchOpen(true)}
            aria-label="Search sessions">
            <Search className="h-4 w-4" />
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onCreate} aria-label="New session">
            <Plus className="h-4 w-4" />
          </Button>
          {showLogout && (
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={handleLogout}
              aria-label="Sign out"
              title={auth.username ? `Sign out (${auth.username})` : 'Sign out'}>
              <LogOut className="h-4 w-4" />
            </Button>
          )}
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onClose} aria-label="Close sidebar">
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {auth.toolApprovalMode === 'skip' && (
        <div className="border-b px-3 pt-3">
          <PermissionsDisabledBanner />
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        <section className="border-b border-border/50 py-2">
          <SectionHeader
            icon={<FolderOpen className="h-3.5 w-3.5" />}
            label="Folders"
            collapsed={collapsedSections.has('folders')}
            onToggle={() => toggleSection('folders')}>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={() => setCreateFolderOpen(true)}
              aria-label="Create folder"
              disabled={creatingFolder}>
              {creatingFolder ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderPlus className="h-4 w-4" />}
            </Button>
          </SectionHeader>
          {!collapsedSections.has('folders') && (
            <>
              {folderGroups.length === 0 && (
                <div className="px-3 py-2 text-xs text-muted-foreground">No folders yet</div>
              )}
              <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleFolderDragEnd}>
                <SortableContext
                  items={folderGroups.map((group) => group.folder.id)}
                  strategy={verticalListSortingStrategy}>
                  {folderGroups.map(({ folder, sessions: groupedSessions }) => (
                    <FolderGroup
                      key={folder.id}
                      folder={folder}
                      groupedSessions={groupedSessions}
                      isCollapsed={!expandedFolderIds.has(folder.id)}
                      isDropTarget={dropFolderId === folder.id}
                      deletingFolderId={deletingFolderId}
                      updatingFolderId={updatingFolderId}
                      draggedSessionId={draggedSessionId}
                      onToggle={toggleFolder}
                      onDragOverFolder={handleDragOverFolder}
                      onDragLeaveFolder={handleDragLeaveFolder}
                      onDropSession={handleDropSession}
                      onOpenColor={setColorTarget}
                      onDeleteFolder={setDeleteFolderTarget}
                      renderSessionRow={renderSessionRow}
                    />
                  ))}
                </SortableContext>
              </DndContext>
            </>
          )}
        </section>

        <section className="py-2">
          <SectionHeader
            icon={<Plus className="h-3.5 w-3.5" />}
            label="Chats"
            collapsed={collapsedSections.has('chats')}
            onToggle={() => toggleSection('chats')}>
            {/* Session Import (PRP-0084, CTR-0016 v4): upload a ZIP bundle as a
                new chat. The animated indicator shows until import completes,
                after which useSession refreshes the list and selects it. */}
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={() => importInputRef.current?.click()}
              aria-label="Import chat"
              title="Import chat from a .zip bundle"
              disabled={importing}>
              {importing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            </Button>
            <input
              ref={importInputRef}
              type="file"
              accept=".zip,application/zip"
              className="hidden"
              onChange={handleImportChange}
            />
          </SectionHeader>
          {!collapsedSections.has('chats') && (
            <>
              {rootSessions.length === 0 ? (
                <div className="px-3 py-2 text-xs text-muted-foreground">
                  {folderGroups.length > 0 ? 'No root chats' : 'No sessions yet'}
                </div>
              ) : (
                rootSessions.map((session) => renderSessionRow(session))
              )}
              {/* Infinite-scroll sentinel (UDR-0091 D3): entering the viewport appends
                  the next page. Rendered only while more chats remain.

                  While the page is in flight we show SKELETON ROWS rather than a spinner
                  (CTR-0016 v7, v0.106.1): they occupy the space the incoming rows will
                  take and show their shape, so the list does not jump when the page lands
                  and it is obvious that *chats* are loading. The sentinel keeps a small
                  idle height so it can still be observed before the fetch starts. */}
              {hasMoreSessions && (
                <div ref={loadMoreSentinelRef}>
                  {isLoadingMoreSessions ? (
                    <>
                      <SessionRowSkeleton />
                      <SessionRowSkeleton />
                      <SessionRowSkeleton />
                    </>
                  ) : (
                    <div className="h-8" />
                  )}
                </div>
              )}
            </>
          )}
        </section>
      </div>

      {/* App info footer (CTR-0101, FEAT-0029): tool launchers + About. The version
          label was removed here (CTR-0176); it stays visible via the About dialog. */}
      <div className="flex h-9 shrink-0 items-center justify-end border-t px-3">
        <div className="flex items-center gap-1">
          {/* Ontology manager launcher (CTR-0173, PRP-0105): next to Declarative Agents;
              shown only when ONTOLOGY_ENABLED (probed via GET /api/ontology/catalog). */}
          {ontologyAvailable && (
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-muted-foreground"
              onClick={() => onOpenOntology?.()}
              aria-label="Ontology"
              title="Ontology (concept models)">
              <Network className="h-4 w-4" />
            </Button>
          )}
          {/* Declarative Agent management (CTR-0144, PRP-0094): self-probing icon next to
              the File Explorer icon; always shown when the endpoint is reachable. */}
          <DeclarativeAgentManager />
          {/* Webhook gateway launcher (CTR-0157, PRP-0097): next to Declarative Agents;
              shown only when WEBHOOK_ENABLED. */}
          {webhookAvailable && (
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-muted-foreground"
              onClick={() => onOpenWebhook?.()}
              aria-label="Webhooks"
              title="Webhooks">
              <Webhook className="h-4 w-4" />
            </Button>
          )}
          {/* Pipeline jobs launcher (CTR-0148, PRP-0096): shown only when PIPELINE_ENABLED. */}
          {pipelineAvailable && (
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-muted-foreground"
              onClick={() => onOpenPipeline?.()}
              aria-label="Pipeline jobs"
              title="Pipeline jobs">
              <Workflow className="h-4 w-4" />
            </Button>
          )}
          {/* File Explorer launcher (CTR-0137, PRP-0091): shown only when FILE_EXPLORER_ENABLED. */}
          {fileExplorerAvailable && (
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-muted-foreground"
              onClick={() => onOpenFiles?.()}
              aria-label="File Explorer"
              title="File Explorer">
              <FolderTree className="h-4 w-4" />
            </Button>
          )}
          {/* Cron scheduler launcher (CTR-0135, PRP-0089): shown only when CRON_ENABLED. */}
          {cronAvailable && (
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-muted-foreground"
              onClick={() => onOpenCron?.()}
              aria-label="Cron scheduler"
              title="Cron scheduler">
              <Clock className="h-4 w-4" />
            </Button>
          )}
          {/* Memory Management launcher (CTR-0167, PRP-0101): edit the built-in
              IDENTITY / USER / MEMORY files. Always shown (identity always exists). */}
          {onOpenMemory && (
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-muted-foreground"
              onClick={() => onOpenMemory()}
              aria-label="Agent memory"
              title="Agent memory">
              <Brain className="h-4 w-4" />
            </Button>
          )}
          {/* Model Settings (CTR-0176, PRP-0111): self-probing icon next to About;
              shown when GET /api/model-offerings is reachable. */}
          <ModelSettingsManager />
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-muted-foreground"
            onClick={() => setAboutOpen(true)}
            aria-label="About ChatWalaʻau"
            title="About ChatWalaʻau">
            <Info className="h-4 w-4" />
          </Button>
        </div>
      </div>

      <AlertDialog open={deleteTarget !== null} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete session?</AlertDialogTitle>
            <AlertDialogDescription>
              &quot;{deleteTarget?.title || 'New session'}&quot; will be permanently deleted. This action cannot be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                if (deleteTarget) {
                  onDelete(deleteTarget.thread_id)
                  setDeleteTarget(null)
                }
              }}>
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Import outcome notice (CTR-0016 v5): a hard failure shows the server's
          reason; a successful-but-partial import lists what was skipped or
          carried with a caveat so the operator knows it may not be fully
          faithful. Previously any failure was silent. */}
      <AlertDialog open={importNotice !== null} onOpenChange={(open) => !open && setImportNotice(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {importNotice?.kind === 'error' ? 'Import failed' : 'Imported with warnings'}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {importNotice?.kind === 'error'
                ? 'The chat could not be imported:'
                : 'The chat was imported, but some attachments may not appear exactly as in the original:'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <ul className="max-h-60 list-disc space-y-1 overflow-y-auto pl-5 text-sm text-muted-foreground">
            {importNotice?.messages.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setImportNotice(null)}>OK</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={deleteFolderTarget !== null} onOpenChange={(open) => !open && setDeleteFolderTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete folder?</AlertDialogTitle>
            <AlertDialogDescription>
              &quot;{deleteFolderTarget?.name || 'Folder'}&quot; will be removed. The {deleteFolderSessionCount} session
              {deleteFolderSessionCount === 1 ? '' : 's'} inside it will stay intact and return to the Chats section.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={!!deletingFolderId}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={!deleteFolderTarget || !!deletingFolderId}
              onClick={async () => {
                if (!deleteFolderTarget) return
                const deleted = await onDeleteFolder(deleteFolderTarget.id)
                if (deleted) setDeleteFolderTarget(null)
              }}>
              {deletingFolderId ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Deleting...
                </>
              ) : (
                'Delete folder'
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog
        open={createFolderOpen}
        onOpenChange={(open) => {
          setCreateFolderOpen(open)
          if (!open) {
            setFolderName('')
            setNewFolderColor(DEFAULT_FOLDER_COLOR)
          }
        }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create folder</DialogTitle>
            <DialogDescription>Create a sidebar folder for related chats.</DialogDescription>
          </DialogHeader>
          <Input
            ref={folderNameRef}
            value={folderName}
            onChange={(e) => setFolderName(e.target.value)}
            onKeyDown={handleCreateFolderKeyDown}
            placeholder="Folder name"
            maxLength={100}
          />
          <div className="space-y-2">
            <span className="text-xs font-medium text-muted-foreground">Color</span>
            <PaletteSwatches value={newFolderColor} onSelect={setNewFolderColor} disabled={creatingFolder} />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateFolderOpen(false)} disabled={creatingFolder}>
              Cancel
            </Button>
            <Button onClick={() => void handleCreateFolder()} disabled={creatingFolder || !folderName.trim()}>
              {creatingFolder ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Creating...
                </>
              ) : (
                'Create folder'
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={colorTarget !== null} onOpenChange={(open) => !open && setColorTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Folder color</DialogTitle>
            <DialogDescription>Pick a color for &quot;{colorTargetLive?.name || 'Folder'}&quot;.</DialogDescription>
          </DialogHeader>
          <div className="flex items-center gap-3 py-2">
            <PaletteSwatches
              value={colorTargetLive?.color ?? DEFAULT_FOLDER_COLOR}
              onSelect={(color) => void handleColorSelect(color)}
              disabled={updatingFolderId === colorTarget?.id}
            />
            {updatingFolderId === colorTarget?.id && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setColorTarget(null)}
              disabled={updatingFolderId === colorTarget?.id}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <SessionSearchDialog sessions={sessions} open={searchOpen} onOpenChange={setSearchOpen} onSelect={onSwitch} />

      <AboutDialog open={aboutOpen} onOpenChange={setAboutOpen} version={auth.version} />
    </aside>
  )
}
