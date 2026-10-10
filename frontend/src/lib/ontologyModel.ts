/**
 * The ontology editing model (CTR-0169 v3 / CTR-0173 v5, PRP-0198 / PRP-0200, UDR-0180 / UDR-0182).
 *
 * The backend serves a STATEMENT-COMPLETE, resource-centric projection: every
 * quad of the ontology is one statement `{p, o, g?}` of the resource for its
 * subject, with every term typed (RDF 1.2 Term JSON). `g` names the statement's
 * named graph; absent = the default graph (UDR-0182 D1). One resource per subject
 * across all graphs. This module is the ONE
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
export const OWL_ON_DATATYPE = `${OWL}onDatatype`
export const RDFS_CLASS = `${RDFS}Class`
export const RDFS_DATATYPE = `${RDFS}Datatype`
export const RDFS_SUB_CLASS_OF = `${RDFS}subClassOf`
export const RDFS_SUB_PROPERTY_OF = `${RDFS}subPropertyOf`
export const RDF_PROPERTY = `${RDF}Property`
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

/** A graph name: an IRI or a blank node (UDR-0182 D1). */
export type GraphTerm = Extract<Term, { type: 'iri' | 'bnode' }>

export interface Statement {
  p: string
  o: Term
  /** The named graph; absent = the default graph. */
  g?: GraphTerm
}

/**
 * A statement as an edit carries it: `g` undefined = keep the edited statement's
 * graph (or, for an added statement, the editor's target graph); `g: null` = the
 * default graph.
 */
export interface StatementInput {
  p: string
  o: Term
  g?: GraphTerm | null
}

/** What the canvas and the search show: every graph, the default graph, or one named graph. */
export type GraphScope = 'all' | 'default' | GraphTerm

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
  /** Named graphs known to the editor (from GET, plus graphs the user added). */
  graphs?: GraphTerm[]
  /** Editor state, never sent: the graph new statements go into (null = default). */
  targetGraph?: GraphTerm | null
}

export interface Diagnostic {
  kind: string
  s: Term
  p: string
  o: Term
  g?: GraphTerm
}

export const EMPTY_MODEL: OntologyModel = {
  document: { prefixes: [], base: null, version: null },
  resources: [],
  graphs: [],
  targetGraph: null,
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

/** The key of a statement's graph ('' = the default graph). */
export function graphKey(g: GraphTerm | null | undefined): string {
  return g ? termKey(g) : ''
}

export function statementEquals(a: Statement, b: Statement): boolean {
  return a.p === b.p && termEquals(a.o, b.o) && graphKey(a.g) === graphKey(b.g)
}

/** The same triple (predicate and object), whatever the graph. */
export function sameTriple(a: Statement, b: Statement): boolean {
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

/** Ranges that make an rdf:Property an attribute, besides xsd: and declared datatypes (UDR-0185 D1). */
export const LITERAL_RANGES = new Set([
  `${RDFS}Literal`,
  RDF_LANG_STRING,
  RDF_DIR_LANG_STRING,
  `${RDF}JSON`,
  `${RDF}HTML`,
  `${RDF}XMLLiteral`,
  `${RDF}PlainLiteral`,
])

/** True for a term typed rdfs:Datatype or carrying owl:onDatatype (a datatype restriction). */
export function declaresDatatype(statements: Statement[]): boolean {
  return statements.some(
    (s) => s.p === OWL_ON_DATATYPE || (s.p === RDF_TYPE && s.o.type === 'iri' && s.o.value === RDFS_DATATYPE),
  )
}

/** Answers "does the model declare the term with this key as a datatype?". */
export type DatatypeLookup = (key: string) => boolean

const NO_DATATYPES: DatatypeLookup = () => false

export function isLiteralRange(term: Term, isDatatype: DatatypeLookup = NO_DATATYPES): boolean {
  if (term.type === 'iri' && (LITERAL_RANGES.has(term.value) || term.value.startsWith(XSD))) return true
  return (term.type === 'iri' || term.type === 'bnode') && isDatatype(termKey(term))
}

/**
 * The display role of a resource (UDR-0185 D1) -- the same rules table the backend
 * runs on GET (`classify_role`); `tests/fixtures/ontology_roles/cases.json` pins
 * both (D2). Never stored, never written.
 */
export function roleOf(term: Term, statements: Statement[], isDatatype: DatatypeLookup = NO_DATATYPES): Role {
  if (term.type !== 'iri') return 'other'
  const types = new Set(
    statements.filter((s) => s.p === RDF_TYPE && s.o.type === 'iri').map((s) => (s.o as { value: string }).value),
  )
  if (types.has(OWL_CLASS) || (types.has(RDFS_CLASS) && !types.has(RDFS_DATATYPE))) return 'entity'
  if (types.has(OWL_OBJECT_PROPERTY)) return 'object_property'
  if (types.has(OWL_DATATYPE_PROPERTY)) return 'datatype_property'
  if (types.has(RDF_PROPERTY)) {
    const ranges = statements.filter((s) => s.p === RDFS_RANGE).map((s) => s.o)
    if (ranges.length === 0) return 'other'
    return ranges.every((r) => isLiteralRange(r, isDatatype)) ? 'datatype_property' : 'object_property'
  }
  return 'other'
}

/** Every resource with its role, classified against the whole list (UDR-0185 D1 / D2). */
export function classifyResources(resources: Resource[]): Resource[] {
  const datatypes = new Set(resources.filter((r) => declaresDatatype(r.statements)).map(resourceKey))
  const isDatatype: DatatypeLookup = (key) => datatypes.has(key)
  return resources.map((r) => {
    const role = roleOf(r.term, r.statements, isDatatype)
    return role === r.role ? r : { ...r, role }
  })
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

// ---- Class hierarchy (UDR-0185 D3 / D5 / D6) -----------------------------------------

export interface SubclassEdgeView {
  id: string
  /** The subclass (edge source). */
  source: string
  /** The superclass (edge target, the arrow end). */
  target: string
}

/** One "is a" edge per IRI `S rdfs:subClassOf O` where both are entities (blank-node superclasses are not drawn). */
export function subclassEdges(model: OntologyModel): SubclassEdgeView[] {
  const entities = entityIris(model)
  const seen = new Set<string>()
  const edges: SubclassEdgeView[] = []
  for (const resource of model.resources) {
    if (resource.role !== 'entity' || resource.term.type !== 'iri') continue
    const source = resource.term.value
    for (const target of iriObjects(resource, RDFS_SUB_CLASS_OF)) {
      const id = `isa|${source}|${target}`
      if (!entities.has(target) || seen.has(id)) continue
      seen.add(id)
      edges.push({ id, source, target })
    }
  }
  return edges
}

/** Add `sub rdfs:subClassOf sup` (one statement; a self link is refused, an existing one is kept as is). */
export function addSubclassOf(model: OntologyModel, sub: string, sup: string): OntologyModel {
  if (sub === sup) return model
  return applyStatementEdit(model, termKey(iri(sub)), {
    index: null,
    statement: { p: RDFS_SUB_CLASS_OF, o: iri(sup) },
  })
}

/** Remove the `sub rdfs:subClassOf sup` statement(s) the canvas shows for `scope`. */
export function removeSubclassOf(
  model: OntologyModel,
  sub: string,
  sup: string,
  scope: GraphScope = 'all',
): OntologyModel {
  const key = termKey(iri(sub))
  return withResource(model, key, (r) => {
    if (!r) return null
    const statements = r.statements.filter(
      (s) => !(s.p === RDFS_SUB_CLASS_OF && s.o.type === 'iri' && s.o.value === sup && inScope(s, scope)),
    )
    return statements.length === 0 ? null : { ...r, statements }
  })
}

/** The IRI superclasses of an entity, and the entities naming it as theirs. */
export function classHierarchy(model: OntologyModel, entityIri: string): { supers: string[]; subs: string[] } {
  const supers = [...new Set(iriObjects(findResource(model, termKey(iri(entityIri))), RDFS_SUB_CLASS_OF))]
  const subs = model.resources
    .filter((r) => r.term.type === 'iri' && iriObjects(r, RDFS_SUB_CLASS_OF).includes(entityIri))
    .map((r) => (r.term as { value: string }).value)
  return { supers: supers.sort(), subs: [...new Set(subs)].sort() }
}

const RESERVED_NAMESPACES = [RDF, RDFS, OWL, XSD]
const CLASS_USE_PREDICATES = new Set([RDFS_SUB_CLASS_OF, RDFS_DOMAIN, RDFS_RANGE])

/**
 * IRIs used as classes (either side of rdfs:subClassOf, an rdfs:domain / rdfs:range
 * object) that the ontology does not declare: not drawn (UDR-0185 D5), marked in
 * the inspector. Literal-like ranges and the rdf / rdfs / owl / xsd namespaces are
 * never counted.
 */
export function undeclaredClasses(model: OntologyModel): Set<string> {
  const entities = entityIris(model)
  const datatypes = new Set(model.resources.filter((r) => declaresDatatype(r.statements)).map(resourceKey))
  const isDatatype: DatatypeLookup = (key) => datatypes.has(key)
  const out = new Set<string>()
  const consider = (term: Term) => {
    if (term.type !== 'iri' || entities.has(term.value) || isLiteralRange(term, isDatatype)) return
    if (RESERVED_NAMESPACES.some((ns) => term.value.startsWith(ns))) return
    out.add(term.value)
  }
  for (const resource of model.resources) {
    for (const statement of resource.statements) {
      if (!CLASS_USE_PREDICATES.has(statement.p)) continue
      consider(statement.o)
      if (statement.p === RDFS_SUB_CLASS_OF) consider(resource.term)
    }
  }
  return out
}

/** True when this statement uses (or, for rdfs:subClassOf, is made by) an undeclared class. */
export function usesUndeclaredClass(undeclared: Set<string>, subject: Term, statement: Statement): boolean {
  if (!CLASS_USE_PREDICATES.has(statement.p)) return false
  if (statement.o.type === 'iri' && undeclared.has(statement.o.value)) return true
  return statement.p === RDFS_SUB_CLASS_OF && subject.type === 'iri' && undeclared.has(subject.value)
}

// ---- Vocabulary style for new terms (UDR-0185 D4) --------------------------------------

export type VocabularyStyle = 'owl' | 'rdfs'

/** OWL when anything uses the owl: namespace; else RDFS when rdfs:Class / rdf:Property is declared; else OWL. */
export function vocabularyStyle(model: OntologyModel): VocabularyStyle {
  let rdfs = false
  for (const resource of model.resources) {
    for (const s of resource.statements) {
      if (s.p.startsWith(OWL)) return 'owl'
      if (s.p !== RDF_TYPE || s.o.type !== 'iri') continue
      if (s.o.value.startsWith(OWL)) return 'owl'
      if (s.o.value === RDFS_CLASS || s.o.value === RDF_PROPERTY) rdfs = true
    }
  }
  return rdfs ? 'rdfs' : 'owl'
}

/** How the toolbar explains the detected style. */
export function vocabularyStyleReason(style: VocabularyStyle, model: OntologyModel): string {
  if (style === 'rdfs') return 'This ontology uses RDFS only (rdfs:Class / rdf:Property), so new terms are RDFS too.'
  return model.resources.length === 0
    ? 'New terms use OWL (owl:Class, owl:ObjectProperty, owl:DatatypeProperty).'
    : 'This ontology uses OWL terms, so new terms are OWL too.'
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
 * shown nested where they are used. The tab passes `individuals: false`: individuals
 * live in the Data view (UDR-0187 D8, UDR-0180 amendment A4).
 */
export function inspectorResources(model: OntologyModel, options: { individuals?: boolean } = {}): Resource[] {
  const drawn = canvasResourceKeys(model)
  const refs = blankReferenceCounts(model)
  const withIndividuals = options.individuals ?? true
  return model.resources.filter((r) => {
    const key = resourceKey(r)
    if (drawn.has(key)) return false
    if (!withIndividuals && isIndividual(r)) return false
    return !(r.term.type === 'bnode' && refs.get(key) === 1)
  })
}

// ---- Graphs (UDR-0182 D1 / D4) -------------------------------------------------------

/** Every named graph: the known ones first, then any a statement uses (first-seen order). */
export function graphsOf(model: OntologyModel): GraphTerm[] {
  const out = new Map<string, GraphTerm>()
  for (const g of model.graphs ?? []) out.set(termKey(g), g)
  for (const resource of model.resources) {
    for (const statement of resource.statements) {
      if (statement.g && !out.has(termKey(statement.g))) out.set(termKey(statement.g), statement.g)
    }
  }
  return [...out.values()]
}

export function inScope(statement: Statement, scope: GraphScope): boolean {
  if (scope === 'all') return true
  if (scope === 'default') return !statement.g
  return graphKey(statement.g) === termKey(scope)
}

const LAYOUT_PREDICATES = new Set([CW_X, CW_Y])

/**
 * The model the canvas draws for a graph selection: only statements in `scope`,
 * plus the layout statements (always in the default graph, UDR-0182 D3). Resources
 * with nothing left are hidden, never removed from the real model.
 */
export function scopeModel(model: OntologyModel, scope: GraphScope): OntologyModel {
  if (scope === 'all') return model
  const resources: Resource[] = []
  for (const resource of model.resources) {
    const kept = resource.statements.filter((s) => inScope(s, scope))
    if (kept.length === 0) continue
    const layout = resource.statements.filter((s) => LAYOUT_PREDICATES.has(s.p) && !kept.includes(s))
    resources.push({ ...resource, statements: [...kept, ...layout] })
  }
  return { ...model, resources: classifyResources(resources) }
}

/** True when the triple of statement `index` of `key` is asserted again in another graph. */
export function assertedElsewhere(model: OntologyModel, key: string, index: number): boolean {
  const resource = findResource(model, key)
  const statement = resource?.statements[index]
  if (!resource || !statement) return false
  return resource.statements.some((s, i) => i !== index && sameTriple(s, statement))
}

/** A statement with `g` resolved (undefined -> `fallback`, null -> the default graph). */
function resolveGraph(input: StatementInput, fallback: GraphTerm | null | undefined): Statement {
  const g = input.g === undefined ? fallback : input.g
  return g ? { p: input.p, o: input.o, g } : { p: input.p, o: input.o }
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
    // Reclassify only the edited resource, looking its ranges up in the model (UDR-0185 D2).
    const isDatatype: DatatypeLookup = (k) => {
      const found = k === key ? next : resources.find((r) => resourceKey(r) === k)
      return found ? declaresDatatype(found.statements) : false
    }
    const normalized = { ...next, role: roleOf(next.term, next.statements, isDatatype) }
    if (index >= 0) resources[index] = normalized
    else resources.push(normalized)
  }
  // A datatype declaration changed: every rdf:Property may change kind (D2).
  const datatypeBefore = current ? declaresDatatype(current.statements) : false
  const datatypeAfter = next ? declaresDatatype(next.statements) : false
  if (datatypeBefore !== datatypeAfter) return { ...model, resources: classifyResources(resources) }
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
  statement: StatementInput | null
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
 * re-pointed at the new one (PRP-0198 Q4: the default) -- unless the old triple is
 * still asserted in another graph, whose occurrence the reifiers may describe
 * (UDR-0182 D9).
 *
 * Graphs: an edited statement keeps its graph unless the edit names one; an added
 * statement goes into the editor's target graph, except layout (`cw:x` / `cw:y`),
 * which always stays in the default graph (UDR-0182 D3).
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
  const fallback = before ? before.g : LAYOUT_PREDICATES.has(edit.statement?.p ?? '') ? null : model.targetGraph
  const after = edit.statement ? resolveGraph(edit.statement, fallback) : null
  let next = withResource(model, key, (resource) => {
    const statements = [...(resource?.statements ?? [])]
    if (edit.index === null) {
      if (after && !statements.some((s) => statementEquals(s, after))) statements.push(after)
    } else if (after === null) {
      statements.splice(edit.index, 1)
    } else {
      statements[edit.index] = after
    }
    if (statements.length === 0) return null
    return { term: resource?.term ?? subject, role: resource?.role ?? 'other', statements }
  })
  const stillAsserted = before
    ? (findResource(next, key)?.statements.some((s) => sameTriple(s, before)) ?? false)
    : false
  if (followReifiers && before && after && !stillAsserted) {
    const oldKey = termKey(asTriple(subject, before))
    const replacement = asTriple(subject, after)
    next = {
      ...next,
      resources: next.resources.map((r) =>
        r.statements.some((s) => s.p === RDF_REIFIES && termKey(s.o) === oldKey)
          ? {
              ...r,
              statements: r.statements.map((s) =>
                s.p === RDF_REIFIES && termKey(s.o) === oldKey ? { ...s, o: replacement } : s,
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

/** Add statements to a resource; statements without a graph go into the target graph (layout: default). */
export function addResource(model: OntologyModel, term: Term, statements: StatementInput[]): OntologyModel {
  const resolved = statements.map((s) => resolveGraph(s, LAYOUT_PREDICATES.has(s.p) ? null : model.targetGraph))
  return withResource(model, termKey(term), (resource) => {
    const merged = [...(resource?.statements ?? [])]
    for (const statement of resolved) if (!merged.some((s) => statementEquals(s, statement))) merged.push(statement)
    return { term, role: 'other', statements: merged }
  })
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
  style: VocabularyStyle = 'owl',
): OntologyModel {
  return addResource(model, iri(entityIri), [
    { p: RDF_TYPE, o: iri(style === 'rdfs' ? RDFS_CLASS : OWL_CLASS) },
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
  style: VocabularyStyle = 'owl',
): OntologyModel {
  return addResource(model, iri(relIri), [
    { p: RDF_TYPE, o: iri(style === 'rdfs' ? RDF_PROPERTY : OWL_OBJECT_PROPERTY) },
    { p: RDFS_LABEL, o: literal('relates to') },
    { p: RDFS_DOMAIN, o: iri(source) },
    { p: RDFS_RANGE, o: iri(target) },
    { p: CW_CARDINALITY, o: literal(DEFAULT_CARDINALITY) },
  ])
}

export function createDatatypeProperty(
  model: OntologyModel,
  propIri: string,
  domain: string,
  style: VocabularyStyle = 'owl',
): OntologyModel {
  return addResource(model, iri(propIri), [
    { p: RDF_TYPE, o: iri(style === 'rdfs' ? RDF_PROPERTY : OWL_DATATYPE_PROPERTY) },
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

// ---- Property characteristics (UDR-0186 D3 / D4) -------------------------------------

/** The seven OWL property characteristics, each one `p rdf:type <iri>` statement. */
export const CHARACTERISTICS = [
  { key: 'functional', label: 'Functional', iri: `${OWL}FunctionalProperty` },
  { key: 'inverseFunctional', label: 'Inverse functional', iri: `${OWL}InverseFunctionalProperty` },
  { key: 'transitive', label: 'Transitive', iri: `${OWL}TransitiveProperty` },
  { key: 'symmetric', label: 'Symmetric', iri: `${OWL}SymmetricProperty` },
  { key: 'asymmetric', label: 'Asymmetric', iri: `${OWL}AsymmetricProperty` },
  { key: 'reflexive', label: 'Reflexive', iri: `${OWL}ReflexiveProperty` },
  { key: 'irreflexive', label: 'Irreflexive', iri: `${OWL}IrreflexiveProperty` },
] as const

export type Characteristic = (typeof CHARACTERISTICS)[number]['key']

function characteristicIri(characteristic: Characteristic): string {
  return (CHARACTERISTICS.find((c) => c.key === characteristic) as { iri: string }).iri
}

function isCharacteristicStatement(statement: Statement, typeIri: string): boolean {
  return statement.p === RDF_TYPE && statement.o.type === 'iri' && statement.o.value === typeIri
}

/** The characteristics a property has: its rdf:type statement exists in any graph. */
export function propertyCharacteristics(resource: Resource | undefined): Set<Characteristic> {
  const out = new Set<Characteristic>()
  if (!resource) return out
  for (const c of CHARACTERISTICS) {
    if (resource.statements.some((s) => isCharacteristicStatement(s, c.iri))) out.add(c.key)
  }
  return out
}

/** The graphs holding a characteristic's statements (null = the default graph), for the tooltip. */
export function characteristicGraphs(
  resource: Resource | undefined,
  characteristic: Characteristic,
): (GraphTerm | null)[] {
  if (!resource) return []
  const typeIri = characteristicIri(characteristic)
  return resource.statements.filter((s) => isCharacteristicStatement(s, typeIri)).map((s) => s.g ?? null)
}

/**
 * Turn a characteristic on (add ONE `rdf:type` statement in the target graph) or off
 * (remove that characteristic's `rdf:type` statements in every graph). Nothing else
 * changes; roles are unaffected (UDR-0186 D3).
 */
export function setCharacteristic(
  model: OntologyModel,
  propIri: string,
  characteristic: Characteristic,
  on: boolean,
): OntologyModel {
  const key = termKey(iri(propIri))
  const resource = findResource(model, key)
  if (!resource) return model
  const typeIri = characteristicIri(characteristic)
  const has = resource.statements.some((s) => isCharacteristicStatement(s, typeIri))
  if (on) {
    if (has) return model
    return applyStatementEdit(model, key, { index: null, statement: { p: RDF_TYPE, o: iri(typeIri) } })
  }
  if (!has) return model
  return withResource(model, key, (r) =>
    r ? { ...r, statements: r.statements.filter((s) => !isCharacteristicStatement(s, typeIri)) } : null,
  )
}

/** Notices for a set of characteristics: contradictions and OWL 2 DL limits; never a refusal (D4). */
export function characteristicNotices(set: Set<Characteristic>): string[] {
  const notices: string[] = []
  if (set.has('symmetric') && set.has('asymmetric')) {
    notices.push('Symmetric and Asymmetric contradict each other.')
  }
  if (set.has('reflexive') && set.has('irreflexive')) {
    notices.push('Reflexive and Irreflexive contradict each other.')
  }
  if (set.has('transitive')) {
    const limited = CHARACTERISTICS.filter(
      (c) =>
        (c.key === 'functional' ||
          c.key === 'inverseFunctional' ||
          c.key === 'asymmetric' ||
          c.key === 'irreflexive') &&
        set.has(c.key),
    ).map((c) => c.label)
    if (limited.length > 0) {
      notices.push(
        `OWL 2 DL does not allow a Transitive property to also be ${limited.join(', ')}: reasoners may reject this.`,
      )
    }
  }
  return notices
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
    for (const statement of resource.statements) {
      visit(statement.o)
      if (statement.g) visit(statement.g)
    }
  }
  for (const g of model.graphs ?? []) visit(g)
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

// ---- Annotations (reifiers; UDR-0182 D8) ----------------------------------------------

/** A fresh reifier IRI in the ontology's namespace: `<base>r-<8 hex>` (PRP-0200 Q5). */
export function mintReifierIri(model: OntologyModel, base: string): string {
  const taken = new Set(model.resources.map((r) => termKey(r.term)))
  for (;;) {
    let hex = ''
    for (let i = 0; i < 8; i += 1) hex += Math.floor(Math.random() * 16).toString(16)
    const candidate = `${base}r-${hex}`
    if (!taken.has(termKey(iri(candidate)))) return candidate
  }
}

/**
 * Annotate statement `index` of `key`: the reifier gets `rdf:reifies <<( s p o )>>`
 * in the statement's graph, plus an optional first annotation in the same graph.
 */
export function annotateStatement(
  model: OntologyModel,
  key: string,
  index: number,
  reifierIri: string,
  annotation: { p: string; o: Term } | null,
): OntologyModel {
  const resource = findResource(model, key)
  const statement = resource?.statements[index]
  if (!resource || !statement) return model
  const g = statement.g ?? null
  const statements: StatementInput[] = [{ p: RDF_REIFIES, o: asTriple(resource.term, statement), g }]
  if (annotation) statements.push({ ...annotation, g })
  return addResource(model, iri(reifierIri), statements)
}

/**
 * Remove statement `index` of `key`. With `deleteAnnotations`, the reifiers of its
 * triple lose that `rdf:reifies` link and a reifier left without one loses all its
 * statements -- unless the triple is still asserted in another graph (UDR-0182 D9).
 */
export function removeStatement(
  model: OntologyModel,
  key: string,
  index: number,
  deleteAnnotations: boolean,
): OntologyModel {
  const resource = findResource(model, key)
  const statement = resource?.statements[index]
  if (!resource || !statement) return model
  let next = applyStatementEdit(model, key, { index, statement: null }, false)
  if (!deleteAnnotations || assertedElsewhere(model, key, index)) return next
  const target = termKey(asTriple(resource.term, statement))
  for (const reifier of model.resources) {
    const links = reifier.statements.filter((s) => s.p === RDF_REIFIES && termKey(s.o) === target)
    if (links.length === 0) continue
    const keepsAnother = reifier.statements.some((s) => s.p === RDF_REIFIES && !links.includes(s))
    next = withResource(next, resourceKey(reifier), (r) =>
      r && keepsAnother
        ? { ...r, statements: r.statements.filter((s) => !links.some((link) => statementEquals(link, s))) }
        : null,
    )
  }
  return next
}

// ---- Statement saves (CTR-0173 v6, UDR-0183 D6 / D7) ---------------------------------

/** One statement as `POST /statements` carries it (`g` absent = the default graph). */
export interface QuadStatement {
  s: Term
  p: string
  o: Term
  g?: GraphTerm
}

export interface StatementOperation extends QuadStatement {
  op: 'add' | 'remove'
}

/** What the editor loaded (or last saved): the save diff is taken against it. */
export interface SaveBase {
  statements: Map<string, QuadStatement>
  document: string
}

/** Every statement of the model by an exact (s, p, o, g) key. */
export function statementSet(model: OntologyModel): Map<string, QuadStatement> {
  const out = new Map<string, QuadStatement>()
  for (const resource of model.resources) {
    const s = termKey(resource.term)
    for (const statement of resource.statements) {
      const key = `${s} <${statement.p}> ${termKey(statement.o)} ${graphKey(statement.g)}`
      out.set(
        key,
        statement.g
          ? { s: resource.term, p: statement.p, o: statement.o, g: statement.g }
          : { s: resource.term, p: statement.p, o: statement.o },
      )
    }
  }
  return out
}

/** The document (prefixes, base, VERSION) as a comparable string. */
export function documentKey(document: OntologyDocument): string {
  return JSON.stringify([document.prefixes, document.base, document.version])
}

export function saveBase(model: OntologyModel): SaveBase {
  return { statements: statementSet(model), document: documentKey(model.document) }
}

function visitLabels(term: Term, out: Set<string>): void {
  if (term.type === 'bnode') out.add(term.value)
  else if (term.type === 'triple') {
    visitLabels(term.s, out)
    visitLabels(term.o, out)
  }
}

function statementLabels(statement: QuadStatement, out: Set<string>): void {
  visitLabels(statement.s, out)
  visitLabels(statement.o, out)
  if (statement.g) visitLabels(statement.g, out)
}

/**
 * The changes since `base` as statement operations: every removed statement, then
 * every added one, plus the blank-node labels this editor created (labels the base
 * does not use), which the server renames when the file uses them (UDR-0183 D6).
 * Reifier follow-ups are already statements of the model, so they travel as well.
 */
export function diffStatements(
  base: Map<string, QuadStatement>,
  model: OntologyModel,
): { operations: StatementOperation[]; freshBlankNodes: string[] } {
  const current = statementSet(model)
  const operations: StatementOperation[] = []
  for (const [key, statement] of base) {
    if (!current.has(key)) operations.push({ op: 'remove', ...statement })
  }
  const added: QuadStatement[] = []
  for (const [key, statement] of current) {
    if (!base.has(key)) {
      operations.push({ op: 'add', ...statement })
      added.push(statement)
    }
  }
  const known = new Set<string>()
  for (const statement of base.values()) statementLabels(statement, known)
  const fresh = new Set<string>()
  for (const statement of added) statementLabels(statement, fresh)
  return { operations, freshBlankNodes: [...fresh].filter((label) => !known.has(label)).sort() }
}

/** The model with blank-node labels renamed by the server (`{ old: new }`). */
export function renameBlankNodes(model: OntologyModel, renamed: Record<string, string>): OntologyModel {
  if (Object.keys(renamed).length === 0) return model
  const rename = <T extends Term>(term: T): T => {
    if (term.type === 'bnode' && term.value in renamed) return { ...term, value: renamed[term.value] }
    if (term.type === 'triple') return { ...term, s: rename(term.s), o: rename(term.o) }
    return term
  }
  return {
    ...model,
    resources: model.resources.map((resource) => ({
      ...resource,
      term: rename(resource.term),
      statements: resource.statements.map((statement) =>
        statement.g
          ? { ...statement, o: rename(statement.o), g: rename(statement.g) }
          : { ...statement, o: rename(statement.o) },
      ),
    })),
    graphs: model.graphs?.map((g) => rename(g)),
    targetGraph: model.targetGraph ? rename(model.targetGraph) : model.targetGraph,
  }
}

/** The model as the PUT body carries it (roles and editor state are not sent; CTR-0169 v3). */
export function toPayload(model: OntologyModel): {
  projection_version: 3
  document: OntologyDocument
  resources: { term: Term; statements: Statement[] }[]
} {
  return {
    projection_version: 3,
    document: model.document,
    resources: model.resources.map((r) => ({ term: r.term, statements: r.statements })),
  }
}

/** Read a GET body into a model (roles re-derived so client and server agree). */
export function fromProjection(data: {
  document?: Partial<OntologyDocument>
  resources?: Resource[]
  graphs?: GraphTerm[]
}): OntologyModel {
  return {
    document: {
      prefixes: data.document?.prefixes ?? [],
      base: data.document?.base ?? null,
      version: data.document?.version ?? null,
    },
    resources: classifyResources(data.resources ?? []),
    graphs: data.graphs ?? [],
    targetGraph: null,
  }
}

// ---- Individuals: the Data view (UDR-0187) -------------------------------------------

export const OWL_NAMED_INDIVIDUAL = `${OWL}NamedIndividual`
const OWL_UNION_OF = `${OWL}unionOf`

function reservedIri(value: string): boolean {
  return RESERVED_NAMESPACES.some((ns) => value.startsWith(ns))
}

/**
 * The classes an individual is asserted to belong to: its `rdf:type` IRIs outside the
 * rdf / rdfs / owl / xsd namespaces (UDR-0187 D1). No inference.
 */
export function individualClasses(resource: Resource): string[] {
  const out: string[] = []
  for (const s of resource.statements) {
    if (s.p !== RDF_TYPE || s.o.type !== 'iri') continue
    const value = s.o.value
    if (reservedIri(value) || out.includes(value)) continue
    out.push(value)
  }
  return out
}

/** An individual: typed by a class outside the reserved namespaces, or by owl:NamedIndividual (D1). */
export function isIndividual(resource: Resource): boolean {
  return resource.statements.some(
    (s) => s.p === RDF_TYPE && s.o.type === 'iri' && (s.o.value === OWL_NAMED_INDIVIDUAL || !reservedIri(s.o.value)),
  )
}

export interface IndividualIndex {
  /** Class IRI -> its individuals (direct `rdf:type` only), in model order. */
  members: Map<string, Resource[]>
  /** Classes used as a type that the ontology does not declare (the "Not declared" group), sorted. */
  undeclared: string[]
  /** Individuals typed only `owl:NamedIndividual` (the "No class" group). */
  noClass: Resource[]
  /** Every individual's key. */
  keys: Set<string>
}

/** One pass over the model: who belongs to which class (memoize per model; UDR-0187 D9). */
export function individualIndex(model: OntologyModel): IndividualIndex {
  const entities = entityIris(model)
  const members = new Map<string, Resource[]>()
  const undeclared = new Set<string>()
  const noClass: Resource[] = []
  const keys = new Set<string>()
  for (const resource of model.resources) {
    if (!isIndividual(resource)) continue
    keys.add(resourceKey(resource))
    const classes = individualClasses(resource)
    if (classes.length === 0) noClass.push(resource)
    for (const cls of classes) {
      const list = members.get(cls)
      if (list) list.push(resource)
      else members.set(cls, [resource])
      if (!entities.has(cls)) undeclared.add(cls)
    }
  }
  return { members, undeclared: [...undeclared].sort(), noClass, keys }
}

export interface ClassTree {
  /** Entities with no entity superclass, plus one entry per otherwise unreachable cycle. */
  roots: string[]
  /** Entity -> its entity subclasses (asserted IRI rdfs:subClassOf), sorted. */
  children: Map<string, string[]>
}

/**
 * The Data view's class tree (UDR-0187 D2): asserted IRI `rdfs:subClassOf` between
 * entities. A class with several superclasses appears under each; the renderer
 * stops at a class already on the current path, so cycles are cut.
 */
export function classTree(model: OntologyModel): ClassTree {
  const entities = entityIris(model)
  const children = new Map<string, string[]>()
  const hasSuper = new Set<string>()
  for (const resource of model.resources) {
    if (resource.role !== 'entity' || resource.term.type !== 'iri') continue
    const sub = resource.term.value
    for (const sup of new Set(iriObjects(resource, RDFS_SUB_CLASS_OF))) {
      if (!entities.has(sup) || sup === sub) continue
      hasSuper.add(sub)
      const list = children.get(sup)
      if (!list) children.set(sup, [sub])
      else if (!list.includes(sub)) list.push(sub)
    }
  }
  for (const list of children.values()) list.sort()
  const sorted = [...entities].sort()
  const roots = sorted.filter((e) => !hasSuper.has(e))
  // A cycle with no way in from a root would vanish: its first class becomes a root.
  const reached = new Set<string>()
  const visit = (start: string) => {
    const stack = [start]
    while (stack.length > 0) {
      const cls = stack.pop() as string
      if (reached.has(cls)) continue
      reached.add(cls)
      for (const child of children.get(cls) ?? []) stack.push(child)
    }
  }
  for (const root of roots) visit(root)
  for (const cls of sorted) {
    if (reached.has(cls)) continue
    roots.push(cls)
    visit(cls)
  }
  return { roots, children }
}

export interface MemberRow {
  resource: Resource
  /** The class the row is listed for (the selected class, or the subclass it came from). */
  cls: string
}

/**
 * The rows of a class's table: its individuals, plus (when `includeSubclasses`) those
 * of every asserted subclass, each individual once. Display only: nothing is inferred
 * or stored (UDR-0187 D2).
 */
export function classMembers(
  index: IndividualIndex,
  tree: ClassTree,
  cls: string,
  includeSubclasses: boolean,
): MemberRow[] {
  const rows: MemberRow[] = []
  const seen = new Set<string>()
  const visited = new Set<string>()
  const queue = [cls]
  while (queue.length > 0) {
    const current = queue.shift() as string
    if (visited.has(current)) continue
    visited.add(current)
    for (const resource of index.members.get(current) ?? []) {
      const key = resourceKey(resource)
      if (seen.has(key)) continue
      seen.add(key)
      rows.push({ resource, cls: current })
    }
    if (includeSubclasses) queue.push(...(tree.children.get(current) ?? []))
  }
  return rows
}

/** The classes and all their asserted IRI superclasses (cycle-safe). */
export function superclassClosure(model: OntologyModel, classes: string[]): Set<string> {
  const out = new Set<string>()
  const stack = [...classes]
  while (stack.length > 0) {
    const cls = stack.pop() as string
    if (out.has(cls)) continue
    out.add(cls)
    stack.push(...iriObjects(findResource(model, termKey(iri(cls))), RDFS_SUB_CLASS_OF))
  }
  return out
}

export interface DataColumn {
  predicate: string
  label: string
  /** Declared by the schema (a domain covers the class); false = only used by the data. */
  inSchema: boolean
  /** The property's single IRI range, if any (drives the input). */
  range: string | null
  /** Values are resources (an object property, or a class range), not literals. */
  objectValued: boolean
  functional: boolean
}

const DATA_HIDDEN_PREDICATES = new Set([RDF_TYPE, RDFS_LABEL])

function domainCovers(model: OntologyModel, domain: Term, classes: Set<string>): boolean {
  if (domain.type === 'iri') return classes.has(domain.value)
  if (domain.type !== 'bnode') return false
  const union = findResource(model, termKey(domain))?.statements.find((s) => s.p === OWL_UNION_OF)
  if (!union) return false
  return (listItems(model, union.o) ?? []).some((t) => t.type === 'iri' && classes.has(t.value))
}

function columnFor(
  resource: Resource | undefined,
  predicate: string,
  inSchema: boolean,
  isDatatype: DatatypeLookup,
): DataColumn {
  const ranges = resource ? resource.statements.filter((s) => s.p === RDFS_RANGE).map((s) => s.o) : []
  const range = ranges.length === 1 && ranges[0].type === 'iri' ? ranges[0].value : null
  const objectValued =
    resource?.role === 'object_property' ||
    (resource?.role !== 'datatype_property' && range !== null && !isLiteralRange(iri(range), isDatatype))
  return {
    predicate,
    label: displayLiteral(resource, RDFS_LABEL) || localName(predicate),
    inSchema,
    range,
    objectValued,
    functional: propertyCharacteristics(resource).has('functional'),
  }
}

/**
 * The table's columns after the IRI and label columns (UDR-0187 D3): first the
 * properties whose rdfs:domain is one of `classes` or an asserted superclass (an
 * owl:unionOf domain counts when it lists one), then every other predicate the rows
 * use, marked not in schema. rdf:type, rdfs:label and the cw: layout terms are not columns.
 */
export function dataColumns(model: OntologyModel, classes: string[], rows: Resource[]): DataColumn[] {
  const covered = superclassClosure(model, classes)
  const datatypes = new Set(model.resources.filter((r) => declaresDatatype(r.statements)).map(resourceKey))
  const isDatatype: DatatypeLookup = (k) => datatypes.has(k)
  const schema: DataColumn[] = []
  const inSchema = new Set<string>()
  for (const resource of model.resources) {
    if (resource.term.type !== 'iri') continue
    const predicate = resource.term.value
    if (DATA_HIDDEN_PREDICATES.has(predicate) || inSchema.has(predicate)) continue
    const domains = resource.statements.filter((s) => s.p === RDFS_DOMAIN).map((s) => s.o)
    if (!domains.some((d) => domainCovers(model, d, covered))) continue
    inSchema.add(predicate)
    schema.push(columnFor(resource, predicate, true, isDatatype))
  }
  const used: DataColumn[] = []
  const seen = new Set<string>()
  for (const row of rows) {
    for (const s of row.statements) {
      if (DATA_HIDDEN_PREDICATES.has(s.p) || s.p.startsWith(CW) || inSchema.has(s.p) || seen.has(s.p)) continue
      seen.add(s.p)
      used.push(columnFor(findResource(model, termKey(iri(s.p))), s.p, false, isDatatype))
    }
  }
  const byLabel = (a: DataColumn, b: DataColumn) =>
    a.label.localeCompare(b.label) || a.predicate.localeCompare(b.predicate)
  return [...schema.sort(byLabel), ...used.sort(byLabel)]
}

export interface CellValue {
  /** The statement's index in its resource (the edit address). */
  index: number
  term: Term
  g: GraphTerm | null
}

/** Every value of `predicate` on the individual, one per statement (UDR-0187 D4). */
export function cellValues(resource: Resource | undefined, predicate: string): CellValue[] {
  if (!resource) return []
  const out: CellValue[] = []
  resource.statements.forEach((s, index) => {
    if (s.p === predicate) out.push({ index, term: s.o, g: s.g ?? null })
  })
  return out
}

/** The same term with a new lexical form (a literal keeps datatype, language and direction) or a new IRI. */
export function relexical(term: Term, text: string): Term {
  if (term.type === 'literal') return { ...term, value: text }
  if (term.type === 'iri') return iri(text)
  return term
}

/** Add one value: one statement in the target graph (D4). */
export function addValue(model: OntologyModel, key: string, predicate: string, term: Term): OntologyModel {
  return applyStatementEdit(model, key, { index: null, statement: { p: predicate, o: term } })
}

/** Remove one value: that statement only (D4). */
export function removeValue(model: OntologyModel, key: string, index: number): OntologyModel {
  return applyStatementEdit(model, key, { index, statement: null })
}

/** Change one value: that statement, staying in its own graph (D4). */
export function replaceValue(model: OntologyModel, key: string, index: number, term: Term): OntologyModel {
  const statement = findResource(model, key)?.statements[index]
  if (!statement || termEquals(statement.o, term)) return model
  return applyStatementEdit(model, key, { index, statement: { p: statement.p, o: term } })
}

const DOUBLE_FORM = /^([+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?|[+-]?INF|NaN)$/
const LEXICAL_FORMS: Record<string, RegExp> = {
  [`${XSD}integer`]: /^[+-]?\d+$/,
  [`${XSD}nonNegativeInteger`]: /^\+?\d+$/,
  [`${XSD}decimal`]: /^[+-]?(\d+(\.\d*)?|\.\d+)$/,
  [`${XSD}double`]: DOUBLE_FORM,
  [`${XSD}float`]: DOUBLE_FORM,
  [`${XSD}boolean`]: /^(true|false|1|0)$/,
  [`${XSD}date`]: /^-?\d{4,}-\d{2}-\d{2}(Z|[+-]\d{2}:\d{2})?$/,
  [`${XSD}dateTime`]: /^-?\d{4,}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$/,
}

/** A notice when a literal's lexical form does not fit its datatype; never a refusal (UDR-0180 D8). */
export function lexicalNotice(term: Term): string | null {
  if (term.type !== 'literal') return null
  const form = LEXICAL_FORMS[term.datatype]
  if (!form || form.test(term.value)) return null
  return `"${term.value}" is not a valid ${localName(term.datatype)}; it is kept as written.`
}

/** A notice when a Functional property holds more than one value (UDR-0187 D4). */
export function functionalNotice(column: DataColumn, count: number): string | null {
  if (!column.functional || count <= 1) return null
  return `${column.label} is Functional but has ${count} values.`
}

/**
 * Create an individual (UDR-0187 D6): exactly `<iri> rdf:type <class>` in the target
 * graph, plus `rdfs:label` only when a label is given. Never owl:NamedIndividual.
 */
export function createIndividual(model: OntologyModel, individualIri: string, cls: string, label = ''): OntologyModel {
  const statements: StatementInput[] = [{ p: RDF_TYPE, o: iri(cls) }]
  if (label.trim()) statements.push({ p: RDFS_LABEL, o: literal(label.trim()) })
  return addResource(model, iri(individualIri), statements)
}

function mentionsTerm(term: Term, target: Term): boolean {
  if (term.type === 'triple') return mentionsTerm(term.s, target) || mentionsTerm(term.o, target)
  return termEquals(term, target)
}

export interface IndividualReferences {
  /** The individual's own statements (all graphs). */
  own: number
  /** Statements of other resources whose object is the individual or a triple term mentioning it. */
  references: number
  /** How many other resources hold those statements. */
  resources: number
  /** Reifiers that lose their rdf:reifies link but keep their other statements (annotations). */
  keptAnnotations: number
}

/** What deleting an individual removes (UDR-0187 D7) -- the confirmation's counts. */
export function individualReferences(model: OntologyModel, key: string): IndividualReferences {
  const resource = findResource(model, key)
  const result: IndividualReferences = {
    own: resource?.statements.length ?? 0,
    references: 0,
    resources: 0,
    keptAnnotations: 0,
  }
  if (!resource) return result
  for (const other of model.resources) {
    if (resourceKey(other) === key) continue
    const hits = other.statements.filter((s) => mentionsTerm(s.o, resource.term))
    if (hits.length === 0) continue
    result.references += hits.length
    result.resources += 1
    if (hits.some((s) => s.p === RDF_REIFIES) && hits.length < other.statements.length) result.keptAnnotations += 1
  }
  return result
}

/**
 * Delete an individual (UDR-0187 D7): its own statements and every statement of
 * another resource whose object is it or a triple term mentioning it, in all graphs.
 * A reifier keeps its other statements (its annotations); a resource left with no
 * statement disappears.
 */
export function deleteIndividual(model: OntologyModel, key: string): OntologyModel {
  const resource = findResource(model, key)
  if (!resource) return model
  let changed = false
  const resources: Resource[] = []
  for (const other of model.resources) {
    if (resourceKey(other) === key) continue
    const kept = other.statements.filter((s) => !mentionsTerm(s.o, resource.term))
    if (kept.length === other.statements.length) {
      resources.push(other)
      continue
    }
    changed = true
    if (kept.length > 0) resources.push({ ...other, statements: kept })
  }
  return { ...model, resources: changed ? classifyResources(resources) : resources }
}
