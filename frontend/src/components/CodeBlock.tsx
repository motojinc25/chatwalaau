import { Check, Copy, Download } from 'lucide-react'
import { useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { Button } from '@/components/ui/button'

interface CodeBlockProps {
  language: string
  value: string
}

const EXTENSION_MAP: Record<string, string> = {
  javascript: 'js',
  typescript: 'ts',
  python: 'py',
  ruby: 'rb',
  rust: 'rs',
  csharp: 'cs',
  cpp: 'cpp',
  java: 'java',
  go: 'go',
  html: 'html',
  css: 'css',
  json: 'json',
  yaml: 'yaml',
  yml: 'yml',
  markdown: 'md',
  bash: 'sh',
  shell: 'sh',
  sql: 'sql',
  xml: 'xml',
  toml: 'toml',
}

export function CodeBlock({ language, value }: CodeBlockProps) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    await navigator.clipboard.writeText(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleDownload = () => {
    const ext = EXTENSION_MAP[language] ?? language ?? 'txt'
    const blob = new Blob([value], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `code.${ext}`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="group relative my-4 overflow-hidden rounded-lg border border-border">
      <div className="flex items-center justify-between bg-zinc-800 px-4 py-1.5 text-xs text-zinc-400">
        <span>{language || 'text'}</span>
        <div className="flex gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-zinc-400 hover:text-zinc-200"
            onClick={handleCopy}
            aria-label="Copy code">
            {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-zinc-400 hover:text-zinc-200"
            onClick={handleDownload}
            aria-label="Download code">
            <Download className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
      <SyntaxHighlighter
        language={language}
        style={oneDark}
        customStyle={{ margin: 0, borderRadius: 0, fontSize: '0.8125rem', lineHeight: '1.5' }}>
        {value}
      </SyntaxHighlighter>
    </div>
  )
}
