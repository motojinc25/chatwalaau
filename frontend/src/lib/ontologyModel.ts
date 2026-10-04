/**
 * The ontology editing model (CTR-0169 v2 / CTR-0173 v3, PRP-0198, UDR-0180).
 *
 * The backend serves a STATEMENT-COMPLETE, resource-centric projection: every
 * triple of the ontology is one statement `{p, o}` of the resource for its
 * subject, with every term typed (RDF 1.2 Term JSON). This module is the ONE
 * place in the frontend that maps those statements to canvas views (entities,
 * datatype properties, relationship edges) and turns canvas / inspector edits
 * into statement edits (UDR-0180 D4). It never parses or serializes RDF text
 * (UDR-0084 D6).
 *
 * Every edit changes only the statements it addresses (UDR-0180 D1): a displayed
 * label is ONE statement, and editing it keeps its language, datatype and
 * direction; other values are untouched; nothing is synthesized.
 *
 * Pure and dependency-free on purpose: the invariant suite runs it under Node.
 */

// ---- Vocabulary ----------------------------------------------------------------

export const RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
export const RDFS = 'http://www.w3.org/2000/01/rdf-schema#'
export const OWL = 'http://www.w3.org/2002/07/owl#'
export const XSD = 'http://www.w3.org/2001/XMLSchema#'
export const CW = 'https://chatwalaau.com/ontology#'

export const RDF_TYPE = `${RDF}type`
export const RDF_REIFIES = `${RDF}reifies`
export const RDF_FIRST = `${RDF}first`
export const RDF_REST = `${RDF}rest`
export const RDF_NIL = `${RDF}nil`
export const RDF_LANG_STRING = `${RDF}langString`
export const RDF_DIR_LANG_STRING = `${RDF}dirLangString`
export const RDFS_LABEL = `${RDFS}label`
export const RDFS_COMMENT = `${RDFS}comment`
export const RDFS_DOMAIN = `${RDFS}domain`
export const RDFS_RANGE = `${RDFS}range`
export const OWL_CLASS = `${OWL}Class`
export const OWL_OBJECT_PROPERTY = `${OWL}ObjectProperty`
export const OWL_DATATYPE_PROPERTY = `${OWL}DatatypeProperty`
export const XSD_STRING = `${XSD}string`
export const XSD_DECIMAL = `${XSD}decimal`
export const XSD_BOOLEAN = `${XSD}boolean`
export const CW_X = `${CW}x`
export const CW_Y = `${CW}y`
export const CW_EMOJI = `${CW}emoji`
export const CW_COLOR = `${CW}color`
export const CW_CARDINALITY = `${CW}cardinality`
export const CW_IS_KEY = `${CW}isKey`

export const CARDINALITIES = ['one-to-one', 'one-to-many', 'many-to-one', 'many-to-many'] as const
export const DEFAULT_CARDINALITY = 'one-to-many'

// ---- Types (CTR-0169 v2) ---------------------------------------------------------

export type Direction = 'ltr' | 'rtl'

export type Term =
  | { type: 'iri'; value: string }
  | { type: 'bnode'; value: string }
  | { type: 'literal'; value: string; datatype: string; language?: string; direction?: Direction }
  | { type: 'triple'; s: Term; p: string; o: Term }

export interface Statement {
  p: string
  o: Term
}

export type Role = 'entity' | 'object_property' | 'datatype_property' | 'other'

export interface Resource {
  term: Term
  role: Role
  statements: Statement[]
}

export interface PrefixDecl {
  prefix: string
  iri: string
}

export interface OntologyDocument {
  prefixes: PrefixDecl[]
  base: string | null
  version: string | null
}

export interface OntologyModel {
  document: OntologyDocument
  resources: Resource[]
}

export interface Diagnostic {
  kind: string
  s: Term
  p: string
  o: Term
}

export const EMPTY_MODEL: OntologyModel = {
  document: { prefixes: [], base: null, version: null },
  resources: [],
}

// ---- Term helpers ------------------------------------------------------------------

export function iri(value: string): Term {
  return { type: 'iri', value }
}

export function bnode(value: string): Term {
  return { type: 'bnode', value }
}

/** A literal; with a language it is rdf:langString (or rdf:dirLangString with a direction). */
export function literal(
  value: string,
  options: { datatype?: string; language?: string; direction?: Direction } = {},
): Term {
  if (options.language) {
    return options.direction
      ? {
          type: 'literal',
          value,
          datatype: RDF_DIR_LANG_STRING,
          language: options.language,
          direction: options.direction,
        }
      : { type: 'literal', value, datatype: RDF_LANG_STRING, language: options.language }
  }
  return { type: 'literal', value, datatype: options.datatype || XSD_STRING }
}

/** A canonical, N-Triples-like key: equal keys <=> equal terms. */
export function termKey(term: Term): string {
  switch (term.type) {
    case 'iri':
      return `<${term.value}>`
    case 'bnode':
      return `_:${term.value}`
    case 'literal':
      return `${JSON.stringify(term.value)}^^<${term.datatype}>${term.language ? `@${term.language.toLowerCase()}` : ''}${
        term.direction ? `--${term.direction}` : ''
      }`
    case 'triple':
      return `<<( ${termKey(term.s)} <${term.p}> ${termKey(term.o)} )>>`
  }
}

export function termEquals(a: Term, b: Term): boolean {
  return termKey(a) === termKey(b)
}

export function statementEquals(a: Statement, b: Statement): boolean {
  return a.p === b.p && termEquals(a.o, b.o)
}

/** The triple term a statement of `subject` denotes (what a reifier points to). */
export function asTriple(subject: Term, statement: Statement): Term {
  return { type: 'triple', s: subject, p: statement.p, o: statement.o }
}

export function localName(value: string): string {
  const hash = value.split('#')
  const tail = hash[hash.length - 1].split('/')
  return tail[tail.length - 1] || value
}

/** A prefixed name when a declared prefix matches (longest namespace wins). */
export function compactIri(value: string, prefixes: PrefixDecl[]): string {
  let best: PrefixDecl | null = null
  for (const decl of prefixes) {
    if (value.startsWith(decl.iri) && (!best || decl.iri.length > best.iri.length)) best = decl
  }
  if (!best) return `<${value}>`
  const local = value.slice(best.iri.length)
  return /^[\p{L}\p{N}_-]*$/u.test(local) ? `${best.prefix}:${local}` : `<${value}>`
}

/** Expand `prefix:local`, `<iri>` or an absolute IRI; null when unresolvable. */
export function expandIri(text: string, prefixes: PrefixDecl[]): string | null {
  const value = text.trim()
  if (!value) return null
  if (value.startsWith('<') && value.endsWith('>')) return value.slice(1, -1) || null
  const colon = value.indexOf(':')
  if (colon >= 0) {
    const decl = prefixes.find((p) => p.prefix === value.slice(0, colon))
    if (decl) return decl.iri + value.slice(colon + 1)
    if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(value)) return value
  }
  return null
}

export function formatDecimal(value: number): string {
  const rounded = Math.round(value * 100) / 100
  return Object.is(rounded, -0) ? '0' : String(rounded)
}

// ---- Resources ---------------------------------------------------------------------

export function resourceKey(resource: Resource): string {
  return termKey(resource.term)
}

const ROLE_BY_TYPE: [string, Role][] = [
  [OWL_CLASS, 'entity'],
  [OWL_OBJECT_PROPERTY, 'object_property'],
  [OWL_DATATYPE_PROPERTY, 'datatype_property'],
]

/** The same classification the backend computes on GET (UDR-0180 D4), re-run after edits. */
export function roleOf(term: Term, statements: Statement[]): Role {
  if (term.type !== 'iri') return 'other'
  const types = new Set(
    statements.filter((s) => s.p === RDF_TYPE && s.o.type === 'iri').map((s) => (s.o as { value: string }).value),
  )
  for (const [typeIri, role] of ROLE_BY_TYPE) if (types.has(typeIri)) return role
  return 'other'
}

export function findResource(model: OntologyModel, key: string): Resource | undefined {
  return model.resources.find((r) => resourceKey(r) === key)
}

export function iriObjects(resource: Resource | undefined, predicate: string): string[] {
  if (!resource) return []
  return resource.statements
    .filter((s) => s.p === predicate && s.o.type === 'iri')
    .map((s) => (s.o as { value: string }).value)
}

function objects(resource: Resource | undefined, predicate: string): Term[] {
  return resource ? resource.statements.filter((s) => s.p === predicate).map((s) => s.o) : []
}

/**
 * The index of the literal shown for `predicate`: untagged / xsd:string first,
 * then English, then the first by language tag. -1 when there is none.
 */
export function pickLiteralIndex(resource: Resource | undefined, predicate: string): number {
  if (!resource) return -1
  let best = -1
  let bestRank = Number.POSITIVE_INFINITY
  let bestTag = ''
  resource.statements.forEach((s, index) => {
    if (s.p !== predicate || s.o.type !== 'literal') return
    const tag = (s.o.language ?? '').toLowerCase()
    const rank = !tag ? 0 : tag === 'en' || tag.startsWith('en-') ? 1 : 2
    if (rank < bestRank || (rank === bestRank && rank === 2 && tag < bestTag)) {
      best = index
      bestRank = rank
      bestTag = tag
    }
  })
  return best
}

export function displayLiteral(resource: Resource | undefined, predicate: string): string {
  const index = pickLiteralIndex(resource, predicate)
  if (!resource || index < 0) return ''
  return (resource.statements[index].o as { value: string }).value
}

/** Other literal values of `predicate` (shown as language chips next to the displayed one). */
export function otherLiterals(resource: Resource | undefined, predicate: string): Term[] {
  const index = pickLiteralIndex(resource, predicate)
  if (!resource) return []
  return resource.statements
    .filter((s, i) => s.p === predicate && s.o.type === 'literal' && i !== index)
    .map((s) => s.o)
}

function firstNumber(resource: Resource | undefined, predicate: string): number | null {
  for (const term of objects(resource, predicate)) {
    if (term.type !== 'literal') continue
    const value = Number(term.value)
    if (term.value.trim() !== '' && Number.isFinite(value)) return value
  }
  return null
}

function isTrue(term: Term): boolean {
  return term.type === 'literal' && ['true', '1'].includes(term.value.trim().toLowerCase())
}

// ---- Canvas views ---------------------------------------------------------------

export interface DatatypePropertyView {
  iri: string
  key: string
  label: string
  /** The single IRI range, or null when the range is absent, multiple or an expression. */
  rangeIri: string | null
  /** True when a range exists but is not a single IRI (a class expression or several ranges). */
  rangeIsExpression: boolean
  isKey: boolean
  /** Listed under more than one entity (several domains = their intersection in OWL). */
  shared: boolean
}

export interface EntityView {
  iri: string
  key: string
  label: string
  comment: string
  emoji: string
  color: string
  x: number | null
  y: number | null
  properties: DatatypePropertyView[]
}

export interface RelationshipEdgeView {
  id: string
  iri: string
  key: string
  source: string
  target: string
  label: string
  cardinality: string
}

export function entityIris(model: OntologyModel): Set<string> {
  return new Set(
    model.resources
      .filter((r) => r.role === 'entity' && r.term.type === 'iri')
      .map((r) => (r.term as { value: string }).value),
  )
}

export function datatypePropertyView(resource: Resource, entities: Set<string>): DatatypePropertyView {
  const value = (resource.term as { value: string }).value
  const ranges = objects(resource, RDFS_RANGE)
  const domains = iriObjects(resource, RDFS_DOMAIN).filter((d) => entities.has(d))
  return {
    iri: value,
    key: resourceKey(resource),
    label: displayLiteral(resource, RDFS_LABEL) || localName(value),
    rangeIri: ranges.length === 1 && ranges[0].type === 'iri' ? ranges[0].value : null,
    rangeIsExpression: ranges.length > 1 || (ranges.length === 1 && ranges[0].type !== 'iri'),
    isKey: objects(resource, CW_IS_KEY).some(isTrue),
    shared: domains.length > 1,
  }
}

export function entityViews(model: OntologyModel): EntityView[] {
  const entities = entityIris(model)
  const propertiesByEntity = new Map<string, DatatypePropertyView[]>()
  for (const resource of model.resources) {
    if (resource.role !== 'datatype_property') continue
    const view = datatypePropertyView(resource, entities)
    for (const domain of new Set(iriObjects(resource, RDFS_DOMAIN))) {
      if (!entities.has(domain)) continue
      const list = propertiesByEntity.get(domain) ?? []
      list.push(view)
      propertiesByEntity.set(domain, list)
    }
  }
  return model.resources
    .filter((r) => r.role === 'entity')
    .map((resource) => {
      const value = (resource.term as { value: string }).value
      return {
        iri: value,
        key: resourceKey(resource),
        label: displayLiteral(resource, RDFS_LABEL) || localName(value),
        comment: displayLiteral(resource, RDFS_COMMENT),
        emoji: displayLiteral(resource, CW_EMOJI),
        color: displayLiteral(resource, CW_COLOR),
        x: firstNumber(resource, CW_X),
        y: firstNumber(resource, CW_Y),
        properties: (propertiesByEntity.get(value) ?? []).sort((a, b) => a.iri.localeCompare(b.iri)),
      }
    })
}

/** One edge per (domain entity, range entity) pair of every object property (UDR-0180 D5). */
export function relationshipEdges(model: OntologyModel): RelationshipEdgeView[] {
  const entities = entityIris(model)
  const edges: RelationshipEdgeView[] = []
  for (const resource of model.resources) {
    if (resource.role !== 'object_property') continue
    const value = (resource.term as { value: string }).value
    const sources = [...new Set(iriObjects(resource, RDFS_DOMAIN))].filter((d) => entities.has(d))
    const targets = [...new Set(iriObjects(resource, RDFS_RANGE))].filter((r) => entities.has(r))
    const label = displayLiteral(resource, RDFS_LABEL) || localName(value)
    const cardinality = displayLiteral(resource, CW_CARDINALITY) || DEFAULT_CARDINALITY
    for (const source of sources) {
      for (const target of targets) {
        edges.push({
          id: `${value}|${source}|${target}`,
          iri: value,
          key: resourceKey(resource),
          source,
          target,
          label,
          cardinality,
        })
      }
    }
  }
  return edges
}

/** How many statements use each blank node as their object (nesting is for count 1). */
export function blankReferenceCounts(model: OntologyModel): Map<string, number> {
  const counts = new Map<string, number>()
  for (const resource of model.resources) {
    for (const statement of resource.statements) {
      if (statement.o.type === 'bnode') {
        const key = termKey(statement.o)
        counts.set(key, (counts.get(key) ?? 0) + 1)
      }
    }
  }
  return counts
}

/** Keys of the resources the canvas draws (entities, their properties, drawable relationships). */
export function canvasResourceKeys(model: OntologyModel): Set<string> {
  const entities = entityIris(model)
  const keys = new Set<string>()
  for (const resource of model.resources) {
    if (resource.role === 'entity') keys.add(resourceKey(resource))
    else if (resource.role === 'datatype_property') {
      if (iriObjects(resource, RDFS_DOMAIN).some((d) => entities.has(d))) keys.add(resourceKey(resource))
    }
  }
  for (const edge of relationshipEdges(model)) keys.add(edge.key)
  return keys
}

/**
 * The resources listed in the inspector's Resources tab (UDR-0180 D9): everything
 * the canvas does not draw, except blank nodes referenced exactly once, which are
 * shown nested where they are used.
 */
export function inspectorResources(model: OntologyModel): Resource[] {
  const drawn = canvasResourceKeys(model)
  const refs = blankReferenceCounts(model)
  return model.resources.filter((r) => {
    const key = resourceKey(r)
    if (drawn.has(key)) return false
    return !(r.term.type === 'bnode' && refs.get(key) === 1)
  })
}

// ---- Edits (all pure; each returns a new model) -----------------------------------

function withResource(
  model: OntologyModel,
  key: string,
  update: (resource: Resource | undefined) => Resource | null,
): OntologyModel {
  const index = model.resources.findIndex((r) => resourceKey(r) === key)
  const current = index >= 0 ? model.resources[index] : undefined
  const next = update(current)
  const resources = [...model.resources]
  if (next === null) {
    if (index >= 0) resources.splice(index, 1)
  } else {
    const normalized = { ...next, role: roleOf(next.term, next.statements) }
    if (index >= 0) resources[index] = normalized
    else resources.push(normalized)
  }
  return { ...model, resources }
}

function termFromKey(model: OntologyModel, key: string): Term | null {
  const found = findResource(model, key)
  if (found) return found.term
  if (key.startsWith('<') && key.endsWith('>')) return iri(key.slice(1, -1))
  if (key.startsWith('_:')) return bnode(key.slice(2))
  return null
}

/** A statement edit: index null = add, statement null = remove. */
export interface StatementEdit {
  index: number | null
  statement: Statement | null
}

/** Reifiers pointing at the statement `index` of `key` (resources with rdf:reifies <<( s p o )>>). */
export function reifiersOf(model: OntologyModel, key: string, index: number): string[] {
  const resource = findResource(model, key)
  const statement = resource?.statements[index]
  if (!resource || !statement) return []
  const target = termKey(asTriple(resource.term, statement))
  return model.resources
    .filter((r) => r.statements.some((s) => s.p === RDF_REIFIES && termKey(s.o) === target))
    .map(resourceKey)
}

/**
 * Apply one statement edit. When `followReifiers` is true and the edited statement
 * is reified, every `rdf:reifies` triple term pointing at the old statement is
 * re-pointed at the new one (PRP-0198 Q4: the default).
 */
export function applyStatementEdit(
  model: OntologyModel,
  key: string,
  edit: StatementEdit,
  followReifiers = true,
): OntologyModel {
  const subject = termFromKey(model, key)
  if (!subject) return model
  const before = edit.index !== null ? findResource(model, key)?.statements[edit.index] : undefined
  let next = withResource(model, key, (resource) => {
    const statements = [...(resource?.statements ?? [])]
    if (edit.index === null) {
      if (edit.statement && !statements.some((s) => statementEquals(s, edit.statement as Statement))) {
        statements.push(edit.statement)
      }
    } else if (edit.statement === null) {
      statements.splice(edit.index, 1)
    } else {
      statements[edit.index] = edit.statement
    }
    if (statements.length === 0) return null
    return { term: resource?.term ?? subject, role: resource?.role ?? 'other', statements }
  })
  if (followReifiers && before && edit.statement) {
    const oldKey = termKey(asTriple(subject, before))
    const replacement = asTriple(subject, edit.statement)
    next = {
      ...next,
      resources: next.resources.map((r) =>
        r.statements.some((s) => s.p === RDF_REIFIES && termKey(s.o) === oldKey)
          ? {
              ...r,
              statements: r.statements.map((s) =>
                s.p === RDF_REIFIES && termKey(s.o) === oldKey ? { p: s.p, o: replacement } : s,
              ),
            }
          : r,
      ),
    }
  }
  return next
}

/** The edit that sets the DISPLAYED literal of `predicate` to `text` (keeps language / datatype / direction). */
export function displayedLiteralEdit(
  resource: Resource | undefined,
  predicate: string,
  text: string,
): StatementEdit | null {
  const index = pickLiteralIndex(resource, predicate)
  if (index < 0 || !resource) {
    return text ? { index: null, statement: { p: predicate, o: literal(text) } } : null
  }
  if (!text) return { index, statement: null }
  const current = resource.statements[index].o as Extract<Term, { type: 'literal' }>
  if (current.value === text) return null
  return { index, statement: { p: predicate, o: { ...current, value: text } } }
}

/** The edit that sets the FIRST object of `predicate` (or adds it); null removes that one statement. */
export function firstObjectEdit(
  resource: Resource | undefined,
  predicate: string,
  term: Term | null,
): StatementEdit | null {
  const index = resource ? resource.statements.findIndex((s) => s.p === predicate) : -1
  if (index < 0) return term ? { index: null, statement: { p: predicate, o: term } } : null
  if (term === null) return { index, statement: null }
  if (termEquals((resource as Resource).statements[index].o, term)) return null
  return { index, statement: { p: predicate, o: term } }
}

export function applyEdits(model: OntologyModel, key: string, edits: (StatementEdit | null)[]): OntologyModel {
  let next = model
  for (const edit of edits) if (edit) next = applyStatementEdit(next, key, edit, true)
  return next
}

/** Write `cw:x` / `cw:y` of an entity (only ever called for a user layout action, UDR-0180 D7). */
export function setPosition(model: OntologyModel, entityIri: string, x: number, y: number): OntologyModel {
  const key = termKey(iri(entityIri))
  let next = model
  const xEdit = firstObjectEdit(findResource(next, key), CW_X, literal(formatDecimal(x), { datatype: XSD_DECIMAL }))
  if (xEdit) next = applyStatementEdit(next, key, xEdit)
  const yEdit = firstObjectEdit(findResource(next, key), CW_Y, literal(formatDecimal(y), { datatype: XSD_DECIMAL }))
  if (yEdit) next = applyStatementEdit(next, key, yEdit)
  return next
}

export function addResource(model: OntologyModel, term: Term, statements: Statement[]): OntologyModel {
  return withResource(model, termKey(term), (resource) => ({
    term,
    role: 'other',
    statements: [...(resource?.statements ?? []), ...statements],
  }))
}

export function removeResource(model: OntologyModel, key: string): OntologyModel {
  return withResource(model, key, () => null)
}

export function createEntity(
  model: OntologyModel,
  entityIri: string,
  label: string,
  x: number,
  y: number,
): OntologyModel {
  return addResource(model, iri(entityIri), [
    { p: RDF_TYPE, o: iri(OWL_CLASS) },
    { p: RDFS_LABEL, o: literal(label) },
    { p: CW_X, o: literal(formatDecimal(x), { datatype: XSD_DECIMAL }) },
    { p: CW_Y, o: literal(formatDecimal(y), { datatype: XSD_DECIMAL }) },
  ])
}

export function createRelationship(
  model: OntologyModel,
  relIri: string,
  source: string,
  target: string,
): OntologyModel {
  return addResource(model, iri(relIri), [
    { p: RDF_TYPE, o: iri(OWL_OBJECT_PROPERTY) },
    { p: RDFS_LABEL, o: literal('relates to') },
    { p: RDFS_DOMAIN, o: iri(source) },
    { p: RDFS_RANGE, o: iri(target) },
    { p: CW_CARDINALITY, o: literal(DEFAULT_CARDINALITY) },
  ])
}

export function createDatatypeProperty(model: OntologyModel, propIri: string, domain: string): OntologyModel {
  return addResource(model, iri(propIri), [
    { p: RDF_TYPE, o: iri(OWL_DATATYPE_PROPERTY) },
    { p: RDFS_LABEL, o: literal('property') },
    { p: RDFS_DOMAIN, o: iri(domain) },
    { p: RDFS_RANGE, o: iri(XSD_STRING) },
  ])
}

/** Toggle the KEY attribute: on adds `cw:isKey true`, off removes the true-valued statements only. */
export function setIsKey(model: OntologyModel, propIri: string, on: boolean): OntologyModel {
  const key = termKey(iri(propIri))
  const resource = findResource(model, key)
  if (!resource) return model
  if (on) {
    if (resource.statements.some((s) => s.p === CW_IS_KEY && isTrue(s.o))) return model
    return applyStatementEdit(model, key, {
      index: null,
      statement: { p: CW_IS_KEY, o: literal('true', { datatype: XSD_BOOLEAN }) },
    })
  }
  return withResource(model, key, (r) =>
    r ? { ...r, statements: r.statements.filter((s) => !(s.p === CW_IS_KEY && isTrue(s.o))) } : null,
  )
}

/** Remove a datatype property from ONE entity: drop that domain, or the property when it was the last one. */
export function removePropertyFromEntity(model: OntologyModel, propIri: string, entityIri: string): OntologyModel {
  const key = termKey(iri(propIri))
  const resource = findResource(model, key)
  if (!resource) return model
  const domains = iriObjects(resource, RDFS_DOMAIN)
  if (domains.filter((d) => d !== entityIri).length === 0) return removeResource(model, key)
  return withResource(model, key, (r) =>
    r
      ? {
          ...r,
          statements: r.statements.filter(
            (s) => !(s.p === RDFS_DOMAIN && s.o.type === 'iri' && s.o.value === entityIri),
          ),
        }
      : null,
  )
}

function mentions(term: Term, target: string): boolean {
  if (term.type === 'iri') return term.value === target
  if (term.type === 'triple') return mentions(term.s, target) || mentions(term.o, target)
  return false
}

function isPropertyLink(resource: Resource, statement: Statement): boolean {
  return (
    (resource.role === 'datatype_property' || resource.role === 'object_property') &&
    (statement.p === RDFS_DOMAIN || statement.p === RDFS_RANGE)
  )
}

/**
 * Statements of OTHER resources that refer to the entity and survive its deletion
 * (instances, rdfs:subClassOf, ...). Property domain / range links are handled by
 * the delete rules and are not counted (PRP-0198 Q3).
 */
export function externalReferenceCount(model: OntologyModel, entityIri: string): number {
  const key = termKey(iri(entityIri))
  let count = 0
  for (const resource of model.resources) {
    if (resourceKey(resource) === key) continue
    for (const statement of resource.statements) {
      if (isPropertyLink(resource, statement)) continue
      if (statement.p === entityIri || mentions(statement.o, entityIri)) count += 1
    }
  }
  return count
}

/**
 * Delete an entity (PRP-0198 2.4): its own statements go; a datatype property whose
 * only domain it was goes, otherwise only that domain link; an object property
 * loses the domain / range links naming it and goes only when it has no drawable
 * pair left. Statements of other resources that refer to it are KEPT.
 */
export function deleteEntity(model: OntologyModel, entityIri: string): OntologyModel {
  const entityKey = termKey(iri(entityIri))
  const remaining = new Set(entityIris(model))
  remaining.delete(entityIri)
  let next = removeResource(model, entityKey)
  for (const resource of model.resources) {
    const key = resourceKey(resource)
    if (key === entityKey) continue
    const links = resource.statements.filter(
      (s) => isPropertyLink(resource, s) && s.o.type === 'iri' && s.o.value === entityIri,
    )
    if (links.length === 0) continue
    const kept = resource.statements.filter((s) => !links.includes(s))
    let drop = false
    if (resource.role === 'datatype_property') {
      drop = !kept.some((s) => s.p === RDFS_DOMAIN)
    } else {
      const sources = kept.filter((s) => s.p === RDFS_DOMAIN && s.o.type === 'iri' && remaining.has(s.o.value))
      const targets = kept.filter((s) => s.p === RDFS_RANGE && s.o.type === 'iri' && remaining.has(s.o.value))
      drop = sources.length === 0 || targets.length === 0
    }
    next = drop ? removeResource(next, key) : withResource(next, key, (r) => (r ? { ...r, statements: kept } : null))
  }
  return next
}

/** All blank-node labels in use (the term editor offers them; new ones must not collide). */
export function blankLabels(model: OntologyModel): Set<string> {
  const labels = new Set<string>()
  const visit = (term: Term) => {
    if (term.type === 'bnode') labels.add(term.value)
    else if (term.type === 'triple') {
      visit(term.s)
      visit(term.o)
    }
  }
  for (const resource of model.resources) {
    visit(resource.term)
    for (const statement of resource.statements) visit(statement.o)
  }
  return labels
}

/** A fresh blank-node label: `n` + random suffix, unique in the model. */
export function mintBlankLabel(model: OntologyModel): string {
  const taken = blankLabels(model)
  for (;;) {
    const label = `n${Math.random().toString(36).slice(2, 10)}`
    if (!taken.has(label)) return label
  }
}

/** The items of an RDF collection starting at `head`, or null when it is not a well-formed list. */
export function listItems(model: OntologyModel, head: Term): Term[] | null {
  const items: Term[] = []
  const seen = new Set<string>()
  let node: Term = head
  for (;;) {
    if (node.type === 'iri' && node.value === RDF_NIL) return items
    if (node.type !== 'bnode') return null
    const key = termKey(node)
    if (seen.has(key)) return null
    seen.add(key)
    const cell = findResource(model, key)
    const first = objects(cell, RDF_FIRST)
    const rest = objects(cell, RDF_REST)
    if (!cell || first.length !== 1 || rest.length !== 1 || cell.statements.length !== 2) return null
    items.push(first[0])
    node = rest[0]
  }
}

/** The model as the PUT body carries it (roles are derived and not sent). */
export function toPayload(model: OntologyModel): {
  document: OntologyDocument
  resources: { term: Term; statements: Statement[] }[]
} {
  return {
    document: model.document,
    resources: model.resources.map((r) => ({ term: r.term, statements: r.statements })),
  }
}

/** Read a GET body into a model (roles re-derived so client and server agree). */
export function fromProjection(data: { document?: Partial<OntologyDocument>; resources?: Resource[] }): OntologyModel {
  return {
    document: {
      prefixes: data.document?.prefixes ?? [],
      base: data.document?.base ?? null,
      version: data.document?.version ?? null,
    },
    resources: (data.resources ?? []).map((r) => ({ ...r, role: roleOf(r.term, r.statements) })),
  }
}
