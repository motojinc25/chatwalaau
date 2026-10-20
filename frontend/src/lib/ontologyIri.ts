/**
 * Ontology IRI namespaces (PRP-0218, UDR-0200; CTR-0171 v15, CTR-0173 v19).
 *
 * The server decides and stores the namespace; these helpers only PREVIEW it in the
 * New ontology dialog and check input early. The rules mirror
 * `backend/src/app/ontology/namespaces.py` (the server stays the authority).
 */

export const DEFAULT_IRI_BASE = 'https://chatwalaau.com/ontology/'

export const SHORT_NAME_PATTERN = /^[a-z][a-z0-9-]{0,38}[a-z0-9]$/

/** Paths stages 2 / 3 may need under the base (UDR-0200 D5). */
export const RESERVED_SHORT_NAMES: ReadonlySet<string> = new Set([
  'api',
  'id',
  'sparql',
  'ontology',
  'ontologies',
  'static',
  'assets',
  'admin',
  'health',
  'docs',
  'login',
  'logout',
  'auth',
  'catalog',
  'graph',
  'graphs',
  'query',
  'search',
  'well-known',
])

/** The `iri` block of a catalog / ontology response (UDR-0200 D10). */
export interface OntologyIris {
  ontology: string
  vocabulary: string
  individuals: string
  short_name: string | null
  custom: boolean
  /** Stored in the catalog entry: it cannot change any more. */
  fixed: boolean
}

/** The stored catalog `namespace` block (CTR-0170 v13). */
export interface CatalogNamespace {
  ontology_iri: string
  short_name: string | null
  custom: boolean
}

/** The three IRIs of a catalog entry (the original form when nothing is stored, D3). */
export function irisOf(id: string, namespace?: CatalogNamespace | null): OntologyIris {
  if (!namespace) {
    const ontology = `${DEFAULT_IRI_BASE}${id}`
    return {
      ontology,
      vocabulary: `${ontology}#`,
      individuals: `${ontology}#`,
      short_name: null,
      custom: false,
      fixed: false,
    }
  }
  const ontology = namespace.ontology_iri
  return {
    ontology,
    vocabulary: `${ontology}#`,
    individuals: `${ontology}/id/`,
    short_name: namespace.short_name,
    custom: namespace.custom,
    fixed: true,
  }
}

/** Why a short name is refused, or null (UDR-0200 D5). */
export function shortNameProblem(name: string): string | null {
  if (!name) return null
  if (!SHORT_NAME_PATTERN.test(name)) {
    return "2 to 40 characters: a-z, 0-9 and '-', starting with a letter and not ending with '-'."
  }
  if (RESERVED_SHORT_NAMES.has(name)) return `'${name}' is reserved.`
  return null
}

/** Why a custom ontology IRI is refused, or null (a quick check; the server decides). */
export function ontologyIriProblem(iri: string): string | null {
  if (!iri) return null
  if (!/^https?:\/\/[^/\s?#]+\/[^\s?#<>"{}|\\^`]+$/i.test(iri)) {
    return 'An absolute http(s) IRI with a path, without spaces, query (?) or fragment (#).'
  }
  if (iri.endsWith('/')) return "It must not end with '/'."
  if (iri.toLowerCase().startsWith(DEFAULT_IRI_BASE)) return 'Use your own domain.'
  return null
}

/** A short name suggested from a display name (ASCII letters and digits only), or ''. */
export function suggestShortName(name: string): string {
  const slug = name
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^[^a-z]+/, '')
    .replace(/-+$/, '')
    .slice(0, 40)
    .replace(/-+$/, '')
  return SHORT_NAME_PATTERN.test(slug) && !RESERVED_SHORT_NAMES.has(slug) ? slug : ''
}

/**
 * The IRIs a NEW ontology would get (UDR-0200 D4), for the dialog's preview; null when
 * it keeps the original form (the id is not known before the server creates it).
 */
export function previewIris(options: {
  base: string
  baseIsDefault: boolean
  shortName: string
  customIri: string
}): OntologyIris | null {
  const custom = options.customIri.trim()
  if (custom) {
    return irisOf('', { ontology_iri: custom, short_name: null, custom: true })
  }
  if (options.baseIsDefault) return null
  const name = options.shortName.trim() || '{id}'
  return irisOf('', {
    ontology_iri: `${options.base}${name}`,
    short_name: options.shortName.trim() || null,
    custom: false,
  })
}

/** The message of a refused namespace request (`detail.message`), else a fallback. */
export async function namespaceRefusal(res: Response, fallback: string): Promise<string> {
  try {
    const body = (await res.clone().json()) as { detail?: { message?: string } | string }
    if (typeof body.detail === 'string') return body.detail
    if (body.detail?.message) return body.detail.message
  } catch {
    // not JSON
  }
  return fallback
}
