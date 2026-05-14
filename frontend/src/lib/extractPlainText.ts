/**
 * Extract plain text from markdown content for TTS synthesis (CTR-0040).
 *
 * Strips markdown formatting, code blocks, math expressions, Mermaid diagrams,
 * and HTML tags to produce clean text suitable for speech synthesis.
 */
export function extractPlainText(content: string): string {
  let text = content

  // Remove fenced code blocks (```...```) including mermaid
  text = text.replace(/```[\s\S]*?```/g, '')

  // Remove LaTeX display math ($$...$$)
  text = text.replace(/\$\$[\s\S]*?\$\$/g, '')

  // Remove LaTeX inline math ($...$)
  text = text.replace(/\$[^$\n]+?\$/g, '')

  // Remove LaTeX delimiters \[...\] and \(...\)
  text = text.replace(/\\\[[\s\S]*?\\\]/g, '')
  text = text.replace(/\\\([\s\S]*?\\\)/g, '')

  // Remove inline code (`...`)
  text = text.replace(/`[^`]+`/g, '')

  // Remove HTML tags
  text = text.replace(/<[^>]+>/g, '')

  // Remove markdown images ![alt](url)
  text = text.replace(/!\[[^\]]*\]\([^)]*\)/g, '')

  // Convert markdown links [text](url) -> text
  text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')

  // Remove markdown headers (# ## ### etc.)
  text = text.replace(/^#{1,6}\s+/gm, '')

  // Remove markdown bold/italic (**text**, *text*, __text__, _text_)
  text = text.replace(/(\*\*|__)(.*?)\1/g, '$2')
  text = text.replace(/(\*|_)(.*?)\1/g, '$2')

  // Remove markdown strikethrough (~~text~~)
  text = text.replace(/~~(.*?)~~/g, '$1')

  // Remove markdown horizontal rules (---, ***, ___)
  text = text.replace(/^[-*_]{3,}\s*$/gm, '')

  // Remove markdown blockquote markers (>)
  text = text.replace(/^>\s?/gm, '')

  // Remove markdown list markers (-, *, +, 1.)
  text = text.replace(/^[\s]*[-*+]\s+/gm, '')
  text = text.replace(/^[\s]*\d+\.\s+/gm, '')

  // Collapse multiple whitespace to single space
  text = text.replace(/\s+/g, ' ')

  // Trim
  text = text.trim()

  return text
}
