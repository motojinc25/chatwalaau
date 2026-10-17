/**
 * SHACL shapes as tables (CTR-0173 v16, PRP-0212 / UDR-0194 D10-D11).
 *
 * The Shapes view shows the node shapes of ONE graph (a declared shapes graph, or the
 * default graph of a shapes ontology) as tables of property shapes. Reading never changes
 * the model. Every edit is an ordinary statement edit of that graph: the constraints the
 * table knows (a SHACL Core subset) are rewritten; every other statement of a shape is
 * shown read-only and kept as it is, so a table edit never loses what it does not
 * understand. Property shapes are new blank nodes (renamed on save when needed).
 */

import {
  bnode,
  type GraphTerm,
  graphKey,
  iri,
  listItems,
  literal,
  mintBlankLabel,
  type OntologyModel,
  RDF_FIRST,
  RDF_NIL,
  RDF_REST,
  RDF_TYPE,
  type Resource,
  resourceKey,
  rewriteStatements,
  type Statement,
  type Term,
  termKey,
  XSD,
} from '@/lib/ontologyModel'

export const SH = 'http://www.w3.org/ns/shacl#'
const sh = (name: string) => `${SH}${name}`

export const SH_NODE_SHAPE = sh('NodeShape')
export const SH_PROPERTY_SHAPE = sh('PropertyShape')
export const SH_PROPERTY = sh('property')
export const SH_PATH = sh('path')
export const SH_INVERSE_PATH = sh('inversePath')

/** The graph a shapes table edits: a named graph, or null for the default graph. */
export type ShapesGraph = GraphTerm | null

export const TARGET_KINDS = ['targetClass', 'targetNode', 'targetSubjectsOf', 'targetObjectsOf'] as const
export type TargetKind = (typeof TARGET_KINDS)[number]

export const SEVERITIES = ['Violation', 'Warning', 'Info'] as const
export const NODE_KINDS = [
  'IRI',
  'BlankNode',
  'Literal',
  'BlankNodeOrIRI',
  'BlankNodeOrLiteral',
  'IRIOrLiteral',
] as const

/** Property-shape predicates the table edits (UDR-0194 D11). */
export const PROPERTY_FIELDS = [
  'path',
  'name',
  'minCount',
  'maxCount',
  'datatype',
  'class',
  'nodeKind',
  'in',
  'hasValue',
  'pattern',
  'flags',
  'minLength',
  'maxLength',
  'minInclusive',
  'maxInclusive',
  'minExclusive',
  'maxExclusive',
  'languageIn',
  'uniqueLang',
  'node',
  'severity',
  'message',
  'deactivated',
] as const
export type PropertyField = (typeof PROPERTY_FIELDS)[number]

const NODE_FIELDS = [...TARGET_KINDS, 'severity', 'message', 'deactivated', 'closed', 'ignoredProperties', 'property']
const LIST_FIELDS = new Set(['in', 'languageIn', 'ignoredProperties'])
const EDITED_PROPERTY = new Set(PROPERTY_FIELDS.map(sh))
const EDITED_NODE = new Set(NODE_FIELDS.map(sh))

export interface PathView {
  /** As written: `ex:p`, `^ex:p`, `ex:a/ex:b`, `(ex:a|ex:b)`, `ex:p*`. */
  text: string
  /** The IRI of a simple path (`p` or `^p`), else null. */
  iri: string | null
  inverse: boolean
  /** Only `p` and `^p` are edited in the table; other paths are read-only. */
  editable: boolean
}

export interface PropertyShapeView {
  term: Term
  path: PathView | null
  values: Partial<Record<PropertyField, Term[]>>
  /** Statements of the property shape the table does not edit, written in Turtle. */
  readOnly: string[]
}

export interface NodeShapeView {
  term: Term
  targets: { kind: TargetKind; value: Term }[]
  values: Partial<Record<'severity' | 'message' | 'deactivated' | 'closed' | 'ignoredProperties', Term[]>>
  /** A property shape with a target (no node shape around it) is its own only row. */
  self: boolean
  properties: PropertyShapeView[]
  readOnly: string[]
}

function inGraph(statement: Statement, g: ShapesGraph): boolean {
  return graphKey(statement.g) === graphKey(g)
}

function statementsIn(resource: Resource | undefined, g: ShapesGraph): Statement[] {
  return resource ? resource.statements.filter((s) => inGraph(s, g)) : []
}

function index(model: OntologyModel): Map<string, Resource> {
  const map = new Map<string, Resource>()
  for (const resource of model.resources) map.set(resourceKey(resource), resource)
  return map
}

/** A term written the Turtle way with the ontology's prefixes. */
export function showTerm(term: Term, compact: (value: string) => string): string {
  switch (term.type) {
    case 'iri':
      return term.value === RDF_NIL ? '()' : compact(term.value)
    case 'bnode':
      return `_:${term.value}`
    case 'literal': {
      const text = JSON.stringify(term.value)
      if (term.language) return `${text}@${term.language}`
      if (term.datatype === `${XSD}string`) return text
      if (term.datatype === `${XSD}integer` && /^[+-]?\d+$/.test(term.value)) return term.value
      if (term.datatype === `${XSD}decimal` && /^[+-]?\d*\.\d+$/.test(term.value)) return term.value
      if (term.datatype === `${XSD}boolean` && /^(true|false)$/.test(term.value)) return term.value
      return `${text}^^${compact(term.datatype)}`
    }
    case 'triple':
      return `<<( ${showTerm(term.s, compact)} ${compact(term.p)} ${showTerm(term.o, compact)} )>>`
  }
}

function pathText(
  node: Term,
  byKey: Map<string, Resource>,
  g: ShapesGraph,
  model: OntologyModel,
  compact: (v: string) => string,
  depth = 0,
): string {
  if (depth > 12) return '...'
  if (node.type === 'iri') return compact(node.value)
  if (node.type !== 'bnode') return showTerm(node, compact)
  const items = listItems(model, node)
  if (items) return items.map((item) => pathText(item, byKey, g, model, compact, depth + 1)).join('/')
  const own = statementsIn(byKey.get(termKey(node)), g)
  const wrap = (inner: Term) => {
    const text = pathText(inner, byKey, g, model, compact, depth + 1)
    return inner.type === 'iri' ? text : `(${text})`
  }
  for (const [name, template] of [
    ['inversePath', '^%'],
    ['zeroOrMorePath', '%*'],
    ['oneOrMorePath', '%+'],
    ['zeroOrOnePath', '%?'],
  ] as const) {
    const found = own.find((s) => s.p === sh(name))
    if (found) return template.replace('%', wrap(found.o))
  }
  const alternatives = own.find((s) => s.p === sh('alternativePath'))
  if (alternatives) {
    const options = listItems(model, alternatives.o) ?? []
    return `(${options.map((item) => pathText(item, byKey, g, model, compact, depth + 1)).join('|')})`
  }
  return `_:${node.value}`
}

function pathView(
  node: Term | undefined,
  byKey: Map<string, Resource>,
  g: ShapesGraph,
  model: OntologyModel,
  compact: (v: string) => string,
): PathView | null {
  if (!node) return null
  if (node.type === 'iri') return { text: compact(node.value), iri: node.value, inverse: false, editable: true }
  const own = node.type === 'bnode' ? statementsIn(byKey.get(termKey(node)), g) : []
  const inverse = own.length === 1 && own[0].p === SH_INVERSE_PATH && own[0].o.type === 'iri' ? own[0].o : null
  if (inverse?.type === 'iri') {
    return { text: `^${compact(inverse.value)}`, iri: inverse.value, inverse: true, editable: true }
  }
  return { text: pathText(node, byKey, g, model, compact), iri: null, inverse: false, editable: false }
}

/** The objects of one field, a list field's items (null for a malformed list). */
function fieldValues(model: OntologyModel, statements: Statement[], field: string): Term[] {
  const objects = statements.filter((s) => s.p === sh(field)).map((s) => s.o)
  if (!LIST_FIELDS.has(field)) return objects
  return objects.flatMap((head) => listItems(model, head) ?? [head])
}

function readOnlyLines(
  statements: Statement[],
  edited: Set<string>,
  model: OntologyModel,
  byKey: Map<string, Resource>,
  g: ShapesGraph,
  compact: (v: string) => string,
): string[] {
  return statements
    .filter((s) => !edited.has(s.p) && !(s.p === RDF_TYPE && s.o.type === 'iri' && s.o.value.startsWith(SH)))
    .map((s) => {
      const items = s.o.type === 'bnode' ? listItems(model, s.o) : null
      const object = items
        ? `( ${items.map((item) => showTerm(item, compact)).join(' ')} )`
        : s.o.type === 'bnode' && byKey.has(termKey(s.o))
          ? `[ ${statementsIn(byKey.get(termKey(s.o)), g)
              .map((inner) => `${compact(inner.p)} ${showTerm(inner.o, compact)}`)
              .join(' ; ')} ]`
          : showTerm(s.o, compact)
      return `${compact(s.p)} ${object}`
    })
}

function propertyView(
  term: Term,
  model: OntologyModel,
  byKey: Map<string, Resource>,
  g: ShapesGraph,
  compact: (v: string) => string,
): PropertyShapeView {
  const statements = statementsIn(byKey.get(termKey(term)), g)
  const values: Partial<Record<PropertyField, Term[]>> = {}
  for (const field of PROPERTY_FIELDS) {
    if (field === 'path') continue
    const found = fieldValues(model, statements, field)
    if (found.length > 0) values[field] = found
  }
  return {
    term,
    path: pathView(statements.find((s) => s.p === SH_PATH)?.o, byKey, g, model, compact),
    values,
    readOnly: readOnlyLines(statements, EDITED_PROPERTY, model, byKey, g, compact),
  }
}

/** The node shapes of graph `g`, in model order (UDR-0194 D10). */
export function shapesOf(model: OntologyModel, g: ShapesGraph, compact: (v: string) => string): NodeShapeView[] {
  const byKey = index(model)
  const propertyShapes = new Set<string>()
  for (const resource of model.resources) {
    for (const s of statementsIn(resource, g)) if (s.p === SH_PROPERTY) propertyShapes.add(termKey(s.o))
  }
  const out: NodeShapeView[] = []
  for (const resource of model.resources) {
    const key = resourceKey(resource)
    if (propertyShapes.has(key)) continue
    const statements = statementsIn(resource, g)
    const typed = statements.some(
      (s) => s.p === RDF_TYPE && s.o.type === 'iri' && (s.o.value === SH_NODE_SHAPE || s.o.value === SH_PROPERTY_SHAPE),
    )
    const targeted = statements.some((s) => TARGET_KINDS.some((kind) => s.p === sh(kind)))
    const hasProperties = statements.some((s) => s.p === SH_PROPERTY)
    if (!typed && !targeted && !hasProperties) continue
    const self = statements.some((s) => s.p === SH_PATH)
    const targets: NodeShapeView['targets'] = []
    for (const kind of TARGET_KINDS) {
      for (const s of statements) if (s.p === sh(kind)) targets.push({ kind, value: s.o })
    }
    const values: NodeShapeView['values'] = {}
    for (const field of ['severity', 'message', 'deactivated', 'closed', 'ignoredProperties'] as const) {
      if (self && field !== 'closed' && field !== 'ignoredProperties') continue
      const found = fieldValues(model, statements, field)
      if (found.length > 0) values[field] = found
    }
    const properties = self
      ? [propertyView(resource.term, model, byKey, g, compact)]
      : statements.filter((s) => s.p === SH_PROPERTY).map((s) => propertyView(s.o, model, byKey, g, compact))
    const edited = self ? new Set([...EDITED_NODE, ...EDITED_PROPERTY]) : EDITED_NODE
    out.push({
      term: resource.term,
      targets,
      values,
      self,
      properties,
      readOnly: readOnlyLines(statements, edited, model, byKey, g, compact),
    })
  }
  return out
}

/** True when graph `g` of the model holds at least one shape. */
export function hasShapes(model: OntologyModel, g: ShapesGraph): boolean {
  return model.resources.some((resource) =>
    statementsIn(resource, g).some(
      (s) =>
        s.p === SH_PROPERTY ||
        TARGET_KINDS.some((kind) => s.p === sh(kind)) ||
        (s.p === RDF_TYPE && s.o.type === 'iri' && (s.o.value === SH_NODE_SHAPE || s.o.value === SH_PROPERTY_SHAPE)),
    ),
  )
}

/** The classes shapes of graph `g` target (the canvas badge, UDR-0194 D12): class IRI -> shapes. */
export function targetedClasses(model: OntologyModel, graphs: ShapesGraph[]): Map<string, Term[]> {
  const out = new Map<string, Term[]>()
  for (const resource of model.resources) {
    for (const s of resource.statements) {
      if (s.p !== sh('targetClass') || s.o.type !== 'iri') continue
      if (!graphs.some((g) => inGraph(s, g))) continue
      out.set(s.o.value, [...(out.get(s.o.value) ?? []), resource.term])
    }
  }
  return out
}

// ---- Edits (pure; each returns a new model) ----------------------------------------

function stmt(p: string, o: Term, g: ShapesGraph): Statement {
  return g ? { p, o, g } : { p, o }
}

/** Blank nodes reachable from `start` through statements of graph `g` (subtree owned by it). */
function blankSubtree(model: OntologyModel, start: Term[], g: ShapesGraph): Term[] {
  const byKey = index(model)
  const seen = new Map<string, Term>()
  const todo = start.filter((t) => t.type === 'bnode')
  while (todo.length > 0) {
    const node = todo.pop() as Term
    const key = termKey(node)
    if (seen.has(key)) continue
    seen.set(key, node)
    for (const s of statementsIn(byKey.get(key), g)) if (s.o.type === 'bnode') todo.push(s.o)
  }
  return [...seen.values()]
}

function referencedElsewhere(model: OntologyModel, node: Term, except: Set<string>): boolean {
  const key = termKey(node)
  return model.resources.some(
    (resource) =>
      !except.has(resourceKey(resource)) &&
      resource.statements.some((s) => s.o.type === 'bnode' && termKey(s.o) === key),
  )
}

/** Remove the statements of graph `g` of the blank nodes `nodes` own (not referenced from outside). */
function dropSubtree(model: OntologyModel, nodes: Term[], g: ShapesGraph): OntologyModel {
  const subtree = blankSubtree(model, nodes, g)
  const keys = new Set(subtree.map(termKey))
  let next = model
  for (const node of subtree) {
    if (referencedElsewhere(model, node, keys)) continue
    next = rewriteStatements(next, node, (s) => inGraph(s, g), [])
  }
  return next
}

/** Build an RDF list of `items` in graph `g`; returns (model, head). */
function buildList(model: OntologyModel, items: Term[], g: ShapesGraph): [OntologyModel, Term] {
  if (items.length === 0) return [model, iri(RDF_NIL)]
  let next = model
  const cells = items.map(() => {
    const cell = bnode(mintBlankLabel(next))
    next = rewriteStatements(next, cell, () => false, [stmt(RDF_FIRST, iri(RDF_NIL), g)])
    return cell
  })
  cells.forEach((cell, i) => {
    next = rewriteStatements(next, cell, (s) => inGraph(s, g), [
      stmt(RDF_FIRST, items[i], g),
      stmt(RDF_REST, i + 1 < cells.length ? cells[i + 1] : iri(RDF_NIL), g),
    ])
  })
  return [next, cells[0]]
}

/** Set one field of a shape (`values` empty = remove it); list fields become RDF lists. */
export function setField(
  model: OntologyModel,
  g: ShapesGraph,
  subject: Term,
  field: string,
  values: Term[],
): OntologyModel {
  const predicate = sh(field)
  const resource = model.resources.find((r) => resourceKey(r) === termKey(subject))
  const old = statementsIn(resource, g).filter((s) => s.p === predicate)
  let next = model
  if (LIST_FIELDS.has(field))
    next = dropSubtree(
      next,
      old.map((s) => s.o),
      g,
    )
  let objects = values
  if (LIST_FIELDS.has(field) && values.length > 0) {
    const [withList, head] = buildList(next, values, g)
    next = withList
    objects = [head]
  }
  return rewriteStatements(
    next,
    subject,
    (s) => s.p === predicate && inGraph(s, g),
    objects.map((o) => stmt(predicate, o, g)),
  )
}

/** Set a property shape's path to `p` or `^p` (an inverse is a new blank node). */
export function setPath(
  model: OntologyModel,
  g: ShapesGraph,
  row: Term,
  pathIri: string,
  inverse: boolean,
): OntologyModel {
  const resource = model.resources.find((r) => resourceKey(r) === termKey(row))
  const old = statementsIn(resource, g).filter((s) => s.p === SH_PATH)
  let next = dropSubtree(
    model,
    old.map((s) => s.o),
    g,
  )
  let object: Term = iri(pathIri)
  if (inverse) {
    object = bnode(mintBlankLabel(next))
    next = rewriteStatements(next, object, () => false, [stmt(SH_INVERSE_PATH, iri(pathIri), g)])
  }
  return rewriteStatements(next, row, (s) => s.p === SH_PATH && inGraph(s, g), [stmt(SH_PATH, object, g)])
}

/** A new node shape `shapeIri` (optionally targeting `targetClass`). */
export function addNodeShape(
  model: OntologyModel,
  g: ShapesGraph,
  shapeIri: string,
  targetClass?: string,
): OntologyModel {
  const add = [stmt(RDF_TYPE, iri(SH_NODE_SHAPE), g)]
  if (targetClass) add.push(stmt(sh('targetClass'), iri(targetClass), g))
  return rewriteStatements(model, iri(shapeIri), () => false, add)
}

/** A new property shape of `shape` on `pathIri`; returns the model and the new row's term. */
export function addPropertyShape(
  model: OntologyModel,
  g: ShapesGraph,
  shape: Term,
  pathIri: string,
): [OntologyModel, Term] {
  const row = bnode(mintBlankLabel(model))
  let next = rewriteStatements(model, row, () => false, [stmt(SH_PATH, iri(pathIri), g)])
  next = rewriteStatements(next, shape, () => false, [stmt(SH_PROPERTY, row, g)])
  return [next, row]
}

/** Remove a property shape: its link from the node shape and every statement it owns. */
export function removePropertyShape(model: OntologyModel, g: ShapesGraph, shape: Term, row: Term): OntologyModel {
  let next = rewriteStatements(
    model,
    shape,
    (s) => s.p === SH_PROPERTY && inGraph(s, g) && termKey(s.o) === termKey(row),
    [],
  )
  if (row.type === 'bnode' && !referencedElsewhere(next, row, new Set())) next = dropSubtree(next, [row], g)
  return next
}

/** Remove a node shape: its statements in `g` and the blank nodes it owns. */
export function removeNodeShape(model: OntologyModel, g: ShapesGraph, shape: Term): OntologyModel {
  const resource = model.resources.find((r) => resourceKey(r) === termKey(shape))
  const owned = statementsIn(resource, g)
    .map((s) => s.o)
    .filter((o) => o.type === 'bnode')
  const next = rewriteStatements(model, shape, (s) => inGraph(s, g), [])
  return dropSubtree(next, owned, g)
}

/** Add or remove one target of a node shape. */
export function setTarget(
  model: OntologyModel,
  g: ShapesGraph,
  shape: Term,
  kind: TargetKind,
  value: Term,
  on: boolean,
): OntologyModel {
  const predicate = sh(kind)
  return rewriteStatements(
    model,
    shape,
    (s) => !on && s.p === predicate && inGraph(s, g) && termKey(s.o) === termKey(value),
    on ? [stmt(predicate, value, g)] : [],
  )
}

/**
 * A literal for a constraint value as typed: integers and decimals as numbers, `true` /
 * `false` as booleans, ISO dates as xsd:date / xsd:dateTime, anything else a string.
 */
export function valueLiteral(text: string): Term {
  const value = text.trim()
  if (/^[+-]?\d+$/.test(value)) return literal(value, { datatype: `${XSD}integer` })
  if (/^[+-]?\d*\.\d+$/.test(value)) return literal(value, { datatype: `${XSD}decimal` })
  if (/^(true|false)$/.test(value)) return literal(value, { datatype: `${XSD}boolean` })
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return literal(value, { datatype: `${XSD}date` })
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$/.test(value)) {
    return literal(value, { datatype: `${XSD}dateTime` })
  }
  return literal(value)
}

/** A count (sh:minCount, sh:minLength, ...) as xsd:integer; null when the text is not one. */
export function countLiteral(text: string): Term | null {
  const value = text.trim()
  return /^\d+$/.test(value) ? literal(value, { datatype: `${XSD}integer` }) : null
}

export function booleanLiteral(on: boolean): Term {
  return literal(on ? 'true' : 'false', { datatype: `${XSD}boolean` })
}
