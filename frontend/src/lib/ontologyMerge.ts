/**
 * Merge a parsed file or another ontology into the open model (PRP-0213, UDR-0195 D1-D6).
 *
 * The server only parses (`POST /api/ontology/{id}/parse` returns the CTR-0169 v3
 * projection and writes nothing; another ontology is read with `GET /{id}`). This
 * module plans the merge -- which statements are new, which graphs are taken, which
 * prefixes are added or skipped, whether VERSION changes -- and applies it to the
 * model as UNSAVED additions. The existing save path writes them (D6): the source's
 * blank nodes get labels the model does not use, so the server receives them as
 * fresh blank nodes (UDR-0183 D6) and never joins them with existing ones.
 *
 * Imported with a relative `.ts` path so the invariant harness can run it under Node.
 */

import {
  classifyResources,
  type GraphTerm,
  graphKey,
  type OntologyModel,
  OWL,
  type PrefixDecl,
  type QuadStatement,
  RDF_DIR_LANG_STRING,
  RDF_TYPE,
  type Resource,
  type Statement,
  statementSet,
  type Term,
  termKey,
} from './ontologyModel.ts'

export const OWL_ONTOLOGY = `${OWL}Ontology`

/** What a merge reads: the source as a model, and whether it is a dataset (named graphs). */
export interface MergeSource {
  model: OntologyModel
  dataset: boolean
  /** The file name or the source ontology's name (shown in the dialog). */
  label: string
}

export interface MergeOptions {
  /** Dataset source: the graph keys taken ('' = the default graph). The partial merge. */
  graphs: Set<string>
  /** Single-graph source: the graph its statements go into (null = the default graph). */
  target: GraphTerm | null
  /** Keep the source's owl:Ontology header statements (D4: off by default). */
  includeHeader: boolean
}

export interface SkippedPrefix {
  prefix: string
  iri: string
  /** The namespace the target binds the same prefix name to (kept). */
  existing: string
}

export interface MergePlan {
  /** The statements to add (relabeled, in their final graphs), new to the model. */
  statements: QuadStatement[]
  /** Statements of the selection the model already holds (RDF is a set: nothing changes). */
  present: number
  /** Header statements left out (D4). */
  headerSkipped: number
  /** Statements of graphs that were not selected. */
  graphSkipped: number
  prefixesAdded: PrefixDecl[]
  prefixesSkipped: SkippedPrefix[]
  /** The VERSION the ontology gets, or null when it does not change. */
  version: string | null
  /** Named graphs the model does not know yet. */
  newGraphs: GraphTerm[]
  /** Merged graphs that are declared SHACL shapes graphs of the target (PRP-0212). */
  shapesGraphs: GraphTerm[]
}

export interface SourceGraph {
  key: string
  graph: GraphTerm | null
  count: number
}

function sourceStatements(model: OntologyModel): QuadStatement[] {
  return [...statementSet(model).values()]
}

/** The graphs of a source with their statement counts ('' first = the default graph). */
export function sourceGraphs(source: MergeSource): SourceGraph[] {
  const out = new Map<string, SourceGraph>()
  for (const statement of sourceStatements(source.model)) {
    const key = graphKey(statement.g)
    const row = out.get(key) ?? { key, graph: statement.g ?? null, count: 0 }
    row.count += 1
    out.set(key, row)
  }
  return [...out.values()].sort((a, b) => (a.key === '' ? -1 : b.key === '' ? 1 : a.key.localeCompare(b.key)))
}

/** The default options: every graph, into the editor's target graph, header left out. */
export function defaultMergeOptions(target: OntologyModel, source: MergeSource): MergeOptions {
  return {
    graphs: new Set(sourceGraphs(source).map((row) => row.key)),
    target: target.targetGraph ?? null,
    includeHeader: false,
  }
}

function blankLabelsOf(term: Term, out: Set<string>): void {
  if (term.type === 'bnode') out.add(term.value)
  else if (term.type === 'triple') {
    blankLabelsOf(term.s, out)
    blankLabelsOf(term.o, out)
  }
}

function modelBlankLabels(model: OntologyModel): Set<string> {
  const out = new Set<string>()
  for (const statement of sourceStatements(model)) {
    blankLabelsOf(statement.s, out)
    blankLabelsOf(statement.o, out)
    if (statement.g) blankLabelsOf(statement.g, out)
  }
  for (const g of model.graphs ?? []) blankLabelsOf(g, out)
  return out
}

function relabel(term: Term, labels: Map<string, string>): Term {
  if (term.type === 'bnode') return { type: 'bnode', value: labels.get(term.value) ?? term.value }
  if (term.type === 'triple') return { ...term, s: relabel(term.s, labels), o: relabel(term.o, labels) }
  return term
}

/** Fresh labels (`m1`, `m2`, ...) for every source blank node, unused by the target (D5). */
export function blankLabelMap(target: OntologyModel, source: OntologyModel): Map<string, string> {
  const taken = modelBlankLabels(target)
  const sourceLabels = modelBlankLabels(source)
  const labels = new Map<string, string>()
  let next = 1
  for (const label of [...sourceLabels].sort()) {
    let fresh = `m${next}`
    while (taken.has(fresh) || sourceLabels.has(fresh)) {
      next += 1
      fresh = `m${next}`
    }
    taken.add(fresh)
    labels.set(label, fresh)
    next += 1
  }
  return labels
}

function headerSubjects(model: OntologyModel): Set<string> {
  const out = new Set<string>()
  for (const resource of model.resources) {
    if (resource.statements.some((s) => s.p === RDF_TYPE && s.o.type === 'iri' && s.o.value === OWL_ONTOLOGY)) {
      out.add(termKey(resource.term))
    }
  }
  return out
}

function visitIris(term: Term, out: Set<string>): void {
  if (term.type === 'iri') out.add(term.value)
  else if (term.type === 'literal') out.add(term.datatype)
  else if (term.type === 'triple') {
    visitIris(term.s, out)
    out.add(term.p)
    visitIris(term.o, out)
  }
}

function usesRdf12(term: Term): boolean {
  if (term.type === 'triple') return true
  return term.type === 'literal' && (term.datatype === RDF_DIR_LANG_STRING || term.direction !== undefined)
}

function quadKey(statement: QuadStatement): string {
  return `${termKey(statement.s)} <${statement.p}> ${termKey(statement.o)} ${graphKey(statement.g)}`
}

/** Plan a merge of `source` into `target` (nothing changes until `applyMerge`). */
export function planMerge(
  target: OntologyModel,
  source: MergeSource,
  options: MergeOptions,
  shapesGraphIris: string[] = [],
): MergePlan {
  const labels = blankLabelMap(target, source.model)
  const header = options.includeHeader ? new Set<string>() : headerSubjects(source.model)
  const existing = statementSet(target)
  const added = new Map<string, QuadStatement>()
  let present = 0
  let headerSkipped = 0
  let graphSkipped = 0
  for (const statement of sourceStatements(source.model)) {
    if (header.has(termKey(statement.s))) {
      headerSkipped += 1
      continue
    }
    let g: GraphTerm | null
    if (source.dataset) {
      if (!options.graphs.has(graphKey(statement.g))) {
        graphSkipped += 1
        continue
      }
      g = statement.g ?? null
    } else {
      g = options.target
    }
    const s = relabel(statement.s, labels)
    const o = relabel(statement.o, labels)
    const graph = g ? (relabel(g, labels) as GraphTerm) : null
    const quad: QuadStatement = graph ? { s, p: statement.p, o, g: graph } : { s, p: statement.p, o }
    const key = quadKey(quad)
    if (existing.has(key)) {
      present += 1
      continue
    }
    added.set(key, quad)
  }
  const statements = [...added.values()]

  // Prefixes: only those the added statements use (D3); a name bound elsewhere is kept.
  const used = new Set<string>()
  let rdf12 = false
  for (const statement of statements) {
    visitIris(statement.s, used)
    used.add(statement.p)
    visitIris(statement.o, used)
    if (statement.g) visitIris(statement.g, used)
    rdf12 = rdf12 || usesRdf12(statement.s) || usesRdf12(statement.o)
  }
  const usesNamespace = (ns: string) => ns !== '' && [...used].some((value) => value.startsWith(ns))
  const byName = new Map(target.document.prefixes.map((p) => [p.prefix, p.iri]))
  const boundIris = new Set(target.document.prefixes.map((p) => p.iri))
  const prefixesAdded: PrefixDecl[] = []
  const prefixesSkipped: SkippedPrefix[] = []
  for (const decl of source.model.document.prefixes) {
    if (!usesNamespace(decl.iri)) continue
    const bound = byName.get(decl.prefix)
    if (bound === decl.iri) continue
    if (bound !== undefined) {
      prefixesSkipped.push({ prefix: decl.prefix, iri: decl.iri, existing: bound })
      continue
    }
    if (boundIris.has(decl.iri)) continue
    prefixesAdded.push(decl)
    byName.set(decl.prefix, decl.iri)
    boundIris.add(decl.iri)
  }

  // VERSION: RDF 1.2 content (or a declared 1.2) needs VERSION 1.2 in the target; any
  // other VERSION of the source changes nothing (the target keeps its own document).
  const wants12 = statements.length > 0 && (rdf12 || source.model.document.version === '1.2')
  const version = wants12 && target.document.version !== '1.2' ? '1.2' : null

  const knownGraphs = new Set((target.graphs ?? []).map((g) => termKey(g)))
  for (const resource of target.resources) {
    for (const statement of resource.statements) if (statement.g) knownGraphs.add(termKey(statement.g))
  }
  const newGraphs = new Map<string, GraphTerm>()
  const mergedGraphs = new Map<string, GraphTerm>()
  for (const statement of statements) {
    if (!statement.g) continue
    const key = termKey(statement.g)
    mergedGraphs.set(key, statement.g)
    if (!knownGraphs.has(key)) newGraphs.set(key, statement.g)
  }
  const shapes = new Set(shapesGraphIris)
  const shapesGraphs = [...mergedGraphs.values()].filter((g) => g.type === 'iri' && shapes.has(g.value))

  return {
    statements,
    present,
    headerSkipped,
    graphSkipped,
    prefixesAdded,
    prefixesSkipped,
    version,
    newGraphs: [...newGraphs.values()],
    shapesGraphs,
  }
}

/** The model with the plan applied: unsaved additions (D5). Statements already present stay single. */
export function applyMerge(target: OntologyModel, plan: MergePlan): OntologyModel {
  const resources = new Map<string, Resource>()
  for (const resource of target.resources) resources.set(termKey(resource.term), resource)
  const existing = statementSet(target)
  for (const quad of plan.statements) {
    if (existing.has(quadKey(quad))) continue
    existing.set(quadKey(quad), quad)
    const key = termKey(quad.s)
    const resource = resources.get(key) ?? { term: quad.s, role: 'other' as const, statements: [] }
    const statement: Statement = quad.g ? { p: quad.p, o: quad.o, g: quad.g } : { p: quad.p, o: quad.o }
    resources.set(key, { ...resource, statements: [...resource.statements, statement] })
  }
  const prefixes = [...target.document.prefixes, ...plan.prefixesAdded]
  return {
    ...target,
    document: {
      ...target.document,
      prefixes,
      version: plan.version ?? target.document.version,
    },
    resources: classifyResources([...resources.values()]),
    graphs: [...(target.graphs ?? []), ...plan.newGraphs],
  }
}
