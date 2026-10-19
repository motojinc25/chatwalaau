/**
 * Table data import: CSV / TSV -> individuals (PRP-0217, UDR-0199, CTR-0247).
 *
 * The server reads the file (`POST /api/ontology/{id}/table/inspect`), converts it with a
 * mapping and validates the stored model plus the rows with the ontology's SHACL
 * configuration (`POST /{id}/table/preview`); both write nothing (D2). This module holds
 * the mapping and report types, applies an accepted preview to the model as UNSAVED
 * edits (the existing save path writes them, UDR-0195 D6) and builds the excluded-rows
 * CSV for the correction loop (D10).
 *
 * Imported with a relative `.ts` path so the invariant harness can run it under Node.
 */

import {
  classifyResources,
  type GraphTerm,
  graphKey,
  type OntologyModel,
  type QuadStatement,
  type Resource,
  type Statement,
  statementSet,
  termKey,
  XSD,
} from './ontologyModel.ts'

export const TABLE_MAPPING_FORMAT = 'chatwalaau.table-mapping/1'
/** File types the dialog offers (CSV / TSV only; `.xlsx` is out of scope, PRP-0217 Q4). */
export const TABLE_ACCEPT = '.csv,.tsv,.tab,.txt'
export const TABLE_ENCODINGS = ['auto', 'utf-8', 'utf-8-sig', 'cp932', 'euc-jp', 'utf-16', 'latin-1'] as const
export const TABLE_DELIMITERS: { value: string; label: string }[] = [
  { value: 'auto', label: 'auto' },
  { value: ',', label: 'comma ,' },
  { value: '\t', label: 'tab' },
  { value: ';', label: 'semicolon ;' },
  { value: '|', label: 'bar |' },
]
/** XSD datatypes offered for a value column (any other IRI can be typed). */
export const TABLE_DATATYPES = [
  'string',
  'integer',
  'decimal',
  'double',
  'boolean',
  'date',
  'dateTime',
  'time',
  'anyURI',
].map((name) => `${XSD}${name}`)

export type ExistingPolicy = 'add' | 'replace' | 'skip'

export interface TableSource {
  encoding: string
  delimiter: string
  header_row: number
  null_values: string[]
  trim: boolean
}

export type ColumnRule =
  | { column: string; kind: 'ignore' }
  | {
      column: string
      kind: 'value'
      property: string
      datatype?: string
      lang?: string
      separator?: string
    }
  | {
      column: string
      kind: 'reference'
      property: string
      separator?: string
      target: { template: string } | { lookup: { class: string; property: string } }
    }

export interface TableMapping {
  format: typeof TABLE_MAPPING_FORMAT
  name: string
  source: TableSource
  class: string
  subject: { template: string }
  graph: string | null
  existing: ExistingPolicy
  columns: ColumnRule[]
  updated_at?: string
}

export interface TableInspection {
  encoding: string
  delimiter: string
  header_row: number
  columns: string[]
  row_count: number
  sample: { row: number; line: number; cells: string[] }[]
  suggestion: TableMapping | null
}

export interface RowReason {
  severity?: 'violation' | 'warning' | 'info'
  column?: string | null
  value?: string | null
  focus?: string | null
  path?: string | null
  shape?: string | null
  component?: string | null
  constraint?: string | null
  message: string
}

export type RowStage = 'convert' | 'existing' | 'shacl' | 'dependency'

export interface ExcludedRow {
  row: number
  line: number
  key: string
  subject: string | null
  stage: RowStage
  reasons: RowReason[]
  depends_on: number[]
  cells: string[]
}

export interface TablePreview {
  revision: string
  mapping: TableMapping
  table: { encoding: string; delimiter: string; columns: string[] }
  rows: { total: number; accepted: number; excluded: number; with_warnings: number }
  subjects: { new: number; existing: number }
  add: QuadStatement[]
  remove: QuadStatement[]
  present: number
  excluded: ExcludedRow[]
  warnings: { row: number; line: number; key: string; subject: string | null; reasons: RowReason[] }[]
  shacl: {
    checked: boolean
    reason?: string
    message?: string | null
    rounds?: number
    settled?: boolean
    preexisting?: number
    unattributed?: number
  }
}

export const STAGE_LABELS: Record<RowStage, string> = {
  convert: 'conversion',
  existing: 'already exists',
  shacl: 'SHACL',
  dependency: 'depends on an excluded row',
}

function localName(iri: string): string {
  const cut = Math.max(iri.lastIndexOf('#'), iri.lastIndexOf('/'))
  return cut >= 0 && cut < iri.length - 1 ? iri.slice(cut + 1) : iri
}

/** A mapping with every column ignored, for a class without a server suggestion. */
export function emptyMapping(inspection: TableInspection, cls: string, baseIri: string): TableMapping {
  const key = inspection.columns[0] ?? 'ID'
  return {
    format: TABLE_MAPPING_FORMAT,
    name: '',
    source: {
      encoding: inspection.encoding,
      delimiter: inspection.delimiter,
      header_row: inspection.header_row,
      null_values: [''],
      trim: true,
    },
    class: cls,
    subject: { template: `${baseIri}${localName(cls).toLowerCase()}-{${key}}` },
    graph: null,
    existing: 'add',
    columns: inspection.columns.map((column) => ({ column, kind: 'ignore' as const })),
  }
}

/** A saved or loaded mapping fitted to this table: rules for unknown columns dropped,
 * columns without a rule ignored (the server refuses a rule for a missing column). */
export function fitMapping(mapping: TableMapping, columns: string[]): { mapping: TableMapping; dropped: string[] } {
  const known = new Set(columns)
  const dropped = mapping.columns.filter((rule) => !known.has(rule.column)).map((rule) => rule.column)
  const byColumn = new Map(mapping.columns.filter((rule) => known.has(rule.column)).map((rule) => [rule.column, rule]))
  return {
    mapping: {
      ...mapping,
      columns: columns.map((column) => byColumn.get(column) ?? { column, kind: 'ignore' as const }),
    },
    dropped,
  }
}

/** The placeholders `{Column}` of a template. */
export function templateColumns(template: string): string[] {
  return [...template.matchAll(/\{([^{}]+)\}/g)].map((m) => m[1])
}

/** Problems the server would refuse, shown before a preview is asked for. */
export function mappingProblems(mapping: TableMapping, columns: string[]): string[] {
  const known = new Set(columns)
  const out: string[] = []
  if (!/^[A-Za-z][A-Za-z0-9+.-]*:/.test(mapping.class)) out.push('Choose the class of the rows.')
  const keys = templateColumns(mapping.subject.template)
  if (keys.length === 0) out.push('The subject template must name a key column, e.g. {ID}.')
  for (const name of keys) if (!known.has(name)) out.push(`The subject template names an unknown column {${name}}.`)
  for (const rule of mapping.columns) {
    if (rule.kind === 'ignore') continue
    if (!/^[A-Za-z][A-Za-z0-9+.-]*:/.test(rule.property)) out.push(`${rule.column}: choose a property.`)
    if (rule.kind === 'reference') {
      if ('template' in rule.target) {
        const names = templateColumns(rule.target.template)
        if (!names.includes(rule.column)) out.push(`${rule.column}: the target template must name {${rule.column}}.`)
        for (const name of names) if (!known.has(name)) out.push(`${rule.column}: unknown column {${name}}.`)
      } else if (!rule.target.lookup.class || !rule.target.lookup.property) {
        out.push(`${rule.column}: choose the class and the property to look up.`)
      }
    }
  }
  return out
}

function quadKey(statement: QuadStatement): string {
  return `${termKey(statement.s)} <${statement.p}> ${termKey(statement.o)} ${graphKey(statement.g)}`
}

/** The model with an accepted preview applied: removals (`replace`), then additions, as
 * UNSAVED edits (D2). Statements already present stay single. */
export function applyTableImport(target: OntologyModel, preview: TablePreview): OntologyModel {
  const gone = new Set(preview.remove.map(quadKey))
  const resources = new Map<string, Resource>()
  for (const resource of target.resources) {
    const s = termKey(resource.term)
    const statements = gone.size
      ? resource.statements.filter(
          (st) =>
            !gone.has(
              quadKey(st.g ? { s: resource.term, p: st.p, o: st.o, g: st.g } : { s: resource.term, p: st.p, o: st.o }),
            ),
        )
      : resource.statements
    resources.set(s, statements === resource.statements ? resource : { ...resource, statements })
  }
  const existing = new Set([...statementSet(target).entries()].filter(([key]) => !gone.has(key)).map(([key]) => key))
  const knownGraphs = new Set((target.graphs ?? []).map((g) => termKey(g)))
  const newGraphs = new Map<string, GraphTerm>()
  for (const quad of preview.add) {
    const key = quadKey(quad)
    if (existing.has(key)) continue
    existing.add(key)
    const subject = termKey(quad.s)
    const resource = resources.get(subject) ?? { term: quad.s, role: 'other' as const, statements: [] }
    const statement: Statement = quad.g ? { p: quad.p, o: quad.o, g: quad.g } : { p: quad.p, o: quad.o }
    resources.set(subject, { ...resource, statements: [...resource.statements, statement] })
    if (quad.g && !knownGraphs.has(termKey(quad.g))) newGraphs.set(termKey(quad.g), quad.g)
  }
  return {
    ...target,
    resources: classifyResources([...resources.values()]),
    graphs: [...(target.graphs ?? []), ...newGraphs.values()],
  }
}

/** One line of text for a row's reasons. */
export function reasonText(reasons: RowReason[]): string {
  return reasons
    .map((r) => {
      const where = r.column ? `${r.column}: ` : r.path ? `${r.path}: ` : ''
      return `${where}${r.message}`
    })
    .join(' / ')
}

function csvCell(value: string, delimiter: string): string {
  return /["\r\n]/.test(value) || value.includes(delimiter) ? `"${value.replaceAll('"', '""')}"` : value
}

/**
 * The excluded rows as a CSV to correct and import again (D10): the original header and
 * cells plus a `_reason` column, UTF-8 with a BOM (so Excel reads it), the file's delimiter.
 */
export function excludedRowsCsv(preview: TablePreview): string {
  const delimiter = preview.table.delimiter || ','
  const header = [...preview.table.columns, '_reason']
  const lines = [header.map((cell) => csvCell(cell, delimiter)).join(delimiter)]
  for (const row of preview.excluded) {
    const cells = preview.table.columns.map((_, i) => row.cells[i] ?? '')
    const reason = `line ${row.line} (${STAGE_LABELS[row.stage]}): ${reasonText(row.reasons)}${
      row.depends_on.length ? ` [depends on row ${row.depends_on.join(', ')}]` : ''
    }`
    lines.push([...cells, reason].map((cell) => csvCell(cell, delimiter)).join(delimiter))
  }
  return `﻿${lines.join('\r\n')}\r\n`
}

/** The mapping as a downloadable JSON document (CTR-0247), without server bookkeeping. */
export function mappingJson(mapping: TableMapping): string {
  const { updated_at: _updated, ...rest } = mapping
  return `${JSON.stringify(rest, null, 2)}\n`
}

/** A mapping read from a `.json` file; the server validates it again on preview / save. */
export function parseMappingFile(text: string): TableMapping {
  const raw = JSON.parse(text)
  if (!raw || typeof raw !== 'object' || (raw.format && raw.format !== TABLE_MAPPING_FORMAT)) {
    throw new Error(`Not a ${TABLE_MAPPING_FORMAT} document`)
  }
  if (typeof raw.class !== 'string' || !raw.subject || typeof raw.subject.template !== 'string') {
    throw new Error('The mapping needs a class and a subject template')
  }
  return {
    format: TABLE_MAPPING_FORMAT,
    name: typeof raw.name === 'string' ? raw.name : '',
    source: {
      encoding: raw.source?.encoding ?? 'auto',
      delimiter: raw.source?.delimiter ?? 'auto',
      header_row: Number.isInteger(raw.source?.header_row) ? raw.source.header_row : 1,
      null_values: Array.isArray(raw.source?.null_values) ? raw.source.null_values : [''],
      trim: raw.source?.trim !== false,
    },
    class: raw.class,
    subject: { template: raw.subject.template },
    graph: typeof raw.graph === 'string' && raw.graph ? raw.graph : null,
    existing: raw.existing === 'replace' || raw.existing === 'skip' ? raw.existing : 'add',
    columns: Array.isArray(raw.columns) ? raw.columns : [],
  }
}
