/**
 * pdf.js bundling setup (CTR-0137, PRP-0093, UDR-0071 D4).
 *
 * The File Explorer PDF viewer uses pdf.js (pdfjs-dist). To preserve the
 * localhost-first / offline posture, the pdf.js worker is SELF-HOSTED (bundled by
 * Vite via `?url`), never loaded from a CDN -- mirroring the monaco worker decision
 * (UDR-0069 D5). Imported once (side-effect) by the PdfViewer before getDocument runs.
 */

import { GlobalWorkerOptions } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

GlobalWorkerOptions.workerSrc = workerUrl
