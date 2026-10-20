/**
 * IRI fields of the Ontology Manager (PRP-0218, UDR-0200 D4-D6; CTR-0173 v19):
 * the IRI section of the New ontology dialog and the IRIs block of the Rename dialog.
 * The server decides and stores the namespace; this only previews and submits it.
 */
import { Check, Copy } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  type CatalogNamespace,
  irisOf,
  type OntologyIris,
  ontologyIriProblem,
  previewIris,
  shortNameProblem,
} from '@/lib/ontologyIri'

const LABEL = 'mb-1 block text-xs font-medium text-muted-foreground'

function IriRow(props: { label: string; value: string; copy?: boolean }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-20 shrink-0 text-muted-foreground">{props.label}</span>
      <code className="min-w-0 flex-1 break-all rounded bg-muted px-1.5 py-0.5">{props.value}</code>
      {props.copy && (
        <button
          type="button"
          aria-label={`Copy ${props.label} IRI`}
          title="Copy"
          className="text-muted-foreground hover:text-foreground"
          onClick={() => {
            void navigator.clipboard?.writeText(props.value).then(() => {
              setCopied(true)
              setTimeout(() => setCopied(false), 1200)
            })
          }}>
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
        </button>
      )}
    </div>
  )
}

function IriTable(props: { iris: OntologyIris; copy?: boolean }) {
  return (
    <div className="space-y-1" data-testid="ontology-iris">
      <IriRow label="Ontology" value={props.iris.ontology} copy={props.copy} />
      <IriRow label="Terms" value={`${props.iris.vocabulary}…`} copy={false} />
      <IriRow label="Individuals" value={`${props.iris.individuals}…`} copy={false} />
    </div>
  )
}

/** The IRI section of the New ontology dialog (UDR-0200 D4). */
export function NewOntologyIriFields(props: {
  base: string
  baseIsDefault: boolean
  shortName: string
  onShortName: (value: string) => void
  customIri: string
  onCustomIri: (value: string) => void
  useCustom: boolean
  onUseCustom: (value: boolean) => void
}) {
  const custom = props.useCustom ? props.customIri : ''
  const preview = previewIris({
    base: props.base,
    baseIsDefault: props.baseIsDefault,
    shortName: props.useCustom ? '' : props.shortName,
    customIri: custom,
  })
  const shortProblem = props.useCustom ? null : shortNameProblem(props.shortName.trim())
  const iriProblem = props.useCustom ? ontologyIriProblem(props.customIri.trim()) : null
  return (
    <div className="space-y-2 rounded-md border p-3" data-testid="ontology-iri-fields">
      <div className="text-xs font-semibold">IRI</div>
      {!props.useCustom &&
        (props.baseIsDefault ? (
          <p className="text-xs text-muted-foreground">
            New terms are named under {props.base}. An administrator can set an IRI base of your organization in App
            Settings &gt; Ontology &gt; IRI base; then ontologies get short names such as https://kg.example.com/plant.
          </p>
        ) : (
          <div>
            <label htmlFor="ontology-short-name" className={LABEL}>
              Short name (optional; cannot be changed later)
            </label>
            <Input
              id="ontology-short-name"
              value={props.shortName}
              onChange={(e) => props.onShortName(e.target.value)}
              placeholder="e.g. plant"
              aria-invalid={shortProblem !== null}
            />
            {shortProblem && <p className="mt-1 text-xs text-red-600">{shortProblem}</p>}
          </div>
        ))}
      <label className="flex items-center gap-2 text-xs">
        <input type="checkbox" checked={props.useCustom} onChange={(e) => props.onUseCustom(e.target.checked)} />
        Use a custom ontology IRI
      </label>
      {props.useCustom && (
        <div>
          <label htmlFor="ontology-custom-iri" className={LABEL}>
            Ontology IRI (cannot be changed later)
          </label>
          <Input
            id="ontology-custom-iri"
            value={props.customIri}
            onChange={(e) => props.onCustomIri(e.target.value)}
            placeholder="https://data.example.com/plant"
            aria-invalid={iriProblem !== null}
          />
          {iriProblem && <p className="mt-1 text-xs text-red-600">{iriProblem}</p>}
        </div>
      )}
      {preview && !shortProblem && !iriProblem && <IriTable iris={preview} />}
    </div>
  )
}

/** True when the New ontology IRI input may be submitted. */
export function newOntologyIriReady(options: { useCustom: boolean; shortName: string; customIri: string }): boolean {
  if (options.useCustom) {
    const iri = options.customIri.trim()
    return iri !== '' && ontologyIriProblem(iri) === null
  }
  return shortNameProblem(options.shortName.trim()) === null
}

/** The IRIs block of the Rename dialog, with the one-time "Set IRI..." (UDR-0200 D6). */
export function OntologyIrisBlock(props: {
  id: string
  namespace?: CatalogNamespace | null
  base: string
  baseIsDefault: boolean
  readOnly: boolean
  onSet: (payload: { short_name?: string; ontology_iri?: string }) => Promise<string | null>
}) {
  const iris = irisOf(props.id, props.namespace)
  const [setting, setSetting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [shortName, setShortName] = useState('')
  const [customIri, setCustomIri] = useState('')
  const [useCustom, setUseCustom] = useState(props.baseIsDefault)
  const [error, setError] = useState<string | null>(null)
  const ready = newOntologyIriReady({ useCustom, shortName, customIri }) && (useCustom || shortName.trim() !== '')
  return (
    <div className="space-y-2" data-testid="ontology-iris-block">
      <div className="text-xs font-semibold">IRIs</div>
      <IriTable iris={iris} copy />
      {!iris.fixed && !props.readOnly && !setting && (
        <Button variant="outline" size="sm" onClick={() => setSetting(true)}>
          Set IRI...
        </Button>
      )}
      {setting && (
        <div className="space-y-2">
          <p className="text-xs text-amber-700">
            Existing IRIs in the ontology are not changed; only new terms use the new namespace. The IRI can be set
            once.
          </p>
          <NewOntologyIriFields
            base={props.base}
            baseIsDefault={props.baseIsDefault}
            shortName={shortName}
            onShortName={setShortName}
            customIri={customIri}
            onCustomIri={setCustomIri}
            useCustom={useCustom}
            onUseCustom={setUseCustom}
          />
          {error && <p className="text-xs text-red-600">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={() => setSetting(false)} disabled={busy}>
              Cancel
            </Button>
            <Button
              size="sm"
              disabled={busy || !ready}
              onClick={() => {
                setBusy(true)
                setError(null)
                void props
                  .onSet(useCustom ? { ontology_iri: customIri.trim() } : { short_name: shortName.trim() })
                  .then((problem) => {
                    setError(problem)
                    if (!problem) setSetting(false)
                  })
                  .finally(() => setBusy(false))
              }}>
              Set IRI
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
