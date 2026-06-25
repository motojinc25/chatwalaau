/**
 * monaco-editor bundling setup (CTR-0137, PRP-0091, UDR-0069 D5).
 *
 * The File Explorer editor is monaco-editor. To preserve the localhost-first /
 * offline posture, monaco and ALL its language workers are SELF-HOSTED (bundled by
 * Vite via `?worker`), never loaded from a CDN. `@monaco-editor/react` is pointed at
 * the bundled `monaco-editor` instance via `loader.config`, so it does not fetch the
 * loader from jsDelivr.
 *
 * Imported once (side-effect) by the FileExplorer before the editor mounts.
 */

import { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import cssWorker from 'monaco-editor/esm/vs/language/css/css.worker?worker'
import htmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker'
import jsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'
import tsWorker from 'monaco-editor/esm/vs/language/typescript/ts.worker?worker'

self.MonacoEnvironment = {
  getWorker(_workerId: string, label: string): Worker {
    switch (label) {
      case 'json':
        return new jsonWorker()
      case 'css':
      case 'scss':
      case 'less':
        return new cssWorker()
      case 'html':
      case 'handlebars':
      case 'razor':
        return new htmlWorker()
      case 'typescript':
      case 'javascript':
        return new tsWorker()
      default:
        return new editorWorker()
    }
  },
}

loader.config({ monaco })
