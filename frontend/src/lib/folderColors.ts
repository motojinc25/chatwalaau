import type { FolderColor } from '@/types/chat'

// Folder palette token -> theme-controlled classes (UDR-0046 D2). Written as literal
// strings so the Tailwind scanner includes them. Shared by the sidebar and the chat-area
// folder cue (PRP-0196, UDR-0178 D3) so a color means the same thing in both places.
//
// `glow` is an edge-only inset shadow (never a background tint): about 35 % opacity in
// the light theme, 45 % in the dark theme, from each color's -500 shade. `neutral` is the
// uncolored folder: no sidebar accent, and a grey glow (operator answer Q3).
export const FOLDER_COLOR_CLASSES: Record<FolderColor, { border: string; icon: string; swatch: string; glow: string }> =
  {
    neutral: {
      border: 'border-l-transparent',
      icon: 'text-muted-foreground',
      swatch: 'bg-muted-foreground/40',
      glow: 'shadow-[inset_0_0_24px_0_rgb(107_114_128/0.35)] dark:shadow-[inset_0_0_24px_0_rgb(107_114_128/0.45)]',
    },
    red: {
      border: 'border-l-red-500',
      icon: 'text-red-500',
      swatch: 'bg-red-500',
      glow: 'shadow-[inset_0_0_24px_0_rgb(239_68_68/0.35)] dark:shadow-[inset_0_0_24px_0_rgb(239_68_68/0.45)]',
    },
    orange: {
      border: 'border-l-orange-500',
      icon: 'text-orange-500',
      swatch: 'bg-orange-500',
      glow: 'shadow-[inset_0_0_24px_0_rgb(249_115_22/0.35)] dark:shadow-[inset_0_0_24px_0_rgb(249_115_22/0.45)]',
    },
    amber: {
      border: 'border-l-amber-500',
      icon: 'text-amber-500',
      swatch: 'bg-amber-500',
      glow: 'shadow-[inset_0_0_24px_0_rgb(245_158_11/0.35)] dark:shadow-[inset_0_0_24px_0_rgb(245_158_11/0.45)]',
    },
    green: {
      border: 'border-l-green-500',
      icon: 'text-green-500',
      swatch: 'bg-green-500',
      glow: 'shadow-[inset_0_0_24px_0_rgb(34_197_94/0.35)] dark:shadow-[inset_0_0_24px_0_rgb(34_197_94/0.45)]',
    },
    blue: {
      border: 'border-l-blue-500',
      icon: 'text-blue-500',
      swatch: 'bg-blue-500',
      glow: 'shadow-[inset_0_0_24px_0_rgb(59_130_246/0.35)] dark:shadow-[inset_0_0_24px_0_rgb(59_130_246/0.45)]',
    },
    violet: {
      border: 'border-l-violet-500',
      icon: 'text-violet-500',
      swatch: 'bg-violet-500',
      glow: 'shadow-[inset_0_0_24px_0_rgb(139_92_246/0.35)] dark:shadow-[inset_0_0_24px_0_rgb(139_92_246/0.45)]',
    },
    pink: {
      border: 'border-l-pink-500',
      icon: 'text-pink-500',
      swatch: 'bg-pink-500',
      glow: 'shadow-[inset_0_0_24px_0_rgb(236_72_153/0.35)] dark:shadow-[inset_0_0_24px_0_rgb(236_72_153/0.45)]',
    },
  }

/** The classes for a folder color, falling back to `neutral` for an unknown key. */
export function folderColorClasses(color: FolderColor | string | undefined) {
  return FOLDER_COLOR_CLASSES[color as FolderColor] ?? FOLDER_COLOR_CLASSES.neutral
}
