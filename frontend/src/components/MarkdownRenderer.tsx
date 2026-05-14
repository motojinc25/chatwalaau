import 'katex/dist/katex.min.css'

import { Check, Copy } from 'lucide-react'
import { useCallback, useRef, useState } from 'react'
import type { Components } from 'react-markdown'
import ReactMarkdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import { CodeBlock } from '@/components/CodeBlock'
import { MermaidBlock } from '@/components/MermaidBlock'
import { Button } from '@/components/ui/button'

interface MarkdownRendererProps {
  content: string
}

/**
 * remark-math v6 creates math/inlineMath mdast nodes but does NOT register
 * toHast handlers with remark-rehype. Without these, mdast-util-to-hast
 * drops math nodes and rehype-katex never sees them.
 *
 * We provide the handlers via remarkRehypeOptions to convert:
 *   inlineMath -> <code class="language-math math-inline">
 *   math       -> <pre><code class="language-math math-display">
 * which is the format rehype-katex expects.
 */
const remarkRehypeOptions = {
  handlers: {
    // biome-ignore lint/suspicious/noExplicitAny: mdast node types are loosely typed
    inlineMath(_state: any, node: { value: string }) {
      return {
        type: 'element' as const,
        tagName: 'code',
        properties: { className: ['language-math', 'math-inline'] },
        children: [{ type: 'text' as const, value: node.value }],
      }
    },
    // biome-ignore lint/suspicious/noExplicitAny: mdast node types are loosely typed
    math(_state: any, node: { value: string }) {
      return {
        type: 'element' as const,
        tagName: 'pre',
        properties: {},
        children: [
          {
            type: 'element' as const,
            tagName: 'code',
            properties: { className: ['language-math', 'math-display'] },
            children: [{ type: 'text' as const, value: node.value }],
          },
        ],
      }
    },
  },
}

function TableBlock({ children, ...props }: React.HTMLAttributes<HTMLTableElement>) {
  const tableRef = useRef<HTMLTableElement>(null)
  const [copied, setCopied] = useState(false)

  const handleCopy = useCallback(async () => {
    const table = tableRef.current
    if (!table) return

    const rows = table.querySelectorAll('tr')
    const lines: string[] = []
    for (const row of rows) {
      const cells = row.querySelectorAll('th, td')
      const values = Array.from(cells).map((c) => (c.textContent ?? '').trim())
      lines.push(`| ${values.join(' | ')} |`)
      // Add separator after header row
      if (row.parentElement?.tagName === 'THEAD') {
        lines.push(`| ${values.map(() => '---').join(' | ')} |`)
      }
    }
    await navigator.clipboard.writeText(lines.join('\n'))
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [])

  return (
    <div className="group/table relative my-4 overflow-auto">
      <Button
        variant="ghost"
        size="icon"
        className="absolute right-1 top-1 z-10 h-7 w-7 opacity-0 transition-opacity group-hover/table:opacity-100"
        onClick={handleCopy}
        aria-label="Copy table as Markdown">
        {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      </Button>
      <table ref={tableRef} className="w-full border-collapse border border-border text-sm" {...props}>
        {children}
      </table>
    </div>
  )
}

const components: Components = {
  code({ className, children, node: _, ...props }) {
    const match = /language-(\w+)/.exec(className || '')
    const value = String(children).replace(/\n$/, '')

    if (!match) {
      return (
        <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[0.8125rem]" {...props}>
          {children}
        </code>
      )
    }

    const language = match[1]

    if (language === 'mermaid') {
      return <MermaidBlock chart={value} />
    }

    return <CodeBlock language={language} value={value} />
  },

  pre({ children }) {
    return <>{children}</>
  },

  a({ href, children, node: _, ...props }) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-600 no-underline hover:underline text-[0.8125rem]"
        {...props}>
        {children}
      </a>
    )
  },

  table({ children, node: _, ...props }) {
    return <TableBlock {...props}>{children}</TableBlock>
  },

  thead({ children, node: _, ...props }) {
    return (
      <thead className="bg-muted" {...props}>
        {children}
      </thead>
    )
  },

  th({ children, node: _, ...props }) {
    return (
      <th className="border border-border px-3 py-2 text-left font-semibold" {...props}>
        {children}
      </th>
    )
  },

  td({ children, node: _, ...props }) {
    return (
      <td className="border border-border px-3 py-2" {...props}>
        {children}
      </td>
    )
  },

  p({ children, node: _, ...props }) {
    return (
      <p className="mb-4 last:mb-0" {...props}>
        {children}
      </p>
    )
  },

  h1({ children, node: _, ...props }) {
    return (
      <h1 className="mt-6 mb-3 text-2xl font-bold first:mt-0" {...props}>
        {children}
      </h1>
    )
  },

  h2({ children, node: _, ...props }) {
    return (
      <h2 className="mt-5 mb-3 text-xl font-bold first:mt-0" {...props}>
        {children}
      </h2>
    )
  },

  h3({ children, node: _, ...props }) {
    return (
      <h3 className="mt-4 mb-2 text-lg font-semibold first:mt-0" {...props}>
        {children}
      </h3>
    )
  },

  ul({ children, node: _, ...props }) {
    return (
      <ul className="mb-4 ml-6 list-disc last:mb-0" {...props}>
        {children}
      </ul>
    )
  },

  ol({ children, node: _, ...props }) {
    return (
      <ol className="mb-4 ml-6 list-decimal last:mb-0" {...props}>
        {children}
      </ol>
    )
  },

  li({ children, node: _, ...props }) {
    return (
      <li className="mb-1" {...props}>
        {children}
      </li>
    )
  },

  blockquote({ children, node: _, ...props }) {
    return (
      <blockquote className="my-4 border-l-4 border-border pl-4 italic text-muted-foreground" {...props}>
        {children}
      </blockquote>
    )
  },

  hr({ node: _, ...props }) {
    return <hr className="my-6 border-border" {...props} />
  },
}

/**
 * Convert LaTeX-style delimiters (\[...\] and \(...\)) to standard
 * math delimiters ($$...$$ and $...$) that remark-math recognizes.
 * Some AI models output formulas using LaTeX delimiters.
 */
function preprocessMath(text: string): string {
  let processed = text.replace(/\\\[/g, '$$').replace(/\\\]/g, '$$')
  processed = processed.replace(/\\\(/g, '$').replace(/\\\)/g, '$')
  return processed
}

export function MarkdownRenderer({ content }: MarkdownRendererProps) {
  return (
    <div className="[&>*:last-child]:mb-0">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, { throwOnError: false, trust: true, strict: false }]]}
        remarkRehypeOptions={remarkRehypeOptions}
        components={components}>
        {preprocessMath(content)}
      </ReactMarkdown>
    </div>
  )
}
