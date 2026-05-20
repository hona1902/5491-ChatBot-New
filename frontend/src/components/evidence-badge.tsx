'use client'

import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { Badge } from '@/components/ui/badge'
import { Shield, Table2, FileText, Search } from 'lucide-react'

/**
 * Evidence v2: Badge component showing evidence routing metadata.
 * Only rendered when evidence_need ≠ 'overview' or fallback_occurred = true.
 */

interface EvidenceMetadata {
  evidence_need: 'overview' | 'factual' | 'legal_comparison'
  evidence_layers_used: string[]
  fallback_occurred: boolean
}

interface EvidenceBadgeProps {
  metadata: EvidenceMetadata | null | undefined
}

const LAYER_LABELS: Record<string, { label: string; icon: React.ReactNode }> = {
  verified_table_data: { label: 'Verified Table Data', icon: <Table2 className="h-3 w-3" /> },
  full_source_evidence: { label: 'Full Source Evidence', icon: <FileText className="h-3 w-3" /> },
  vector_search: { label: 'Vector Search', icon: <Search className="h-3 w-3" /> },
}

const NEED_LABELS: Record<string, string> = {
  overview: 'Overview',
  factual: 'Factual',
  legal_comparison: 'Legal/Comparison',
}

export function EvidenceBadge({ metadata }: EvidenceBadgeProps) {
  if (!metadata) return null

  // Only show badge when evidence_need ≠ 'overview' or fallback occurred
  if (metadata.evidence_need === 'overview' && !metadata.fallback_occurred) {
    return null
  }

  const needLabel = NEED_LABELS[metadata.evidence_need] || metadata.evidence_need
  const variant = metadata.fallback_occurred ? 'secondary' : 'outline'

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge variant={variant} className="gap-1 text-xs cursor-help">
            <Shield className="h-3 w-3" />
            {needLabel}
            {metadata.fallback_occurred && ' ↑'}
          </Badge>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-xs">
          <div className="space-y-1.5">
            <p className="font-medium text-sm">Evidence Routing</p>
            <p className="text-xs text-muted-foreground">
              Evidence tier: <span className="font-medium">{needLabel}</span>
              {metadata.fallback_occurred && (
                <span className="ml-1 text-amber-500">(upgraded by heuristic)</span>
              )}
            </p>
            <div className="text-xs">
              <p className="text-muted-foreground mb-1">Layers used:</p>
              <ul className="space-y-0.5">
                {metadata.evidence_layers_used.map((layer) => {
                  const info = LAYER_LABELS[layer]
                  return (
                    <li key={layer} className="flex items-center gap-1">
                      {info?.icon || <Search className="h-3 w-3" />}
                      <span>{info?.label || layer}</span>
                    </li>
                  )
                })}
              </ul>
            </div>
          </div>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
