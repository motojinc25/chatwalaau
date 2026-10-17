// Inferred relationships for the canvas and the Data view (PRP-0211, UDR-0193 D17-D20).
//
// The statements come from the STORED entailment result of the saved ontology version
// (GET /api/ontology/{id}/entailment/statements); the browser never reasons. These pure
// functions decide what is worth drawing:
//
// - an inferred "is a" only when its superclass is not already reachable through the
//   drawn (stated) "is a" edges -- transitivity adds nothing;
// - inferred owl:equivalentClass between drawn classes as ONE "same as" edge;
// - an inferred relationship (domain x range of a property) only when no pair of the same
//   property is more specific on both ends -- a sub-property's inherited domain and range
//   appear, the super-classes of a stated domain do not;
// - for `p owl:inverseOf q`, q's edge reversed from p's when q has no domain / range of its
//   own: DERIVED from owl:inverseOf (OWL 2 RL gives an inverse no domain or range).
//
// Inferred and derived items are never part of the editing model.

import type { Term } from '@/lib/ontologyModel'

export type InferredMode = 'off' | 'rdfs' | 'owl-rl'

export const INFERRED_STORAGE_KEY = 'chatwalaau.ontology.inferred'
/** At most this many inferred / derived edges are drawn (UDR-0193 D18). */
export const MAX_INFERRED_EDGES = 500

const RDFS = 'http://www.w3.org/2000/01/rdf-schema#'
const OWL = 'http://www.w3.org/2002/07/owl#'
const RDF_TYPE = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'
const SUB_CLASS_OF = `${RDFS}subClassOf`
const EQUIVALENT_CLASS = `${OWL}equivalentClass`
const DOMAIN = `${RDFS}domain`
const RANGE = `${RDFS}range`

export interface InferredStatement {
  s: Term
  p: Term
  o: Term
  graph: Term | null
  kind: 'schema' | 'type' | 'value'
}

export interface InferredStatements {
  mode: Exclude<InferredMode, 'off'>
  revision: string | null
  state: string
  message: string | null
  statements: InferredStatement[]
  truncated: boolean
}

export function readInferredMode(): InferredMode {
  try {
    const value = window.localStorage.getItem(INFERRED_STORAGE_KEY)
    return value === 'rdfs' || value === 'owl-rl' ? value : 'off'
  } catch {
    return 'off'
  }
}

export function writeInferredMode(mode: InferredMode): void {
  try {
    window.localStorage.setItem(INFERRED_STORAGE_KEY, mode)
  } catch {
    // per-browser convenience only
  }
}

function iriOf(term: Term): string | null {
  return term.type === 'iri' ? term.value : null
}

/** A statement for Explain: Term JSON, graph included. */
export function statementOf(s: Term, p: Term, o: Term, graph: Term | null = null) {
  return { s, p, o, graph }
}

export interface InferredIsaEdge {
  id: string
  source: string
  target: string
  statement: InferredStatement
}

export interface InferredSameEdge {
  id: string
  source: string
  target: string
  statement: InferredStatement
}

export interface InferredRelationshipEdge {
  id: string
  property: string
  source: string
  target: string
  /** Derived from owl:inverseOf (not inferred by the reasoner). */
  derived: boolean
  /** For a derived edge: the property it is the inverse of. */
  inverseOf?: string
  /** The statement to explain (an inferred domain or range), absent for derived edges. */
  statement?: InferredStatement
}

export interface StatedProperty {
  iri: string
  domains: string[]
  ranges: string[]
  inverses: string[]
}

export interface InferredCanvas {
  isa: InferredIsaEdge[]
  same: InferredSameEdge[]
  relationships: InferredRelationshipEdge[]
  /** Edges left out by the cap. */
  hidden: number
}

/** Classes reachable from `start` through `edges` (sub -> supers). */
function reachable(start: string, supers: Map<string, string[]>): Set<string> {
  const seen = new Set<string>()
  const todo = [start]
  while (todo.length > 0) {
    const current = todo.pop() as string
    for (const next of supers.get(current) ?? []) {
      if (!seen.has(next)) {
        seen.add(next)
        todo.push(next)
      }
    }
  }
  return seen
}

/**
 * The inferred and derived edges worth drawing between the drawn classes (`entities`).
 * `statedIsa` are the drawn "is a" edges; `properties` the object properties of the model
 * with their stated domains, ranges and owl:inverseOf.
 */
export function inferredCanvas(
  statements: InferredStatement[],
  entities: Set<string>,
  statedIsa: { source: string; target: string }[],
  properties: StatedProperty[],
  focus: string | null = null,
): InferredCanvas {
  const statedSupers = new Map<string, string[]>()
  for (const edge of statedIsa) statedSupers.set(edge.source, [...(statedSupers.get(edge.source) ?? []), edge.target])
  // Every sub-class relation known (stated + inferred) for the "more specific" test.
  const allSupers = new Map<string, Set<string>>()
  const addSuper = (sub: string, sup: string) => {
    const set = allSupers.get(sub) ?? new Set<string>()
    set.add(sup)
    allSupers.set(sub, set)
  }
  for (const edge of statedIsa) addSuper(edge.source, edge.target)
  const domains = new Map<string, { cls: string; statement?: InferredStatement }[]>()
  const ranges = new Map<string, { cls: string; statement?: InferredStatement }[]>()
  const isa: InferredIsaEdge[] = []
  const same: InferredSameEdge[] = []
  const sameSeen = new Set<string>()
  for (const statement of statements) {
    if (statement.kind !== 'schema') continue
    const s = iriOf(statement.s)
    const o = iriOf(statement.o)
    const p = iriOf(statement.p)
    if (!s || !o || !p) continue
    if (p === SUB_CLASS_OF) addSuper(s, o)
    if (p === DOMAIN) domains.set(s, [...(domains.get(s) ?? []), { cls: o, statement }])
    if (p === RANGE) ranges.set(s, [...(ranges.get(s) ?? []), { cls: o, statement }])
    if (p === EQUIVALENT_CLASS && s !== o && entities.has(s) && entities.has(o)) {
      const key = s < o ? `${s}|${o}` : `${o}|${s}`
      if (!sameSeen.has(key)) {
        sameSeen.add(key)
        same.push({ id: `inferred-same|${key}`, source: s, target: o, statement })
      }
    }
  }
  for (const statement of statements) {
    if (statement.kind !== 'schema' || iriOf(statement.p) !== SUB_CLASS_OF) continue
    const s = iriOf(statement.s)
    const o = iriOf(statement.o)
    if (!s || !o || s === o || !entities.has(s) || !entities.has(o)) continue
    if (reachable(s, statedSupers).has(o)) continue // transitivity adds nothing
    // Equivalent classes draw one "same as" edge, not two "is a" edges.
    const key = s < o ? `${s}|${o}` : `${o}|${s}`
    if (sameSeen.has(key)) continue
    isa.push({ id: `inferred-isa|${s}|${o}`, source: s, target: o, statement })
  }
  const isSubOrEqual = (a: string, b: string) => a === b || (allSupers.get(a)?.has(b) ?? false)
  const relationships: InferredRelationshipEdge[] = []
  const byIri = new Map(properties.map((p) => [p.iri, p]))
  for (const property of new Set([...domains.keys(), ...ranges.keys()])) {
    const stated = byIri.get(property)
    const allDomains = [
      ...(stated?.domains ?? []).map((cls) => ({ cls, statement: undefined as InferredStatement | undefined })),
      ...(domains.get(property) ?? []),
    ]
    const allRanges = [
      ...(stated?.ranges ?? []).map((cls) => ({ cls, statement: undefined as InferredStatement | undefined })),
      ...(ranges.get(property) ?? []),
    ]
    const pairs: { d: string; r: string; statement?: InferredStatement; stated: boolean }[] = []
    for (const d of allDomains) {
      for (const r of allRanges) {
        pairs.push({ d: d.cls, r: r.cls, statement: d.statement ?? r.statement, stated: !d.statement && !r.statement })
      }
    }
    for (const pair of pairs) {
      if (pair.stated || !entities.has(pair.d) || !entities.has(pair.r)) continue
      const moreSpecific = pairs.some(
        (other) =>
          (other.d !== pair.d || other.r !== pair.r) && isSubOrEqual(other.d, pair.d) && isSubOrEqual(other.r, pair.r),
      )
      if (moreSpecific) continue
      relationships.push({
        id: `inferred-rel|${property}|${pair.d}|${pair.r}`,
        property,
        source: pair.d,
        target: pair.r,
        derived: false,
        statement: pair.statement,
      })
    }
  }
  // owl:inverseOf: q's edge, reversed from p's, when q has no domain / range of its own.
  const derivedSeen = new Set<string>()
  for (const property of properties) {
    for (const inverse of property.inverses) {
      for (const [p, q] of [
        [property.iri, inverse],
        [inverse, property.iri],
      ] as const) {
        const own = byIri.get(q)
        if (own && (own.domains.length > 0 || own.ranges.length > 0)) continue
        const base = byIri.get(p)
        if (!base) continue
        for (const d of base.domains) {
          for (const r of base.ranges) {
            if (!entities.has(d) || !entities.has(r)) continue
            const id = `derived-inv|${q}|${r}|${d}`
            if (derivedSeen.has(id)) continue
            derivedSeen.add(id)
            relationships.push({ id, property: q, source: r, target: d, derived: true, inverseOf: p })
          }
        }
      }
    }
  }
  const all = isa.length + same.length + relationships.length
  if (all <= MAX_INFERRED_EDGES) return { isa, same, relationships, hidden: 0 }
  // The cap: edges touching the focused class first, then in order.
  const touches = (edge: { source: string; target: string }) =>
    focus !== null && (edge.source === focus || edge.target === focus) ? 0 : 1
  const ranked = [
    ...isa.map((e) => ({ kind: 'isa' as const, e })),
    ...same.map((e) => ({ kind: 'same' as const, e })),
    ...relationships.map((e) => ({ kind: 'rel' as const, e })),
  ].sort((a, b) => touches(a.e) - touches(b.e))
  const kept = ranked.slice(0, MAX_INFERRED_EDGES)
  return {
    isa: kept.filter((k) => k.kind === 'isa').map((k) => k.e as InferredIsaEdge),
    same: kept.filter((k) => k.kind === 'same').map((k) => k.e as InferredSameEdge),
    relationships: kept.filter((k) => k.kind === 'rel').map((k) => k.e as InferredRelationshipEdge),
    hidden: all - kept.length,
  }
}

export interface InferredData {
  /** Individual key -> the classes it is inferred to belong to (with the statement). */
  types: Map<string, { cls: string; statement: InferredStatement }[]>
  /** Class -> the keys of the individuals inferred to belong to it. */
  members: Map<string, string[]>
  /** Individual key -> inferred property values. */
  values: Map<string, InferredStatement[]>
}

/** The Data view's inferred memberships and values; `keyOf` gives a term's resource key. */
export function inferredData(statements: InferredStatement[], keyOf: (term: Term) => string): InferredData {
  const types = new Map<string, { cls: string; statement: InferredStatement }[]>()
  const members = new Map<string, string[]>()
  const values = new Map<string, InferredStatement[]>()
  for (const statement of statements) {
    const key = keyOf(statement.s)
    if (statement.kind === 'type' && iriOf(statement.p) === RDF_TYPE) {
      const cls = iriOf(statement.o)
      if (!cls) continue
      types.set(key, [...(types.get(key) ?? []), { cls, statement }])
      members.set(cls, [...(members.get(cls) ?? []), key])
    } else if (statement.kind === 'value') {
      values.set(key, [...(values.get(key) ?? []), statement])
    }
  }
  return { types, members, values }
}
