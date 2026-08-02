import { EyeOff, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { usePrivacyScreen } from '@/hooks/usePrivacyScreen'
import { cn } from '@/lib/utils'

/**
 * The scope statement (UDR-0107 D12).
 *
 * It MUST name what is NOT covered and MUST NOT imply total coverage ("safe to
 * share", "everything private is hidden"). Coverage is partial by construction:
 * assistant replies stay legible on purpose (UDR-0107 D9), and the P1 surfaces
 * -- memory, prompt templates, tool arguments, workspace files, the account name
 * -- are out of scope for this version. A partial control described as total is
 * worse than no control, because the operator stops being careful.
 */
const SCOPE_ON =
  'Privacy Screen is ON: chat history, your messages, and attachments are scrambled. ' +
  'It does NOT hide assistant replies, tool arguments, memory, templates, workspace files, ' +
  'or your account name. Click to turn off.'

const SCOPE_OFF =
  'Turn on Privacy Screen for screen sharing: scrambles chat history, your messages, and ' +
  'attachments. It does NOT hide assistant replies, tool arguments, memory, templates, ' +
  'workspace files, or your account name.'

/**
 * Top-right control that toggles Privacy Screen (CTR-0190, PRP-0124).
 *
 * Rendered immediately to the LEFT of the Temporary Chat control on the
 * full-page /chat scenario only (UDR-0107 D11); the parent gates this. There is
 * deliberately no keyboard shortcut -- the failure mode of an accidental press
 * is un-redacting a live shared screen (UDR-0107 D13).
 *
 * Inactive: an EyeOff icon button. Active: a highlighted pill showing EyeOff + a
 * "Privacy" label + an X affordance. The pill matches the Temporary Chat pill's
 * shape but uses a distinct color, because both modes can be active at once and
 * must stay distinguishable.
 */
export function PrivacyScreenToggle() {
  const { enabled, enable, disable } = usePrivacyScreen()

  if (enabled) {
    return (
      <button
        type="button"
        onClick={disable}
        title={SCOPE_ON}
        aria-label="Turn off Privacy Screen"
        aria-pressed="true"
        className={cn(
          'inline-flex h-8 items-center gap-1.5 rounded-full px-3 text-xs font-medium',
          'bg-amber-500 text-white hover:bg-amber-600 transition-colors',
        )}>
        <EyeOff className="h-4 w-4" />
        <span>Privacy</span>
        <X className="h-3.5 w-3.5 opacity-70" />
      </button>
    )
  }

  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-8 w-8 text-muted-foreground hover:text-foreground"
      onClick={enable}
      title={SCOPE_OFF}
      aria-label="Turn on Privacy Screen"
      aria-pressed="false">
      <EyeOff className="h-4 w-4" />
    </Button>
  )
}
