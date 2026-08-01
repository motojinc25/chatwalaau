/**
 * Structured Output schema validation (CTR-0118 v2, FEAT-0040 / UDR-0058; v0.117.5).
 *
 * An explicit JSON Schema is forwarded to the provider VERBATIM and sent with
 * `strict: true` (CTR-0102), whose requirements are narrower than plain JSON Schema.
 * The editor previously checked only that the text parsed as JSON, so a schema that
 * was valid JSON but invalid under strict mode was accepted silently and the turn
 * died at the provider with an HTTP 400 the user never saw.
 *
 * These checks mirror the rules OpenAI / Azure OpenAI enforce for
 * `text.format = {type: "json_schema", strict: true}`:
 *
 *   - the root must be an object schema;
 *   - every object must declare `properties`;
 *   - every object must declare `additionalProperties: false`;
 *   - every key in `properties` must appear in `required` (strict has no optional
 *     keys -- use a `["string", "null"]` type union to express "may be absent");
 *   - every array must declare `items`.
 *
 * Reported as human-readable problems with a JSON path, so the message names the
 * offending property rather than the schema as a whole.
 */

export interface SchemaProblem {
  /** Dotted/bracketed path to the offending sub-schema, e.g. `properties.steps`. */
  path: string
  message: string
}

export interface ParsedSchema {
  /** The parsed schema, or null when the text is empty or not valid JSON. */
  schema: Record<string, unknown> | null
  /** Parser message when the text is non-empty and did NOT parse; null otherwise. */
  error: string | null
}

/**
 * Parse the schema editor's text (CTR-0118 v2, v0.117.5).
 *
 * Returns the parser's own message on failure instead of collapsing to `null`.
 * Text that does not parse used to be indistinguishable from an empty editor, and
 * both degraded silently to the generic "any JSON object" mode -- so a schema with
 * a stray trailing comma was ignored and the answer came back in an unrelated
 * shape, with nothing saying the schema had been dropped.
 */
export function parseSchemaText(text: string): ParsedSchema {
  const trimmed = text.trim()
  if (!trimmed) return { schema: null, error: null }
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch (e) {
    // V8's message points at the offending character, e.g. "Expected double-quoted
    // property name in JSON at position 118" for a trailing comma -- exactly what
    // the user needs to find it.
    return { schema: null, error: e instanceof Error ? e.message : 'not valid JSON' }
  }
  if (!isObject(parsed)) {
    return { schema: null, error: 'the schema must be a JSON object, not an array or a scalar.' }
  }
  return { schema: parsed, error: null }
}

type Json = Record<string, unknown>

function isObject(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** `type` may be a string or a union array; true when it includes `name`. */
function hasType(schema: Json, name: string): boolean {
  const t = schema.type
  if (typeof t === 'string') return t === name
  if (Array.isArray(t)) return t.includes(name)
  return false
}

function join(path: string, key: string): string {
  return path ? `${path}.${key}` : key
}

function walk(schema: unknown, path: string, problems: SchemaProblem[], depth: number): void {
  // Cheap recursion guard; a schema this deep is beyond what strict mode allows anyway.
  if (depth > 20 || !isObject(schema)) return

  // A composition keyword stands in for the sub-schemas; validate each branch.
  for (const key of ['anyOf', 'oneOf', 'allOf'] as const) {
    const branches = schema[key]
    if (Array.isArray(branches)) {
      branches.forEach((branch, i) => {
        walk(branch, `${join(path, key)}[${i}]`, problems, depth + 1)
      })
    }
  }

  if (hasType(schema, 'object')) {
    const properties = schema.properties
    if (!isObject(properties)) {
      problems.push({
        path: path || '(root)',
        message: 'object schema must declare "properties".',
      })
    } else {
      if (schema.additionalProperties !== false) {
        problems.push({
          path: path || '(root)',
          message: 'object schema must set "additionalProperties": false.',
        })
      }
      const declared = Object.keys(properties)
      const required = Array.isArray(schema.required) ? schema.required.filter((r) => typeof r === 'string') : []
      const missing = declared.filter((key) => !required.includes(key as string))
      if (missing.length > 0) {
        problems.push({
          path: path || '(root)',
          message:
            'strict mode has no optional keys: "required" must list every property. ' +
            `Missing: ${missing.map((m) => `"${m}"`).join(', ')}. ` +
            'To allow an empty value, keep the key required and widen its type ' +
            '(e.g. "type": ["string", "null"]).',
        })
      }
      for (const [key, sub] of Object.entries(properties)) {
        walk(sub, join(join(path, 'properties'), key), problems, depth + 1)
      }
    }
  }

  if (hasType(schema, 'array')) {
    if (!isObject(schema.items) && !Array.isArray(schema.items)) {
      problems.push({
        path: path || '(root)',
        message: 'array schema must declare "items" (the type of each element).',
      })
    } else {
      walk(schema.items, join(path, 'items'), problems, depth + 1)
    }
  }

  // $defs / definitions are referenced by $ref; check them so a broken shared
  // definition is reported at its source rather than at every use site.
  for (const key of ['$defs', 'definitions'] as const) {
    const defs = schema[key]
    if (isObject(defs)) {
      for (const [name, sub] of Object.entries(defs)) {
        walk(sub, join(join(path, key), name), problems, depth + 1)
      }
    }
  }
}

/**
 * Validate a parsed schema against the provider's strict-mode requirements.
 * Returns an empty array when the schema is usable as-is.
 */
export function validateStrictSchema(schema: unknown): SchemaProblem[] {
  const problems: SchemaProblem[] = []
  if (!isObject(schema)) {
    return [{ path: '(root)', message: 'the schema must be a JSON object.' }]
  }
  if (!hasType(schema, 'object')) {
    problems.push({
      path: '(root)',
      message: 'the root schema must be "type": "object" -- a top-level array or scalar is not accepted.',
    })
  }
  walk(schema, '', problems, 0)
  return problems
}
